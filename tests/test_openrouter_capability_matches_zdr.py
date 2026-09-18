#!/usr/bin/env python3
"""
Unit tests for issue #142: every review whose primary is `anthropic/claude-
opus-5` (the shipped default) died before the first token with an OpenRouter
404, because `model-policy/openrouter.json` declared `structured_outputs:
true` for an id whose ZERO-DATA-RETENTION endpoint does not actually accept
`response_format` -- `backend/src/model_client.py::OpenRouterModelClient.
invoke` adds that key whenever `output_schema` is supplied and the policy
capability reads True, independent of `OPENROUTER_STRUCTURED_OUTPUT` (which
only governs the SEPARATE forced-tool `tools`/`tool_choice` path). Live
probe, 2026-09-16, against this deployment's own provider block (`zdr:
true`, `data_collection: "deny"`, `require_parameters: true` --
`OPENROUTER_PROVIDER_ROUTING`, never weakened by this fix): a
`response_format` request for `anthropic/claude-opus-5`, `anthropic/claude-
opus-4.8`, and `anthropic/claude-sonnet-5` 404s (`failed_routing_step:
"Filter by Data Policy"`); the same ids' forced-tool requests route fine.

## What is asserted here

  1. The shipped policy (`model-policy/openrouter.json`) no longer declares
     `structured_outputs` for the three measured ids -- the exact assertion
     from the issue's own "Required verification" script, kept here so a
     future edit to the policy file re-trips it instead of only a one-off
     scratch run. `anthropic/claude-sonnet-4.6` (unaffected -- declares
     nothing either way, both before and after this issue) is asserted
     unchanged as a guard against a future fix accidentally sweeping it in.
  2. `OpenRouterModelClient.invoke`, against the LIVE policy-pinned primary
     (`anthropic/claude-opus-5`, capability-False since this fix): a call
     passing `output_schema` carries NO `response_format` key at all --
     byte-identical to a call that never passed `output_schema` -- proving
     the request builder actually reads the corrected capability, not just
     that the policy file's bytes changed.
  3. The forced-tool path is independent of, and unaffected by, capability
     #2: the same model_id, given `tool_spec` (what a caller threads when
     `OPENROUTER_STRUCTURED_OUTPUT=1`, the default -- `backend/src/
     config.py::structured_output_enabled`), carries `tools`/`tool_choice`
     regardless of `structured_outputs`'s value -- schema-enforced output
     for this id still works, just not via `response_format`. Asserted
     both with `tool_spec` alone and with `tool_spec` AND `output_schema`
     together (the real shape a default review sends today), to prove
     `response_format`'s absence is not an accidental side effect of some
     other key crowding it out.
  4. The policy-declared CORRECTION is per-id, not a blanket flip: the
     critic pin (`anthropic/claude-sonnet-4.6`, never measured either way)
     still resolves all-False, unchanged.

Fully offline: the policy file is read straight off disk (no network), and
`OpenRouterModelClient` is constructed with an injected fake HTTP client
(this file's own, mirroring the shape `tests/test_structured_output_
request.py` and `tests/test_openrouter_model_client.py` already use -- a
new copy per this codebase's per-file test-process isolation convention,
not an import of another test file's fixtures).

Run: python3 tests/test_openrouter_capability_matches_zdr.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import model_client as mc  # noqa: E402
from openrouter_sse_double import sse_stream_adapter  # noqa: E402

# The ids issue #142's live probe measured against the production provider
# block. anthropic/claude-opus-4.8 is deliberately NOT a variable here: it
# is not in model-policy/openrouter.json at all (removed from `selectable`
# by an earlier owner decision, #604/#589's history) -- the policy-assertion
# test below reads it exactly as the issue's own verification script does,
# where an absent id resolves to an empty dict and the assertion passes
# vacuously, same as "this file declares nothing false-positive for an id
# it does not mention."
MEASURED_404_MODEL_IDS = (
    "anthropic/claude-opus-5",
    "anthropic/claude-opus-4.8",
    "anthropic/claude-sonnet-5",
)

# The live policy-pinned primary (model-policy/openrouter.json). Used for
# the invoke()-level tests below because it is the id the issue's incident
# actually broke -- every default review runs on it.
PRIMARY_MODEL_ID = "anthropic/claude-opus-5"
CRITIC_MODEL_ID = "anthropic/claude-sonnet-4.6"
# A `selectable` id that STILL declares `structured_outputs: true` -- one of
# the four non-Anthropic entries issue #142's live probe did not cover, so
# this fix left its declaration alone. Used for the positive half of the
# request-builder pair below.
CAPABLE_SELECTABLE_MODEL_ID = "openai/gpt-5.6-sol"


class TestPolicyNoLongerDeclaresStructuredOutputsForZdrRejectedIds(unittest.TestCase):
    """The issue's own "Required verification" script, kept as a permanent
    regression test rather than a one-off scratch run: a future policy edit
    (a repin, a new selectable entry, a careless restore of Opus 4.8) that
    reintroduces `structured_outputs: true` for one of these ids re-trips
    this before it ever reaches OpenRouter and 404s a real review.
    """

    def setUp(self) -> None:
        self.policy = mc.load_openrouter_policy()

    def _declaration_sites(self, model_id: str) -> list[tuple[str, dict]]:
        """EVERY place the shipped policy declares capabilities for this
        model_id -- its `selectable` entry and any role pin naming it.

        Deliberately not a one-entry-wins dict. `openrouter_model_capabilities`
        resolves the ROLE PIN ahead of the `selectable` twin, so a dict keyed
        by model_id hides one site behind the other whichever order it is
        built in: build it selectable-first (the issue's own scratch script)
        and a pin that reintroduced `structured_outputs: true` passes
        unnoticed; build it pin-first and the reverse does. Asserting over
        the LIST catches either, which matters because this file's
        DEFAULTS-ARE-SELECTABLE rule means the ids at stake here are declared
        in two places at once.
        """
        sites: list[tuple[str, dict]] = [
            (f"selectable[{model_id}]", entry)
            for entry in self.policy.get("selectable", [])
            if entry.get("model_id") == model_id
        ]
        for role, role_entry in (self.policy.get("models") or {}).items():
            if isinstance(role_entry, dict) and role_entry.get("model_id") == model_id:
                sites.append((f"models.{role}", role_entry))
        return sites

    def test_every_measured_404_id_declares_no_structured_outputs(self) -> None:
        for model_id in MEASURED_404_MODEL_IDS:
            for label, entry in self._declaration_sites(model_id):
                with self.subTest(model_id=model_id, site=label):
                    self.assertFalse(
                        entry.get("structured_outputs"),
                        f"{label} declares structured_outputs but {model_id}'s "
                        f"ZDR endpoint rejects response_format (OpenRouter 404, "
                        f"2026-09-16, issue #142)",
                    )

    def test_the_two_pinned_ids_are_each_declared_somewhere(self) -> None:
        """The loop above is vacuous for an id the policy never mentions --
        which is correct for anthropic/claude-opus-4.8 (removed from
        `selectable` by owner decision) but would silently hollow the test out
        if a repin ever dropped opus-5 or sonnet-5 from the artifact. Pin the
        two ids that MUST still be declared, so "no sites" cannot masquerade
        as "no violation" for them."""
        for model_id in ("anthropic/claude-opus-5", "anthropic/claude-sonnet-5"):
            with self.subTest(model_id=model_id):
                self.assertTrue(self._declaration_sites(model_id), model_id)
        # ...and the vacuous one is vacuous on purpose, not by accident.
        self.assertEqual(self._declaration_sites("anthropic/claude-opus-4.8"), [])

    def test_sonnet_4_6_capability_is_unaffected_by_this_fix(self) -> None:
        # Guard against a fix that swept in more than the measured ids:
        # sonnet-4.6 was never measured either way and must stay exactly
        # where it was before issue #142 -- absent, all-False.
        self.assertEqual(
            mc.openrouter_model_capabilities(CRITIC_MODEL_ID),
            {"structured_outputs": False, "prompt_caching": False},
        )

    def test_primary_pin_resolves_capability_false_through_the_real_lookup(self) -> None:
        # Not just "the raw policy dict lacks the key" (test above) but
        # "the function every caller actually uses to decide whether to add
        # response_format resolves False for the shipped default."
        self.assertFalse(
            mc.openrouter_model_capabilities(PRIMARY_MODEL_ID)["structured_outputs"]
        )


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeHttpClient:
    """Records the single POST and returns a canned response. `.stream()`
    is routed through the canned SSE adapter (issue #657) so a real
    `OpenRouterModelClient.invoke()` call -- which always streams -- works
    against this double exactly as it would against a non-streamed `.post`
    fake, per the shared adapter's own docstring.
    """

    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    stream = sse_stream_adapter

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


class TestOpenRouterInvokeOmitsResponseFormatForThePrimaryPin(unittest.TestCase):
    """OpenRouterModelClient.invoke() against the LIVE policy file -- no
    injected/synthetic policy dict -- so a regression in the shipped
    artifact (not just in the lookup function) fails this suite.
    """

    def _client(self, http: FakeHttpClient) -> mc.OpenRouterModelClient:
        return mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=0, sleep_fn=lambda _s: None
        )

    def test_output_schema_alone_adds_no_response_format(self) -> None:
        schema = {"type": "object", "properties": {"decision": {"type": "string"}}}
        http = FakeHttpClient(_content_response('{"decision":"ACCEPT"}'))
        with patch.dict("os.environ", {}, clear=True):
            self._client(http).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                output_schema=schema,
            )
        body = http.calls[0]["json"]
        self.assertNotIn("response_format", body)

    def test_output_schema_alone_is_byte_identical_to_no_output_schema(self) -> None:
        schema = {"type": "object", "properties": {"decision": {"type": "string"}}}
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
                output_schema=schema,
            )
        self.assertEqual(http_a.calls[0]["json"], http_b.calls[0]["json"])

    def test_tool_spec_alone_still_carries_tools_and_tool_choice(self) -> None:
        # The forced-tool path (what OPENROUTER_STRUCTURED_OUTPUT=1 causes a
        # caller to thread as `tool_spec`) is a separate request key from
        # `response_format` and must be completely unaffected by the
        # capability correction above -- this id still gets schema-enforced
        # output, just not via response_format.
        tool_schema = {"type": "object", "properties": {"decision": {"type": "string"}}}
        http = FakeHttpClient(_content_response('{"decision":"ACCEPT"}'))
        with patch.dict("os.environ", {}, clear=True):
            self._client(http).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                tool_spec=tool_schema,
            )
        body = http.calls[0]["json"]
        self.assertEqual(
            body["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": mc.STRUCTURED_OUTPUT_TOOL_NAME,
                        "parameters": tool_schema,
                    },
                }
            ],
        )
        self.assertEqual(
            body["tool_choice"],
            {"type": "function", "function": {"name": mc.STRUCTURED_OUTPUT_TOOL_NAME}},
        )
        self.assertNotIn("response_format", body)

    def test_tool_spec_and_output_schema_together_carry_tools_but_never_response_format(
        self,
    ) -> None:
        # The real shape a default review sends today: both kwargs passed
        # on the same call. `tools`/`tool_choice` must still be present;
        # `response_format` must still be absent -- proving the capability
        # gate on `response_format` is independent of whatever `tool_spec`
        # carries, not a coincidental side effect of only one key being set.
        tool_schema = {"type": "object", "properties": {"decision": {"type": "string"}}}
        output_schema = {"type": "object", "properties": {"decision": {"type": "string"}}}
        http = FakeHttpClient(_content_response('{"decision":"ACCEPT"}'))
        with patch.dict("os.environ", {}, clear=True):
            self._client(http).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                tool_spec=tool_schema,
                output_schema=output_schema,
            )
        body = http.calls[0]["json"]
        self.assertIn("tools", body)
        self.assertIn("tool_choice", body)
        self.assertNotIn("response_format", body)

    def test_a_still_capable_id_does_get_response_format(self) -> None:
        # The other branch of the same `if`, driven through the same real
        # invoke() and the same live policy file. Without this, every
        # assertion above would stay green if `response_format` stopped
        # being emitted for ANY id -- the absence proved here is
        # capability-driven, not universal. openai/gpt-5.6-sol is a
        # `selectable` entry that still declares `structured_outputs: true`
        # (one of the four non-Anthropic ids issue #142's live probe did not
        # cover, so its declaration is untouched by this fix).
        self.assertTrue(
            mc.openrouter_model_capabilities(CAPABLE_SELECTABLE_MODEL_ID)[
                "structured_outputs"
            ],
            CAPABLE_SELECTABLE_MODEL_ID,
        )
        schema = {"type": "object", "properties": {"decision": {"type": "string"}}}
        http = FakeHttpClient(_content_response('{"decision":"ACCEPT"}'))
        with patch.dict("os.environ", {}, clear=True):
            self._client(http).invoke(
                model_id=CAPABLE_SELECTABLE_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                output_schema=schema,
            )
        body = http.calls[0]["json"]
        self.assertEqual(
            body["response_format"],
            {
                "type": "json_schema",
                "json_schema": {
                    "name": mc.STRUCTURED_OUTPUT_SCHEMA_NAME,
                    "strict": True,
                    "schema": schema,
                },
            },
        )


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestPolicyNoLongerDeclaresStructuredOutputsForZdrRejectedIds,
        TestOpenRouterInvokeOmitsResponseFormatForThePrimaryPin,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
