#!/usr/bin/env python3
"""
CI gate for issue #22: Orphan reconciler — handle dead executions; add
execution timeout and semaphore lease recovery.

Four invariants asserted by this gate (matching the issue #22 acceptance
criteria):

  GATE 1 — Dead-execution path: reconciler transitions to ERROR with release
    ARCHITECTURE.md must state that the orphan reconciler handles executions in
    FAILED / TIMED_OUT / ABORTED status — not only the missing-ARN case — and
    that it transitions the review to ERROR and releases the spend reservation
    and concurrency slot.

  GATE 2 — Execution-level timeout set and asserted
    ARCHITECTURE.md must state that the Step Functions state machine has an
    overall execution-level timeout (not only per-step timeouts), so a stuck
    RUNNING execution is automatically terminated rather than requiring manual
    intervention.

  GATE 3 — Semaphore lease / slot-leak recovery implemented and documented
    ARCHITECTURE.md must state that concurrency-semaphore slots held by dead
    executions are reclaimed — either via lease/TTL semantics on the semaphore
    entries, or via a reaper that reconciles held slots against live executions.
    The stale-PENDING alarm must be extended (or a companion alarm added) to
    cover PENDING-with-dead-ARN and stale RUNNING.

  GATE 4 — RUNBOOK "stuck reviews" entry updated with expected observations
    RUNBOOK.md "stuck reviews" section must document the new observations:
    - PENDING-with-dead-ARN (ExecutionAlreadyExists returning a terminal
      execution) and how the reconciler handles it.
    - Stale RUNNING reviews (execution timeout fires, slot reaper recovers).

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
# GATE 1: Reconciler handles dead executions (FAILED/TIMED_OUT/ABORTED)
# ---------------------------------------------------------------------------
#
# The issue: the orphan reconciler only re-drives the missing-ARN case
# ("ensure execution started"). If an execution dies after its ARN is stored
# but before its error-handling states run, DescribeExecution returns
# ExecutionAlreadyExists and the reconciler "records the existing execution"
# — pinning the review to a corpse in a terminal-but-unhandled state. The
# review sits PENDING forever *with* an ARN, invisible to the stale-PENDING
# alarm that only fires on missing ARNs.
#
# The fix: the reconciler must call DescribeExecution on any non-terminal
# review that has an ARN, detect FAILED/TIMED_OUT/ABORTED, and transition
# the review to ERROR with reservation + slot release.
#
# Required patterns (all must match somewhere in ARCHITECTURE.md):
DEAD_EXEC_PATTERNS = [
    # Reconciler must reference checking DescribeExecution status
    re.compile(
        r"(?:reconcil\w+|orphan).{0,400}"
        r"(?:DescribeExecution|execution\s+status|dead\s+execution"
        r"|FAILED|TIMED_OUT|ABORTED|terminal\s+execution)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Reconciler must drive the review to ERROR and release slot+reservation
    re.compile(
        r"(?:reconcil\w+|orphan|dead\s+execution|FAILED|TIMED_OUT|ABORTED).{0,500}"
        r"(?:ERROR|transition\w*\s+to\s+ERROR|releases?\s+.{0,60}"
        r"(?:reservation|slot|semaphore))",
        re.IGNORECASE | re.DOTALL,
    ),
    # The PENDING-with-dead-ARN stuck state must be explicitly addressed
    re.compile(
        r"(?:PENDING.{0,60}(?:ARN|execution)|dead.?ARN"
        r"|corpse|ARN.{0,60}PENDING|execution.{0,60}PENDING.{0,60}ARN)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 2: State machine has an overall execution-level timeout
# ---------------------------------------------------------------------------
#
# The issue: per-step timeouts exist but no state-machine-level timeout.
# A pathological execution can sit in RUNNING for days, leaking a concurrency
# slot and never triggering the stale-RUNNING recovery path.
#
# Required patterns (all must match somewhere in ARCHITECTURE.md):
EXEC_TIMEOUT_PATTERNS = [
    # State machine or execution has an overall timeout
    re.compile(
        r"(?:state.?machine|Step\s+Functions|execution).{0,300}"
        r"(?:overall\s+.{0,60}timeout|execution.?level\s+timeout"
        r"|machine.?level\s+timeout|overall\s+execution\s+timeout"
        r"|timeout\s+.{0,60}(?:state.?machine|execution.?level))",
        re.IGNORECASE | re.DOTALL,
    ),
    # The infra must assert this timeout is set (not just mentioned)
    re.compile(
        r"(?:execution.?level\s+timeout|overall\s+.{0,40}timeout|machine.?level\s+timeout"
        r"|timeout\s+.{0,20}set|timeout\s+.{0,30}assert).{0,200}"
        r"(?:state.?machine|Step\s+Functions|infra|CDK|asserted|set)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 3: Semaphore lease / slot-leak recovery
# ---------------------------------------------------------------------------
#
# The issue: the concurrency semaphore releases the slot only on handled-failure
# paths (states that cleanly run the release step). A hard-killed execution
# (process kill, Lambda OOM, Fargate SIGKILL) never runs the release, leaking a
# slot permanently at the system's low concurrency cap.
#
# Two acceptable implementations:
#   (a) Lease/TTL semantics on the semaphore entries: a slot entry carries an
#       expiry; a reaper or the next acquire checks and reclaims expired entries.
#   (b) A slot reaper that reconciles held slots against live Step Functions
#       executions: any slot held by an execution not in RUNNING state is
#       reclaimed.
#
# The stale-PENDING alarm must also be extended (or a companion alarm added)
# to cover PENDING-with-dead-ARN and stale RUNNING.
#
# Required patterns (all must match somewhere in ARCHITECTURE.md):
SEMAPHORE_LEASE_PATTERNS = [
    # Lease / TTL or slot reaper must be stated
    re.compile(
        r"(?:semaphore|slot|concurrency).{0,400}"
        r"(?:lease|TTL|expir\w+|reaper|reclaim\w*"
        r"|slot.?leak\s+recov\w*|leak.{0,60}recov\w*)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Stale-RUNNING alarm coverage: the alarm or the reconciler extends to RUNNING
    re.compile(
        r"(?:alarm|reconcil\w+).{0,400}"
        r"(?:stale\s+RUNNING|RUNNING.{0,60}stale"
        r"|PENDING.{0,60}(?:dead.?ARN|dead\s+execution|ARN.{0,40}(?:FAILED|TIMED_OUT))"
        r"|dead.?ARN.{0,60}PENDING)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 4: RUNBOOK "stuck reviews" section updated
# ---------------------------------------------------------------------------
#
# The RUNBOOK.md "stuck reviews" section must document:
#   (a) PENDING-with-dead-ARN: what the reconciler does when DescribeExecution
#       returns a terminal status.
#   (b) Stale RUNNING: the execution timeout fires and/or the slot reaper
#       reclaims the leaked slot.
#
# Required patterns (both must match in RUNBOOK.md):
RUNBOOK_STUCK_PATTERNS = [
    # PENDING-with-dead-ARN must be in the RUNBOOK stuck-reviews section
    re.compile(
        r"(?:PENDING.{0,120}(?:dead.?ARN|terminal|FAILED|TIMED_OUT|ABORTED)"
        r"|dead.?ARN.{0,120}PENDING"
        r"|DescribeExecution.{0,200}(?:FAILED|TIMED_OUT|ABORTED|terminal)"
        r"|(?:FAILED|TIMED_OUT|ABORTED).{0,200}review.{0,100}ERROR)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Stale RUNNING with slot reaper / lease recovery must be documented
    re.compile(
        r"(?:stale\s+RUNNING|RUNNING.{0,120}(?:timeout|reaper|lease|slot.?recov\w*)"
        r"|execution.?level\s+timeout.{0,200}(?:RUNNING|slot|recov\w*)"
        r"|slot.{0,120}(?:reaper|reclaim|lease).{0,200}RUNNING)",
        re.IGNORECASE | re.DOTALL,
    ),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _check(patterns: list, text: str, label: str) -> list[str]:
    """Return a list of failure messages for patterns that do not match."""
    failures = []
    for i, pat in enumerate(patterns, 1):
        if pat.search(text):
            print(f"  [PASS] {label} — pattern {i}")
        else:
            msg = f"  [FAIL] {label} — pattern {i} not matched"
            print(msg)
            print(f"         Pattern: {pat.pattern[:120]!r}")
            failures.append(msg)
    return failures


def main() -> int:
    arch = read_text(ARCHITECTURE_PATH)
    runbook = read_text(RUNBOOK_PATH)

    all_failures: list[str] = []

    print("\nGATE 1: Dead-execution path — reconciler drives review to ERROR + release")
    all_failures += _check(DEAD_EXEC_PATTERNS, arch, "ARCHITECTURE.md dead-execution handling")

    print("\nGATE 2: Execution-level timeout on the state machine")
    all_failures += _check(EXEC_TIMEOUT_PATTERNS, arch, "ARCHITECTURE.md execution-level timeout")

    print("\nGATE 3: Semaphore lease / slot-leak recovery")
    all_failures += _check(SEMAPHORE_LEASE_PATTERNS, arch, "ARCHITECTURE.md semaphore lease/reaper")

    print("\nGATE 4: RUNBOOK stuck-reviews section updated")
    all_failures += _check(RUNBOOK_STUCK_PATTERNS, runbook, "RUNBOOK.md stuck-reviews observations")

    print()
    if all_failures:
        print(f"FAIL — {len(all_failures)} check(s) did not pass.")
        return 1
    print("PASS — all orphan-reconciler robustness checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
