#!/usr/bin/env python3
"""
Unit tests for OpenRouterModelClient + the OpenRouter model-ID resolvers
(backend/src/model_client.py) — the Docker Compose deployment's direct-provider model
adapter. Fully offline: an injected fake HTTP client stands in for httpx.

Covered:
  1. invoke() builds an OpenAI-compatible Chat Completions request (model,
     system+user messages, max_tokens) with a Bearer key, NO sampling params,
     and returns choices[0].message.content verbatim.
  2. Non-200 raises ModelInvocationError carrying only the status (no body).
  3. Malformed response (missing choices) raises ModelInvocationError.
  4. Transport error raises ModelInvocationError without echoing the request.
  5. No-substance discipline: the error text never contains prompt/response.
  6. Resolvers read openrouter.json and honor the env override; they do NOT
     run the Bedrock single-region check.
  7. The adapter satisfies the same invoke() shape as FakeBedrockClient
     (drop-in for the review passes).
  8. Runtime policy-pin assertion (issue #269): invoke() refuses a model_id
     that matches neither the policy pin nor an active
     OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID override, and allows + logs an
     explicit override.
  9. Zero-data-retention routing (issue #444): EVERY request -- including
     every retry attempt -- carries provider.zdr=true /
     provider.data_collection="deny" / provider.require_parameters=true, and
     no env var can switch the enforcement off. The Bedrock payload is
     unaffected.
 10. Reasoning-token usage capture (issue #661): a response reporting
     `usage.completion_tokens_details.reasoning_tokens` surfaces the count
     on `last_usage`; one that does not omits the key entirely rather than
     defaulting it to 0; the count never reaches `cumulative_usage` (it is
     already inside `completion_tokens`); and the request payload is
     unchanged, with a declared allowance still ADDED to the caller's
     content budget.

Run: python3 tests/test_openrouter_model_client.py
Exit 0 = pass, 1 = fail.
"""

import json
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

SECRET_PROMPT = "CONFIDENTIAL clause: liability capped at $150,000."

# The current policy-pinned ids (model-policy/openrouter.json). Tests below
# that are NOT exercising the policy-pin assertion itself use these so they
# stay focused on transport behavior instead of tripping the new check.
PRIMARY_MODEL_ID = "anthropic/claude-opus-5"  # issue #604 moved this off 4.8
NO_ALLOWANCE_MODEL_ID = "anthropic/claude-sonnet-5"
CRITIC_MODEL_ID = "anthropic/claude-sonnet-4.6"


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeHttpClient:
    """Records the single POST and returns a canned response."""

    def __init__(self, response: FakeResponse | None = None, raise_exc: Exception | None = None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls: list[dict] = []
        self.closed = False

    # Issue #657: the client streams; route .stream() through the
    # canned .post() below (tests/openrouter_sse_double.py).
    stream = sse_stream_adapter

    def post(self, url, json=None, headers=None):  # noqa: A002 - mirror httpx sig
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response

    def close(self):
        self.closed = True


def _ok_response(content: str) -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


class TestOpenRouterInvoke(unittest.TestCase):
    def _client(self, http: FakeHttpClient, **kwargs) -> mc.OpenRouterModelClient:
        # No-op sleep_fn + zero retries by default: these tests exercise the
        # request/response shape, not the issue #270 retry policy (covered
        # separately in tests/test_openrouter_client_hardening.py), and a
        # real time.sleep would slow the gate for no benefit here.
        kwargs.setdefault("max_retries", 0)
        kwargs.setdefault("sleep_fn", lambda _seconds: None)
        return mc.OpenRouterModelClient(
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            http_client=http,
            **kwargs,
        )

    def test_builds_openai_compatible_request_and_returns_content(self) -> None:
        http = FakeHttpClient(_ok_response('{"decision":"ACCEPT"}'))
        with patch.dict("os.environ", {}, clear=True):
            out = self._client(http).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt=SECRET_PROMPT,
                max_output_tokens=8000,
            )
        self.assertEqual(out, '{"decision":"ACCEPT"}')
        self.assertEqual(len(http.calls), 1)
        call = http.calls[0]
        self.assertTrue(call["url"].endswith("/chat/completions"))
        body = call["json"]
        self.assertEqual(body["model"], PRIMARY_MODEL_ID)
        # Issue #677: the primary carries a pinned reasoning allowance, and
        # `max_tokens` is the caller's budget PLUS that allowance -- the
        # thinking is granted on top of the content budget, not carved out of
        # it. Read the pin rather than restating a number that moves with it.
        self.assertEqual(
            body["max_tokens"],
            8000 + mc.openrouter_reasoning_max_tokens(PRIMARY_MODEL_ID),
        )
        self.assertEqual(
            body["messages"],
            [
                {"role": "system", "content": "SYS"},
                {"role": "user", "content": SECRET_PROMPT},
            ],
        )
        # Request contract: no sampling params.
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, body)
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk-test")
        self.assertTrue(http.closed is False)  # injected client is not owned/closed

    def test_non_200_raises_without_body(self) -> None:
        http = FakeHttpClient(FakeResponse(429, {"error": SECRET_PROMPT}))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelInvocationError) as ctx:
                self._client(http).invoke(
                    model_id=PRIMARY_MODEL_ID,
                    system_prompt="s",
                    user_prompt=SECRET_PROMPT,
                    max_output_tokens=10,
                )
        msg = str(ctx.exception)
        self.assertIn("429", msg)
        self.assertNotIn(SECRET_PROMPT, msg)

    def test_malformed_response_raises(self) -> None:
        http = FakeHttpClient(FakeResponse(200, {"unexpected": "shape"}))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelInvocationError):
                self._client(http).invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u",
                    max_output_tokens=10,
                )

    def test_transport_error_raises_without_echoing_request(self) -> None:
        http = FakeHttpClient(raise_exc=OSError(SECRET_PROMPT))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelInvocationError) as ctx:
                self._client(http).invoke(
                    model_id=PRIMARY_MODEL_ID,
                    system_prompt="s",
                    user_prompt=SECRET_PROMPT,
                    max_output_tokens=10,
                )
        self.assertNotIn(SECRET_PROMPT, str(ctx.exception))

    def test_empty_api_key_rejected(self) -> None:
        with self.assertRaises(ValueError):
            mc.OpenRouterModelClient(api_key="")

    def test_is_drop_in_for_the_invoke_protocol(self) -> None:
        # Same keyword-only shape FakeBedrockClient exposes -> injectable at
        # the same call sites in the review passes.
        http = FakeHttpClient(_ok_response("ok"))
        client = self._client(http)
        self.assertTrue(hasattr(client, "invoke"))
        with patch.dict("os.environ", {}, clear=True):
            out = client.invoke(
                model_id=CRITIC_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=1
            )
        self.assertEqual(out, "ok")

    def test_invoke_refuses_unpinned_model_id(self) -> None:
        # Issue #269: invoke() refuses a model_id that matches neither the
        # policy pin nor an active override -- never spends a request on it.
        http = FakeHttpClient(_ok_response("should not be reached"))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.OpenRouterModelPolicyViolation):
                self._client(http).invoke(
                    model_id="openai/gpt-4o",
                    system_prompt="s",
                    user_prompt="u",
                    max_output_tokens=10,
                )
        self.assertEqual(http.calls, [])  # refused before any HTTP call

    def test_invoke_allows_and_logs_explicit_override(self) -> None:
        # An explicit OPENROUTER_PRIMARY_MODEL_ID override is honored, but
        # logged so an operator can see the deployment is running off-policy.
        http = FakeHttpClient(_ok_response("ok"))
        with patch.dict(
            "os.environ", {"OPENROUTER_PRIMARY_MODEL_ID": "openai/gpt-4o"}, clear=True
        ):
            with self.assertLogs("model_client", level="WARNING") as log_ctx:
                out = self._client(http).invoke(
                    model_id="openai/gpt-4o",
                    system_prompt="s",
                    user_prompt="u",
                    max_output_tokens=10,
                )
        self.assertEqual(out, "ok")
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(any("override" in msg.lower() for msg in log_ctx.output))


class TestOpenRouterResolvers(unittest.TestCase):
    def test_reads_policy_file(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(mc.openrouter_primary_model_id(), PRIMARY_MODEL_ID)
            self.assertEqual(mc.openrouter_critic_model_id(), CRITIC_MODEL_ID)

    def test_env_override_wins(self) -> None:
        with patch.dict(
            "os.environ",
            {"OPENROUTER_PRIMARY_MODEL_ID": "openai/gpt-4o", "OPENROUTER_CRITIC_MODEL_ID": "x/y"},
            clear=True,
        ):
            self.assertEqual(mc.openrouter_primary_model_id(), "openai/gpt-4o")
            self.assertEqual(mc.openrouter_critic_model_id(), "x/y")

    def test_resolvers_do_not_run_bedrock_single_region_check(self) -> None:
        # A provider/model form id would be fine anyway, but confirm no
        # ModelPolicyViolation is raised for OpenRouter ids.
        with patch.dict("os.environ", {}, clear=True):
            # Must not raise.
            mc.openrouter_primary_model_id()

    def test_bedrock_check_still_rejects_cross_region_prefix(self) -> None:
        # The existing Bedrock invariant is untouched.
        with self.assertRaises(mc.ModelPolicyViolation):
            mc.enforce_single_region_native_model_id("us.anthropic.claude-opus-4-8")


class TestEnforceOpenRouterPolicyModelId(unittest.TestCase):
    """Direct unit coverage of enforce_openrouter_policy_model_id (issue
    #269), independent of the HTTP invoke() path exercised above."""

    def test_pinned_primary_and_critic_ids_pass(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            mc.enforce_openrouter_policy_model_id(PRIMARY_MODEL_ID)  # must not raise
            mc.enforce_openrouter_policy_model_id(CRITIC_MODEL_ID)  # must not raise

    def test_unpinned_id_with_no_override_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.OpenRouterModelPolicyViolation) as ctx:
                mc.enforce_openrouter_policy_model_id("openai/gpt-4o")
        msg = str(ctx.exception)
        self.assertIn("openai/gpt-4o", msg)
        self.assertIn(PRIMARY_MODEL_ID, msg)
        self.assertIn(CRITIC_MODEL_ID, msg)

    def test_matching_primary_override_passes_and_logs(self) -> None:
        with patch.dict(
            "os.environ", {"OPENROUTER_PRIMARY_MODEL_ID": "openai/gpt-4o"}, clear=True
        ):
            with self.assertLogs("model_client", level="WARNING") as log_ctx:
                mc.enforce_openrouter_policy_model_id("openai/gpt-4o")  # must not raise
        self.assertTrue(any("override" in msg.lower() for msg in log_ctx.output))

    def test_matching_critic_override_passes_and_logs(self) -> None:
        with patch.dict(
            "os.environ", {"OPENROUTER_CRITIC_MODEL_ID": "mistral/large"}, clear=True
        ):
            with self.assertLogs("model_client", level="WARNING") as log_ctx:
                mc.enforce_openrouter_policy_model_id("mistral/large")  # must not raise
        self.assertTrue(any("override" in msg.lower() for msg in log_ctx.output))

    def test_id_not_matching_active_override_still_raises(self) -> None:
        # An override env var is set, but the invoked id doesn't match it
        # (or the pin) -- still refused, not silently allowed because SOME
        # override happens to be active.
        with patch.dict(
            "os.environ", {"OPENROUTER_PRIMARY_MODEL_ID": "openai/gpt-4o"}, clear=True
        ):
            with self.assertRaises(mc.OpenRouterModelPolicyViolation):
                mc.enforce_openrouter_policy_model_id("some/other-model")


class TestOpenRouterDataRetentionPosture(unittest.TestCase):
    """Issue #444: client contracts must never be routed to a provider that
    retains them or may train on them. The guarantee is a per-REQUEST
    parameter (an account-level toggle is invisible to this codebase and lost
    when the key is swapped), so it is asserted on the wire payload the real
    `invoke()` code path builds -- not on a mock of the payload builder."""

    def _client(self, http: FakeHttpClient, **kwargs) -> mc.OpenRouterModelClient:
        kwargs.setdefault("max_retries", 0)
        kwargs.setdefault("sleep_fn", lambda _seconds: None)
        return mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, **kwargs
        )

    def _invoke_ok(self, http: FakeHttpClient, env: dict | None = None) -> None:
        with patch.dict("os.environ", env or {}, clear=True):
            self._client(http).invoke(
                model_id=PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt=SECRET_PROMPT,
                max_output_tokens=8000,
            )

    def _assert_enforced(self, body: dict) -> None:
        self.assertIn(
            "provider",
            body,
            "OpenRouter request has no `provider` block -- the contract is "
            "eligible to route to a retaining/training provider (issue #444).",
        )
        provider = body["provider"]
        self.assertIs(provider["zdr"], True)
        self.assertEqual(provider["data_collection"], "deny")
        self.assertIs(provider["require_parameters"], True)

    def test_request_carries_zdr_and_denies_data_collection(self) -> None:
        http = FakeHttpClient(_ok_response('{"decision":"ACCEPT"}'))
        self._invoke_ok(http)
        self._assert_enforced(http.calls[0]["json"])

    def test_every_retry_attempt_also_carries_the_provider_block(self) -> None:
        # A 429 is retried (issue #270). Enforcement must ride on EVERY
        # attempt, not just the first -- a retry that dropped it would
        # disclose the contract on exactly the paths nobody watches.
        http = FakeHttpClient(FakeResponse(429, {}))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelInvocationError):
                self._client(http, max_retries=2).invoke(
                    model_id=PRIMARY_MODEL_ID,
                    system_prompt="SYS",
                    user_prompt=SECRET_PROMPT,
                    max_output_tokens=8000,
                )
        self.assertEqual(len(http.calls), 3)  # initial + 2 retries
        for call in http.calls:
            self._assert_enforced(call["json"])

    def test_no_env_var_can_switch_the_enforcement_off(self) -> None:
        # Guards against a future opt-out knob: a deployment reviewing real
        # agreements has no legitimate non-ZDR mode, so these must be inert.
        http = FakeHttpClient(_ok_response("{}"))
        self._invoke_ok(
            http,
            {
                "OPENROUTER_ZDR": "false",
                "OPENROUTER_DATA_COLLECTION": "allow",
                "OPENROUTER_REQUIRE_PARAMETERS": "false",
                "OPENROUTER_PROVIDER_ROUTING": "",
                "OPENROUTER_ALLOW_DATA_RETENTION": "1",
            },
        )
        self._assert_enforced(http.calls[0]["json"])

    def test_payload_mutation_cannot_poison_the_next_request(self) -> None:
        # The payload embeds a COPY of the module-level policy; a caller (or a
        # retry path) mutating one request's block must not edit the policy
        # out from under every later call in the process.
        http = FakeHttpClient(_ok_response("{}"))
        self._invoke_ok(http)
        http.calls[0]["json"]["provider"]["zdr"] = False
        self._invoke_ok(http)
        self._assert_enforced(http.calls[1]["json"])

    def test_bedrock_payload_is_unaffected(self) -> None:
        # Bedrock's data posture is governed by AWS contract terms, not by an
        # OpenRouter routing field -- the Bedrock request must not grow one.
        captured: dict = {}

        class FakeBody:
            @staticmethod
            def read() -> bytes:
                return b'{"content": [{"text": "ok"}]}'

        class FakeRuntime:
            @staticmethod
            def invoke_model(**kwargs):
                captured.update(kwargs)
                return {"body": FakeBody()}

        out = mc.LiveBedrockModelClient(
            bedrock_runtime_client=FakeRuntime()
        ).invoke(
            model_id="anthropic.claude-opus-4-8",
            system_prompt="SYS",
            user_prompt=SECRET_PROMPT,
            max_output_tokens=8000,
        )
        self.assertEqual(out, "ok")
        self.assertNotIn("provider", json.loads(captured["body"]))


# ---------------------------------------------------------------------------
# Issue #661: reasoning-token usage capture.
#
# `max_tokens` on an OpenRouter Chat Completions request is a COMBINED
# ceiling across a model's reasoning AND content tokens, so whatever the
# served model spends thinking is silently taken out of the content budget
# `invoke()` set. Nothing recorded that number, which is why the per-model
# `reasoning_max_tokens` allowance could only ever be a guess. These tests
# pin the OBSERVABILITY half: the count reaches `last_usage` when the
# provider reports it, is ABSENT (never 0) when it does not, and changes
# neither the request payload nor any usage-derived cost figure.
#
# Every response body below is fed through the SAME `stream = sse_stream_
# adapter` transport double the rest of this file uses, so the count is read
# off the usage-only terminal SSE chunk production actually parses -- not
# poked onto `last_usage` directly.
# ---------------------------------------------------------------------------


def _usage_response(content: str, usage: dict) -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": content}}], "usage": usage})


class TestOpenRouterReasoningTokenUsage(unittest.TestCase):
    def _invoke(self, usage: dict, *, model_id: str = PRIMARY_MODEL_ID, max_output_tokens: int = 8000):
        http = FakeHttpClient(_usage_response('{"decision":"ACCEPT"}', usage))
        client = mc.OpenRouterModelClient(
            api_key="sk-test",
            http_client=http,
            max_retries=0,
            sleep_fn=lambda _seconds: None,
        )
        with patch.dict("os.environ", {}, clear=True):
            client.invoke(
                model_id=model_id,
                system_prompt="SYS",
                user_prompt=SECRET_PROMPT,
                max_output_tokens=max_output_tokens,
            )
        return client, http

    def test_parser_carries_reported_reasoning_tokens(self) -> None:
        parsed = mc.parse_openrouter_usage(
            {
                "usage": {
                    "prompt_tokens": 61000,
                    "completion_tokens": 9400,
                    "completion_tokens_details": {"reasoning_tokens": 2100},
                }
            }
        )
        self.assertEqual(parsed["reasoning_tokens"], 2100)
        # Reasoning tokens are already counted INSIDE completion_tokens by
        # the provider, so the base counts must be untouched -- adding them
        # anywhere would double-bill the same tokens at settlement.
        self.assertEqual(parsed["input_tokens"], 61000)
        self.assertEqual(parsed["output_tokens"], 9400)

    def test_parser_omits_reasoning_tokens_when_not_reported(self) -> None:
        """Absent, never 0: "the response did not report reasoning tokens"
        and "the model spent zero tokens thinking" are different facts, and
        only one of them is supported by a missing field."""
        for label, usage in (
            ("no details block", {"prompt_tokens": 10, "completion_tokens": 5}),
            (
                "details block without the key",
                {"prompt_tokens": 10, "completion_tokens": 5, "completion_tokens_details": {}},
            ),
            (
                "non-dict details block",
                {"prompt_tokens": 10, "completion_tokens": 5, "completion_tokens_details": None},
            ),
            (
                "non-int count",
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "completion_tokens_details": {"reasoning_tokens": "2100"},
                },
            ),
        ):
            with self.subTest(label):
                parsed = mc.parse_openrouter_usage({"usage": usage})
                self.assertNotIn("reasoning_tokens", parsed)
                self.assertEqual(parsed["input_tokens"], 10)
                self.assertEqual(parsed["output_tokens"], 5)

    def test_parser_keeps_a_genuinely_reported_zero(self) -> None:
        """The other side of the branch: a provider that DID report the
        field as 0 (a reasoning-class model that happened not to think on
        this call) must record 0, not absence."""
        parsed = mc.parse_openrouter_usage(
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                }
            }
        )
        self.assertEqual(parsed["reasoning_tokens"], 0)

    def test_invoke_surfaces_reasoning_tokens_on_last_usage(self) -> None:
        client, _http = self._invoke(
            {
                "prompt_tokens": 61000,
                "completion_tokens": 9400,
                "completion_tokens_details": {"reasoning_tokens": 2100},
            }
        )
        self.assertEqual(client.last_usage["reasoning_tokens"], 2100)
        self.assertEqual(client.last_usage["input_tokens"], 61000)
        self.assertEqual(client.last_usage["output_tokens"], 9400)

    def test_invoke_omits_reasoning_tokens_when_the_response_has_none(self) -> None:
        client, _http = self._invoke({"prompt_tokens": 61000, "completion_tokens": 9400})
        self.assertNotIn("reasoning_tokens", client.last_usage)

    def test_cumulative_usage_never_accumulates_reasoning_tokens(self) -> None:
        """The running instance total feeds
        `reviews.compute_actual_usd_cents_from_usage`. Reasoning tokens are
        already inside `completion_tokens`, so accumulating them there would
        bill the same tokens twice."""
        client, _http = self._invoke(
            {
                "prompt_tokens": 61000,
                "completion_tokens": 9400,
                "completion_tokens_details": {"reasoning_tokens": 2100},
            }
        )
        self.assertEqual(
            client.cumulative_usage, {"input_tokens": 61000, "output_tokens": 9400}
        )

    def test_request_payload_is_unchanged_for_a_model_with_no_allowance(self) -> None:
        """A model with NO declared allowance must have a byte-identical
        request: `max_tokens` is exactly the caller's content budget, and no
        reasoning/thinking key appears anywhere in the body. Recording a
        provider's reasoning spend never changes what we send.

        Issue #677 pinned an allowance for the PRIMARY model, so this asserts
        the invariant against an id that genuinely carries none."""
        # Issue #677 pinned a reasoning allowance for the PRIMARY model, so the
        # invariant this test protects -- "a model with NO declared allowance
        # sends the caller's content budget unchanged and no reasoning key" --
        # is now asserted against an id that genuinely carries none, rather than
        # against whichever model happens to be pinned today.
        self.assertEqual(mc.openrouter_reasoning_max_tokens(NO_ALLOWANCE_MODEL_ID), 0)
        _client, http = self._invoke(
            {
                "prompt_tokens": 61000,
                "completion_tokens": 9400,
                "completion_tokens_details": {"reasoning_tokens": 2100},
            },
            model_id=NO_ALLOWANCE_MODEL_ID,
        )
        body = http.calls[0]["json"]
        self.assertEqual(body["max_tokens"], 8000)
        self.assertNotIn("reasoning", body)
        self.assertNotIn("thinking", body)
        self.assertNotIn("reasoning_tokens", body)

    def test_declared_allowance_is_still_added_on_top_of_the_content_budget(self) -> None:
        """The allowance stays ADDED to the caller's budget, never carved
        out of it -- read off the shipped policy rather than hardcoded, so
        this cannot silently agree with a mirror of the code under test."""
        policy = mc.load_openrouter_policy()
        with_allowance = [
            entry
            for entry in (policy.get("selectable") or [])
            if int(entry.get("reasoning_max_tokens") or 0) > 0
        ]
        self.assertTrue(
            with_allowance,
            "model-policy/openrouter.json must still pin at least one non-zero "
            "reasoning_max_tokens for this assertion to mean anything.",
        )
        entry = with_allowance[0]
        allowance = int(entry["reasoning_max_tokens"])
        _client, http = self._invoke(
            {"prompt_tokens": 10, "completion_tokens": 5},
            model_id=str(entry["model_id"]),
            max_output_tokens=8000,
        )
        self.assertEqual(http.calls[0]["json"]["max_tokens"], 8000 + allowance)

    def test_record_defaults_reasoning_tokens_to_none(self) -> None:
        record = mc.ModelInvocationRecord(
            review_id="r",
            pass_name="primary",
            model_id=PRIMARY_MODEL_ID,
            attempt_number=1,
            outcome="success",
            input_tokens_est=1,
            output_tokens_est=1,
        )
        self.assertIsNone(record.reasoning_tokens)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestOpenRouterInvoke))
    suite.addTests(loader.loadTestsFromTestCase(TestOpenRouterResolvers))
    suite.addTests(loader.loadTestsFromTestCase(TestEnforceOpenRouterPolicyModelId))
    suite.addTests(loader.loadTestsFromTestCase(TestOpenRouterDataRetentionPosture))
    suite.addTests(loader.loadTestsFromTestCase(TestOpenRouterReasoningTokenUsage))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
