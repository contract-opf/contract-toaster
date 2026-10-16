#!/usr/bin/env python3
"""
The Floor judge's evidence_quote: a 200-char target, a 500-char hard bound,
and a retry that says what was wrong (issue #160).

A 2026-10-06 live review on Claude Haiku 4.5 had three of four Floor judge
verdicts refused: each quoted 264 characters against a hard 200, and the
retry re-sent the identical prompt, so one invariant ended unjudged and the
review failed closed as `floor_invariant_unjudged`. Owner decision
2026-10-16: 200 stays the target the prompt asks for, 500 is the hard bound,
and an over-length quote is re-asked with the limit and the overshoot
stated. Never truncated.

Checks:
  [1] the constants are 200 (target) and 500 (bound), and the system prompt
      states both numbers;
  [2] 264- and 500-char quotes are accepted verbatim; 501 is refused with the
      `evidence_quote_too_long` token and a detail naming length, bound and
      overshoot;
  [3] THE TICKET'S REQUIRED TEST: an over-length quote on attempt 1 produces
      a retry prompt that states the limit and the overshoot, and a compliant
      attempt 2 is accepted. Attempt 1's prompt carries no correction, and the
      correction never echoes the rejected quote;
  [4] both attempts over-length -> unjudged, fail closed;
  [5] the ledger rows carry the token (#157 convention): `retry_reason` on
      the retried row, `error_token` on the retried and terminal rows, ""
      on a success;
  [6] a non-length fault gets the generic shape correction, its own token;
  [7] the output budget still derives from the 500 bound.

Run with: python3 tests/test_floor_judge_evidence_quote_limit_160.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import floor_judge  # noqa: E402
import primary_review_pass as pp  # noqa: E402

INV = "inv-synthetic-1"
INVARIANT = {"id": INV, "statement": "The agreement must not waive liability for gross negligence.", "rationale": "r"}
CONTEXT = "Synthetic agreement text. " * 40


def _quote(length: int) -> str:
    quote = ("synthetic clause text " * 40)[:length]
    assert len(quote) == length
    return quote


def _verdict(quote: str, invariant_id: str = INV) -> str:
    return json.dumps({"invariant_id": invariant_id, "violated": True, "evidence_quote": quote})


class _ScriptedClient:
    """Returns the scripted responses in order and records every user prompt."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.last_usage = None

    def invoke(self, *, model_id: str, system_prompt: str, user_prompt: str, max_output_tokens: int) -> str:
        self.prompts.append(user_prompt)
        return self.responses.pop(0)


def _judge(responses: list[str]) -> tuple[Any, _ScriptedClient, list[Any]]:
    client = _ScriptedClient(responses)
    rows: list[Any] = []
    judgment = floor_judge.judge_floor_invariants(
        invariants=[INVARIANT],
        review_context=CONTEXT,
        model_client=client,
        model_id="synthetic-model",
        review_id="review-160",
        ledger_write=rows.append,
    )
    return judgment, client, rows


def main() -> int:
    failures: list[str] = []

    # [1]
    if floor_judge._EVIDENCE_QUOTE_MAX_CHARS != 500 or floor_judge._EVIDENCE_QUOTE_TARGET_CHARS != 200:
        failures.append("[1] expected target 200 / bound 500 (#160 owner decision)")
    prompt = floor_judge._SYSTEM_PROMPT
    if "about 200 characters" not in prompt or "past 500 characters" not in prompt:
        failures.append("[1] the system prompt does not state the 200 target and the 500 bound")

    # [2]
    for length, expect in ((264, True), (500, True), (501, False)):
        verdict, error = floor_judge._check_judge_response(_verdict(_quote(length)), expected_invariant_id=INV)
        if (verdict is not None) is not expect:
            failures.append(f"[2] {length}-char quote: expected accepted={expect}, got {verdict is not None}")
        elif verdict is not None and verdict["evidence_quote"] != _quote(length):
            failures.append(f"[2] {length}-char quote was altered")
        if length == 501:
            if pp._error_token(error) != "evidence_quote_too_long":
                failures.append(f"[2] 501-char token is {pp._error_token(error)!r}")
            for part in ("501 characters", "the maximum is 500", "1 over"):
                if part not in error:
                    failures.append(f"[2] 501-char detail lacks {part!r}: {error!r}")

    # [3] the ticket's required test
    long_quote = _quote(620)
    judgment, client, rows = _judge([_verdict(long_quote), _verdict(_quote(150))])
    if judgment.unjudged or len(judgment.verdicts) != 1:
        failures.append(f"[3] a compliant attempt 2 was not accepted: {judgment!r}")
    elif judgment.verdicts[0]["evidence_quote"] != _quote(150):
        failures.append("[3] the accepted quote is not attempt 2's")
    if len(client.prompts) != 2:
        failures.append(f"[3] expected 2 judge calls, saw {len(client.prompts)}")
    else:
        first, retry = client.prompts
        if floor_judge._RETRY_CORRECTION_HEADING in first:
            failures.append("[3] attempt 1 carried a correction")
        for part in (floor_judge._RETRY_CORRECTION_HEADING, "620 characters", "the maximum is 500", "120 over", "aim for about 200"):
            if part not in retry:
                failures.append(f"[3] the retry prompt does not state {part!r}")
        if long_quote in retry:
            failures.append("[3] the correction echoed the rejected quote back")
        if not retry.startswith(first):
            failures.append("[3] the retry did not keep attempt 1's prompt and append the correction")

    # [5] ledger, on the [3] run
    if [r.outcome for r in rows] != ["retry", "success"]:
        failures.append(f"[5] outcomes {[r.outcome for r in rows]}, expected retry, success")
    else:
        if (rows[0].error_token, rows[0].retry_reason) != ("evidence_quote_too_long",) * 2:
            failures.append(f"[5] retried row ledgered {rows[0].error_token!r}/{rows[0].retry_reason!r}")
        if (rows[1].error_token, rows[1].retry_reason) != ("", ""):
            failures.append("[5] the success row carries an error token")
        if rows[1].input_tokens_est <= rows[0].input_tokens_est:
            failures.append("[5] the retry's input estimate does not include its correction")

    # [4] + [5] terminal
    judgment, _client, rows = _judge([_verdict(_quote(600)), _verdict(_quote(550))])
    if judgment.unjudged != [INV] or not judgment.fail_closed:
        failures.append(f"[4] two over-length attempts did not fail closed: {judgment!r}")
    if [(r.outcome, r.error_token, r.retry_reason) for r in rows] != [
        ("retry", "evidence_quote_too_long", "evidence_quote_too_long"),
        ("failure", "evidence_quote_too_long", ""),
    ]:
        failures.append(f"[5] terminal ledger rows wrong: {[(r.outcome, r.error_token, r.retry_reason) for r in rows]}")

    # [6]
    judgment, client, rows = _judge([_verdict("", invariant_id="someone-else"), _verdict("")])
    if judgment.unjudged:
        failures.append("[6] a corrected attempt 2 was not accepted")
    if rows and rows[0].error_token != "wrong_invariant_id":
        failures.append(f"[6] token {rows[0].error_token!r}, expected wrong_invariant_id")
    if len(client.prompts) == 2 and "did not match the required response shape" not in client.prompts[1]:
        failures.append("[6] a non-length fault did not get the shape correction")

    # [7]
    largest = json.loads(floor_judge._LARGEST_ACCEPTABLE_VERDICT)
    if len(largest["evidence_quote"]) != floor_judge._EVIDENCE_QUOTE_MAX_CHARS:
        failures.append("[7] _LARGEST_ACCEPTABLE_VERDICT no longer derives from the bound")
    if pp.estimate_tokens(floor_judge._LARGEST_ACCEPTABLE_VERDICT) > floor_judge._VERDICT_CONTENT_TOKENS:
        failures.append("[7] the content budget does not cover the largest acceptable verdict")

    if failures:
        print("FAIL: Floor judge evidence_quote limit (issue #160).")
        for f in failures:
            print(f"  {f}")
        return 1
    print("PASS: Floor judge evidence_quote target/bound and corrective retry (issue #160).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
