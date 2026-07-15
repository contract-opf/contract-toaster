#!/usr/bin/env python3
"""
Unit tests for OpenRouterModelClient + the OpenRouter model-ID resolvers
(backend/src/model_client.py) — the DTS deployment's direct-provider model
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

Run: python3 tests/test_openrouter_model_client.py
Exit 0 = pass, 1 = fail.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import model_client as mc  # noqa: E402

SECRET_PROMPT = "CONFIDENTIAL clause: liability capped at $150,000."

# The current policy-pinned ids (model-policy/openrouter.json). Tests below
# that are NOT exercising the policy-pin assertion itself use these so they
# stay focused on transport behavior instead of tripping the new check.
PRIMARY_MODEL_ID = "anthropic/claude-opus-4.8"
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
        self.assertEqual(body["max_tokens"], 8000)
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


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestOpenRouterInvoke))
    suite.addTests(loader.loadTestsFromTestCase(TestOpenRouterResolvers))
    suite.addTests(loader.loadTestsFromTestCase(TestEnforceOpenRouterPolicyModelId))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
