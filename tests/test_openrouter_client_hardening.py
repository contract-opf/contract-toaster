#!/usr/bin/env python3
"""
Slice test (TDD) for issue #270: "OpenRouterModelClient hardening:
connection reuse, bounded retries, clean context-length fail-closed path".

## Root problem this proves fixed

Before this slice, `OpenRouterModelClient.invoke` (backend/src/model_client.py)
created a FRESH `httpx.Client` per call (no connection reuse), had NO
in-client retry for a transient 429/5xx/connection failure (the only retry
was the pass-level schema retry in `scripts/primary_review_pass.py`, which
re-pays the full prompt on every attempt), and a provider context-length
4xx surfaced as a generic `ModelInvocationError` that propagated to a
generic pipeline `ERROR` instead of the clean, documented oversize path
(`docs/output-contract.md` -> "Oversized-document user message").

This test FAILS on the unmodified tree:
  - `mc.ModelContextLengthExceededError` does not exist (AttributeError).
  - The single-instance connection-reuse and bounded-retry assertions fail
    against the old "fresh client per call, no retry" behavior.
Run with: python3 tests/test_openrouter_client_hardening.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
for _dir in (BACKEND_SRC, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client as mc  # noqa: E402
import primary_review_pass as pp  # noqa: E402

PRIMARY_MODEL_ID = "anthropic/claude-opus-4.8"
SECRET_PROMPT = "CONFIDENTIAL clause: liability capped at $150,000."


def _valid_primary_response_text() -> str:
    return (MODEL_RESPONSES_DIR / "primary_accept_valid.json").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class ScriptedHttpClient:
    """Returns/raises a scripted sequence of outcomes, one per `.post()`
    call, so a test can drive "fails N times then succeeds"."""

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url, json=None, headers=None):  # noqa: A002 - mirror httpx sig
        self.calls.append({"url": url, "json": json, "headers": headers})
        if not self._outcomes:
            raise AssertionError("ScriptedHttpClient ran out of scripted outcomes")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


def _ok_response(content: str = "ok") -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


def _no_sleep(_seconds: float) -> None:
    pass  # tests must never actually sleep -- offline/fast gate


class TestConnectionReuse(unittest.TestCase):
    """Scope item 1: one shared httpx.Client per instance, closed properly."""

    def test_httpx_client_constructed_once_and_reused_across_invocations(self) -> None:
        with patch("httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value
            mock_instance.post.return_value = _ok_response("first")
            client = mc.OpenRouterModelClient(
                api_key="sk-test", max_retries=0, sleep_fn=_no_sleep
            )
            with patch.dict("os.environ", {}, clear=True):
                client.invoke(
                    model_id=PRIMARY_MODEL_ID,
                    system_prompt="s",
                    user_prompt="u",
                    max_output_tokens=10,
                )
                client.invoke(
                    model_id=PRIMARY_MODEL_ID,
                    system_prompt="s",
                    user_prompt="u",
                    max_output_tokens=10,
                )
        self.assertEqual(
            mock_client_cls.call_count,
            1,
            "httpx.Client must be constructed exactly ONCE per OpenRouterModelClient "
            "instance and reused, not once per invoke() call.",
        )
        self.assertEqual(mock_instance.post.call_count, 2)
        mock_instance.close.assert_not_called()
        client.close()
        mock_instance.close.assert_called_once()

    def test_close_leaves_an_injected_client_open(self) -> None:
        http = ScriptedHttpClient([_ok_response("ok")])
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=0, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            client.invoke(
                model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=10
            )
        client.close()
        self.assertFalse(
            http.closed, "An injected http_client belongs to the caller -- close() must not touch it."
        )

    def test_context_manager_closes_owned_client(self) -> None:
        with patch("httpx.Client") as mock_client_cls:
            mock_instance = mock_client_cls.return_value
            mock_instance.post.return_value = _ok_response("ok")
            with patch.dict("os.environ", {}, clear=True):
                with mc.OpenRouterModelClient(
                    api_key="sk-test", max_retries=0, sleep_fn=_no_sleep
                ) as client:
                    client.invoke(
                        model_id=PRIMARY_MODEL_ID,
                        system_prompt="s",
                        user_prompt="u",
                        max_output_tokens=10,
                    )
        mock_instance.close.assert_called_once()


class TestBoundedJitteredRetries(unittest.TestCase):
    """Scope item 2: bounded retry with backoff for 429/5xx/connection
    errors only; no retry on other 4xx; retries never duplicate the
    pass-level attempt/ledger accounting."""

    def test_429_then_success_retries_and_returns_content(self) -> None:
        http = ScriptedHttpClient([FakeResponse(429, {"error": "rate limited"}), _ok_response("recovered")])
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=3, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            out = client.invoke(
                model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=10
            )
        self.assertEqual(out, "recovered")
        self.assertEqual(len(http.calls), 2)

    def test_503_then_success_retries(self) -> None:
        http = ScriptedHttpClient([FakeResponse(503, {}), FakeResponse(502, {}), _ok_response("ok")])
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=3, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            out = client.invoke(
                model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=10
            )
        self.assertEqual(out, "ok")
        self.assertEqual(len(http.calls), 3)

    def test_connection_error_then_success_retries(self) -> None:
        http = ScriptedHttpClient([OSError("connection reset"), _ok_response("ok")])
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=3, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            out = client.invoke(
                model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=10
            )
        self.assertEqual(out, "ok")
        self.assertEqual(len(http.calls), 2)

    def test_retries_are_bounded_then_raises(self) -> None:
        http = ScriptedHttpClient([FakeResponse(500, {})] * 10)
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=2, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelInvocationError):
                client.invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=10
                )
        # max_retries=2 -> 1 initial attempt + 2 retries = 3 total calls, never more.
        self.assertEqual(len(http.calls), 3)

    def test_non_429_4xx_is_never_retried(self) -> None:
        http = ScriptedHttpClient([FakeResponse(401, {"error": "unauthorized"})] * 5)
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=3, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelInvocationError):
                client.invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=10
                )
        self.assertEqual(len(http.calls), 1, "A non-429 4xx is a deterministic rejection -- must not be retried.")

    def test_backoff_is_bounded_and_jittered(self) -> None:
        http = ScriptedHttpClient([FakeResponse(500, {})] * 3 + [_ok_response("ok")])
        sleeps: list[float] = []
        client = mc.OpenRouterModelClient(
            api_key="sk-test",
            http_client=http,
            max_retries=3,
            backoff_base_seconds=1.0,
            backoff_max_seconds=2.5,
            backoff_jitter_seconds=0.1,
            sleep_fn=sleeps.append,
        )
        with patch.dict("os.environ", {}, clear=True):
            client.invoke(
                model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=10
            )
        self.assertEqual(len(sleeps), 3)  # one sleep before each of the 3 retries
        for i, delay in enumerate(sleeps):
            uncapped_base = 1.0 * (2**i)
            capped_base = min(uncapped_base, 2.5)
            self.assertGreaterEqual(delay, capped_base)
            self.assertLessEqual(delay, capped_base + 0.1)

    def test_retry_is_opaque_to_the_caller_no_duplicate_spend_settle(self) -> None:
        # A caller (primary_review_pass.py) ledgers exactly ONE pass-level
        # attempt per invoke() call, however many transport-level retries
        # happened underneath -- retries must never duplicate spend settle.
        http = ScriptedHttpClient([FakeResponse(429, {})] * 2 + [_ok_response(_valid_primary_response_text())])
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=3, sleep_fn=_no_sleep
        )
        ledger: list = []
        with patch.dict("os.environ", {}, clear=True):
            result = pp.run_primary_pass(
                review_id="review-retry-opaque",
                diff_hunks=[],
                anchored_clauses=[],
                retrieved_precedent=[],
                playbook={"policy_id": "p", "clauses": []},
                model_client=client,
                model_id=PRIMARY_MODEL_ID,
                ledger_write=ledger.append,
                doc_text="short doc",
            )
        self.assertEqual(result.get("status"), "OK")
        self.assertEqual(result.get("attempts"), 1)  # ONE pass-level attempt
        self.assertEqual(len(ledger), 1)  # ONE ledger row, not 3
        self.assertEqual(len(http.calls), 3)  # even though 3 HTTP calls happened underneath


class TestContextLengthFailClosed(unittest.TestCase):
    """Scope item 3: a provider context-length/413-style rejection maps to
    the documented oversize status, not a generic ERROR."""

    def test_413_raises_context_length_error_not_generic(self) -> None:
        http = ScriptedHttpClient([FakeResponse(413, {"error": SECRET_PROMPT})])
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=3, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelContextLengthExceededError) as ctx:
                client.invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt=SECRET_PROMPT, max_output_tokens=10
                )
        self.assertNotIn(SECRET_PROMPT, str(ctx.exception))
        self.assertEqual(len(http.calls), 1, "A context-length rejection is deterministic -- must not be retried.")

    def test_400_with_context_length_error_code_raises_context_length_error(self) -> None:
        http = ScriptedHttpClient(
            [FakeResponse(400, {"error": {"code": "context_length_exceeded", "message": SECRET_PROMPT}})]
        )
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=3, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelContextLengthExceededError):
                client.invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt=SECRET_PROMPT, max_output_tokens=10
                )

    def test_400_with_context_length_message_raises_context_length_error(self) -> None:
        http = ScriptedHttpClient(
            [FakeResponse(400, {"error": {"message": "This model's maximum context length is 128000 tokens."}})]
        )
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=3, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelContextLengthExceededError):
                client.invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=10
                )

    def test_ordinary_400_is_not_misclassified_as_context_length(self) -> None:
        http = ScriptedHttpClient([FakeResponse(400, {"error": {"code": "invalid_request", "message": "bad shape"}})])
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=3, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelInvocationError) as ctx:
                client.invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u", max_output_tokens=10
                )
        self.assertNotIsInstance(ctx.exception, mc.ModelContextLengthExceededError)

    def test_primary_pass_maps_context_length_error_to_document_too_large(self) -> None:
        # Integration: run_primary_pass (scripts/primary_review_pass.py)
        # catches ModelContextLengthExceededError and returns the SAME
        # {"status": "MANUAL_REVIEW_REQUIRED", "reason": "document_too_large"}
        # shape the step-14 pre-call estimate produces -- never a generic
        # ERROR (no exception escapes to the caller).
        class _RejectingClient:
            def __init__(self) -> None:
                self.calls = 0

            def invoke(self, **kwargs):
                self.calls += 1
                raise mc.ModelContextLengthExceededError(
                    "OpenRouter rejected the request as exceeding the model's context length (HTTP 413)."
                )

        client = _RejectingClient()
        ledger: list = []
        result = pp.run_primary_pass(
            review_id="review-context-length",
            diff_hunks=[],
            anchored_clauses=[],
            retrieved_precedent=[],
            playbook={"policy_id": "p", "clauses": []},
            model_client=client,
            model_id=PRIMARY_MODEL_ID,
            ledger_write=ledger.append,
            doc_text="short doc",
        )
        self.assertEqual(result.get("status"), "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(result.get("reason"), "document_too_large")
        self.assertEqual(client.calls, 1, "A context-length rejection must not be retried at the pass level either.")
        self.assertEqual(len(ledger), 1, "Exactly one ledger row -- the rejected attempt -- must still be written.")
        self.assertEqual(ledger[0].outcome, "failure")


class TestNoSubstanceDiscipline(unittest.TestCase):
    """Scope item: no document substance in any error/log (existing
    discipline preserved for the new exception + retry paths)."""

    def test_context_length_error_never_echoes_prompt_or_body(self) -> None:
        http = ScriptedHttpClient([FakeResponse(413, {"error": {"message": SECRET_PROMPT}})])
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=3, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelContextLengthExceededError) as ctx:
                client.invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt=SECRET_PROMPT, user_prompt=SECRET_PROMPT, max_output_tokens=10
                )
        self.assertNotIn(SECRET_PROMPT, str(ctx.exception))

    def test_retry_exhausted_error_never_echoes_prompt(self) -> None:
        http = ScriptedHttpClient([FakeResponse(500, {"error": SECRET_PROMPT})] * 5)
        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=1, sleep_fn=_no_sleep
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelInvocationError) as ctx:
                client.invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt=SECRET_PROMPT, max_output_tokens=10
                )
        self.assertNotIn(SECRET_PROMPT, str(ctx.exception))


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestConnectionReuse,
        TestBoundedJitteredRetries,
        TestContextLengthFailClosed,
        TestNoSubstanceDiscipline,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
