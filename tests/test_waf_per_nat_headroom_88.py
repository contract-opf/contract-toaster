#!/usr/bin/env python3
"""Issue #88 -- per-egress-IP headroom of the WAF rate rules.

WAF rate rules aggregate by source IP, so an office behind one NAT shares a
single budget while the UI's own budget is per review. This test computes how
many reviewers one egress IP supports from the two things that decide it, and
pins the figure the docs state:

  * the WAF limits, parsed from infra/lib/nested/waf-stack.ts (rule 4
    RateLimitUploadEndpoint, rule 5 RateLimitPollingEndpoint), and
  * the UI poll constants, parsed from frontend/src/ReviewSubmission.tsx.

If either side drifts, or docs/threat-model.md stops stating the figure, this
fails. Offline and deterministic (no synth, no network).

Run with: python3 tests/test_waf_per_nat_headroom_88.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WAF_SOURCE = REPO_ROOT / "infra" / "lib" / "nested" / "waf-stack.ts"
UI_SOURCE = REPO_ROOT / "frontend" / "src" / "ReviewSubmission.tsx"
THREAT_MODEL = REPO_ROOT / "docs" / "threat-model.md"

WINDOW_MS = 5 * 60 * 1000
# poll-budget-waf.test.tsx reserves half of rule 5's budget for History,
# downloads, cancel and preflight traffic.
POLL_SHARE_OF_BUDGET = 0.5


def _rule_limit(rule_name: str) -> int:
    source = WAF_SOURCE.read_text(encoding="utf-8")
    start = source.index(f"name: '{rule_name}'")
    match = re.search(r"\blimit:\s*(\d+)", source[start:])
    assert match, f"no numeric limit after {rule_name}"
    return int(match.group(1))


def _ui_constant(name: str) -> int:
    text = UI_SOURCE.read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = ([\d_]+);", text)
    assert match, f"{name} not found in ReviewSubmission.tsx"
    return int(match.group(1).replace("_", ""))


def _polls_in_window(offset_ms: int, fast: int, slow: int, fast_phase: int) -> int:
    count, t, end = 0, offset_ms, offset_ms + WINDOW_MS
    while t < end:
        count += 1
        t += fast if t < fast_phase else slow
    return count


def compute() -> dict[str, int]:
    fast = _ui_constant("POLL_INTERVAL_MS")
    slow = _ui_constant("POLL_INTERVAL_SLOW_MS")
    fast_phase = _ui_constant("POLL_FAST_PHASE_MS")
    worst = max(
        _polls_in_window(offset, fast, slow, fast_phase)
        for offset in range(0, WINDOW_MS + 1, 1000)
    )
    upload_limit = _rule_limit("RateLimitUploadEndpoint")
    poll_limit = _rule_limit("RateLimitPollingEndpoint")
    return {
        "worst_polls_per_review_per_window": worst,
        "upload_limit": upload_limit,
        "poll_limit": poll_limit,
        # Polling alone, nothing else on the IP.
        "reviewers_polling_only": poll_limit // worst,
        # Keeping half the budget for everything else the UI sends.
        "reviewers_with_reserve": int(poll_limit * POLL_SHARE_OF_BUDGET) // worst,
        # Uploads (POST /api/reviews exactly) per egress IP per window.
        "submissions_per_window": upload_limit,
    }


def main() -> int:
    figures = compute()
    print("Per-egress-IP headroom (issue #88):")
    for key, value in figures.items():
        print(f"  {key} = {value}")

    failures: list[str] = []

    def check(ok: bool, label: str) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    check(figures["worst_polls_per_review_per_window"] == 58, "worst-case poll window is 58 GETs")
    check(figures["reviewers_polling_only"] == 5, "5 concurrent reviewers per IP on polling alone")
    check(
        figures["reviewers_with_reserve"] == 2,
        "2 concurrent reviewers per IP once half the budget is reserved for other traffic",
    )
    check(figures["submissions_per_window"] == 10, "10 submissions per IP per 5 minutes")

    doc = " ".join(THREAT_MODEL.read_text(encoding="utf-8").split())
    section = doc[doc.find("Per-IP assumptions") :] if "Per-IP assumptions" in doc else ""
    check(bool(section), "docs/threat-model.md has a 'Per-IP assumptions' paragraph")
    check(
        "about five concurrent reviews per egress IP" in section
        and "about two" in section
        and "10 submissions per 5 minutes" in section,
        "the paragraph states the 5 / 2 / 10 figures this test computes",
    )
    check(
        "EXACTLY /api/reviews" in section or "exactly `/api/reviews`" in section,
        "the paragraph states that rule 4 is scoped to the exact upload path",
    )

    if failures:
        print(f"\nFAIL: {len(failures)} check(s) failed.")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
