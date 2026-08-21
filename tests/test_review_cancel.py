#!/usr/bin/env python3
"""
Regression test: a reviewer can stop a running review, and stopping it is not
recorded as a failure.

## Root problem this proves fixed

Reported 2026-08-04: a review wedged on stage 1 "kept ticking" for many
minutes with nothing in the UI to press. There was no cancel path at ANY
layer -- `POST /api/reviews` was the only review-mutating route in the API,
and neither the spine, the passes, nor the model client had a seam that could
be told to stop. The only exits were the pipeline finishing or the container
being restarted.

The worst case was not "a few minutes": the primary pass allows 2 attempts,
each of which allows 1 + 3 transport attempts at a 120s timeout, and the
critic pass allows the same again -- so a wedged provider could hold a review
for the better part of quarter of an hour while the reviewer watched.

Cancellation is COOPERATIVE, and these tests pin the two properties that
makes load-bearing:

  1. The checkpoint is consulted where the time is actually spent (before
     each pass attempt and before each transport retry), not only between the
     spine's four stages -- a stop must land mid-pass, since mid-pass is
     exactly where the reported review was stuck.
  2. A cancellation propagates as itself. If any retry loop caught it, the
     stop would be swallowed and the review would carry on spending money.

Run with: python3 tests/test_review_cancel.py
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


class Stopped(Exception):
    """Stands in for pipeline_runner.ReviewCancelled.

    Deliberately a plain, unrelated exception type: it proves the passes and
    the client propagate WHATEVER the checkpoint raises, rather than
    recognising one blessed class.
    """


def _fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _stop_on_call(n: int):
    """A checkpoint that raises on its `n`-th call (1-based) and records how
    many times it was asked."""
    state = {"calls": 0}

    def checkpoint() -> None:
        state["calls"] += 1
        if state["calls"] >= n:
            raise Stopped("cancelled")

    checkpoint.state = state  # type: ignore[attr-defined]
    return checkpoint


# ---------------------------------------------------------------------------
# 1. The primary pass stops before spending, and stops between attempts.
# ---------------------------------------------------------------------------


def test_primary_pass_stops_before_the_first_call(failures: list[str]) -> None:
    client = model_client.FakeBedrockClient({_TEST_MODEL_ID: [_fixture("primary_request_change_valid.json")]})
    try:
        pp.run_primary_pass(
            review_id="cancel-1",
            diff_hunks=[],
            anchored_clauses=[],
            retrieved_precedent=[],
            playbook=_playbook(),
            model_client=client,
            model_id=_TEST_MODEL_ID,
            ledger_write=[].append,
            doc_text="Section 8.",
            cancel_checkpoint=_stop_on_call(1),
        )
    except Stopped:
        if client.calls:
            failures.append(
                f"[1a] A review cancelled before it started must cost zero model calls; "
                f"got {len(client.calls)}"
            )
        return
    failures.append("[1b] The cancellation must propagate out of run_primary_pass")


def test_primary_pass_stops_between_attempts(failures: list[str]) -> None:
    """The retry is where the wall clock goes. A stop requested during
    attempt 1 must prevent attempt 2, not be noticed after it."""
    client = model_client.FakeBedrockClient(
        {
            _TEST_MODEL_ID: [
                _fixture("schema_invalid_missing_issues.json"),
                _fixture("primary_request_change_valid.json"),
            ]
        }
    )
    try:
        pp.run_primary_pass(
            review_id="cancel-2",
            diff_hunks=[],
            anchored_clauses=[],
            retrieved_precedent=[],
            playbook=_playbook(),
            model_client=client,
            model_id=_TEST_MODEL_ID,
            ledger_write=[].append,
            doc_text="Section 8.",
            cancel_checkpoint=_stop_on_call(2),
        )
    except Stopped:
        if len(client.calls) != 1:
            failures.append(
                f"[2a] Expected the stop to land after attempt 1 and before attempt 2; "
                f"got {len(client.calls)} model call(s)"
            )
        return
    failures.append("[2b] The cancellation must propagate out of the retry loop")


def test_no_checkpoint_is_the_unchanged_path(failures: list[str]) -> None:
    """Every existing caller passes nothing. That path must be exactly what
    it was: one attempt, one call, status OK."""
    client = model_client.FakeBedrockClient({_TEST_MODEL_ID: [_fixture("primary_request_change_valid.json")]})
    result = pp.run_primary_pass(
        review_id="cancel-3",
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=_playbook(),
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=[].append,
        doc_text="Section 8.",
    )
    if result.get("status") != "OK" or len(client.calls) != 1:
        failures.append(f"[3a] The no-checkpoint path must be unchanged; got {result!r}")


# ---------------------------------------------------------------------------
# 2. The critic pass honours the same contract.
# ---------------------------------------------------------------------------


def test_critic_pass_stops_before_the_first_call(failures: list[str]) -> None:
    client = model_client.FakeBedrockClient({_TEST_MODEL_ID: [_fixture("critic_no_delta_accept_valid.json")]})
    try:
        cp.run_critic_pass(
            review_id="cancel-4",
            diff_hunks=[],
            anchored_clauses=[],
            primary_output=json.loads(_fixture("primary_request_change_valid.json")),
            playbook=_playbook(),
            model_client=client,
            model_id=_TEST_MODEL_ID,
            ledger_write=[].append,
            cancel_checkpoint=_stop_on_call(1),
        )
    except Stopped:
        if client.calls:
            failures.append(f"[4a] Expected zero critic calls; got {len(client.calls)}")
        return
    failures.append("[4b] The cancellation must propagate out of run_critic_pass")


# ---------------------------------------------------------------------------
# 3. The model client asks before each transport attempt.
# ---------------------------------------------------------------------------


class AlwaysFailingHttp:
    """Every post raises a transport error, so the client exhausts its retry
    budget -- the exact shape of the wedged provider this checkpoint exists
    to escape."""

    def __init__(self) -> None:
        self.posts = 0

    def post(self, url: str, **kwargs: Any) -> Any:
        self.posts += 1
        raise ConnectionError("provider unreachable")


def test_client_stops_retrying_when_cancelled(failures: list[str]) -> None:
    http = AlwaysFailingHttp()
    client = model_client.OpenRouterModelClient(
        api_key="test-key",
        http_client=http,
        sleep_fn=lambda _seconds: None,
        cancel_checkpoint=_stop_on_call(2),
    )
    try:
        client.invoke(
            model_id="anthropic/claude-opus-4.8",
            system_prompt="s",
            user_prompt="u",
            max_output_tokens=100,
        )
    except Stopped:
        if http.posts != 1:
            failures.append(
                f"[5a] Expected the stop to cut the retry budget short after 1 attempt; "
                f"got {http.posts}"
            )
        return
    except model_client.ModelInvocationError:
        failures.append(
            "[5b] The cancellation was swallowed by the transport-retry handler and the "
            "client kept retrying a review nobody is waiting for"
        )
        return
    failures.append("[5c] The cancellation must propagate out of invoke()")


def test_client_without_checkpoint_still_retries(failures: list[str]) -> None:
    """The unchanged path: no checkpoint means the bounded retry behaves
    exactly as it always has (1 + max_retries attempts, then a real error)."""
    http = AlwaysFailingHttp()
    client = model_client.OpenRouterModelClient(
        api_key="test-key",
        http_client=http,
        sleep_fn=lambda _seconds: None,
        max_retries=2,
    )
    try:
        client.invoke(
            model_id="anthropic/claude-opus-4.8",
            system_prompt="s",
            user_prompt="u",
            max_output_tokens=100,
        )
    except model_client.ModelInvocationError:
        if http.posts != 3:
            failures.append(f"[6a] Expected 1 + 2 retries = 3 attempts; got {http.posts}")
        return
    failures.append("[6b] An exhausted retry budget must still raise ModelInvocationError")


# ---------------------------------------------------------------------------
# 4. CANCELLED is a terminal status, and it is not a failure status.
# ---------------------------------------------------------------------------


def test_cancelled_is_terminal_but_not_a_failure(failures: list[str]) -> None:
    import reviews

    if "CANCELLED" not in reviews.REVIEW_STATUSES_TERMINAL:
        failures.append("[7a] CANCELLED must be a terminal status or the UI will poll forever")
    if "CANCELLED" in reviews.REVIEW_STATUSES_NON_TERMINAL:
        failures.append("[7b] CANCELLED must not also be non-terminal")
    if "CANCELLED" in set(reviews.STAGE_FAILURE_REASON_STATUS.values()):
        failures.append(
            "[7c] No stage-failure reason may resolve to CANCELLED -- a stop the user "
            "asked for must never be reachable from the failure taxonomy"
        )


def test_runner_treats_cancellation_separately_from_failure(failures: list[str]) -> None:
    """`classify_failure_reason` must not have an opinion about a
    cancellation: if it mapped one, a stop would land in Diagnostics as an
    incident to investigate."""
    import pipeline_runner

    reason = pipeline_runner.classify_failure_reason(
        pipeline_runner.ReviewCancelled("stopped")
    )
    if reason != "unhandled_exception":
        failures.append(
            f"[8a] ReviewCancelled must not be given a failure token of its own; got {reason!r}"
        )
    if not issubclass(pipeline_runner.ReviewCancelled, Exception):
        failures.append("[8b] ReviewCancelled must be an Exception")


TESTS = [
    test_primary_pass_stops_before_the_first_call,
    test_primary_pass_stops_between_attempts,
    test_no_checkpoint_is_the_unchanged_path,
    test_critic_pass_stops_before_the_first_call,
    test_client_stops_retrying_when_cancelled,
    test_client_without_checkpoint_still_retries,
    test_cancelled_is_terminal_but_not_a_failure,
    test_runner_treats_cancellation_separately_from_failure,
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
