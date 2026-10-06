#!/usr/bin/env python3
"""
The Floor judge accepts a verdict wrapped in a markdown fence.

A 2026-10-06 live review on Claude Haiku 4.5 failed closed as
`floor_invariant_unjudged`: every judge call returned a valid verdict inside
a ```json fence, and `floor_judge._validate_judge_response` handed the raw
text straight to `json.loads`. The review passes already unwrap a fence or a
prose preamble with `primary_review_pass._extract_json_object`; the judge now
does too. Unwrapping is not repair: a malformed body inside the fence, or a
response with no object at all, is still refused, and so is
anything after the object other than its closing fence.

Run with: python3 tests/test_floor_judge_fenced_verdict.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import floor_judge  # noqa: E402

INV = "inv-synthetic-1"
VERDICT = json.dumps({"invariant_id": INV, "violated": True, "evidence_quote": "synthetic clause"})


def check(failures: list[str], label: str, raw: object, expect_valid: bool) -> None:
    ok, parsed = floor_judge._validate_judge_response(raw, expected_invariant_id=INV)
    if ok is not expect_valid:
        failures.append(f"{label}: expected valid={expect_valid}, got {ok}")
    if expect_valid and ok and parsed != json.loads(VERDICT):
        failures.append(f"{label}: parsed verdict differs: {parsed!r}")
    print(f"{label} ... {'PASS' if ok is expect_valid else 'FAIL'}")


def main() -> int:
    failures: list[str] = []
    check(failures, "bare JSON", VERDICT, True)
    check(failures, "```json fence", f"```json\n{VERDICT}\n```", True)
    check(failures, "bare ``` fence", f"```\n{VERDICT}\n```", True)
    check(failures, "prose preamble", f"Here is my verdict:\n{VERDICT}", True)
    check(failures, "malformed body inside a fence", '```json\n{"invariant_id": "inv-synthetic-1", "violated": tru}\n```', False)
    check(failures, "fenced verdict for the wrong invariant", "```json\n" + VERDICT.replace(INV, "other") + "\n```", False)
    check(failures, "no object at all", "I cannot judge this.", False)
    # A judge that retracts its verdict must not pass as its first answer.
    retracted = VERDICT.replace("true", "false")
    check(failures, "verdict then a correcting object", f"{retracted} Correction: {VERDICT}", False)
    check(failures, "fenced verdict then retracting prose", f"```json\n{retracted}\n```\nOn reflection this IS violated.", False)
    check(failures, "non-string response", None, False)
    if failures:
        print("\n".join(failures))
        return 1
    print("PASS: the Floor judge unwraps a fenced verdict and still refuses a malformed one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
