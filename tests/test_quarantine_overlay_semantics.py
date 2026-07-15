#!/usr/bin/env python3
"""
CI gate for issue #23: QUARANTINED/SUPERSEDED as post-terminal overlays;
close the rollback in-flight race.

Three invariants asserted by this gate:

  GATE 1 — Projection-invariant completeness
    The `status` field's documented mapping from `confidence_state` must be
    exhaustive — and QUARANTINED/SUPERSEDED must be explicitly called out as
    *post-terminal administrative overlays* (not pipeline-projected states).
    Without this, the invariant "status is derived solely from confidence_state"
    appears to be violated by quarantine mutations on DONE reviews.

  GATE 2 — Rollback in-flight race closed
    ARCHITECTURE.md must specify what happens to a review that is RUNNING under
    a bad bundle at the moment a rollback sweep fires.  The specification must:
      (a) state that in-flight executions are allowed to finish (not aborted by
          the sweep), and
      (b) state that a second quarantine sweep keyed by bundle hash runs after
          completion so RUNNING→DONE reviews are not silently missed.

  GATE 3 — data-handling field dictionary updated
    docs/data-handling.md field table must include the `administrative_overlay`
    field (or an explicit note that QUARANTINED/SUPERSEDED are overlays carried
    in `status` itself under documented exception semantics), so every `reviews`
    field has classification and retention documented.

Exit codes: 0 = pass, 1 = fail
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
DATA_HANDLING_PATH = REPO_ROOT / "docs" / "data-handling.md"
RUNBOOK_PATH = REPO_ROOT / "RUNBOOK.md"


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# GATE 1: QUARANTINED/SUPERSEDED are post-terminal administrative overlays
# ---------------------------------------------------------------------------
#
# The issue: ARCHITECTURE.md states status is the "terminal projection" of
# confidence_state, but confidence_state has no QUARANTINED/SUPERSEDED values.
# When a rollback sweep mutates a DONE review to QUARANTINED, the invariant
# appears broken — but only because the overlay semantics were never documented.
#
# The fix: ARCHITECTURE.md must explicitly state that QUARANTINED and SUPERSEDED
# are *post-terminal administrative overlays* applied after a review reaches a
# pipeline-terminal state, and are therefore a documented exception to (or
# extension of) the projection rule — not pipeline-derived values.
#
# Required patterns (all must match somewhere in ARCHITECTURE.md):
OVERLAY_PATTERNS = [
    # The word "overlay" must appear in connection with QUARANTINED/SUPERSEDED
    re.compile(
        r"(?:QUARANTINED|SUPERSEDED).{0,400}"
        r"(?:overlay|post.?terminal|administrative)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Must state these are NOT pipeline-derived / not from confidence_state
    # (i.e., they are applied by an admin action or rollback sweep, not the pipeline)
    re.compile(
        r"(?:post.?terminal|administrative\s+overlay|overlay.{0,80}(?:QUARANTINED|SUPERSEDED)"
        r"|(?:QUARANTINED|SUPERSEDED).{0,200}(?:admin|rollback\s+sweep|not.*pipeline"
        r"|not.*confidence_state|outside.*pipeline))",
        re.IGNORECASE | re.DOTALL,
    ),
    # The projection-invariant paragraph must be updated to acknowledge the exception
    re.compile(
        r"confidence_state.{0,600}"
        r"(?:QUARANTINED|SUPERSEDED|overlay|exception|post.?terminal|administrative)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 2: Rollback in-flight race closed
# ---------------------------------------------------------------------------
#
# The issue: when a rollback fires and marks all reviews under the bad bundle
# as QUARANTINED, a review currently RUNNING under that bundle may:
#   (a) finish and be written as DONE — then missed by the sweep that already ran
#   (b) be aborted mid-execution — losing work and spending already incurred
#
# The fix: the spec must say "let in-flight executions finish; a second quarantine
# sweep keyed by bundle hash (not review status) catches reviews that land DONE
# after the initial sweep."
#
# Required patterns (all must match somewhere in ARCHITECTURE.md):
INFLIGHT_ROLLBACK_PATTERNS = [
    # In-flight reviews are let to finish (not aborted by the rollback sweep)
    re.compile(
        r"(?:in.?flight|RUNNING).{0,400}"
        r"(?:finish|complete|let.{0,40}finish|not.{0,40}abort|allow.{0,40}finish"
        r"|run.{0,40}to.{0,40}completion|finish.{0,40}then)",
        re.IGNORECASE | re.DOTALL,
    ),
    # A second/follow-up quarantine sweep keyed by bundle hash catches late completions
    re.compile(
        r"(?:second\s+sweep|follow.?up\s+sweep|sweep\s+keyed|bundle.?hash\s+sweep"
        r"|quarantin.{0,200}bundle.?hash|bundle.?hash.{0,200}quarantin"
        r"|keyed\s+by\s+bundle|sweep.{0,100}bundle.?hash|post.?completion\s+sweep"
        r"|second\s+quarantin|quarantin\s+sweep.{0,100}(?:completion|finish|DONE))",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 3: data-handling field dictionary covers QUARANTINED/SUPERSEDED overlays
# ---------------------------------------------------------------------------
#
# The issue: QUARANTINED and SUPERSEDED are status values written by admin
# actions, not the pipeline. If a new `administrative_overlay` field is
# introduced (or the existing `status` field gets an overlay exception note),
# the canonical field dictionary in docs/data-handling.md must be updated.
#
# The gate is satisfied if EITHER:
#   (a) data-handling.md mentions "administrative_overlay" as a field, or
#   (b) data-handling.md explicitly acknowledges QUARANTINED/SUPERSEDED as
#       post-terminal overlay values on the `status` field (not pipeline-derived).
#
# Required: at least one of these patterns must match in data-handling.md:
DATA_HANDLING_PATTERNS = [
    re.compile(
        r"administrative[_\s]overlay",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:QUARANTINED|SUPERSEDED).{0,400}"
        r"(?:overlay|post.?terminal|administrative|not.*pipeline|not.*confidence_state"
        r"|admin\s+action|rollback\s+sweep)",
        re.IGNORECASE | re.DOTALL,
    ),
]


def gate_1_overlay_semantics(arch_text: str) -> list[str]:
    failures = []
    labels = [
        "QUARANTINED/SUPERSEDED connected to 'overlay' or 'post-terminal' or 'administrative'",
        "post-terminal administrative overlay — not pipeline-derived from confidence_state",
        "projection-invariant paragraph acknowledges the QUARANTINED/SUPERSEDED exception",
    ]
    for i, (pattern, label) in enumerate(zip(OVERLAY_PATTERNS, labels), 1):
        if pattern.search(arch_text):
            print(f"  [PASS] Gate 1.{i}: {label}")
        else:
            msg = (
                f"  [FAIL] Gate 1.{i}: {label}\n"
                f"         Pattern: {pattern.pattern[:120]!r}\n"
                f"         Required: ARCHITECTURE.md must define QUARANTINED/SUPERSEDED as\n"
                f"         post-terminal administrative overlays and update the projection\n"
                f"         invariant paragraph to acknowledge the exception."
            )
            print(msg)
            failures.append(msg)
    return failures


def gate_2_inflight_rollback(arch_text: str) -> list[str]:
    failures = []
    labels = [
        "in-flight RUNNING reviews are let to finish (not aborted by rollback sweep)",
        "second/follow-up quarantine sweep keyed by bundle hash catches late DONE reviews",
    ]
    for i, (pattern, label) in enumerate(zip(INFLIGHT_ROLLBACK_PATTERNS, labels), 1):
        if pattern.search(arch_text):
            print(f"  [PASS] Gate 2.{i}: {label}")
        else:
            msg = (
                f"  [FAIL] Gate 2.{i}: {label}\n"
                f"         Pattern: {pattern.pattern[:120]!r}\n"
                f"         Required: ARCHITECTURE.md must specify that in-flight RUNNING\n"
                f"         reviews under a bad bundle are let to finish, and that a second\n"
                f"         quarantine sweep keyed by bundle hash catches any that land DONE\n"
                f"         after the initial rollback sweep."
            )
            print(msg)
            failures.append(msg)
    return failures


def gate_3_data_handling(data_handling_text: str) -> list[str]:
    failures = []
    # Gate 3 passes if ANY of the patterns matches
    for pattern in DATA_HANDLING_PATTERNS:
        if pattern.search(data_handling_text):
            print(
                "  [PASS] Gate 3: data-handling.md documents QUARANTINED/SUPERSEDED "
                "overlay semantics or administrative_overlay field"
            )
            return []
    # None matched
    msg = (
        "  [FAIL] Gate 3: docs/data-handling.md does not acknowledge QUARANTINED/SUPERSEDED\n"
        "         as post-terminal overlays (or introduce an administrative_overlay field).\n"
        "         Pattern options checked:\n"
        + "\n".join(f"           {p.pattern[:100]!r}" for p in DATA_HANDLING_PATTERNS)
        + "\n"
        "         Required: the field dictionary must cover how QUARANTINED/SUPERSEDED are\n"
        "         set and what classification/retention applies."
    )
    print(msg)
    failures.append(msg)
    return failures


def main() -> int:
    errors = []
    try:
        arch_text = read_text(ARCHITECTURE_PATH)
    except FileNotFoundError as exc:
        print(f"FAIL: {exc}")
        return 1

    try:
        data_handling_text = read_text(DATA_HANDLING_PATH)
    except FileNotFoundError as exc:
        print(f"FAIL: {exc}")
        return 1

    print("Gate 1: QUARANTINED/SUPERSEDED defined as post-terminal administrative overlays")
    errors += gate_1_overlay_semantics(arch_text)

    print()
    print("Gate 2: Rollback in-flight race closed — in-flight reviews finish, second sweep catches late completions")
    errors += gate_2_inflight_rollback(arch_text)

    print()
    print("Gate 3: data-handling.md field dictionary updated for overlay semantics")
    errors += gate_3_data_handling(data_handling_text)

    print()
    if errors:
        print(
            f"FAIL — {len(errors)} check(s) did not pass. "
            "See issue #23 for the remediation plan."
        )
        return 1

    print("PASS — all QUARANTINED/SUPERSEDED overlay and rollback in-flight race checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
