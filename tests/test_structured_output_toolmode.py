#!/usr/bin/env python3
"""
Unit tests for issue #418: structured output via forced tool-use,
env-flagged (`OPENROUTER_STRUCTURED_OUTPUT`, default OFF).

## What is asserted here (mirrors the issue's Acceptance criteria)

  1. `backend/src/config.py::structured_output_enabled()` -- default OFF,
     truthy strings ("1"/"true"/"yes", any case) turn it on, anything else
     (including unset) stays off.
  2. `scripts/model_output_schema.py::model_facing_output_schema()` --
     valid JSON Schema; top-level `required` carries `decision`/
     `confidence_state`/`issues` but NOT `schema_version` (and the property
     itself is gone, not just un-required); the shared `Issue` definition's
     `required` drops `provenance` (property gone too) -- reached from
     BOTH `issues` and `critic_delta.added_issues` since both `$ref` it;
     every "valid" fixture in tests/fixtures/model_responses/ validates
     against it once un-stamped (schema_version / provenance removed), and
     FAILS if left stamped (proving the properties were actually stripped,
     not just made optional).
  3. `OpenRouterModelClient.invoke`:
       a. `tool_spec` omitted, or explicitly `None` -- request payload is
          byte-identical either way, and carries no `tools`/`tool_choice`
          key at all (today's behavior, unchanged).
       b. `tool_spec` given -- payload carries exactly one `tools` entry
          (`type: function`, `function.name: submit_review`,
          `function.parameters` == the schema verbatim) and a matching
          forced `tool_choice`.
       c. A response carrying `tool_calls[0].function.arguments` is
          returned as that string, and round-trips through
          `primary_review_pass.validate_model_response` to the SAME
          stamped, valid object as the equivalent prose-`content` path.
       d. A tool-mode call (tool_spec given) whose response comes back as
          plain `content` with no `tool_calls` (a provider ignoring
          `tool_choice`) still parses via the `content` fallback rather
          than raising.
  4. Protocol signature parity: `FakeBedrockClient.invoke` accepts
     `tool_spec` and records it on `self.calls` (for assertions);
     `LiveBedrockModelClient.invoke` accepts and silently ignores it -- the
     Bedrock InvokeModel payload is unaffected either way.
  5. Threading (`scripts/primary_review_pass.py::run_primary_pass` /
     `scripts/critic_review_pass.py::run_critic_pass`): the `tool_spec`
     keyword is passed to `model_client.invoke()` ONLY when
     `structured_output_enabled()` is True -- proved against a
     pre-existing-shaped fake whose `invoke()` signature has no
     `tool_spec` parameter at all, so a flag-off run against it still
     succeeds (a regression guard: every hand-rolled test double already
     in this repo predates this kwarg).

Fully offline: no network, an injected fake HTTP client stands in for
httpx exactly like tests/test_openrouter_model_client.py.

Run: python3 tests/test_structured_output_toolmode.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"

for _dir in (BACKEND_SRC, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import config  # noqa: E402
import jsonschema  # noqa: E402
import model_client as mc  # noqa: E402
import model_output_schema as mos  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import critic_review_pass as cp  # noqa: E402

PRIMARY_MODEL_ID = "anthropic/claude-opus-4.8"
CRITIC_MODEL_ID = "anthropic/claude-sonnet-4.6"

# The only two "valid" fixture filenames whose top-level shape this file's
# round-trip tests exercise by name; the full-corpus AC (every valid
# fixture accepted once un-stamped) is covered separately by iterating
# every *_valid.json file in the directory.
_PRIMARY_VALID_FIXTURE = "primary_request_change_valid.json"


def _load_fixture(name: str) -> dict[str, Any]:
    with open(MODEL_RESPONSES_DIR / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _unstamp(parsed: dict[str, Any]) -> dict[str, Any]:
    """The inverse of primary_review_pass._stamp_pipeline_envelope: strip
    `schema_version` and every issue's `provenance` (top-level `issues` AND
    `critic_delta.added_issues`) -- what a tool-mode model response would
    look like BEFORE the pipeline stamps it back in."""
    unstamped = json.loads(json.dumps(parsed))  # deep copy, stdlib-only
    unstamped.pop("schema_version", None)
    for issue in unstamped.get("issues") or []:
        issue.pop("provenance", None)
    critic_delta = unstamped.get("critic_delta")
    if isinstance(critic_delta, dict):
        for added in critic_delta.get("added_issues") or []:
            added.pop("provenance", None)
    return unstamped


class TestStructuredOutputEnabledFlag(unittest.TestCase):
    def test_unset_is_off(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(config.structured_output_enabled())

    def test_empty_string_is_off(self) -> None:
        with patch.dict("os.environ", {"OPENROUTER_STRUCTURED_OUTPUT": ""}, clear=True):
            self.assertFalse(config.structured_output_enabled())

    def test_zero_is_off(self) -> None:
        with patch.dict("os.environ", {"OPENROUTER_STRUCTURED_OUTPUT": "0"}, clear=True):
            self.assertFalse(config.structured_output_enabled())

    def test_garbage_value_is_off(self) -> None:
        with patch.dict("os.environ", {"OPENROUTER_STRUCTURED_OUTPUT": "nope"}, clear=True):
            self.assertFalse(config.structured_output_enabled())

    def test_truthy_values_are_on(self) -> None:
        for value in ("1", "true", "True", "TRUE", "yes", "YES"):
            with patch.dict("os.environ", {"OPENROUTER_STRUCTURED_OUTPUT": value}, clear=True):
                self.assertTrue(config.structured_output_enabled(), f"{value!r} should enable it")


class TestModelFacingOutputSchema(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = mos.model_facing_output_schema()

    def test_is_a_valid_json_schema(self) -> None:
        jsonschema.Draft7Validator.check_schema(self.schema)

    def test_top_level_required_drops_schema_version_keeps_the_rest(self) -> None:
        self.assertEqual(
            set(self.schema["required"]), {"decision", "confidence_state", "issues"}
        )

    def test_top_level_properties_no_longer_declares_schema_version(self) -> None:
        # Stripped from properties too, not just required -- see the
        # module docstring's "required/properties" contract.
        self.assertNotIn("schema_version", self.schema["properties"])
        # Untouched sibling fields stay exactly as the source schema has them.
        self.assertIn("decision", self.schema["properties"])
        self.assertIn("confidence_state", self.schema["properties"])
        self.assertIn("issues", self.schema["properties"])

    def test_issue_definition_required_drops_provenance(self) -> None:
        issue_def = self.schema["definitions"]["Issue"]
        self.assertNotIn("provenance", issue_def["required"])
        # Every other originally-required Issue field is untouched.
        self.assertEqual(
            set(issue_def["required"]),
            {
                "section_ref",
                "section_title",
                "counterparty_change_summary",
                "decision",
                "external_rationale_for_footnote",
                "proposed_replacement_text",
                "playbook_topic_id",
                "internal_precedent_citation",
            },
        )

    def test_issue_definition_properties_no_longer_declares_provenance(self) -> None:
        self.assertNotIn("provenance", self.schema["definitions"]["Issue"]["properties"])

    def test_source_schema_on_disk_is_untouched(self) -> None:
        # This is a PROJECTION -- the function must never mutate the file
        # it reads (each call opens+reads fresh; re-deriving must show the
        # exact same stripped shape every time, not an accumulating one).
        second = mos.model_facing_output_schema()
        self.assertEqual(self.schema, second)
        with open(mos.OUTPUT_SCHEMA_PATH, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertIn("schema_version", on_disk["required"])
        self.assertIn("provenance", on_disk["definitions"]["Issue"]["required"])

    def test_every_valid_fixture_accepted_once_unstamped(self) -> None:
        valid_fixtures = sorted(MODEL_RESPONSES_DIR.glob("*_valid.json"))
        self.assertGreater(len(valid_fixtures), 0, "expected at least one *_valid.json fixture")
        for path in valid_fixtures:
            with self.subTest(fixture=path.name):
                parsed = json.loads(path.read_text(encoding="utf-8"))
                unstamped = _unstamp(parsed)
                jsonschema.validate(instance=unstamped, schema=self.schema)

    def test_still_stamped_fixture_is_rejected_additional_properties(self) -> None:
        # Proves properties (not just required) were actually stripped:
        # additionalProperties: false means a still-present schema_version
        # now trips "not allowed", not merely "not required".
        parsed = _load_fixture(_PRIMARY_VALID_FIXTURE)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(instance=parsed, schema=self.schema)


# ---------------------------------------------------------------------------
# OpenRouterModelClient request/response shape under tool_spec.
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeHttpClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None):  # noqa: A002 - mirror httpx sig
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response

    def close(self) -> None:
        pass


def _content_response(content: str, finish_reason: str = "stop") -> FakeResponse:
    return FakeResponse(
        200,
        {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]},
    )


def _tool_call_response(arguments: str, finish_reason: str = "tool_calls") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": mc.STRUCTURED_OUTPUT_TOOL_NAME, "arguments": arguments},
                            }
                        ],
                    },
                    "finish_reason": finish_reason,
                }
            ]
        },
    )


class TestOpenRouterInvokeToolSpec(unittest.TestCase):
    def _client(self, http: FakeHttpClient) -> mc.OpenRouterModelClient:
        return mc.OpenRouterModelClient(
            api_key="sk-test",
            http_client=http,
            max_retries=0,
            sleep_fn=lambda _s: None,
        )

    def test_tool_spec_omitted_and_explicit_none_are_byte_identical_payloads(self) -> None:
        http_a = FakeHttpClient(_content_response('{"decision":"ACCEPT"}'))
        http_b = FakeHttpClient(_content_response('{"decision":"ACCEPT"}'))
        with patch.dict("os.environ", {}, clear=True):
            self._client(http_a).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
            )
            self._client(http_b).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                tool_spec=None,
            )
        body_a = http_a.calls[0]["json"]
        body_b = http_b.calls[0]["json"]
        self.assertEqual(body_a, body_b)
        self.assertNotIn("tools", body_a)
        self.assertNotIn("tool_choice", body_a)

    def test_tool_spec_given_adds_exactly_one_forced_tool(self) -> None:
        schema = mos.model_facing_output_schema()
        http = FakeHttpClient(_tool_call_response('{"decision":"ACCEPT","issues":[]}'))
        with patch.dict("os.environ", {}, clear=True):
            self._client(http).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                tool_spec=schema,
            )
        body = http.calls[0]["json"]
        self.assertEqual(len(body["tools"]), 1)
        tool = body["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["function"]["name"], mc.STRUCTURED_OUTPUT_TOOL_NAME)
        self.assertEqual(tool["function"]["parameters"], schema)
        self.assertEqual(
            body["tool_choice"],
            {"type": "function", "function": {"name": mc.STRUCTURED_OUTPUT_TOOL_NAME}},
        )
        # Every other request-contract field (no sampling params, ZDR
        # routing) is unaffected by tool mode.
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, body)
        self.assertEqual(body["provider"]["zdr"], True)

    def test_tool_calls_arguments_preferred_over_content(self) -> None:
        arguments = '{"decision":"ACCEPT","issues":[]}'
        http = FakeHttpClient(_tool_call_response(arguments))
        with patch.dict("os.environ", {}, clear=True):
            out = self._client(http).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                tool_spec=mos.model_facing_output_schema(),
            )
        self.assertEqual(out, arguments)

    def test_tool_mode_call_that_comes_back_as_plain_content_falls_back(self) -> None:
        # A provider that (illegally) ignores tool_choice and answers with
        # ordinary `content` and no `tool_calls` at all must still parse,
        # not raise -- the documented fallback.
        http = FakeHttpClient(_content_response('{"decision":"ACCEPT","issues":[]}'))
        with patch.dict("os.environ", {}, clear=True):
            out = self._client(http).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                tool_spec=mos.model_facing_output_schema(),
            )
        self.assertEqual(out, '{"decision":"ACCEPT","issues":[]}')

    def test_arguments_round_trip_through_validate_model_response(self) -> None:
        # A fake tool-mode response carrying the UN-STAMPED fixture as its
        # arguments string must round-trip to the SAME stamped, valid
        # object the prose-content path produces for the fully-stamped
        # fixture.
        stamped_fixture = _load_fixture(_PRIMARY_VALID_FIXTURE)
        unstamped_arguments = json.dumps(_unstamp(stamped_fixture))

        tool_http = FakeHttpClient(_tool_call_response(unstamped_arguments))
        prose_http = FakeHttpClient(_content_response(json.dumps(stamped_fixture)))
        with patch.dict("os.environ", {}, clear=True):
            tool_raw = self._client(tool_http).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                tool_spec=mos.model_facing_output_schema(),
            )
            prose_raw = self._client(prose_http).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
            )

        tool_ok, tool_parsed = pp.validate_model_response(tool_raw, issue_provenance="model")
        prose_ok, prose_parsed = pp.validate_model_response(prose_raw, issue_provenance="model")

        self.assertTrue(tool_ok, tool_parsed)
        self.assertTrue(prose_ok, prose_parsed)
        self.assertEqual(tool_parsed, prose_parsed)
        self.assertEqual(tool_parsed["schema_version"], "output-schema-v1")
        self.assertEqual(tool_parsed["issues"][0]["provenance"], "model")


class TestProtocolSignatureParity(unittest.TestCase):
    def test_fake_bedrock_client_records_tool_spec(self) -> None:
        client = mc.FakeBedrockClient({"m": ['{"decision":"ACCEPT"}']})
        schema = {"type": "object"}
        client.invoke(
            model_id="m",
            system_prompt="s",
            user_prompt="u",
            max_output_tokens=1,
            tool_spec=schema,
        )
        self.assertEqual(client.calls[0]["tool_spec"], schema)

    def test_fake_bedrock_client_defaults_tool_spec_to_none(self) -> None:
        client = mc.FakeBedrockClient({"m": ['{"decision":"ACCEPT"}']})
        client.invoke(model_id="m", system_prompt="s", user_prompt="u", max_output_tokens=1)
        self.assertIsNone(client.calls[0]["tool_spec"])

    def test_live_bedrock_client_ignores_tool_spec(self) -> None:
        captured: dict[str, Any] = {}

        class FakeBody:
            def read(self) -> bytes:
                return json.dumps({"content": [{"text": "ok"}]}).encode("utf-8")

        class FakeRuntime:
            @staticmethod
            def invoke_model(**kwargs):
                captured.update(kwargs)
                return {"body": FakeBody()}

        client = mc.LiveBedrockModelClient(bedrock_runtime_client=FakeRuntime())
        out = client.invoke(
            model_id="anthropic.claude-opus-4-8",
            system_prompt="SYS",
            user_prompt="USER",
            max_output_tokens=100,
            tool_spec={"type": "object"},
        )
        self.assertEqual(out, "ok")
        body = json.loads(captured["body"])
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_spec", body)


# ---------------------------------------------------------------------------
# Threading: run_primary_pass / run_critic_pass pass tool_spec ONLY when
# structured_output_enabled() -- proved against a pre-existing-shaped fake
# whose invoke() signature has no tool_spec parameter, so a flag-off run
# is a regression guard for every hand-rolled test double already in this
# repo.
# ---------------------------------------------------------------------------


class LegacyShapedFakeClient:
    """Mirrors the fixed keyword-only `invoke()` signature every
    pre-#418 hand-rolled test double in this repo uses (no `tool_spec`
    parameter, no **kwargs) -- see e.g.
    tests/test_primary_pass_retry_recovery.py. Calling it with an
    unexpected `tool_spec` kwarg raises TypeError, exactly as a real
    pre-existing double would."""

    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.calls: list[dict[str, Any]] = []

    def invoke(self, *, model_id: str, system_prompt: str, user_prompt: str, max_output_tokens: int) -> str:
        self.calls.append({"model_id": model_id, "max_output_tokens": max_output_tokens})
        return self._response_text


def _sample_playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _sample_diff_hunks() -> list[dict[str, Any]]:
    return [{"kind": "modified_new", "anchor": "sec-8", "text": "text"}]


def _sample_anchored_clauses() -> list[dict[str, Any]]:
    return [
        {
            "anchor": "sec-8",
            "standard_text": "standard",
            "counterparty_text": "counterparty",
            "delta": "delta",
        }
    ]


class TestRunPrimaryPassThreading(unittest.TestCase):
    def test_flag_off_never_sends_tool_spec_even_to_a_legacy_shaped_client(self) -> None:
        legacy = LegacyShapedFakeClient(json.dumps(_load_fixture(_PRIMARY_VALID_FIXTURE)))
        with patch.dict("os.environ", {}, clear=True):
            result = pp.run_primary_pass(
                review_id="r-1",
                diff_hunks=_sample_diff_hunks(),
                anchored_clauses=_sample_anchored_clauses(),
                retrieved_precedent=[],
                playbook=_sample_playbook(),
                model_client=legacy,
                model_id="anthropic.claude-opus-4-8",
                ledger_write=lambda _rec: None,
                doc_text="Section 8 text.",
            )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(legacy.calls), 1)

    def test_flag_on_passes_the_model_facing_schema_as_tool_spec(self) -> None:
        stamped = _load_fixture(_PRIMARY_VALID_FIXTURE)
        client = mc.FakeBedrockClient(
            {"anthropic.claude-opus-4-8": [json.dumps(_unstamp(stamped))]}
        )
        with patch.dict("os.environ", {"OPENROUTER_STRUCTURED_OUTPUT": "1"}, clear=True):
            result = pp.run_primary_pass(
                review_id="r-2",
                diff_hunks=_sample_diff_hunks(),
                anchored_clauses=_sample_anchored_clauses(),
                retrieved_precedent=[],
                playbook=_sample_playbook(),
                model_client=client,
                model_id="anthropic.claude-opus-4-8",
                ledger_write=lambda _rec: None,
                doc_text="Section 8 text.",
            )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(client.calls[0]["tool_spec"], mos.model_facing_output_schema())


class TestRunCriticPassThreading(unittest.TestCase):
    def test_flag_off_never_sends_tool_spec_even_to_a_legacy_shaped_client(self) -> None:
        legacy = LegacyShapedFakeClient(json.dumps(_load_fixture("critic_no_delta_accept_valid.json")))
        primary_output = _load_fixture(_PRIMARY_VALID_FIXTURE)
        with patch.dict("os.environ", {}, clear=True):
            result = cp.run_critic_pass(
                review_id="r-3",
                diff_hunks=_sample_diff_hunks(),
                anchored_clauses=_sample_anchored_clauses(),
                primary_output=primary_output,
                playbook=_sample_playbook(),
                model_client=legacy,
                model_id="anthropic.claude-sonnet-4-6",
                ledger_write=lambda _rec: None,
            )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(legacy.calls), 1)

    def test_flag_on_passes_the_model_facing_schema_as_tool_spec(self) -> None:
        stamped = _load_fixture("critic_no_delta_accept_valid.json")
        client = mc.FakeBedrockClient(
            {"anthropic.claude-sonnet-4-6": [json.dumps(_unstamp(stamped))]}
        )
        primary_output = _load_fixture(_PRIMARY_VALID_FIXTURE)
        with patch.dict("os.environ", {"OPENROUTER_STRUCTURED_OUTPUT": "1"}, clear=True):
            result = cp.run_critic_pass(
                review_id="r-4",
                diff_hunks=_sample_diff_hunks(),
                anchored_clauses=_sample_anchored_clauses(),
                primary_output=primary_output,
                playbook=_sample_playbook(),
                model_client=client,
                model_id="anthropic.claude-sonnet-4-6",
                ledger_write=lambda _rec: None,
            )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(client.calls[0]["tool_spec"], mos.model_facing_output_schema())


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestStructuredOutputEnabledFlag,
        TestModelFacingOutputSchema,
        TestOpenRouterInvokeToolSpec,
        TestProtocolSignatureParity,
        TestRunPrimaryPassThreading,
        TestRunCriticPassThreading,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
