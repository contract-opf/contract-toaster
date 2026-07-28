#!/usr/bin/env python3
"""
CI gate for issue #20: Corpus snapshot store pinning, lifecycle, and rollback window.

Four invariants asserted by this gate (matching the issue #20 acceptance criteria):

  GATE 1 — Physical KB store pinned per execution (not just snapshot version)
    ARCHITECTURE.md must state that the physical store ID / KB ID is pinned into
    the Step Functions execution input at start, and that retrieval queries that
    exact store rather than resolving "active" at query time.

  GATE 2 — Rollback window (N retained stores) specified
    ARCHITECTURE.md must define a rollback window: N most recent snapshot stores
    are retained, and re-ingestion is the documented recovery path beyond that
    window.

  GATE 3 — POST /api/corpus/reindex removed or redefined as staging-only
    ARCHITECTURE.md must either:
      (a) show that POST /api/corpus/reindex has been removed from the routes
          table (404s or is absent), OR
      (b) show it is redefined to ingest into a new staging index and produce a
          draft snapshot (never mutates the active store).

  GATE 4 — In-flight-execution semantics documented and interlock restated
    ARCHITECTURE.md must:
      (a) document what happens to in-flight executions during an activation
          repoint (either allow old-store queries until completion, or drain),
      (b) restate the ingestion interlock as a meaningful condition: the resolved
          store must be active and have no in-progress ingestion job.

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
# GATE 1: Physical store ID pinned into Step Functions execution input
# ---------------------------------------------------------------------------
#
# The issue: step 10 records corpus_snapshot_version at start, but step 13
# queries "the active snapshot" — an activation repoint in between yields
# silently empty/partial retrieval. The fix: pin the physical KB ID / store ID
# into the execution input so retrieval hits the same store for the life of that
# execution.
#
# Required patterns (all must match somewhere in ARCHITECTURE.md):
PHYSICAL_STORE_PIN_PATTERNS = [
    # Explicit mention of physical store or KB ID being pinned per execution
    re.compile(
        r"physical\s+(?:store|KB|knowledge.?base)\s+(?:id|identifier)",
        re.IGNORECASE,
    ),
    # The pinning happens at execution start (not just snapshot version)
    re.compile(
        r"(?:pin|pinned|record)\s+.{0,80}"
        r"(?:execution\s+(?:input|start)|start.{0,40}execution)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Retrieval queries that exact store, not "the active snapshot" at query time
    re.compile(
        r"(?:retriev|quer(?:y|ies)).{0,120}"
        r"(?:exact\s+store|pinned\s+store|pinned\s+kb|kb\s+id|store\s+id)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 2: Rollback window — N most recent stores retained
# ---------------------------------------------------------------------------
#
# After repoint, the previous store must not be immediately reaped. A rollback
# window of N stores must be defined so bundle rollback can actually serve
# queries against a prior snapshot.
#
# Required patterns (all must match somewhere in ARCHITECTURE.md):
ROLLBACK_WINDOW_PATTERNS = [
    # N most recent stores retained (or equivalent numeric/policy statement)
    re.compile(
        r"(?:retain|keep|preserv).{0,120}"
        r"(?:N|most\s+recent|rollback\s+window|snapshot\s+stores?)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Re-ingestion is the recovery path beyond the window
    re.compile(
        r"re.?ingest(?:ion)?.{0,200}(?:recovery|beyond|window)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# ---------------------------------------------------------------------------
# GATE 3: POST /api/corpus/reindex removed or redefined as staging-only
# ---------------------------------------------------------------------------
#
# The original route "re-index after corpus changes" is never reconciled with
# draft/staging/activation — it implies a mutating path that is either dead or
# dangerous. It must be removed (404) or redefined to create a new staging index
# + draft snapshot (never mutate active).
#
# We check: the routes table in ARCHITECTURE.md either omits the route entirely,
# OR carries language making it staging-only / draft-creating.
#
# We look for the route in the table:
REINDEX_ROUTE_PATTERN = re.compile(
    r"POST\s+.{0,10}/api/corpus/reindex",
    re.IGNORECASE,
)
# If the route is present, it must be accompanied by staging/draft semantics:
REINDEX_STAGING_PATTERN = re.compile(
    r"/api/corpus/reindex.{0,400}"
    r"(?:staging|draft\s+snapshot|new\s+staging\s+index|404|removed)",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# GATE 4: In-flight semantics documented + interlock restated meaningfully
# ---------------------------------------------------------------------------
#
# (a) In-flight executions during activation: ARCHITECTURE.md must say what
#     happens — either old-store queries complete (execution pins the store),
#     or there is a drain/grace period.
# (b) Interlock restated: "ingestion targeting the active store is in progress"
#     was dead under the staging design. The meaningful condition is:
#     "the resolved store must be active and have no in-progress ingestion job."
#
# Required patterns:
IN_FLIGHT_SEMANTICS_PATTERNS = [
    # In-flight execution semantics during activation
    re.compile(
        r"(?:in.?flight|in\s+flight).{0,200}"
        r"(?:execution|review|old.?store|complete|drain|activat)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Restated interlock: resolved store is active + no in-progress ingestion
    re.compile(
        r"(?:interlock|refuse|refuses).{0,300}"
        r"(?:resolved\s+store|active\s+store|no\s+in.?progress|in.?progress\s+ingestion)",
        re.IGNORECASE | re.DOTALL,
    ),
]


# ---------------------------------------------------------------------------
# Gate runner helpers
# ---------------------------------------------------------------------------

def gate_1_physical_store_pin(arch_text: str) -> list[str]:
    failures = []
    labels = [
        "physical store/KB ID mentioned by name (not just snapshot version)",
        "store is pinned at execution start (recorded in execution input)",
        "retrieval queries the exact pinned store, not 'active' at query time",
    ]
    for i, (pattern, label) in enumerate(
        zip(PHYSICAL_STORE_PIN_PATTERNS, labels), 1
    ):
        if not pattern.search(arch_text):
            failures.append(
                f"  Gate 1.{i}: ARCHITECTURE.md does not contain the required "
                f"language for: {label}.\n"
                f"  Missing pattern: {pattern.pattern!r}\n"
                f"  Required: ARCHITECTURE.md must state that the physical KB store ID "
                f"is pinned into the Step Functions execution input at start and that "
                f"retrieval queries that exact store for the life of the execution."
            )
    return failures


def gate_2_rollback_window(arch_text: str) -> list[str]:
    failures = []
    labels = [
        "N most recent snapshot stores are retained (rollback window defined)",
        "re-ingestion is the documented recovery path beyond the rollback window",
    ]
    for i, (pattern, label) in enumerate(
        zip(ROLLBACK_WINDOW_PATTERNS, labels), 1
    ):
        if not pattern.search(arch_text):
            failures.append(
                f"  Gate 2.{i}: ARCHITECTURE.md does not contain the required "
                f"language for: {label}.\n"
                f"  Missing pattern: {pattern.pattern!r}\n"
                f"  Required: ARCHITECTURE.md must define a rollback window (retain N "
                f"most recent snapshot stores) and document re-ingestion as the recovery "
                f"path for snapshots beyond that window."
            )
    return failures


def gate_3_reindex_route(arch_text: str) -> list[str]:
    failures = []

    route_present = REINDEX_ROUTE_PATTERN.search(arch_text)

    if route_present:
        # Route is still in the doc — it must have staging/draft/removed semantics
        if not REINDEX_STAGING_PATTERN.search(arch_text):
            failures.append(
                "  Gate 3: ARCHITECTURE.md still contains POST /api/corpus/reindex "
                "but does NOT state that it is either removed (404) or redefined to "
                "ingest into a new staging index and produce a draft snapshot.\n"
                "  The original route description ('Re-index after corpus changes') "
                "implies a mutating path that conflicts with the draft/staging/activation "
                "design. It must be removed or redefined.\n"
                f"  Route pattern found: {REINDEX_ROUTE_PATTERN.pattern!r}\n"
                f"  Missing staging/removed pattern: {REINDEX_STAGING_PATTERN.pattern!r}\n"
                "  Required: either remove the route from the table (it 404s) or redefine "
                "it as 'ingest into new staging index → new draft snapshot' — never mutate active."
            )
    # If route is absent from the table, that satisfies the gate (it was removed).
    # No failure in that case.
    return failures


def gate_4_in_flight_semantics(arch_text: str) -> list[str]:
    failures = []
    labels = [
        "in-flight execution semantics during activation are documented (old-store completes or drain)",
        "interlock restated as: resolved store is active AND has no in-progress ingestion job",
    ]
    for i, (pattern, label) in enumerate(
        zip(IN_FLIGHT_SEMANTICS_PATTERNS, labels), 1
    ):
        if not pattern.search(arch_text):
            failures.append(
                f"  Gate 4.{i}: ARCHITECTURE.md does not contain the required "
                f"language for: {label}.\n"
                f"  Missing pattern: {pattern.pattern!r}\n"
                f"  Required: ARCHITECTURE.md must document in-flight execution "
                f"semantics during activation and restate the ingestion interlock as "
                f"a meaningful, non-dead condition."
            )
    return failures


def main() -> int:
    try:
        arch_text = read_text(ARCHITECTURE_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    all_failures: list[str] = []

    # ── Gate 1 ──────────────────────────────────────────────────────────────
    print("Gate 1: Physical KB store ID pinned per execution (not just snapshot version)")
    g1 = gate_1_physical_store_pin(arch_text)
    if g1:
        for f in g1:
            print(f)
        all_failures.extend(g1)
    else:
        print("  PASS")

    print()
    # ── Gate 2 ──────────────────────────────────────────────────────────────
    print("Gate 2: Rollback window (N retained stores) specified + re-ingestion recovery path")
    g2 = gate_2_rollback_window(arch_text)
    if g2:
        for f in g2:
            print(f)
        all_failures.extend(g2)
    else:
        print("  PASS")

    print()
    # ── Gate 3 ──────────────────────────────────────────────────────────────
    print("Gate 3: POST /api/corpus/reindex removed or redefined as staging-only")
    g3 = gate_3_reindex_route(arch_text)
    if g3:
        for f in g3:
            print(f)
        all_failures.extend(g3)
    else:
        print("  PASS")

    print()
    # ── Gate 4 ──────────────────────────────────────────────────────────────
    print("Gate 4: In-flight-execution semantics documented + interlock restated")
    g4 = gate_4_in_flight_semantics(arch_text)
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
            "See issue #20 for the full remediation plan."
        )
        return 1
    else:
        print("PASS: all corpus store pinning and lifecycle gates satisfied.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
