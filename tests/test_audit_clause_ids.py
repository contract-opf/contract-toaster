#!/usr/bin/env python3
"""
CI gate for issue #27: Record retrieved clause_ids in the non-substantive
audit record.

After a review completes the non-substantive audit record must contain the
retrieved clause_ids (+ polarity/channel) so that a corpus-poisoning
investigation discovered after the upload purge can still determine exactly
which clauses the model saw — not just the candidate pool the snapshot
contained.

Three gates:

  GATE 1 — ARCHITECTURE.md data-flow step 22 includes retrieved clause_ids
    The pipeline's audit step (step 22) must list retrieved clause_ids
    (with polarity and channel) as a non-substantive field recorded in the
    immutable audit entry.

  GATE 2 — ARCHITECTURE.md Audit posture section records clause_ids
    The Audit posture paragraph that enumerates what audit rows include must
    mention retrieved clause_ids (polarity/channel).

  GATE 3 — ARCHITECTURE.md reproducibility claim is accurate
    The "Frozen content-addressed manifest" mechanism (corpus snapshot
    description) must NOT claim a review is reproducible against "an exact,
    known set of clauses" — the manifest gives the candidate pool, but
    top-K retrieval over an approximate vector index is not reproducible from
    the manifest alone.  The correct claim is:
      "candidate pool reproducible; retrieved set recorded"
    or equivalent language that separates the manifest guarantee (candidate
    pool) from the new audit guarantee (retrieved set recorded).

  GATE 4 — docs/data-handling.md whitelist includes retrieved clause_ids
    The canonical audit-substance whitelist in docs/data-handling.md must
    include retrieved_clause_ids (opaque identifiers, polarity/channel) as a
    non-substantive, indefinitely-retained field.

  GATE 5 — docs/audit-queries.md has "which clauses informed review X" query
    The audit query catalogue must include a query that answers "which clauses
    informed review X", keyed by review_id.

Exit codes: 0 = all checks pass, 1 = one or more checks fail.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
DATA_HANDLING_PATH = REPO_ROOT / "docs" / "data-handling.md"
AUDIT_QUERIES_PATH = REPO_ROOT / "docs" / "audit-queries.md"


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# GATE 1 — step 22 includes retrieved clause_ids
# ---------------------------------------------------------------------------

def gate_1_step22_clause_ids() -> list[str]:
    """Data-flow step 22 must record retrieved clause_ids (polarity/channel)."""
    failures: list[str] = []
    arch = read_text(ARCHITECTURE_PATH)

    # Find the step 22 block — it describes the audit append in the data flow.
    # Accept any phrasing that includes clause_ids alongside polarity or channel.
    step22_pattern = re.compile(
        r"22\.\s.*?(?=\n\s*\n|\n\s*2[3-9]\.|$)",
        re.DOTALL,
    )
    m = step22_pattern.search(arch)
    step22_text = m.group(0) if m else ""

    has_clause_ids = bool(re.search(
        r"clause[_\s]ids?|clauseIds?",
        step22_text,
        re.IGNORECASE,
    ))
    has_polarity_or_channel = bool(re.search(
        r"polarity|channel",
        step22_text,
        re.IGNORECASE,
    ))

    if not has_clause_ids:
        failures.append(
            "GATE 1 FAIL: ARCHITECTURE.md data-flow step 22 does not mention "
            "retrieved clause_ids.\n"
            "  The pipeline audit step must record retrieved clause_ids so a "
            "corpus-poisoning investigation can determine exactly which clauses "
            "the model saw, even after the upload purge window expires."
        )
    if not has_polarity_or_channel:
        failures.append(
            "GATE 1 FAIL: ARCHITECTURE.md data-flow step 22 mentions clause_ids "
            "but not polarity or channel.\n"
            "  Polarity (positive/negative) and channel are non-substantive "
            "identifiers that belong in the audit record alongside clause_ids."
        )

    return failures


# ---------------------------------------------------------------------------
# GATE 2 — Audit posture paragraph includes clause_ids
# ---------------------------------------------------------------------------

def gate_2_audit_posture_clause_ids() -> list[str]:
    """Audit posture 'rows include ...' paragraph must mention retrieved clause_ids."""
    failures: list[str] = []
    arch = read_text(ARCHITECTURE_PATH)

    # Find the paragraph starting "Audit rows include ..."
    m = re.search(
        r"Audit rows include\b.*?(?=\n\n|\Z)",
        arch,
        re.DOTALL,
    )
    posture_text = m.group(0) if m else ""

    if not re.search(r"clause[_\s]ids?|clauseIds?", posture_text, re.IGNORECASE):
        failures.append(
            "GATE 2 FAIL: ARCHITECTURE.md Audit posture 'Audit rows include ...' "
            "paragraph does not mention retrieved clause_ids.\n"
            "  Add retrieved clause_ids (polarity/channel) to the enumeration of "
            "non-substantive fields in that paragraph."
        )

    return failures


# ---------------------------------------------------------------------------
# GATE 3 — Reproducibility claim is accurate (no "exact, known set of clauses")
# ---------------------------------------------------------------------------

OVERSTATED_CLAIM = re.compile(
    r"reproducible against an exact,?\s*known set of clauses",
    re.IGNORECASE,
)

CORRECTED_CLAIM = re.compile(
    r"candidate pool reproducible.*retrieved set recorded"
    r"|retrieved set recorded.*candidate pool reproducible",
    re.IGNORECASE | re.DOTALL,
)


def gate_3_reproducibility_claim() -> list[str]:
    """Frozen-manifest bullet must not overstate the reproducibility guarantee."""
    failures: list[str] = []
    arch = read_text(ARCHITECTURE_PATH)

    if OVERSTATED_CLAIM.search(arch):
        failures.append(
            "GATE 3 FAIL: ARCHITECTURE.md still contains the overstated claim "
            "'reproducible against an exact, known set of clauses'.\n"
            "  The snapshot manifest gives the candidate pool, but top-K retrieval "
            "over an approximate vector index is not reproducible from the manifest "
            "alone.  Soften to: 'candidate pool reproducible; retrieved set recorded' "
            "(or equivalent wording that separates the two guarantees)."
        )

    if not CORRECTED_CLAIM.search(arch):
        failures.append(
            "GATE 3 FAIL: ARCHITECTURE.md does not contain the corrected "
            "reproducibility language ('candidate pool reproducible; retrieved set "
            "recorded' or equivalent).\n"
            "  Update the 'Frozen content-addressed manifest' bullet to separate "
            "the manifest guarantee (candidate pool) from the new audit guarantee "
            "(retrieved set recorded in audit)."
        )

    return failures


# ---------------------------------------------------------------------------
# GATE 4 — docs/data-handling.md whitelist includes retrieved_clause_ids
# ---------------------------------------------------------------------------

def gate_4_data_handling_whitelist() -> list[str]:
    """data-handling.md canonical whitelist must include retrieved_clause_ids."""
    failures: list[str] = []
    dh = read_text(DATA_HANDLING_PATH)

    if not re.search(r"retrieved_clause_ids?|retrievedClauseIds?", dh, re.IGNORECASE):
        failures.append(
            "GATE 4 FAIL: docs/data-handling.md canonical audit-substance whitelist "
            "does not include retrieved_clause_ids.\n"
            "  Add a row for retrieved_clause_ids (opaque identifiers + polarity/"
            "channel) classified as Internal, retained indefinitely (audit)."
        )

    return failures


# ---------------------------------------------------------------------------
# GATE 5 — docs/audit-queries.md has "which clauses informed review X" query
# ---------------------------------------------------------------------------

def gate_5_audit_queries_clause_query() -> list[str]:
    """audit-queries.md must have a query for 'which clauses informed review X'."""
    failures: list[str] = []
    aq = read_text(AUDIT_QUERIES_PATH)

    # Accept any phrasing that mentions clauses AND a review lookup (review_id /
    # review X / informed / retrieved / clause_ids).
    has_clause_query = bool(re.search(
        r"claus.*(?:inform|retriev|review)|(?:inform|retriev|review).*claus",
        aq,
        re.IGNORECASE | re.DOTALL,
    ))

    if not has_clause_query:
        failures.append(
            "GATE 5 FAIL: docs/audit-queries.md does not have a 'which clauses "
            "informed review X' query.\n"
            "  Add a row to the Standard queries table for retrieving the "
            "clause_ids that informed a given review, keyed by review_id."
        )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Audit clause_ids gate (issue #27)")
    print("=" * 60)

    all_failures: list[str] = []
    for gate_fn in [
        gate_1_step22_clause_ids,
        gate_2_audit_posture_clause_ids,
        gate_3_reproducibility_claim,
        gate_4_data_handling_whitelist,
        gate_5_audit_queries_clause_query,
    ]:
        failures = gate_fn()
        all_failures.extend(failures)
        for msg in failures:
            print(msg)

    print("\n" + "=" * 60)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.")
        return 1

    print("\nPASS: all audit clause_ids checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
