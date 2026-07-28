#!/usr/bin/env python3
"""Generate the synthetic OPF 0.3 fixture playbook(s) — FICTIONAL parties only.

Reproducible, self-contained generator (no playbook-engine import) for the
gold OPF 0.3 fixtures used across the OPF-0.3 launch tests. All parties are
invented ("Acme University", "FixtureCorp College", "Example Institute"); there
is NO real corpus content here. Run:

    python3 tests/gold-fixtures-opf/generate_opf_fixture.py

Emits, next to this script:
    acme-university.opf.json / .opf.html               (full fixture, 4 clauses)
    acme-university-empty-floor.opf.json / .opf.html   (floor.invariants == [])
    acme-university-real-shape.opf.json / .opf.html    (posture == {}, floor == {}
                                                        -- the REAL playbook's shape)

The digest section is computed by a faithful port of playbook-engine
``playbook_engine/digest.py`` so the fixture's digest reflects its evidence
exactly like a real compile. ``identity.content_hash`` + ``section_digests``
are computed with scripts/opf_canonicalize.py, so the fixture verifies on
ingest. Both artifacts are checked in; the tests validate them against the
vendored 0.3 schema and re-derive/compare the hash, so an accidental hand-edit
is caught.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_canonicalize  # noqa: E402
import opf_html  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "playbooks" / "opf" / "playbook.schema-0.3.json"


# --------------------------------------------------------------------------
# Digest builder — faithful port of playbook_engine/digest.py, pinned at engine
# commit 1cc0237 ("cap preferred_variations and enforce the 40K-token budget by
# construction"), DIGEST_VERSION 2.
#
# Ported rather than imported: the sibling repo is not present in CI, and this
# generator must produce the fixture reproducibly anywhere. Keep in sync when
# re-vendoring the schema — tests/test_opf_schema_sync.py's shape contract
# guards the format, and the fixture's own schema validation catches a port
# that has fallen behind.
# --------------------------------------------------------------------------
DIGEST_VERSION = "2"
EXEMPLAR_TOP_N = 5
DIGEST_TOKEN_BUDGET = 40_000
_MIN_TOP_N = 3
_BAND_OFTEN_MIN = 10
_BAND_SOMETIMES_MIN = 2


def _normalize_text(text: str) -> str:
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _band(n: int) -> str:
    if n >= _BAND_OFTEN_MIN:
        return "often"
    if n >= _BAND_SOMETIMES_MIN:
        return "sometimes"
    return "rare"


def _is_material(obs: dict) -> bool:
    risk = obs.get("risk_delta") or {}
    return isinstance(risk, dict) and risk.get("magnitude") == "material"


def _dedupe_rank(observations: list, *, include_deviation: bool, top_n: int = EXEMPLAR_TOP_N) -> list:
    """Dedupe observations by normalized text and rank by frequency.

    The digest's one size discipline, applied uniformly to exemplar forms,
    concessions, and unacceptable variations (engine commit 3993b4f: "dedupe/cap
    concessions and unacceptable like exemplar forms"). Group by normalized
    full_text (falling back to text_summary); n sums precedent_count (default 1);
    keep the top EXEMPLAR_TOP_N groups by n plus every group carrying material
    risk. Entries carry text_summary ONLY -- never full_text; example_ref is the
    drill-down path.
    """
    groups: dict = {}
    order: list = []
    for obs in observations:
        key = _normalize_text(str(obs.get("full_text") or obs.get("text_summary") or ""))
        if not key:
            continue
        if key not in groups:
            groups[key] = {"n": 0, "rep": obs, "material": False}
            order.append(key)
        g = groups[key]
        g["n"] += int(obs.get("precedent_count") or 1)
        if _is_material(obs):
            g["material"] = True
            g["rep"] = obs
    first_seen = {k: i for i, k in enumerate(order)}
    ranked = sorted(order, key=lambda k: (-groups[k]["n"], first_seen[k]))
    keep = set(ranked[:top_n]) | {k for k in ranked if groups[k]["material"]}
    forms: list = []
    for key in ranked:
        if key not in keep:
            continue
        g = groups[key]
        rep = g["rep"]
        form: dict = {"text_summary": rep.get("text_summary", ""), "n": g["n"], "band": _band(g["n"])}
        if include_deviation and rep.get("deviation") is not None:
            form["deviation"] = rep["deviation"]
        if rep.get("risk_delta") is not None:
            form["risk_delta"] = rep["risk_delta"]
        if rep.get("example_ref") is not None:
            form["example_ref"] = rep["example_ref"]
        forms.append(form)
    return forms


def _exemplar_forms(observed_positions: list, top_n: int = EXEMPLAR_TOP_N) -> list:
    return _dedupe_rank(observed_positions, include_deviation=True, top_n=top_n)


def _preferred_variations(clause: dict, top_n: int) -> list:
    """Project acceptable_if entries to the digest: dedupe, rank, cap.

    Same discipline as the other three lists. Grouping key: normalized if+to
    (or the whole entry for legacy bare strings). Rank weight n: the
    precedent_count of the underlying observation, resolved by matching
    observation_ref against the clause's own observed_positions (1 when
    unresolvable). Surviving dict entries ship if/to VERBATIM plus
    observation_ref, n and band -- the compiler-generated `rationale` narration
    stays in the full OPF (reachable via the clause-evidence lookup tool).
    """
    entries = (clause.get("summary") or {}).get("acceptable_if") or []
    if not entries:
        return []

    obs_by_ref: dict = {}
    for pos in clause.get("observed_positions") or []:
        pos_ref = pos.get("example_ref") or {}
        obs_by_ref[(pos_ref.get("document_id"), pos_ref.get("version"), pos_ref.get("clause_path"))] = pos

    groups: dict = {}
    order: list = []
    for entry in entries:
        obs: dict = {}
        if isinstance(entry, str):
            key = _normalize_text(entry)
        else:
            key = _normalize_text(f"{entry.get('if', '')} {entry.get('to', '')}")
            ref = entry.get("observation_ref") or {}
            obs = obs_by_ref.get((ref.get("document_id"), ref.get("version"), ref.get("clause_path")), {})
        if not key:
            continue
        n = int(obs.get("precedent_count") or 1)
        if key not in groups:
            groups[key] = {"n": 0, "rep": entry, "rep_n": -1, "material": False}
            order.append(key)
        g = groups[key]
        g["n"] += n
        if _is_material(obs):
            g["material"] = True
        if n > g["rep_n"]:
            g["rep"], g["rep_n"] = entry, n

    first_seen = {k: i for i, k in enumerate(order)}
    ranked = sorted(order, key=lambda k: (-groups[k]["n"], first_seen[k]))
    keep = set(ranked[:top_n]) | {k for k in ranked if groups[k]["material"]}

    out: list = []
    for key in ranked:
        if key not in keep:
            continue
        g = groups[key]
        rep = g["rep"]
        if isinstance(rep, str):
            out.append(rep)
            continue
        projected: dict = {"if": rep.get("if"), "to": rep.get("to")}
        if rep.get("observation_ref") is not None:
            projected["observation_ref"] = rep["observation_ref"]
        projected["n"] = g["n"]
        projected["band"] = _band(g["n"])
        out.append(projected)
    return out


def digest_token_estimate(digest: dict) -> int:
    """Rough token estimate — canonical chars / 4, the repo-wide rule of thumb."""
    return len(opf_canonicalize.canonicalize(digest)) // 4


def _build_digest_at(playbook: dict, top_n: int) -> dict:
    """Build the digest with a fixed per-list cap of *top_n*."""
    digest_clauses: list = []
    for clause in playbook["evidence"]["clauses"]:
        summary = clause.get("summary") or {}
        our_standard = clause.get("our_standard")
        entry = {
            "id": clause.get("id"),
            "taxonomy_id": clause.get("taxonomy_id"),
            "title": clause.get("title"),
            "historical_stance": summary.get("historical_stance"),
            "stance_detail": summary.get("stance_detail"),
            "our_standard": our_standard if isinstance(our_standard, dict) else None,
            "preferred_variations": _preferred_variations(clause, top_n),
            "concessions": _dedupe_rank(summary.get("fallbacks") or [], include_deviation=False, top_n=top_n),
            "unacceptable": _dedupe_rank(summary.get("rejected") or [], include_deviation=False, top_n=top_n),
            "exemplar_forms": _exemplar_forms(clause.get("observed_positions") or [], top_n),
        }
        digest_clauses.append(entry)
    return {
        "digest_version": DIGEST_VERSION,
        "clause_count": len(digest_clauses),
        "clauses": digest_clauses,
    }


def build_digest(playbook: dict, *, token_budget: int | None = DIGEST_TOKEN_BUDGET) -> dict:
    """Build the digest, enforcing *token_budget* by construction.

    Starts at the loosest per-list cap (EXEMPLAR_TOP_N) and tightens stepwise
    down to _MIN_TOP_N until the digest fits. Material-risk entries are never
    dropped, so an extreme corpus can still exceed the budget at the tightest
    cap -- the engine's CLI warns in that case rather than truncating.
    """
    digest = _build_digest_at(playbook, EXEMPLAR_TOP_N)
    if token_budget is None:
        return digest
    for top_n in range(EXEMPLAR_TOP_N - 1, _MIN_TOP_N - 1, -1):
        if digest_token_estimate(digest) <= token_budget:
            break
        digest = _build_digest_at(playbook, top_n)
    return digest


# --------------------------------------------------------------------------
# Fixture body — fictional Educational Affiliation Agreement
# --------------------------------------------------------------------------
def _cite(document_id: str, version, clause_path: str, char_span=None) -> dict:
    c = {"document_id": document_id, "version": version, "clause_path": clause_path}
    if char_span is not None:
        c["char_span"] = char_span
    return c


def _clauses() -> list:
    return [
        {
            "id": "clause.indemnification",
            "taxonomy_id": "indemnification",
            "title": "Indemnification",
            "our_standard": {
                "text": (
                    "Each party shall indemnify the other against third-party claims "
                    "arising from its own negligence or willful misconduct."
                ),
                "source_ref": _cite("template", "template", "8", [0, 110]),
            },
            "observed_positions": [
                {
                    "text_summary": "Mutual indemnification limited to each party's own negligence.",
                    "full_text": (
                        "Each party shall indemnify, defend, and hold harmless the other "
                        "party from third-party claims to the extent caused by the "
                        "indemnifying party's own negligence or willful misconduct."
                    ),
                    "example_ref": _cite("acme-university", 3, "8.1", [0, 160]),
                    "deviation": "reworded_equivalent",
                    "risk_delta": {"direction": "neutral", "magnitude": "none"},
                    "provenance": "our_paper",
                    "outcome": "signed",
                    "precedent_count": 6,
                },
                {
                    "text_summary": "Mutual indemnification, negligence-based, with a notice-and-cooperation proviso.",
                    "full_text": (
                        "Each party shall indemnify the other for third-party claims arising "
                        "from its negligence, provided the indemnified party gives prompt "
                        "notice and reasonable cooperation."
                    ),
                    "example_ref": _cite("fixture-college", 2, "8.1", [0, 150]),
                    "deviation": "reworded_equivalent",
                    "risk_delta": {"direction": "better", "magnitude": "minor"},
                    "provenance": "our_paper",
                    "outcome": "signed",
                    "precedent_count": 3,
                },
                {
                    "text_summary": "One-way indemnity running to the counterparty for IP infringement claims.",
                    "full_text": (
                        "FixtureCorp shall indemnify Acme University against any claim that "
                        "the placement materials infringe a third party's intellectual "
                        "property rights."
                    ),
                    "example_ref": _cite("example-institute", 4, "8.2", [0, 140]),
                    "deviation": "substantive",
                    "risk_delta": {"direction": "worse", "magnitude": "material"},
                    "provenance": "counterparty_paper",
                    "outcome": "signed",
                    "precedent_count": 1,
                },
            ],
            "summary": {
                "historical_stance": "usually_held",
                "stance_detail": {"held": 8, "of": 10, "basis": "our_paper"},
                "acceptable_if": [
                    {
                        "if": "mutual, negligence-limited indemnification",
                        "to": (
                            "Mutual indemnification limited to each party's own negligence, "
                            "worded to match the counterparty's standard drafting."
                        ),
                        "rationale": (
                            "Signed at neutral risk in 6 of 8 held opportunities; "
                            "reworded_equivalent only, no risk movement."
                        ),
                        "observation_ref": _cite("acme-university", 3, "8.1", [0, 160]),
                    }
                ],
                "fallbacks": [
                    {
                        "text_summary": "One-way indemnity running to the counterparty for IP infringement claims.",
                        "full_text": (
                            "FixtureCorp shall indemnify Acme University against any claim that "
                            "the placement materials infringe a third party's intellectual "
                            "property rights."
                        ),
                        "example_ref": _cite("example-institute", 4, "8.2", [0, 140]),
                        "deviation": "substantive",
                        "risk_delta": {"direction": "worse", "magnitude": "material"},
                        "provenance": "counterparty_paper",
                        "outcome": "signed",
                        "precedent_count": 1,
                    }
                ],
                "rejected": [
                    {
                        "text_summary": (
                            "Uncapped, one-way indemnity covering all claims including the "
                            "counterparty's own negligence."
                        ),
                        "full_text": (
                            "FixtureCorp shall indemnify Acme University against any and all "
                            "claims of any kind, including those arising from Acme "
                            "University's own negligence, without limitation."
                        ),
                        "example_ref": _cite("acme-university", 1, "8.1", [0, 150]),
                        "deviation": "substantive",
                        "risk_delta": {"direction": "worse", "magnitude": "material"},
                        "provenance": "counterparty_paper",
                        "outcome": "proposed_then_reversed",
                        "precedent_count": 2,
                    }
                ],
                "confidence": {
                    "score": 0.72,
                    "basis": "precedent_count + provenance_mix",
                    "n_our_paper": 8,
                    "n_counterparty_paper": 2,
                    "evidence_sufficient": True,
                },
            },
        },
        {
            "id": "clause.governing-law",
            "taxonomy_id": "governing_law",
            "title": "Governing Law",
            "our_standard": {
                "text": "This Agreement is governed by the laws of the State of Fixtureland.",
                "source_ref": _cite("template", "template", "14", [0, 66]),
            },
            "observed_positions": [
                {
                    "text_summary": "Counterparty's home-state law, no forum-selection clause.",
                    "full_text": "This Agreement shall be governed by the laws of the State of Acmeland.",
                    "example_ref": _cite("acme-university", 3, "14", [0, 70]),
                    "deviation": "substantive",
                    "risk_delta": {"direction": "neutral", "magnitude": "minor"},
                    "provenance": "counterparty_paper",
                    "outcome": "signed",
                    "precedent_count": 4,
                },
                {
                    "text_summary": "Our home-state law retained.",
                    "full_text": "This Agreement shall be governed by the laws of the State of Fixtureland.",
                    "example_ref": _cite("fixture-college", 2, "14", [0, 72]),
                    "deviation": "none",
                    "risk_delta": {"direction": "neutral", "magnitude": "none"},
                    "provenance": "our_paper",
                    "outcome": "signed",
                    "precedent_count": 3,
                },
            ],
            "summary": {
                "historical_stance": "mixed",
                "stance_detail": {"held": 3, "of": 7, "basis": "all"},
                "acceptable_if": [
                    {
                        "if": "counterparty insists on its home-state law with no exclusive forum clause",
                        "to": "Counterparty's home-state governing law, provided no exclusive forum-selection clause is added.",
                        "rationale": "Conceded in 4 of 7 opportunities at neutral/minor risk; governing law alone rarely moves substantive exposure.",
                        "observation_ref": _cite("acme-university", 3, "14", [0, 70]),
                    }
                ],
                "fallbacks": [],
                "rejected": [],
                "confidence": {
                    "score": 0.55,
                    "basis": "precedent_count + provenance_mix",
                    "n_our_paper": 3,
                    "n_counterparty_paper": 4,
                    "evidence_sufficient": True,
                },
            },
        },
        {
            "id": "clause.term-termination",
            "taxonomy_id": "term_termination",
            "title": "Term and Termination",
            "our_standard": {
                "text": "Either party may terminate for convenience on thirty (30) days' written notice.",
                "source_ref": _cite("template", "template", "11", [0, 78]),
            },
            "observed_positions": [
                {
                    "text_summary": "Mutual termination for convenience on 30 days' notice.",
                    "full_text": "Either party may terminate this Agreement for convenience upon thirty (30) days' prior written notice.",
                    "example_ref": _cite("acme-university", 3, "11.1", [0, 100]),
                    "deviation": "reworded_equivalent",
                    "risk_delta": {"direction": "neutral", "magnitude": "none"},
                    "provenance": "our_paper",
                    "outcome": "signed",
                    "precedent_count": 5,
                },
                {
                    "text_summary": "Termination for convenience extended to 60 days' notice.",
                    "full_text": "Either party may terminate for convenience upon sixty (60) days' prior written notice.",
                    "example_ref": _cite("example-institute", 4, "11.1", [0, 90]),
                    "deviation": "substantive",
                    "risk_delta": {"direction": "worse", "magnitude": "minor"},
                    "provenance": "counterparty_paper",
                    "outcome": "signed",
                    "precedent_count": 2,
                },
            ],
            "summary": {
                "historical_stance": "usually_held",
                "stance_detail": {"held": 5, "of": 7, "basis": "our_paper"},
                "acceptable_if": [
                    {
                        "if": "counterparty asks for a longer convenience-termination notice window",
                        "to": "Termination for convenience on up to sixty (60) days' written notice.",
                        "rationale": "Signed twice at 60 days with only minor risk movement; the notice window is negotiable within reason.",
                        "observation_ref": _cite("example-institute", 4, "11.1", [0, 90]),
                    }
                ],
                "fallbacks": [
                    {
                        "text_summary": "Termination for convenience extended to 60 days' notice.",
                        "full_text": "Either party may terminate for convenience upon sixty (60) days' prior written notice.",
                        "example_ref": _cite("example-institute", 4, "11.1", [0, 90]),
                        "deviation": "substantive",
                        "risk_delta": {"direction": "worse", "magnitude": "minor"},
                        "provenance": "counterparty_paper",
                        "outcome": "signed",
                        "precedent_count": 2,
                    }
                ],
                "rejected": [],
                "confidence": {
                    "score": 0.68,
                    "basis": "precedent_count + provenance_mix",
                    "n_our_paper": 5,
                    "n_counterparty_paper": 2,
                    "evidence_sufficient": True,
                },
            },
        },
        {
            "id": "clause.confidentiality",
            "taxonomy_id": "confidentiality",
            "title": "Confidentiality",
            "our_standard": {
                "text": "Confidential Information shall be protected for three (3) years after disclosure.",
                "source_ref": _cite("template", "template", "9", [0, 80]),
            },
            "observed_positions": [
                {
                    "text_summary": "Mutual confidentiality, 3-year survival.",
                    "full_text": "Each party shall protect the other's Confidential Information for three (3) years following disclosure.",
                    "example_ref": _cite("acme-university", 3, "9.1", [0, 100]),
                    "deviation": "none",
                    "risk_delta": {"direction": "neutral", "magnitude": "none"},
                    "provenance": "our_paper",
                    "outcome": "signed",
                    "precedent_count": 7,
                },
                {
                    "text_summary": "Perpetual confidentiality for student records.",
                    "full_text": "Confidential Information consisting of student education records shall be protected in perpetuity.",
                    "example_ref": _cite("fixture-college", 2, "9.2", [0, 95]),
                    "deviation": "substantive",
                    "risk_delta": {"direction": "better", "magnitude": "minor"},
                    "provenance": "counterparty_paper",
                    "outcome": "signed",
                    "precedent_count": 3,
                },
            ],
            "summary": {
                "historical_stance": "consistently_held",
                "stance_detail": {"held": 9, "of": 9, "basis": "all"},
                "acceptable_if": [
                    {
                        "if": "counterparty asks for perpetual protection of student education records specifically",
                        "to": "Perpetual confidentiality limited to student education records; three years for all other Confidential Information.",
                        "rationale": "Signed repeatedly; a records-only carve-out reduces our risk and aligns with education-privacy norms.",
                        "observation_ref": _cite("fixture-college", 2, "9.2", [0, 95]),
                    }
                ],
                "fallbacks": [],
                "rejected": [],
                "confidence": {
                    "score": 0.9,
                    "basis": "precedent_count + provenance_mix",
                    "n_our_paper": 7,
                    "n_counterparty_paper": 3,
                    "evidence_sufficient": True,
                },
            },
        },
    ]


def build_body(empty_floor: bool = False, *, empty_posture: bool = False, absent_floor: bool = False) -> dict:
    """The fixture body.

    Three shapes, and the distinction between the last two matters:

      - default: a Posture prose block and one Floor invariant.
      - ``empty_floor``: ``floor.invariants == []`` -- the section exists and
        declares no invariants.
      - ``absent_floor`` + ``empty_posture``: ``floor == {}`` and
        ``posture == {}``, which is THE REAL PLAYBOOK'S SHAPE (both the private
        one and the public twin at contract-opf.github.io ship it), and is
        schema-valid: the 0.3 schema requires the `posture` and `floor` keys but
        requires nothing INSIDE them. A fixture that only ever carries prose and
        invariants tests a shape production does not have.
    """
    invariants = (
        []
        if empty_floor
        else [
            {
                "id": "no-uncapped-liability",
                "statement": "Never accept uncapped liability or indemnity covering the counterparty's own negligence.",
                "rationale": "Uncapped exposure is categorically unacceptable regardless of deal value.",
            }
        ]
    )
    posture: dict = (
        {}
        if empty_posture
        else {
            "system_prompt": (
                "This is a generally low-risk agreement type; default toward ACCEPT. "
                "Hold firm on indemnification and confidentiality; governing law is negotiable."
            ),
            "generation": {
                "generated_by": "playbook-engine (synthetic fixture)",
                "generated_at": "2026-01-01T00:00:00Z",
                "interview": [
                    {"q": "rounds", "question": "How many rounds do you typically go?", "answer": "Usually 2."}
                ],
                "grounded_in": "evidence@fixture",
            },
        }
    )
    return {
        "opf_version": "0.3",
        "agreement_type": {
            "id": "educational-affiliation",
            "name": "Educational Affiliation Agreement",
            "aliases": ["acme-university", "eiaa-fixture"],
        },
        "baseline": {
            "has_canonical_template": True,
            "template_ref": {
                "document_id": "template",
                "title": "FixtureCorp Affiliation Template",
                "source": "template/affiliation-template.docx",
            },
        },
        "taxonomy": {
            "source": "custom",
            "entries": [
                {"id": "indemnification", "label": "Indemnification", "status": "active",
                 "cuad_origin": "Indemnification", "description": "Who bears third-party claim risk."},
                {"id": "governing_law", "label": "Governing Law", "status": "active",
                 "cuad_origin": "Governing Law", "description": "Which jurisdiction's law applies."},
                {"id": "term_termination", "label": "Term and Termination", "status": "active",
                 "cuad_origin": "Termination For Convenience", "description": "How and when the agreement ends."},
                {"id": "confidentiality", "label": "Confidentiality", "status": "active",
                 "cuad_origin": "Confidentiality", "description": "Protection of exchanged information."},
            ],
        },
        "perspective": {"party": "FixtureCorp", "counterparty_type": "Educational Institution"},
        "de_minimis": ["typo fixes", "renumbering with no substantive change"],
        "evidence": {"clauses": _clauses(), "clause_library": []},
        "posture": posture,
        "floor": {} if absent_floor else {"invariants": invariants},
        "corpus": {
            "documents": [
                {"document_id": "acme-university", "title": "Acme University Affiliation Agreement",
                 "provenance": "our_paper", "in_scope": True,
                 "scope_rationale": "Educational affiliation agreement matching target type.",
                 "scope_confidence": 0.98, "versions": 3, "signed_version": 3,
                 "version_order_basis": "edit_distance_chain+signed_anchor"},
                {"document_id": "fixture-college", "title": "FixtureCorp College Placement Agreement",
                 "provenance": "our_paper", "in_scope": True,
                 "scope_rationale": "Educational affiliation agreement matching target type.",
                 "scope_confidence": 0.95, "versions": 2, "signed_version": 2,
                 "version_order_basis": "edit_distance_chain+signed_anchor"},
                {"document_id": "example-institute", "title": "Example Institute Clinical Placement Agreement",
                 "provenance": "counterparty_paper", "in_scope": True,
                 "scope_rationale": "Counterparty-paper affiliation agreement in scope.",
                 "scope_confidence": 0.9, "versions": 4, "signed_version": 4,
                 "version_order_basis": "edit_distance_chain+signed_anchor"},
            ],
            "stats": {"documents_total": 3, "documents_in_scope": 3, "versions_total": 9},
        },
        "compiler": {
            "name": "playbook-engine",
            "version": "0.3.0",
            "run_id": "fixture-run-001",
            "generated_at": "2026-01-01T00:00:00Z",
            "stub_basis_present": False,
        },
    }


def finalize(body: dict) -> dict:
    """Attach the digest section and the identity block (hash + section digests)."""
    doc = copy.deepcopy(body)
    doc["digest"] = build_digest(doc)
    doc["identity"] = {
        "id": doc["agreement_type"]["id"],
        "version": "1.0.0",
        "supersedes": None,
        "content_hash": opf_canonicalize.content_hash(doc),
        "section_digests": opf_canonicalize.compute_section_digests(doc),
    }
    return doc


def _validate(doc: dict) -> None:
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=doc, schema=schema)
    # Sanity: the declared hash must verify.
    assert opf_canonicalize.verify_content_hash(doc), "content_hash does not verify"


def _write(doc: dict, stem: str) -> None:
    json_path = HERE / f"{stem}.opf.json"
    html_path = HERE / f"{stem}.opf.html"
    json_text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    json_path.write_text(json_text, encoding="utf-8")
    html = opf_html.wrap_opf_html(
        json_text, digest=doc.get("digest"), title=f"OPF playbook — {stem}"
    )
    html_path.write_text(html, encoding="utf-8")
    print(f"wrote {json_path.relative_to(REPO_ROOT)} ({len(json_text)} bytes)")
    print(f"wrote {html_path.relative_to(REPO_ROOT)} ({len(html)} bytes)")
    print(f"  content_hash = {doc['identity']['content_hash']}")


def main() -> int:
    full = finalize(build_body(empty_floor=False))
    _validate(full)
    _write(full, "acme-university")

    empty = finalize(build_body(empty_floor=True))
    _validate(empty)
    _write(empty, "acme-university-empty-floor")

    # The real playbook's shape: posture == {} and floor == {}. See build_body.
    real_shape = finalize(build_body(empty_posture=True, absent_floor=True))
    _validate(real_shape)
    assert real_shape["posture"] == {}, "real-shape fixture must ship posture == {}"
    assert real_shape["floor"] == {}, "real-shape fixture must ship floor == {}"
    assert "posture" in real_shape["identity"]["section_digests"], (
        "section_digests must still carry a posture digest for an empty posture -- "
        "the overrides.posture bind path keys off it"
    )
    _write(real_shape, "acme-university-real-shape")

    # Round-trip the HTML envelope to prove extract == embed.
    html = (HERE / "acme-university.opf.html").read_text(encoding="utf-8")
    extracted = opf_html.extract_opf_from_html(html)
    assert extracted == full, "HTML round-trip mismatch"
    print("OK: schema-valid, hash verifies, HTML round-trips.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
