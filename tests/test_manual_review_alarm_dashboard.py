#!/usr/bin/env python3
"""
Gate for issue #37 (owner, alarm, tile, next-step copy), restated for issue
#133 (owner decision 2026-09-16): a review never concludes as "manual review
required". The two manual-review statuses #37 built its alarm, tile, queue
and status-keyed copy around are retired; a run that does not complete is
ERROR and its `reason` token says what to do. What #37 guaranteed -- a failed
review cannot silently wait, an admin can see and filter the failures, an
owner checks them daily, and the uploader is told what happens next -- must
survive the change, now keyed by reason. So:

  AC1 — The stale alarm covers failed (ERROR) reviews.
  AC2 — ARCHITECTURE.md documents a dashboard tile counting failures by
        reason, and an admin queue that lists them.
  AC3 — docs/output-contract.md explains a failed review by its reason token
        and no longer defines status-keyed copy for the retired statuses;
        RUNBOOK.md names an owner and a daily cadence for the failures queue.

Usage:
    python3 tests/test_manual_review_alarm_dashboard.py
    Exit code 0 = all ACs pass; non-zero = one or more ACs fail.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE_MD = REPO_ROOT / "ARCHITECTURE.md"
RUNBOOK_MD = REPO_ROOT / "RUNBOOK.md"
OUTPUT_CONTRACT_MD = REPO_ROOT / "docs" / "output-contract.md"


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


# AC1 — the stale alarm fires for a failed review left unacknowledged.
AC1_PATTERN = re.compile(
    r"manual-review-stale.{0,300}(?:failed|ERROR)",
    re.IGNORECASE | re.DOTALL,
)

# AC2 — a dashboard tile counting failures by reason, and an admin queue.
AC2_TILE_PATTERN = re.compile(
    r"(?:dashboard|tile).{0,300}(?:failed|ERROR).{0,120}by\s+`?reason",
    re.IGNORECASE | re.DOTALL,
)
AC2_QUEUE_PATTERN = re.compile(
    r"failures\s+queue.{0,200}(?:lists|list)",
    re.IGNORECASE | re.DOTALL,
)

# AC3a — a failed review is explained by its reason token.
AC3_REASON_COPY_PATTERN = re.compile(
    r"Failed reviews: user-facing next-step copy.{0,1500}"
    r"`reason`\s+token.{0,300}(?:what happened|what to do)",
    re.IGNORECASE | re.DOTALL,
)

# AC3b — the retired status-keyed promise is gone from the contract's table.
RETIRED_STATUS_COPY_ROW = re.compile(
    r"^\|\s*`(?:ERROR_)?MANUAL_REVIEW_REQUIRED`\s*\|.*legal admin will review",
    re.MULTILINE,
)

# AC3c — named owner + daily cadence for the failures queue.
AC3_OWNER_CADENCE_PATTERN = re.compile(
    r"(?:legal\s+admin|general\s+counsel).{0,200}failures\s+queue.{0,80}daily",
    re.IGNORECASE | re.DOTALL,
)


def check_ac1_alarm_coverage(arch_text: str, runbook_text: str) -> list[str]:
    if AC1_PATTERN.search(arch_text + "\n" + runbook_text):
        return []
    return [
        "  AC1 FAIL: neither ARCHITECTURE.md nor RUNBOOK.md says the\n"
        "  manual-review-stale alarm fires for a failed (ERROR) review.\n"
        f"  Missing pattern: {AC1_PATTERN.pattern!r}"
    ]


def check_ac2_dashboard_queue(arch_text: str) -> list[str]:
    failures = []
    if not AC2_TILE_PATTERN.search(arch_text):
        failures.append(
            "  AC2a FAIL: ARCHITECTURE.md does not document a dashboard tile\n"
            "  counting failed reviews by reason.\n"
            f"  Missing pattern: {AC2_TILE_PATTERN.pattern!r}"
        )
    if not AC2_QUEUE_PATTERN.search(arch_text):
        failures.append(
            "  AC2b FAIL: ARCHITECTURE.md does not document the admin failures\n"
            "  queue that lists failed reviews.\n"
            f"  Missing pattern: {AC2_QUEUE_PATTERN.pattern!r}"
        )
    return failures


def check_ac3_copy_and_owner(output_contract_text: str, runbook_text: str) -> list[str]:
    failures = []
    if not AC3_REASON_COPY_PATTERN.search(output_contract_text):
        failures.append(
            "  AC3a FAIL: docs/output-contract.md does not say a failed review is\n"
            "  explained to the uploader by its reason token.\n"
            f"  Missing pattern: {AC3_REASON_COPY_PATTERN.pattern!r}"
        )
    if RETIRED_STATUS_COPY_ROW.search(output_contract_text):
        failures.append(
            "  AC3b FAIL: docs/output-contract.md still defines status-keyed copy\n"
            "  for a retired manual-review status (issue #133)."
        )
    if not AC3_OWNER_CADENCE_PATTERN.search(runbook_text):
        failures.append(
            "  AC3c FAIL: RUNBOOK.md does not name an owner and a daily check\n"
            "  cadence for the failures queue.\n"
            f"  Missing pattern: {AC3_OWNER_CADENCE_PATTERN.pattern!r}"
        )
    return failures


def main() -> int:
    try:
        arch_text = read_text(ARCHITECTURE_MD)
        runbook_text = read_text(RUNBOOK_MD)
        output_contract_text = read_text(OUTPUT_CONTRACT_MD)
    except FileNotFoundError as exc:
        print(f"FAIL: {exc}")
        return 1

    all_failures: list[str] = []

    ac1 = check_ac1_alarm_coverage(arch_text, runbook_text)
    ac2 = check_ac2_dashboard_queue(arch_text)
    ac3 = check_ac3_copy_and_owner(output_contract_text, runbook_text)

    print("AC1: Stale alarm covers failed reviews")
    if ac1:
        for f in ac1:
            print(f)
        all_failures.extend(ac1)
    else:
        print("  PASS")

    print()
    print("AC2: Dashboard tile + failures queue by reason")
    if ac2:
        for f in ac2:
            print(f)
        all_failures.extend(ac2)
    else:
        print("  PASS")

    print()
    print("AC3: User-facing copy in output-contract.md; named owner + cadence in RUNBOOK.md")
    if ac3:
        for f in ac3:
            print(f)
        all_failures.extend(ac3)
    else:
        print("  PASS")

    print()
    if all_failures:
        print(
            f"FAIL: {len(all_failures)} issue(s) found.  "
            "See issues #37 and #133."
        )
        return 1
    else:
        print("PASS: all failures alarm/dashboard/copy gates satisfied.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
