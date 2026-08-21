#!/usr/bin/env python3
"""
Regression test: the primary pass's bounded retry must actually be a RETRY --
it must change something about the next attempt, and it must cover the two
ways a real provider refuses an otherwise-good review.

## Root problem this proves fixed

Observed 2026-08-04 against the real educational-affiliation playbook and a
real 23-clause affiliation agreement, with two different model pairs:

  1. The model returned a complete, genuinely useful review whose
     `confidence_state` was the natural-language word "medium". The strict
     schema rejected it. `run_primary_pass` then re-sent a BYTE-IDENTICAL
     prompt, got the same word back, and returned
     ERROR_MANUAL_REVIEW_REQUIRED -- two full-price model calls and 5.6
     minutes of wall clock to arrive at the same wrong answer twice. The
     "bounded retry" was pure waste: nothing about attempt 2 differed from
     attempt 1, so nothing about the outcome could differ either.

  2. `ModelOutputTruncatedError` (finish_reason == "length") was raised by the
     client and caught by NOBODY in the pass -- it propagated straight out of
     `run_review`, killing the whole review on a single truncated response and
     discarding every token already paid for. The pass's own retry budget,
     sitting right there, was never offered the chance to ask for more room.

Both are fixed by making the retry INFORMED: the next attempt carries what
went wrong (a validation error fed back, per issue #417) or a bigger content
budget (a truncation retried with more room), instead of replaying the same
request and hoping.

Run with: python3 tests/test_primary_pass_retry_recovery.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_review_pass as cp  # noqa: E402
import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402

MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
_TEST_MODEL_ID = "anthropic.claude-opus-4-8"
_CRITIC_MODEL_ID = "anthropic.claude-sonnet-4-6"


def _fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _run(client: Any, ledger: list[Any] | None = None, **overrides: Any) -> dict[str, Any]:
    return pp.run_primary_pass(
        review_id="retry-recovery",
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=_playbook(),
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=(ledger if ledger is not None else []).append,
        doc_text="Section 8. Each party's aggregate liability shall not exceed $75,000.",
        **overrides,
    )


def _run_critic(client: Any, ledger: list[Any] | None = None, **overrides: Any) -> dict[str, Any]:
    return cp.run_critic_pass(
        review_id="critic-retry-recovery",
        diff_hunks=[],
        anchored_clauses=[],
        primary_output=json.loads(_fixture("primary_request_change_valid.json")),
        playbook=_playbook(),
        model_client=client,
        model_id=_CRITIC_MODEL_ID,
        ledger_write=(ledger if ledger is not None else []).append,
        **overrides,
    )


# ---------------------------------------------------------------------------
# The real-model shape: a complete review whose confidence_state is a word the
# schema does not know. Built from the VALID fixture so the only thing wrong
# with it is the one field -- exactly what the live runs produced.
# ---------------------------------------------------------------------------
def _review_with_bad_confidence_state() -> str:
    body = json.loads(_fixture("primary_request_change_valid.json"))
    body["confidence_state"] = "medium"
    return json.dumps(body)


# Critic-loop counterpart -- built from a VALID critic fixture the same way,
# so the only thing wrong with it is the same one field the live incident
# actually produced.
def _critic_review_with_bad_confidence_state() -> str:
    body = json.loads(_fixture("critic_no_delta_accept_valid.json"))
    body["confidence_state"] = "medium"
    return json.dumps(body)


class TruncatingThenValidClient:
    """Raises the client's real truncation error on the first call, then
    returns a valid review -- recording the budget each attempt asked for."""

    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.calls: list[dict[str, Any]] = []

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        self.calls.append({"max_output_tokens": max_output_tokens, "user_prompt": user_prompt})
        if len(self.calls) == 1:
            raise model_client.ModelOutputTruncatedError(
                "OpenRouter truncated the response before it finished "
                "(finish_reason='length', HTTP 200).",
                status_code=200,
            )
        return self._response_text


class AlwaysTruncatingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke(self, *, model_id: str, system_prompt: str, user_prompt: str, max_output_tokens: int) -> str:
        self.calls.append({"max_output_tokens": max_output_tokens})
        raise model_client.ModelOutputTruncatedError(
            "OpenRouter truncated the response before it finished "
            "(finish_reason='length', HTTP 200).",
            status_code=200,
        )


# ---------------------------------------------------------------------------
# 1. Issue #417 — the validation error is fed back to the next attempt.
# ---------------------------------------------------------------------------


def test_schema_error_is_fed_back_into_the_retry_prompt(failures: list[str]) -> None:
    client = model_client.FakeBedrockClient(
        {
            _TEST_MODEL_ID: [
                _review_with_bad_confidence_state(),
                _fixture("primary_request_change_valid.json"),
            ]
        }
    )
    result = _run(client)

    if result.get("status") != "OK":
        failures.append(f"[1a] Expected OK after an informed retry; got {result!r}")
    if len(client.calls) != 2:
        failures.append(f"[1b] Expected exactly 2 attempts; got {len(client.calls)}")
        return

    first, second = client.calls[0]["user_prompt"], client.calls[1]["user_prompt"]
    if first == second:
        failures.append(
            "[1c] The retry re-sent a byte-identical user prompt -- a retry that changes "
            "nothing about the request cannot change anything about the response"
        )
    if "confidence_state" not in second:
        failures.append(
            f"[1d] The retry prompt must name the field that failed validation; "
            f"it did not mention confidence_state"
        )
    if pp.RETRY_CORRECTION_HEADING not in second:
        failures.append(
            f"[1e] The retry prompt must carry the delimited retry-correction block "
            f"({pp.RETRY_CORRECTION_HEADING!r}); it did not"
        )
    if "schema_invalid" not in second:
        failures.append(
            "[1f] The retry prompt must include the exact schema_invalid validator "
            "error token from attempt 1, not just the field name"
        )


def test_the_first_attempt_prompt_is_never_polluted(failures: list[str]) -> None:
    """Attempt 1 must be exactly what it always was.

    The correction is appended for the NEXT attempt only -- a review that
    validates first time must send a prompt with no error-feedback text in it,
    or every successful review starts paying for a fault it never had.
    """
    client = model_client.FakeBedrockClient(
        {_TEST_MODEL_ID: [_fixture("primary_request_change_valid.json")]}
    )
    result = _run(client)

    if result.get("status") != "OK":
        failures.append(f"[2a] Expected OK on the first attempt; got {result!r}")
    if len(client.calls) != 1:
        failures.append(f"[2b] Expected exactly 1 attempt; got {len(client.calls)}")
        return
    if pp.RETRY_CORRECTION_HEADING in client.calls[0]["user_prompt"]:
        failures.append("[2c] The first attempt's prompt must carry no correction block")


# ---------------------------------------------------------------------------
# 1b. Issue #417's second acceptance criterion -- "Same property for the
# critic loop." `run_critic_pass` (scripts/critic_review_pass.py) mirrors
# `run_primary_pass` deliberately (see that module's docstring); the ticket's
# Scope forbids extracting a shared module for this fix, so the property has
# to be proven against the critic's own wiring, not inferred from the
# primary pass's.
# ---------------------------------------------------------------------------


def test_critic_schema_error_is_fed_back_into_the_retry_prompt(failures: list[str]) -> None:
    client = model_client.FakeBedrockClient(
        {
            _CRITIC_MODEL_ID: [
                _critic_review_with_bad_confidence_state(),
                _fixture("critic_no_delta_accept_valid.json"),
            ]
        }
    )
    result = _run_critic(client)

    if result.get("status") != "OK":
        failures.append(f"[6a] Expected OK after an informed critic retry; got {result!r}")
    if len(client.calls) != 2:
        failures.append(f"[6b] Expected exactly 2 critic attempts; got {len(client.calls)}")
        return

    first, second = client.calls[0]["user_prompt"], client.calls[1]["user_prompt"]
    if first == second:
        failures.append(
            "[6c] The critic retry re-sent a byte-identical user prompt -- a retry that "
            "changes nothing about the request cannot change anything about the response"
        )
    if pp.RETRY_CORRECTION_HEADING not in second:
        failures.append(
            f"[6d] The critic retry prompt must carry the delimited retry-correction block "
            f"({pp.RETRY_CORRECTION_HEADING!r}); it did not"
        )
    if "schema_invalid" not in second:
        failures.append(
            "[6e] The critic retry prompt must include the exact schema_invalid validator "
            "error token from attempt 1"
        )


def test_critic_first_attempt_prompt_is_never_polluted(failures: list[str]) -> None:
    """Critic-loop counterpart of `test_the_first_attempt_prompt_is_never_polluted`
    (AC3): attempt 1 must be exactly what it always was, with no correction
    block, for a critic review that validates on the first try."""
    client = model_client.FakeBedrockClient(
        {_CRITIC_MODEL_ID: [_fixture("critic_no_delta_accept_valid.json")]}
    )
    result = _run_critic(client)

    if result.get("status") != "OK":
        failures.append(f"[7a] Expected OK on the critic's first attempt; got {result!r}")
    if len(client.calls) != 1:
        failures.append(f"[7b] Expected exactly 1 critic attempt; got {len(client.calls)}")
        return
    if pp.RETRY_CORRECTION_HEADING in client.calls[0]["user_prompt"]:
        failures.append("[7c] The critic's first attempt prompt must carry no correction block")


# ---------------------------------------------------------------------------
# 2. Truncation is retried with more room instead of killing the review.
# ---------------------------------------------------------------------------


def test_truncation_retries_with_a_larger_budget(failures: list[str]) -> None:
    client = TruncatingThenValidClient(_fixture("primary_request_change_valid.json"))
    result = _run(client)

    if result.get("status") != "OK":
        failures.append(
            f"[3a] A single truncated response must not kill the review -- "
            f"expected OK after retrying with more room; got {result!r}"
        )
    if len(client.calls) != 2:
        failures.append(f"[3b] Expected exactly 2 attempts; got {len(client.calls)}")
        return
    first, second = client.calls[0]["max_output_tokens"], client.calls[1]["max_output_tokens"]
    if second <= first:
        failures.append(
            f"[3c] The retry after a truncation must ask for MORE room; "
            f"attempt 1 asked for {first}, attempt 2 asked for {second}"
        )


def test_exhausted_truncation_still_surfaces_the_truncation_cause(failures: list[str]) -> None:
    """When even the bigger budget truncates, the review still fails -- but as
    a truncation, not as some other cause.

    backend/src/pipeline_runner.classify_failure_reason maps the exception to
    `model_output_truncated`, which is the token Diagnostics and the result
    panel key their copy off; swallowing it into a generic schema failure
    would send the operator to the wrong fix.
    """
    client = AlwaysTruncatingClient()
    raised: BaseException | None = None
    try:
        _run(client)
    except model_client.ModelOutputTruncatedError as exc:
        raised = exc

    if raised is None:
        failures.append("[4a] An unrecoverable truncation must still reach the caller")
    if len(client.calls) != 2:
        failures.append(
            f"[4b] Expected the truncation to consume both attempts before giving up; "
            f"got {len(client.calls)}"
        )


def test_every_truncated_attempt_is_still_ledgered(failures: list[str]) -> None:
    """Issue #81's "every attempt ledgered" invariant must survive the new
    except path -- a truncated attempt was still paid for."""
    client = TruncatingThenValidClient(_fixture("primary_request_change_valid.json"))
    ledger: list[Any] = []
    _run(client, ledger=ledger)

    if len(ledger) != 2:
        failures.append(f"[5a] Expected a ledger row per attempt (2); got {len(ledger)}")
        return
    if ledger[0].outcome != "retry":
        failures.append(f"[5b] The truncated attempt must ledger outcome=retry; got {ledger[0].outcome!r}")
    if ledger[1].outcome != "success":
        failures.append(f"[5c] The recovering attempt must ledger outcome=success; got {ledger[1].outcome!r}")


TESTS = [
    test_schema_error_is_fed_back_into_the_retry_prompt,
    test_the_first_attempt_prompt_is_never_polluted,
    test_critic_schema_error_is_fed_back_into_the_retry_prompt,
    test_critic_first_attempt_prompt_is_never_polluted,
    test_truncation_retries_with_a_larger_budget,
    test_exhausted_truncation_still_surfaces_the_truncation_cause,
    test_every_truncated_attempt_is_still_ledgered,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for failure in failures[before:]:
                print(f"FAIL: {failure}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print(f"PASS: all {len(TESTS)} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
