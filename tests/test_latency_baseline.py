#!/usr/bin/env python3
"""
Red gate for issue #31: One latency baseline; Lambda (not Fargate) for the LLM stages.

Three invariants checked here:

  AC1 — Exactly one canonical latency figure across living docs.
        The canonical latency budget is "1–3 minutes typical, 5 minutes p95".
        No old seconds-based figures ("20–90 seconds", "15–90 seconds", etc.)
        may appear.  Both docs-lint Check B and this test enforce this; this
        test is the issue-specific assertion.

  AC2 — Lambda decision for LLM stages recorded in design-notes.md.
        The decision to use Lambda (not Fargate) for the LLM stages must be
        documented with rationale in docs/design-notes.md.  Lambda's 15-minute
        limit is ample for a single Bedrock InvokeModel with retries; the
        concern about cold-start/memory that favoured Fargate does not apply to
        thin API callers.

  AC3 — Phase-0 issue #11 notes updated to reflect Lambda for LLM stages.
        The phase-0-issues.md notes section for issue #11 must reference Lambda
        for the LLM stages, not Fargate.

Usage:
    python3 tests/test_latency_baseline.py
    Exit 0 = all checks pass; non-zero = one or more checks fail.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
DESIGN_NOTES = REPO_ROOT / "docs" / "design-notes.md"
PHASE0_ISSUES = REPO_ROOT / "docs" / "phase-0-issues.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── AC1: Canonical latency figure ─────────────────────────────────────────────

# The canonical figure is expressed in minutes, not seconds.
# Both "typical" and "p95" components must be present together.
CANONICAL_LATENCY_TYPICAL = re.compile(
    r"1[–\-]3\s+minutes?\s+typical",
    re.IGNORECASE,
)
CANONICAL_LATENCY_P95 = re.compile(
    r"5\s+minutes?\s+p95",
    re.IGNORECASE,
)

# Old seconds-based figures must not appear in living docs.
# Matches e.g. "20–90 seconds", "15–60 seconds", "15–90 seconds"
OLD_SECONDS_LATENCY = re.compile(
    r"\b\d+[–\-]\d+\s+seconds?\s+typical",
    re.IGNORECASE,
)

# Candidate living docs where the latency figure may appear
LATENCY_DOCS = [
    ARCHITECTURE,
    DESIGN_NOTES,
    PHASE0_ISSUES,
]


def check_ac1_canonical_latency() -> list[str]:
    """
    The canonical latency budget ('1–3 minutes typical, 5 minutes p95') must
    appear at least once in the documentation, and no old seconds-based figure
    ('N–M seconds typical') may appear in living docs.
    """
    failures = []

    # Check that the new canonical figure is present somewhere
    found_typical = False
    found_p95 = False
    for path in LATENCY_DOCS:
        if not path.exists():
            continue
        text = read(path)
        if CANONICAL_LATENCY_TYPICAL.search(text):
            found_typical = True
        if CANONICAL_LATENCY_P95.search(text):
            found_p95 = True

    if not found_typical:
        failures.append(
            "  AC1 FAIL: canonical latency figure '1–3 minutes typical' not found "
            "in any of: ARCHITECTURE.md, docs/design-notes.md, docs/phase-0-issues.md"
        )
    if not found_p95:
        failures.append(
            "  AC1 FAIL: canonical latency p95 '5 minutes p95' not found "
            "in any of: ARCHITECTURE.md, docs/design-notes.md, docs/phase-0-issues.md"
        )

    # Check that no old seconds-based figure remains
    for path in LATENCY_DOCS:
        if not path.exists():
            continue
        text = read(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            if OLD_SECONDS_LATENCY.search(line):
                failures.append(
                    f"  AC1 FAIL: {path.relative_to(REPO_ROOT)}:{lineno} — "
                    f"stale seconds-based latency figure (must be '1–3 minutes "
                    f"typical, 5 minutes p95'):\n"
                    f"    > {line.strip()}"
                )

    return failures


# ── AC2: Lambda decision recorded in design-notes.md ─────────────────────────

# The decision note must say Lambda is used for LLM stages (not Fargate).
# We require both that "Lambda" appears in the context of LLM stages, and that
# a clear rationale is given (15-minute limit is ample).
LAMBDA_LLM_STAGE_PATTERN = re.compile(
    r"Lambda[^\n]{0,120}LLM\s+stage"
    r"|LLM\s+stage[^\n]{0,120}Lambda",
    re.IGNORECASE,
)
LAMBDA_RATIONALE_PATTERN = re.compile(
    r"15.minut[^\n]{0,80}(?:ample|limit|cap)"
    r"|(?:ample|limit|cap)[^\n]{0,80}15.minut",
    re.IGNORECASE,
)


def check_ac2_lambda_decision() -> list[str]:
    """
    docs/design-notes.md must record the decision to use Lambda (not Fargate)
    for the LLM stages, with the rationale that Lambda's 15-minute limit is
    ample for a single Bedrock InvokeModel call with retries.
    """
    failures = []

    if not DESIGN_NOTES.exists():
        failures.append("  AC2 FAIL: docs/design-notes.md not found")
        return failures

    text = read(DESIGN_NOTES)

    if not LAMBDA_LLM_STAGE_PATTERN.search(text):
        failures.append(
            "  AC2 FAIL: docs/design-notes.md does not record the decision to use\n"
            "  Lambda for the LLM stages.  Required: a note that Lambda (not Fargate)\n"
            "  is used for the LLM stages (primary review, adversarial review) in the\n"
            "  Step Functions pipeline — these are thin Bedrock API callers that do not\n"
            "  need Fargate's provision time or large memory."
        )

    if not LAMBDA_RATIONALE_PATTERN.search(text):
        failures.append(
            "  AC2 FAIL: docs/design-notes.md does not record the 15-minute-limit\n"
            "  rationale for using Lambda for LLM stages.  Required: a statement that\n"
            "  Lambda's 15-minute limit is ample for a single InvokeModel call with\n"
            "  retries, so Lambda is the right compute for thin API-caller stages."
        )

    return failures


# ── AC3: Phase-0 issue #11 notes prefer Lambda for LLM stages ────────────────

# The old note said "Prefer Fargate tasks for the LLM stages".
# The updated note must reference Lambda for the LLM stages.
OLD_FARGATE_LLM_NOTE = re.compile(
    r"Prefer\s+Fargate\s+tasks\s+for\s+the\s+LLM\s+stages",
    re.IGNORECASE,
)
NEW_LAMBDA_LLM_NOTE = re.compile(
    r"Lambda\s+for\s+the\s+LLM\s+stages"
    r"|LLM\s+stages[^\n]{0,60}Lambda",
    re.IGNORECASE,
)


def check_ac3_phase0_issue11_notes() -> list[str]:
    """
    docs/phase-0-issues.md issue #11 notes must not say 'Prefer Fargate tasks
    for the LLM stages' and must reference Lambda for the LLM stages.
    """
    failures = []

    if not PHASE0_ISSUES.exists():
        failures.append("  AC3 FAIL: docs/phase-0-issues.md not found")
        return failures

    text = read(PHASE0_ISSUES)

    if OLD_FARGATE_LLM_NOTE.search(text):
        for lineno, line in enumerate(text.splitlines(), 1):
            if OLD_FARGATE_LLM_NOTE.search(line):
                failures.append(
                    f"  AC3 FAIL: {PHASE0_ISSUES.relative_to(REPO_ROOT)}:{lineno} — "
                    f"stale note preferring Fargate for LLM stages (issue #31 "
                    f"supersedes this; Lambda is preferred for LLM stages):\n"
                    f"    > {line.strip()}"
                )

    if not NEW_LAMBDA_LLM_NOTE.search(text):
        failures.append(
            "  AC3 FAIL: docs/phase-0-issues.md does not reference Lambda for the\n"
            "  LLM stages in issue #11 notes.  Required: an updated note that Lambda\n"
            "  (not Fargate) is preferred for the LLM stages (primary review,\n"
            "  adversarial review), which are thin Bedrock API callers."
        )

    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        (
            "AC1",
            "Exactly one canonical latency figure (1–3 min typical, 5 min p95)",
            check_ac1_canonical_latency,
        ),
        (
            "AC2",
            "Lambda decision for LLM stages recorded in docs/design-notes.md",
            check_ac2_lambda_decision,
        ),
        (
            "AC3",
            "Phase-0 issue #11 notes updated: Lambda (not Fargate) for LLM stages",
            check_ac3_phase0_issue11_notes,
        ),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"{code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All latency-baseline checks passed.")
        return 0
    else:
        print("One or more latency-baseline checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
