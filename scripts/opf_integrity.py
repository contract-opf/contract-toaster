#!/usr/bin/env python3
"""
Derived-section integrity for an uploaded OPF 0.6 playbook (QA 2026-11-03 #24).

`identity.content_hash` proves the bytes are the bytes that were hashed. It
does not prove the digest, the hard-rule manifest or the section digests were
derived from the document's evidence and Floor: anyone can edit a count and
re-stamp the hash with the algorithm this repo vendors. OPF-SPEC §3.12 says the
digest, manifest, dossiers and provenance index are each a pure function of the
rest of the document and MUST equal their recomputation.

THE SCOPE OF THIS CHECK. This repo does not carry the reference compiler's full
`build_digest_v5` (capping to a token budget, equivalence collapse, part-map
derivation: thousands of lines and a second source of truth that would drift
from the 15 vendored conformance vectors). It carries the part of the contract
that is a plain function of the document and cannot be argued with:

  - `identity.section_digests` equal the recomputation (the content hash
    excludes `identity`, so nothing else covers them);
  - `digest.perspective` equals the top-level `perspective`;
  - every digest clause names an evidence clause, and its title and its
    uncapped totals (`n_deals`, `n_signed_standard`, `n_opened_standard`,
    `n_kept_standard`, `n_variants_total`, `n_refused_total`,
    `n_changed_openings_total`) equal that clause's evidence counts;
  - `n_signed_without_verified` / `n_signed_without_possible` equal the
    distinct deals of `evidence.absence` for the clause, and every
    `verified_absent_deals[].absence_id` resolves to a verified record;
  - every precedent id the digest and the dossiers cite resolves into
    `evidence.precedent[]` (no dangling reference);
  - `manifest.hard_rules` equals its recomputation from the Floor and the
    clause evidence (§3.12.3), rule by rule;
  - a retired Floor line (`floor.x_retired`) is not also an active invariant;
  - a signed invariant whose `x_signed_hash` does not match its signed fields
    is refused (§3.7: it MUST NOT be presented as signed).

What it does NOT do is re-derive the capped variant lists, the labels or the
part maps; a forged text inside a variant is still caught only by the hash and
the engine-side validator. The error messages carry JSON pointers only, never
a value from the document.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_canonicalize  # noqa: E402

#: The invariant fields a sign-off covers (OPF-SPEC §3.7); the hash is excluded.
SIGNED_FIELDS = (
    "id",
    "statement",
    "rationale",
    "x_taxonomy_id",
    "x_direction",
    "x_required_presence",
    "x_condition",
    "x_permissible_proof",
    "x_signed_by",
    "x_signed_at",
)

#: `x_direction` values (§3.7): keep this clause, or never accept this term.
DIRECTIONS = ("hold", "refuse")

#: digest clause field -> evidence clause field (§3.12.2: the totals are uncapped).
_COUNT_FIELDS = (
    ("n_deals", "n_deals"),
    ("n_signed_standard", "n_signed_standard"),
    ("n_opened_standard", "n_opened_standard"),
    ("n_kept_standard", "n_kept_standard"),
    ("n_variants_total", "n_variants"),
    ("n_refused_total", "n_refused"),
    ("n_changed_openings_total", "n_changed_openings"),
)


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def signed_fields_hash(invariant: dict[str, Any]) -> str:
    """`sha256:` over the canonical form of the signed fields present."""
    return opf_canonicalize.sha256_hex(
        opf_canonicalize.canonicalize({k: invariant[k] for k in SIGNED_FIELDS if k in invariant})
    )


def signed_binding_status(invariant: Any) -> str | None:
    """`None` when the invariant carries no sign-off; `"bound"` when its
    `x_signed_hash` matches the signed fields; `"unbound"` for a sign-off made
    before the hash existed (reported, not refused); `"altered"` when the
    hash does not match (the text changed after it was signed)."""
    if not isinstance(invariant, dict):
        return None
    signer = invariant.get("x_signed_by")
    if not (isinstance(signer, str) and signer.strip()):
        return None
    recorded = invariant.get("x_signed_hash")
    if recorded is None:
        return "unbound"
    return "bound" if recorded == signed_fields_hash(invariant) else "altered"


def retired_ids(floor: Any) -> set[str]:
    """Ids recorded under `floor.x_retired`: lines that bind nothing."""
    records = floor.get("x_retired") if isinstance(floor, dict) else None
    return {r["id"] for r in _dicts(records) if isinstance(r.get("id"), str)}


def active_invariants(floor: Any) -> list[dict[str, Any]]:
    """The Floor's invariants that are not also recorded as retired."""
    invariants = floor.get("invariants") if isinstance(floor, dict) else None
    gone = retired_ids(floor)
    return [inv for inv in _dicts(invariants) if inv.get("id") not in gone]


def direction_of(invariant: Any) -> str | None:
    """`"hold"` / `"refuse"` when the invariant states its direction."""
    value = invariant.get("x_direction") if isinstance(invariant, dict) else None
    return value if value in DIRECTIONS else None


def _expected_hard_rules(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """`manifest.hard_rules` as §3.12.3 derives it: one per Floor invariant, in
    Floor order, from the signed Floor only."""
    clauses = _dicts((doc.get("evidence") or {}).get("clauses"))
    by_taxonomy: dict[str, dict[str, Any]] = {}
    for clause in clauses:
        taxonomy_id = clause.get("taxonomy_id")
        if isinstance(taxonomy_id, str) and taxonomy_id not in by_taxonomy:
            by_taxonomy[taxonomy_id] = clause
    rules: list[dict[str, Any]] = []
    for invariant in _dicts((doc.get("floor") or {}).get("invariants")):
        named = invariant.get("x_taxonomy_id")
        taxonomy_id = named if isinstance(named, str) else None
        match = by_taxonomy.get(taxonomy_id) if taxonomy_id else None
        clause = match if match is not None else {}
        standard = clause.get("our_standard")
        fallback = standard.get("text") if isinstance(standard, dict) else None
        condition = invariant.get("x_condition")
        proof = invariant.get("x_permissible_proof")
        rules.append(
            {
                "rule_id": invariant.get("id"),
                "clause_id": clause.get("id") if match is not None else None,
                "taxonomy_id": taxonomy_id,
                "statement": invariant.get("statement"),
                "required_presence": invariant.get("x_required_presence") is True,
                "condition": condition if condition is not None else "judged",
                "permissible_proof": proof if isinstance(proof, list) else [],
                "fallback_language": fallback if isinstance(fallback, str) else None,
            }
        )
    return rules


def _walk_precedent_refs(node: Any, path: str, out: list[tuple[str, str]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            if key in ("precedent_ids", "omitted_precedent_ids") and isinstance(value, list):
                out.extend((f"{here}[{i}]", item) for i, item in enumerate(value) if isinstance(item, str))
            elif key == "precedent_id" and isinstance(value, str):
                out.append((here, value))
            else:
                _walk_precedent_refs(value, here, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk_precedent_refs(value, f"{path}[{index}]", out)


def derivation_errors(doc: dict[str, Any]) -> list[str]:
    """Every way the document's derived sections fail to follow from its
    evidence and Floor, as pointer-only messages. Empty list: consistent."""
    errors: list[str] = []
    identity = doc.get("identity")
    declared = identity.get("section_digests") if isinstance(identity, dict) else None
    if isinstance(declared, dict):
        recomputed = opf_canonicalize.compute_section_digests(doc)
        for name, value in recomputed.items():
            if declared.get(name) != value:
                errors.append(f"/identity/section_digests/{name}: differs from the digest recomputed over the section")

    evidence: dict[str, Any] = doc["evidence"] if isinstance(doc.get("evidence"), dict) else {}
    clauses = _dicts(evidence.get("clauses"))
    clause_by_id = {c["id"]: c for c in clauses if isinstance(c.get("id"), str)}
    precedent_ids = {p["id"] for p in _dicts(evidence.get("precedent")) if isinstance(p.get("id"), str)}

    digest = doc.get("digest")
    if isinstance(digest, dict):
        if digest.get("perspective") != doc.get("perspective"):
            errors.append("/digest/perspective: differs from the top-level perspective")
        absence: dict[str, Any] = evidence["absence"] if isinstance(evidence.get("absence"), dict) else {}
        records = _dicts(absence.get("records"))
        record_status = {r["id"]: r.get("status") for r in records if isinstance(r.get("id"), str)}
        for index, entry in enumerate(_dicts(digest.get("clauses"))):
            where = f"/digest/clauses/{index}"
            clause = clause_by_id.get(entry.get("id"))
            if clause is None:
                errors.append(f"{where}/id: names no evidence clause")
                continue
            for field in ("taxonomy_id", "title"):
                if entry.get(field) != clause.get(field):
                    errors.append(f"{where}/{field}: differs from the evidence clause")
            for digest_field, evidence_field in _COUNT_FIELDS:
                if entry.get(digest_field) != clause.get(evidence_field):
                    errors.append(f"{where}/{digest_field}: differs from the evidence count {evidence_field}")
            taxonomy_id = clause.get("taxonomy_id")
            mine = [r for r in records if r.get("taxonomy_id") == taxonomy_id]
            for field, status in (
                ("n_signed_without_verified", "absent_verified"),
                ("n_signed_without_possible", "possibly_present"),
            ):
                distinct = len({r.get("document_id") for r in mine if r.get("status") == status})
                if entry.get(field) != distinct:
                    errors.append(f"{where}/{field}: differs from the distinct deals of evidence.absence")
            for position, deal in enumerate(_dicts(entry.get("verified_absent_deals"))):
                if record_status.get(deal.get("absence_id")) != "absent_verified":
                    errors.append(
                        f"{where}/verified_absent_deals/{position}/absence_id: "
                        "resolves to no verified absence record"
                    )

    refs: list[tuple[str, str]] = []
    for section in ("digest", "dossiers"):
        if section in doc:
            _walk_precedent_refs(doc[section], f"/{section}", refs)
    errors.extend(f"{path}: dangling precedent id" for path, ref in refs if ref not in precedent_ids)

    floor: dict[str, Any] = doc["floor"] if isinstance(doc.get("floor"), dict) else {}
    invariants = _dicts(floor.get("invariants"))
    active_ids = {inv.get("id") for inv in invariants}
    for index, invariant in enumerate(invariants):
        if invariant.get("id") in retired_ids(floor):
            errors.append(f"/floor/invariants/{index}: is also recorded as retired under /floor/x_retired")
        if signed_binding_status(invariant) == "altered":
            errors.append(
                f"/floor/invariants/{index}/x_signed_hash: the signed fields changed after the sign-off"
            )
        direction = invariant.get("x_direction")
        if direction is not None and direction not in DIRECTIONS:
            errors.append(f"/floor/invariants/{index}/x_direction: must be one of {list(DIRECTIONS)}")

    manifest = doc.get("manifest")
    if isinstance(manifest, dict):
        actual = manifest.get("hard_rules")
        wanted = _expected_hard_rules(doc)
        if not isinstance(actual, list) or len(actual) != len(wanted):
            errors.append("/manifest/hard_rules: does not hold exactly one rule per Floor invariant")
        else:
            for index, (got, want) in enumerate(zip(actual, wanted, strict=True)):
                if not isinstance(got, dict) or any(got.get(k) != v for k, v in want.items()):
                    errors.append(f"/manifest/hard_rules/{index}: differs from its recomputation from the Floor")

    dossiers = doc.get("dossiers")
    if isinstance(dossiers, dict):
        # The dossier key is uploader text: never echoed, only counted.
        for ordinal, (key, dossier) in enumerate(dossiers.items()):
            if key not in clause_by_id:
                errors.append(f"/dossiers (entry {ordinal}): names no evidence clause")
                continue
            for position, rule in enumerate(_dicts((dossier or {}).get("floor_rules"))):
                if rule.get("rule_id") not in active_ids:
                    errors.append(f"/dossiers (entry {ordinal})/floor_rules/{position}: names no Floor invariant")
    return errors
