#!/usr/bin/env python3
"""
Red gate for issue #37: Manual-review states alarm, dashboard tile, named
owner, and user-facing next-step copy.

Three acceptance criteria asserted here (all fail against the current repo
state before the green implementation):

  AC1 — Alarm path covers manual-review states.
        The stale-review alarm coverage in ARCHITECTURE.md or RUNBOOK.md must
        include MANUAL_REVIEW_REQUIRED and ERROR_MANUAL_REVIEW_REQUIRED, not
        only stale PENDING/RUNNING reviews.  A review entering either
        manual-review terminal state must raise the (extended) stale-review
        alarm path so it cannot silently go unnoticed.

  AC2 — Admin dashboard tile + filter for manual-review states.
        ARCHITECTURE.md must specify a CloudWatch dashboard tile that counts
        reviews in MANUAL_REVIEW_REQUIRED and ERROR_MANUAL_REVIEW_REQUIRED
        states, and a filter view in the admin UI that lists them.

  AC3 — User-facing next-step copy per manual-review state in output-contract.md.
        docs/output-contract.md must contain at least one sentence of
        user-facing copy for each of MANUAL_REVIEW_REQUIRED and
        ERROR_MANUAL_REVIEW_REQUIRED telling the uploader what happens next.
        RUNBOOK.md must name an owner and a check cadence for the manual-review
        filter (e.g. "legal admin checks the manual-review filter daily").

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


# ---------------------------------------------------------------------------
# AC1 — Alarm path covers manual-review states
# ---------------------------------------------------------------------------

# The alarm list in ARCHITECTURE.md / RUNBOOK.md currently covers stale
# PENDING/RUNNING only.  We need the stale-review alarm (or a companion alarm)
# to also fire for MANUAL_REVIEW_REQUIRED / ERROR_MANUAL_REVIEW_REQUIRED so
# that a 4:55pm failure does not silently wait.

AC1_PATTERNS = [
    # The alarm spec must mention manual-review states in the context of alarms
    re.compile(
        r"(?:alarm|alert).{0,300}"
        r"(?:MANUAL_REVIEW_REQUIRED|ERROR_MANUAL_REVIEW_REQUIRED|manual[- ]review\s+state)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Or: manual-review states mentioned as part of the stale-review alarm path
    re.compile(
        r"(?:MANUAL_REVIEW_REQUIRED|ERROR_MANUAL_REVIEW_REQUIRED).{0,300}"
        r"(?:alarm|alert|notif)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# AC2 — Dashboard tile + admin filter for manual-review states
# ---------------------------------------------------------------------------

# ARCHITECTURE.md observability section must mention a dashboard tile and/or
# admin filter view for manual-review states.

AC2_TILE_PATTERN = re.compile(
    r"(?:dashboard|tile).{0,400}"
    r"(?:MANUAL_REVIEW_REQUIRED|ERROR_MANUAL_REVIEW_REQUIRED|manual[- ]review)",
    re.IGNORECASE | re.DOTALL,
)
AC2_FILTER_PATTERN = re.compile(
    r"(?:filter|list).{0,400}"
    r"(?:MANUAL_REVIEW_REQUIRED|ERROR_MANUAL_REVIEW_REQUIRED|manual[- ]review)",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# AC3a — User-facing copy per state in output-contract.md
# ---------------------------------------------------------------------------

# docs/output-contract.md must contain user-facing next-step copy for each
# manual-review state.  We check that both state names appear near phrases
# like "what happens next", "your document", "legal admin", or "will be
# reviewed" — i.e. copy directed at an uploader rather than an operator.

AC3_COPY_MRR_PATTERN = re.compile(
    r"MANUAL_REVIEW_REQUIRED.{0,600}"
    r"(?:what happens|legal admin|will be reviewed|next step|someone will|"
    r"reviewed by|contact|check.*daily|daily.*check)",
    re.IGNORECASE | re.DOTALL,
)
AC3_COPY_EMRR_PATTERN = re.compile(
    r"ERROR_MANUAL_REVIEW_REQUIRED.{0,600}"
    r"(?:what happens|legal admin|will be reviewed|next step|someone will|"
    r"reviewed by|contact|check.*daily|daily.*check)",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# AC3b — Named owner + check cadence in RUNBOOK.md
# ---------------------------------------------------------------------------

# RUNBOOK.md must name an owner (legal admin / legal operations) and a
# check cadence (daily / each day) for the manual-review filter.

AC3_OWNER_CADENCE_PATTERN = re.compile(
    r"(?:legal\s+admin|legal\s+operations|GC|general\s+counsel).{0,300}"
    r"(?:manual[- ]review\s+filter|MANUAL_REVIEW_REQUIRED|manual\s+review).{0,300}"
    r"(?:daily|each\s+day|every\s+day|check.*day|day.*check)",
    re.IGNORECASE | re.DOTALL,
)
# Also accept the order owner → cadence → filter name
AC3_OWNER_CADENCE_PATTERN_ALT = re.compile(
    r"(?:daily|each\s+day|every\s+day).{0,300}"
    r"(?:manual[- ]review\s+filter|MANUAL_REVIEW_REQUIRED|manual\s+review)",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Gate functions
# ---------------------------------------------------------------------------

def check_ac1_alarm_coverage(arch_text: str, runbook_text: str) -> list[str]:
    """AC1: stale-review alarm path extended to cover manual-review states."""
    combined = arch_text + "\n" + runbook_text
    failures = []
    matched = any(p.search(combined) for p in AC1_PATTERNS)
    if not matched:
        failures.append(
            "  AC1 FAIL: Neither ARCHITECTURE.md nor RUNBOOK.md extends the stale-\n"
            "  review alarm to cover MANUAL_REVIEW_REQUIRED or\n"
            "  ERROR_MANUAL_REVIEW_REQUIRED.\n"
            "  Required: the alarm list / alarm path must include both terminal\n"
            "  manual-review states so that a 4:55 PM failure does not silently wait.\n"
            "  Hint: extend the 'stale review status' alarm entry in ARCHITECTURE.md\n"
            "  observability section and/or RUNBOOK.md to explicitly mention both\n"
            "  manual-review states."
        )
    return failures


def check_ac2_dashboard_filter(arch_text: str) -> list[str]:
    """AC2: admin dashboard tile and filter view for manual-review states."""
    failures = []
    if not AC2_TILE_PATTERN.search(arch_text):
        failures.append(
            "  AC2a FAIL: ARCHITECTURE.md observability section does not document\n"
            "  a dashboard tile for MANUAL_REVIEW_REQUIRED / ERROR_MANUAL_REVIEW_REQUIRED.\n"
            "  Required: add a tile to the CloudWatch dashboard that counts reviews\n"
            "  in each manual-review state."
        )
    if not AC2_FILTER_PATTERN.search(arch_text):
        failures.append(
            "  AC2b FAIL: ARCHITECTURE.md does not document an admin filter view\n"
            "  that lists reviews in manual-review states.\n"
            "  Required: document an admin filter or list view for manual-review reviews."
        )
    return failures


def check_ac3_copy_and_owner(
    output_contract_text: str, runbook_text: str
) -> list[str]:
    """AC3: user-facing next-step copy in output-contract.md; named owner + cadence in RUNBOOK.md."""
    failures = []

    if not AC3_COPY_MRR_PATTERN.search(output_contract_text):
        failures.append(
            "  AC3a FAIL: docs/output-contract.md does not contain user-facing copy\n"
            "  for MANUAL_REVIEW_REQUIRED telling the uploader what happens next.\n"
            "  Required: at least one sentence in output-contract.md explaining to\n"
            "  the uploader what happens when their review enters\n"
            "  MANUAL_REVIEW_REQUIRED (e.g. 'a legal admin will review it')."
        )

    if not AC3_COPY_EMRR_PATTERN.search(output_contract_text):
        failures.append(
            "  AC3b FAIL: docs/output-contract.md does not contain user-facing copy\n"
            "  for ERROR_MANUAL_REVIEW_REQUIRED telling the uploader what happens next.\n"
            "  Required: at least one sentence in output-contract.md explaining to\n"
            "  the uploader what happens when their review enters\n"
            "  ERROR_MANUAL_REVIEW_REQUIRED."
        )

    owner_cadence_ok = (
        AC3_OWNER_CADENCE_PATTERN.search(runbook_text)
        or AC3_OWNER_CADENCE_PATTERN_ALT.search(runbook_text)
    )
    if not owner_cadence_ok:
        failures.append(
            "  AC3c FAIL: RUNBOOK.md does not name an owner and check cadence for\n"
            "  the manual-review filter.\n"
            "  Required: a sentence like 'legal admin checks the manual-review\n"
            "  filter daily' with an SLA so there is a named human responsible\n"
            "  for acting on manual-review states."
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
    ac2 = check_ac2_dashboard_filter(arch_text)
    ac3 = check_ac3_copy_and_owner(output_contract_text, runbook_text)

    print("AC1: Alarm path covers manual-review states")
    if ac1:
        for f in ac1:
            print(f)
        all_failures.extend(ac1)
    else:
        print("  PASS")

    print()
    print("AC2: Admin dashboard tile + filter for manual-review states")
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
            "See issue #37 for the full remediation plan."
        )
        return 1
    else:
        print("PASS: all manual-review alarm/dashboard/copy gates satisfied.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
