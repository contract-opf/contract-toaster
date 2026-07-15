#!/usr/bin/env python3
"""
CI gate for issue #21: Resolve the release bundle once at submission; execution
verifies, never re-resolves.

Four invariants asserted by this gate (matching the issue #21 acceptance criteria):

  GATE 1 — Single resolution point: submission stores the bundle hash
    ARCHITECTURE.md data-flow step 3 must state that the resolved bundle hash is
    STORED on the submission record (not just used for the idempotency key).
    The record must carry the bundle hash so step 10 can verify it rather than
    re-resolve.

  GATE 2 — Execution verifies, never re-resolves
    ARCHITECTURE.md data-flow step 10 must state that the pipeline VERIFIES or
    READS the bundle hash from the submission record (stored at step 3), and
    does NOT re-resolve the active bundle independently.

  GATE 3 — Retired-bundle-before-start behavior defined
    ARCHITECTURE.md must define what happens when the submission-time bundle is
    retired or quarantined before execution starts: the review must be refused
    or re-routed with a specific status — not silently run under the new active
    bundle.

  GATE 4 — RUNBOOK documents the retired-bundle-before-start operational behavior
    RUNBOOK.md must document the retired-bundle-before-start case and its
    operational handling (e.g. the review lands in a named terminal status, and
    the operator procedure for it).

Exit codes: 0 = all checks pass, 1 = one or more checks fail.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
RUNBOOK_PATH = REPO_ROOT / "RUNBOOK.md"


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# GATE 1: Submission (step 3) stores the resolved bundle hash on the record
# ---------------------------------------------------------------------------
#
# The issue: step 3 resolves the bundle and uses the hash only for the
# idempotency key. The fix: step 3 must also store the resolved bundle hash
# on the submission record so the execution can read it back at step 10.
#
# Required patterns (all must match somewhere in the step 3 / submission section
# of ARCHITECTURE.md):
STEP3_STORES_BUNDLE_PATTERNS = [
    # Step 3 must say the bundle hash is stored/recorded on the submission record
    re.compile(
        r"(?:step\s+3|submission\s+record|submission\s+step).{0,400}"
        r"(?:store[sd]?|record[sd]?|write[sd]?|persist[sd]?).{0,150}"
        r"(?:bundle\s+hash|release.?bundle\s+hash|resolved\s+bundle)",
        re.IGNORECASE | re.DOTALL,
    ),
    # The submission record must carry the bundle hash field explicitly
    re.compile(
        r"(?:submission\s+record|record\s+owns|submission/idempotency\s+record).{0,500}"
        r"(?:bundle\s+hash|release.?bundle\s+hash|resolved.?bundle)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 2: Execution start (step 10) verifies, never re-resolves
# ---------------------------------------------------------------------------
#
# Step 10 must say it reads/verifies the bundle hash stored at submission,
# NOT that it resolves "the active release bundle" independently.
#
# Required patterns (all must match somewhere in ARCHITECTURE.md):
STEP10_VERIFIES_PATTERNS = [
    # Step 10 must reference verifying/reading from the submission record
    re.compile(
        r"(?:step\s+10|execution\s+start).{0,600}"
        r"(?:verif|read[sd]?\s+.{0,60}bundle|stored\s+at\s+submission|submission.?time\s+bundle"
        r"|bundle\s+recorded\s+at\s+submission|bundle\s+from\s+the\s+submission)",
        re.IGNORECASE | re.DOTALL,
    ),
    # The architectural doc must explicitly state that execution never re-resolves
    re.compile(
        r"(?:execut\w+|step\s+10).{0,400}"
        r"(?:never\s+re.?resolv|does\s+not\s+re.?resolv|verif\w+.{0,100}bundle"
        r"|single\s+resolution|resolv\w+\s+once)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 3: Retired-bundle-before-start behavior defined in ARCHITECTURE.md
# ---------------------------------------------------------------------------
#
# If the submission-time bundle is retired or quarantined between submission
# (step 3) and execution start (step 10), the review must not silently run
# under the new active bundle. A specific status / refusal behavior must be
# defined.
#
# Required patterns (both must match somewhere in ARCHITECTURE.md):
RETIRED_BUNDLE_BEHAVIOR_PATTERNS = [
    # The scenario: bundle retired/quarantined before execution starts
    re.compile(
        r"(?:retire[sd]?|quarantin\w+).{0,300}"
        r"(?:before\s+execution\s+start|before\s+execution\s+begins"
        r"|between\s+submission|before\s+step\s+10)",
        re.IGNORECASE | re.DOTALL,
    ),
    # The behavior: refused/re-routed with a specific status (not silently continued)
    re.compile(
        r"(?:bundle.{0,80}(?:retire[sd]?|quarantin\w+)|retire[sd]?.{0,80}bundle).{0,500}"
        r"(?:refus\w+|re.?rout\w+|QUARANTINED|specific\s+status|not\s+silently"
        r"|cannot\s+run|must\s+not\s+run|reject\w*)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 4: RUNBOOK documents the retired-bundle-before-start operational behavior
# ---------------------------------------------------------------------------
#
# RUNBOOK.md must include operational guidance for the case where a submission's
# bundle is retired or quarantined before its execution starts, naming the
# resulting status and what the operator does.
#
# Required patterns (all must match somewhere in RUNBOOK.md):
RUNBOOK_RETIRED_BUNDLE_PATTERNS = [
    # RUNBOOK must mention bundle retired/quarantined before execution / at start
    re.compile(
        r"(?:retire[sd]?|quarantin\w+).{0,400}"
        r"(?:before\s+execution|start|submission.?time|pending\s+review)",
        re.IGNORECASE | re.DOTALL,
    ),
    # RUNBOOK must name the resulting status or the recovery action
    re.compile(
        r"(?:QUARANTINED|bundle\s+retir|retir\w+\s+bundle).{0,500}"
        r"(?:re.?run|re.?submit|operator|status\s*=|landing\s+in|land\s+in|result\w*\s+in)",
        re.IGNORECASE | re.DOTALL,
    ),
]


# ---------------------------------------------------------------------------
# Gate runner helpers
# ---------------------------------------------------------------------------

def gate_1_submission_stores_bundle(arch_text: str) -> list[str]:
    failures = []
    labels = [
        "step 3 stores/records the resolved bundle hash on the submission record",
        "submission record explicitly carries the bundle hash field",
    ]
    for i, (pattern, label) in enumerate(
        zip(STEP3_STORES_BUNDLE_PATTERNS, labels), 1
    ):
        if not pattern.search(arch_text):
            failures.append(
                f"  Gate 1.{i}: ARCHITECTURE.md does not contain the required "
                f"language for: {label}.\n"
                f"  Missing pattern: {pattern.pattern!r}\n"
                f"  Required: data-flow step 3 must explicitly store the resolved "
                f"release-bundle hash on the submission record so that step 10 can "
                f"read and verify it rather than re-resolving the active bundle."
            )
    return failures


def gate_2_execution_verifies_not_resolves(arch_text: str) -> list[str]:
    failures = []
    labels = [
        "step 10 reads/verifies the bundle from the submission record (not re-resolves)",
        "ARCHITECTURE.md explicitly states execution never re-resolves the bundle",
    ]
    for i, (pattern, label) in enumerate(
        zip(STEP10_VERIFIES_PATTERNS, labels), 1
    ):
        if not pattern.search(arch_text):
            failures.append(
                f"  Gate 2.{i}: ARCHITECTURE.md does not contain the required "
                f"language for: {label}.\n"
                f"  Missing pattern: {pattern.pattern!r}\n"
                f"  Required: data-flow step 10 must state that it reads/verifies the "
                f"bundle hash stored at submission (step 3) and does NOT independently "
                f"re-resolve 'the active bundle' at execution start."
            )
    return failures


def gate_3_retired_bundle_behavior_defined(arch_text: str) -> list[str]:
    failures = []
    labels = [
        "scenario: bundle retired/quarantined between submission and execution start",
        "behavior: review refused/re-routed with specific status — not silently continued",
    ]
    for i, (pattern, label) in enumerate(
        zip(RETIRED_BUNDLE_BEHAVIOR_PATTERNS, labels), 1
    ):
        if not pattern.search(arch_text):
            failures.append(
                f"  Gate 3.{i}: ARCHITECTURE.md does not contain the required "
                f"language for: {label}.\n"
                f"  Missing pattern: {pattern.pattern!r}\n"
                f"  Required: ARCHITECTURE.md must define the behavior when the "
                f"submission-time bundle is retired or quarantined before execution "
                f"starts: the review must be refused or re-routed with a specific named "
                f"status, not silently run under the newly active bundle."
            )
    return failures


def gate_4_runbook_retired_bundle(runbook_text: str) -> list[str]:
    failures = []
    labels = [
        "RUNBOOK documents the bundle-retired-before-start scenario",
        "RUNBOOK names the resulting status or describes the operator recovery action",
    ]
    for i, (pattern, label) in enumerate(
        zip(RUNBOOK_RETIRED_BUNDLE_PATTERNS, labels), 1
    ):
        if not pattern.search(runbook_text):
            failures.append(
                f"  Gate 4.{i}: RUNBOOK.md does not contain the required "
                f"language for: {label}.\n"
                f"  Missing pattern: {pattern.pattern!r}\n"
                f"  Required: RUNBOOK.md must document the operational case where a "
                f"submission's release bundle is retired or quarantined before its "
                f"Step Functions execution begins, name the resulting review status, "
                f"and describe what the operator does (e.g. re-run, re-submit)."
            )
    return failures


def main() -> int:
    try:
        arch_text = read_text(ARCHITECTURE_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    try:
        runbook_text = read_text(RUNBOOK_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    all_failures: list[str] = []

    # ── Gate 1 ──────────────────────────────────────────────────────────────
    print("Gate 1: Submission (step 3) stores the resolved bundle hash on the record")
    g1 = gate_1_submission_stores_bundle(arch_text)
    if g1:
        for f in g1:
            print(f)
        all_failures.extend(g1)
    else:
        print("  PASS")

    print()
    # ── Gate 2 ──────────────────────────────────────────────────────────────
    print("Gate 2: Execution start (step 10) verifies stored bundle, never re-resolves")
    g2 = gate_2_execution_verifies_not_resolves(arch_text)
    if g2:
        for f in g2:
            print(f)
        all_failures.extend(g2)
    else:
        print("  PASS")

    print()
    # ── Gate 3 ──────────────────────────────────────────────────────────────
    print("Gate 3: Retired-bundle-before-start behavior defined in ARCHITECTURE.md")
    g3 = gate_3_retired_bundle_behavior_defined(arch_text)
    if g3:
        for f in g3:
            print(f)
        all_failures.extend(g3)
    else:
        print("  PASS")

    print()
    # ── Gate 4 ──────────────────────────────────────────────────────────────
    print("Gate 4: RUNBOOK.md documents the retired-bundle-before-start operational behavior")
    g4 = gate_4_runbook_retired_bundle(runbook_text)
    if g4:
        for f in g4:
            print(f)
        all_failures.extend(g4)
    else:
        print("  PASS")

    print()
    if all_failures:
        print(
            f"FAIL: {len(all_failures)} issue(s) found. "
            "See issue #21 for the full remediation plan."
        )
        return 1
    else:
        print("PASS: all bundle-resolve-once gates satisfied.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
