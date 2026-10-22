#!/usr/bin/env python3
"""Generate the synthetic OPF 0.6 fixture playbooks — FICTIONAL parties only.

The gold OPF fixtures every OPF test in this repo runs against. All parties are
invented ("Acme University", "FixtureCorp College", "Example Institute", ...);
there is NO real corpus content here. Run:

    python3 tests/gold-fixtures-opf/generate_opf_fixture.py

Emits, next to this script:
    acme-university-0.6.opf.json / .opf.html              (full fixture, 4 clauses)
    acme-university-0.6-empty-floor.opf.json / .opf.html  (floor.invariants == [])
    acme-university-0.6-real-shape.opf.json / .opf.html   (posture == {}, floor == {}
                                                           -- the REAL playbook's shape)
and, for the no-digest path:
    tests/fixtures/opf/synthetic-eiaa.opf.json            (OPF 0.6, NO digest section)

## Only the INPUTS are authored here; every derived value is the engine's

OPF 0.6 / digest_version 5 is the one format this repo reads (issues #128,
#177). Its derived values -- precedent ids, the clause counts, our standard's
and our former forms' part ids, the `evidence.absence` records, the whole
`digest`, and the `manifest` / `dossiers` / `provenance_index` sections --
are normative functions of the document that the engine's
validator recomputes and compares. A fixture whose digest was hand-built, or
built by a port of the engine's code, would be a shape the engine refuses to
emit: tests asserting over it would prove nothing about what production can
receive. So this script authors only what a corpus would supply (the deals, the
texts, who opened with what, the equivalence judgments) and STAMPS everything
derived with the engine's own reference functions -- the same ones its
conformance-vector generator uses -- then runs the engine's validator over the
result and refuses to write a document it would reject.

That makes the engine a GENERATION-TIME dependency: the sibling checkout
(`../playbook-engine`, or `$PLAYBOOK_ENGINE_REPO`) must be present to run this
script, and it is imported read-only (`sys.dont_write_bytecode`, so not even a
`__pycache__` lands in it). It is NOT a test-time dependency: the committed
artifacts are what every test reads, and CI has no sibling.
`tests/test_opf_fixture.py` regenerates and compares them, and re-runs the
engine's validator, only when the sibling is present -- the same local-only
convention as `tests/test_opf_schema_sync.py`'s source-parity check.

Pinned engine: main `fe89bc6` (the commit `playbooks/opf/playbook.schema-0.6.json`
was vendored from). A different engine commit may stamp a different digest;
that is a drift signal to understand, not a reason to regenerate blindly.

## What OPF 0.6 adds to the corpus authored here (issue #177)

Every 0.6 fact the toaster renders is exercised by at least two variants, so
no branch is green on one shape alone:

  - signing dates: a full date, a month, a signed deal with no date and an
    unsigned deal, on four different `signed_on_basis` values;
  - how settled a judgment is: `agreed`, `adjudicated`, `unchecked` (never
    checked) and `owner` judgments;
  - a former form of ours (`baseline.prior_templates`, one supplied form):
    the Confidentiality clause's 2022 form, judged against today's standard,
    one deal that opened on it and conceded to an equivalent rewording, and
    the owner's `why_changed` note; the other three clauses have none;
  - absence: `evidence.absence` is the engine's own `build_absence` over the
    deals' signed-text nodes, with one deal verifiably signed without a
    clause and one where a passage may carry it (`possibly_present`).

`identity` is sealed with `scripts/opf_canonicalize.py` (this repo's own copy
of the canonical form), so the fixtures also prove the two implementations
agree.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_canonicalize  # noqa: E402
import opf_html  # noqa: E402
import opf_load  # noqa: E402

SCHEMA_PATH = opf_load.OPF_SCHEMA_PATH
SYNTHETIC_EIAA_PATH = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"

OPF_VERSION = opf_load.OPF_VERSION

FULL_STEM = f"acme-university-{OPF_VERSION}"
EMPTY_FLOOR_STEM = f"acme-university-{OPF_VERSION}-empty-floor"
REAL_SHAPE_STEM = f"acme-university-{OPF_VERSION}-real-shape"

COMPILER_VERSION = "1.0.0"


# --------------------------------------------------------------------------
# The engine, imported read-only from the sibling checkout.
# --------------------------------------------------------------------------
def engine_repo() -> Path | None:
    """The playbook-engine checkout, or None when it is not on disk."""
    env = os.environ.get("PLAYBOOK_ENGINE_REPO")
    candidates = [Path(env)] if env else []
    candidates.append(REPO_ROOT.parent / "playbook-engine")
    for candidate in candidates:
        if (candidate / "playbook_engine" / "digest.py").is_file():
            return candidate
    return None


def load_engine() -> SimpleNamespace | None:
    """The engine's reference functions, or None when the sibling is absent.

    Read-only: bytecode writing is switched off for the import so nothing is
    written into the sibling checkout.
    """
    repo = engine_repo()
    if repo is None:
        return None
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        with warnings.catch_warnings():
            # Compiling the engine's sources without a bytecode cache surfaces
            # its own docstring-escape SyntaxWarnings; they are not ours.
            warnings.simplefilter("ignore", SyntaxWarning)
            # Imported lazily by the grouping-key normalizer; import it here,
            # inside the filter, so its warnings stay silent too.
            import playbook_engine.version_orderer  # noqa: F401, PLC0415
            from playbook_engine.absence import build_absence  # noqa: PLC0415
            from playbook_engine.digest import build_digest_v5  # noqa: PLC0415
            from playbook_engine.dossiers import build_derived_sections  # noqa: PLC0415
            from playbook_engine.equivalence import standard_parts  # noqa: PLC0415
            from playbook_engine.observation_builder import (  # noqa: PLC0415
                Observation,
                ObservationCitation,
            )
            from playbook_engine.precedent import restamp_evidence  # noqa: PLC0415
            from playbook_engine.validator import validate_document  # noqa: PLC0415
    finally:
        sys.dont_write_bytecode = previous
    return SimpleNamespace(
        repo=repo,
        build_absence=build_absence,
        build_digest_v5=build_digest_v5,
        build_derived_sections=build_derived_sections,
        standard_parts=standard_parts,
        Observation=Observation,
        ObservationCitation=ObservationCitation,
        restamp_evidence=restamp_evidence,
        validate_document=validate_document,
    )


# --------------------------------------------------------------------------
# Authoring helpers (inputs only).
# --------------------------------------------------------------------------
#: Placeholder for the one part of a one-part standard, replaced by the real
#: `part_id` once the engine has stamped it.
_PART = "__part__"


def _cite(document_id: str, version: Any, clause_path: str, char_span: list[int] | None = None) -> dict:
    c: dict[str, Any] = {"document_id": document_id, "version": version, "clause_path": clause_path}
    if char_span is not None:
        c["char_span"] = char_span
    return c


def _standard(text: str, clause_path: str) -> dict:
    return {
        "text": text,
        "source_ref": _cite("template", "template", clause_path, [0, len(text)]),
        "parts": [{"part_id": "", "order": 1, "text": text}],
    }


#: The blind-check outcomes a producer records (OPF-SPEC section 3.5.6), by
#: the `settled` value each derives in the digest (issue #248): `agreed`
#: (the check reached the same label and parts) and `adjudicated` (it
#: disagreed on the label and a fresh agent decided). `None` is never
#: checked (`unchecked`); an owner judgment (`basis: owner`) carries none.
_CHECKS: dict[str, dict[str, Any]] = {
    "agreed": {"agreed": True, "label_agreed": True, "parts_agreed": True,
               "by": "fixture-checker", "adjudicated": False},
    "adjudicated": {"agreed": False, "label_agreed": False, "parts_agreed": True,
                    "by": "fixture-adjudicator", "adjudicated": True},
}


def _vs(label: str, reason: str, *, check: str | None = None, basis: str = "agent") -> dict:
    """A `vs_standard` judgment over a one-part standard whose parts DERIVE
    *label* (OPF-SPEC section 3.5.6: the label is a function of the parts and
    additions, and the engine's validator recomputes it). *check* names a
    blind-check outcome (`_CHECKS`), None for an unchecked one; *basis*
    `owner` is an owner's correction, which carries no check."""
    relation = {"equivalent": "equivalent", "more_protective": "more_protective",
                "less_protective": "less_protective"}[label]
    part: dict[str, Any] = {"part_id": _PART, "status": "present", "relation": relation}
    if relation != "equivalent":
        part["reason"] = reason
    return {
        "label": label,
        "reason": reason,
        "parts": [part],
        "additions": [],
        "basis": basis,
        "check": copy.deepcopy(_CHECKS[check]) if check is not None else None,
    }


def _text(text: str, document_id: str, version: int, clause_path: str, vs: dict | None = None) -> dict:
    entry: dict[str, Any] = {"text": text, "ref": _cite(document_id, version, clause_path, [0, len(text)])}
    entry["vs_standard"] = vs
    return entry


def _prec(
    document_id: str,
    taxonomy_id: str,
    *,
    paper: str,
    signed_text: dict | None,
    opened_with: str | None,
    opening_text: dict | None = None,
    standard: bool = False,
    signed: bool = True,
    rounds: int = 0,
    refused_asks: list[dict] | None = None,
    opened_with_form: str | None = None,
) -> dict:
    """One precedent record. `signed_on` is left None here and set from the
    record's corpus document by `_date_precedent` (OPF 0.6: a precedent's
    date equals its document's, and is null when `signed` is false)."""
    asks = refused_asks or []
    record: dict[str, Any] = {
        "id": "",
        "taxonomy_id": taxonomy_id,
        "document_id": document_id,
        "paper": paper,
        "paper_basis": "provenance_detection",
        "paper_confidence": 0.9,
        "signed": signed,
        "signed_on": None,
        "rounds": rounds,
        "signed_text": signed_text,
        "opened_with": opened_with,
    }
    if opened_with_form is not None:
        record["opened_with_form"] = opened_with_form
    record.update({
        "opening_text": opening_text,
        "standard": standard,
        "moved": bool(rounds or opening_text is not None or asks),
        "refused_asks": asks,
    })
    return record


def _ask(text: str, document_id: str, version: int, clause_path: str, vs: dict | None) -> dict:
    return {
        "text": text,
        "round": version - 1,
        "ref": _cite(document_id, version, clause_path, [0, len(text)]),
        "vs_standard": vs,
    }


# --------------------------------------------------------------------------
# The fixture corpus — a fictional Educational Affiliation Agreement.
# --------------------------------------------------------------------------
INDEMNITY_STANDARD = (
    "Each party shall indemnify the other against third-party claims arising from its "
    "own negligence or willful misconduct."
)
GOVERNING_LAW_STANDARD = "This Agreement is governed by the laws of the State of Fixtureland."
TERM_STANDARD = "Either party may terminate for convenience on thirty (30) days' written notice."
CONFIDENTIALITY_STANDARD = (
    "Confidential Information shall be protected for three (3) years after disclosure."
)

ACMELAND_LAW = "This Agreement shall be governed by the laws of the State of Acmeland."

#: Our former form (OPF 0.6 `baseline.prior_templates`, issue #249): the
#: 2022 affiliation template, whose Confidentiality clause protected
#: information for two years. One deal opened on it.
PRIOR_FORM_2022 = "2022 affiliation form"
CONFIDENTIALITY_2022 = (
    "Confidential Information shall be protected for two (2) years after disclosure."
)
#: The owner's note on why the Confidentiality standard changed (OPF 0.6
#: `why_changed`, basis owner). Synthetic.
CONFIDENTIALITY_WHY_CHANGED = (
    "We lengthened confidentiality protection from two years to three after "
    "placements began sharing student records."
)

#: When each fixture deal was signed (OPF 0.6 `corpus.documents[].signed_on`
#: and its `signed_on_basis`): a full date, a month, a signed deal with no
#: date, and the unsigned deal (no date, no basis).
SIGNED_ON = {
    "acme-university": ("2024-03-14", "document_text"),
    "fixture-college": ("2024-09", "signature_block"),
    "example-institute": ("2025-02-03", "filename"),
    "acme-nursing-school": (None, None),
    "example-academy": (None, None),
}


def _clauses() -> list[dict]:
    """The four clauses. OPF 0.6 requires `prior_standards` and `why_changed`
    on every clause; only Confidentiality has a former form and a note."""
    clauses = _clause_entries()
    for clause in clauses:
        clause.setdefault("prior_standards", [])
        clause.setdefault("why_changed", None)
    return clauses


def _clause_entries() -> list[dict]:
    return [
        {"id": "clause.indemnification", "taxonomy_id": "indemnification", "title": "Indemnification",
         "our_standard": _standard(INDEMNITY_STANDARD, "8")},
        {"id": "clause.governing-law", "taxonomy_id": "governing_law", "title": "Governing Law",
         "our_standard": _standard(GOVERNING_LAW_STANDARD, "14")},
        {"id": "clause.term-termination", "taxonomy_id": "term_termination", "title": "Term and Termination",
         "our_standard": _standard(TERM_STANDARD, "11")},
        {"id": "clause.confidentiality", "taxonomy_id": "confidentiality", "title": "Confidentiality",
         "our_standard": _standard(CONFIDENTIALITY_STANDARD, "9"),
         # Our 2022 form's clause, judged once against today's standard; the
         # check disagreed and a fresh agent decided (settled: adjudicated).
         "prior_standards": [{
             "form_label": PRIOR_FORM_2022,
             "text": CONFIDENTIALITY_2022,
             "parts": [{"part_id": "", "order": 1, "text": CONFIDENTIALITY_2022}],
             "source_ref": _cite(f"prior_template:{PRIOR_FORM_2022}", "prior_template", "9",
                                 [0, len(CONFIDENTIALITY_2022)]),
             "vs_standard": _vs("less_protective", "Protects information for two years, not three.",
                                check="adjudicated"),
         }],
         "why_changed": {"text": CONFIDENTIALITY_WHY_CHANGED, "basis": "owner"}},
    ]


def _precedent() -> list[dict]:
    """One record per (deal, clause type). Five deals: four signed, one not."""
    au, fc, ei, an, ea = (
        "acme-university", "fixture-college", "example-institute", "acme-nursing-school", "example-academy",
    )
    return [
        # --- indemnification -------------------------------------------------
        # Opened with our standard and signed it unchanged.
        _prec(au, "indemnification", paper="ours", opened_with="standard", standard=True,
              signed_text=_text(INDEMNITY_STANDARD, au, 3, "8.1")),
        # Opened with our standard, conceded a notice-and-cooperation proviso;
        # an uncapped one-way indemnity was asked for and refused.
        _prec(fc, "indemnification", paper="ours", opened_with="standard", rounds=1,
              opening_text=_text(INDEMNITY_STANDARD, fc, 1, "8.1"),
              signed_text=_text(
                  "Each party shall indemnify the other for third-party claims arising from its "
                  "negligence, provided the indemnified party gives prompt notice and reasonable "
                  "cooperation.", fc, 2, "8.1",
                  _vs("less_protective", "Conditions our indemnity on prompt notice and cooperation.",
                      check="agreed")),
              refused_asks=[_ask(
                  "FixtureCorp shall indemnify Acme University against any and all claims of any "
                  "kind, including those arising from Acme University's own negligence, without "
                  "limitation.", fc, 2, "8.1",
                  _vs("less_protective", "Makes our indemnity uncapped and one-way.",
                      check="adjudicated"))]),
        # Counterparty paper: a one-way IP indemnity signed as proposed (not judged).
        _prec(ei, "indemnification", paper="theirs", opened_with="non_standard",
              signed_text=_text(
                  "FixtureCorp shall indemnify Example Institute against any claim that the "
                  "placement materials infringe a third party's intellectual property rights.",
                  ei, 4, "8.2")),
        # Opened with a no-indemnity clause and ended at our standard.
        _prec(an, "indemnification", paper="theirs", opened_with="non_standard", standard=True, rounds=1,
              opening_text=_text(
                  "Each party is responsible for its own acts and omissions, and neither party "
                  "shall indemnify the other.", an, 1, "7",
                  _vs("less_protective", "Removes the mutual indemnity entirely.")),
              signed_text=_text(INDEMNITY_STANDARD, an, 2, "7")),
        # No executed copy: counts nowhere. Its last draft is our standard, and
        # `standard` states that fact whether or not the deal was signed.
        _prec(ea, "indemnification", paper="ours", opened_with=None, signed=False, standard=True,
              signed_text=_text(INDEMNITY_STANDARD, ea, 1, "8.1")),
        # --- governing law ---------------------------------------------------
        _prec(au, "governing_law", paper="ours", opened_with="standard", rounds=1,
              opening_text=_text(GOVERNING_LAW_STANDARD, au, 1, "14"),
              signed_text=_text(ACMELAND_LAW, au, 3, "14",
                                _vs("less_protective", "Substitutes the counterparty's home-state law for ours."))),
        _prec(fc, "governing_law", paper="ours", opened_with="standard", standard=True,
              signed_text=_text(GOVERNING_LAW_STANDARD, fc, 2, "14")),
        _prec(ei, "governing_law", paper="theirs", opened_with="non_standard",
              signed_text=_text(
                  "This Agreement shall be governed by the laws of the State of Exampleland.",
                  ei, 4, "16",
                  _vs("less_protective", "Substitutes the counterparty's home-state law for ours.",
                      basis="owner"))),
        _prec(an, "governing_law", paper="theirs", opened_with="standard", rounds=1,
              opening_text=_text(GOVERNING_LAW_STANDARD, an, 1, "12"),
              signed_text=_text(ACMELAND_LAW, an, 2, "12",
                                _vs("less_protective", "Substitutes the counterparty's home-state law for ours."))),
        # --- term and termination -------------------------------------------
        _prec(au, "term_termination", paper="ours", opened_with="standard", standard=True,
              signed_text=_text(TERM_STANDARD, au, 3, "11.1")),
        _prec(fc, "term_termination", paper="ours", opened_with="standard", standard=True,
              signed_text=_text(TERM_STANDARD, fc, 2, "11.1")),
        # Opened at ninety days, settled at sixty: a variant AND a changed opening.
        _prec(ei, "term_termination", paper="theirs", opened_with="non_standard", rounds=2,
              opening_text=_text(
                  "Either party may terminate for convenience upon ninety (90) days' prior written "
                  "notice.", ei, 1, "11.1",
                  _vs("less_protective", "Triples the notice period before either party may exit.")),
              signed_text=_text(
                  "Either party may terminate for convenience upon sixty (60) days' prior written "
                  "notice.", ei, 4, "11.1",
                  _vs("less_protective", "Doubles the notice period before either party may exit."))),
        # --- confidentiality -------------------------------------------------
        _prec(au, "confidentiality", paper="ours", opened_with="standard", standard=True,
              signed_text=_text(CONFIDENTIALITY_STANDARD, au, 3, "9.1")),
        # Counterparty-proposed perpetual protection for student records, signed as proposed.
        _prec(fc, "confidentiality", paper="ours", opened_with="non_standard",
              signed_text=_text(
                  "Confidential Information consisting of student education records shall be "
                  "protected in perpetuity.", fc, 2, "9.2",
                  _vs("more_protective", "Extends protection of student records without a time limit.",
                      check="agreed"))),
        # Our standard struck before signing.
        _prec(ei, "confidentiality", paper="theirs", opened_with="standard", rounds=1,
              opening_text=_text(CONFIDENTIALITY_STANDARD, ei, 1, "9"),
              signed_text=None),
        # Opened on our 2022 former form (OPF 0.6 `prior_standard`) and
        # conceded to a rewording of today's standard: an equivalent variant
        # whose origin is ours.
        _prec(an, "confidentiality", paper="theirs", opened_with="prior_standard", rounds=1,
              opened_with_form=PRIOR_FORM_2022,
              opening_text=_text(CONFIDENTIALITY_2022, an, 1, "9"),
              signed_text=_text(
                  "Each party shall protect the other's Confidential Information for three (3) "
                  "years following disclosure.", an, 2, "9",
                  _vs("equivalent", "Same three-year protection, reworded."))),
    ]


def _date_precedent(records: list[dict], documents: list[dict]) -> list[dict]:
    """Set each record's `signed_on` from its corpus document (OPF 0.6: equal
    to the document's, and null on an unsigned record)."""
    dates = {d["document_id"]: d.get("signed_on") for d in documents}
    for record in records:
        record["signed_on"] = dates.get(record["document_id"]) if record["signed"] else None
    return records


#: Passages of the deals' executed copies that are NOT precedent records
#: (OPF 0.6 absence, issue #251): `(document_id, version, clause_path,
#: filed_under, text)`. Acme Nursing School's copy carries an unclassified
#: notice passage that reads like a termination clause, so its signed-without
#: Term and Termination is only `possibly_present`; Example Institute's
#: struck Confidentiality has no such passage, so it is `absent_verified`.
_EXTRA_NODES: tuple[tuple[str, int, str, str | None, str], ...] = (
    ("acme-nursing-school", 2, "15", None,
     "Either party may terminate this agreement for convenience on written notice to the other."),
    ("acme-nursing-school", 2, "16", None,
     "Notices under this agreement shall be delivered in writing to the addresses set out above."),
)


def _absence(doc: dict, engine: SimpleNamespace, extra_nodes: tuple = _EXTRA_NODES) -> dict:
    """`evidence.absence`, written by the engine's own `build_absence` over the
    deals' signed-text nodes (one per signed precedent record, filed under its
    clause type, plus `extra_nodes`) -- never by hand, the same rule the
    engine's conformance generator follows: a validator cannot see
    `n_checked` or `filed_under`, so a hand-written record could state what no
    compile produces."""
    evidence = doc["evidence"]
    versions = {d["document_id"]: d.get("signed_version") for d in doc["corpus"]["documents"]}

    def node(document_id: str, version: int, clause_path: str, taxonomy_id: str | None, text: str):
        return engine.Observation(
            observation_id=f"{document_id}/{version}/{clause_path}",
            taxonomy_id=taxonomy_id,
            text_summary=text[:100],
            full_text=text,
            citation=engine.ObservationCitation(
                document_id=document_id, version=version, clause_path=clause_path,
                char_span=(0, len(text)),
            ),
            deviation="none",
            risk_delta={"direction": "neutral", "magnitude": "none"},
            provenance="our_paper",
            outcome="signed",
        )

    nodes = [
        node(p["document_id"], p["signed_text"]["ref"]["version"], p["signed_text"]["ref"]["clause_path"],
             p["taxonomy_id"], p["signed_text"]["text"])
        for p in evidence["precedent"]
        if p["signed"] and p["signed_text"]
    ]
    nodes += [
        node(document_id, version, path, filed_under, text)
        for document_id, version, path, filed_under, text in extra_nodes
        if versions.get(document_id) is not None
    ]
    return engine.build_absence(
        agreement_type_id=doc["agreement_type"]["id"],
        clauses=evidence["clauses"],
        precedent=evidence["precedent"],
        observations=nodes,
        corpus_documents=doc["corpus"]["documents"],
    )


def _documents() -> list[dict]:
    def doc(document_id: str, title: str, provenance: str, versions: int, signed_version: int | None) -> dict:
        signed_on, basis = SIGNED_ON[document_id]
        return {
            "document_id": document_id, "title": title, "provenance": provenance, "in_scope": True,
            "scope_rationale": "Educational affiliation agreement matching target type.",
            "scope_confidence": 0.95, "versions": versions, "signed_version": signed_version,
            "version_order_basis": "edit_distance_chain+signed_anchor",
            "signed_on": signed_on, "signed_on_basis": basis,
        }

    return [
        doc("acme-university", "Acme University Affiliation Agreement", "our_paper", 3, 3),
        doc("fixture-college", "FixtureCorp College Placement Agreement", "our_paper", 2, 2),
        doc("example-institute", "Example Institute Clinical Placement Agreement", "counterparty_paper", 4, 4),
        doc("acme-nursing-school", "Acme Nursing School Affiliation Agreement", "counterparty_paper", 2, 2),
        doc("example-academy", "Example Academy Affiliation Agreement (unsigned)", "our_paper", 1, None),
    ]


def build_body(empty_floor: bool = False, *, empty_posture: bool = False, absent_floor: bool = False) -> dict:
    """The authored fixture inputs, before any derived value is stamped.

    Three shapes, and the distinction between the last two matters:

      - default: a Posture prose block and one Floor invariant.
      - ``empty_floor``: ``floor.invariants == []`` -- the section exists and
        declares no invariants.
      - ``absent_floor`` + ``empty_posture``: ``floor == {}`` and
        ``posture == {}``, which is THE REAL PLAYBOOK'S SHAPE, and is
        schema-valid: the 0.6 schema requires the `posture` and `floor` keys
        but requires nothing INSIDE them. A fixture that only ever carries
        prose and invariants tests a shape production does not have.
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
        "opf_version": OPF_VERSION,
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
            # OPF 0.6 (issue #249): one supplied former form, with the owner's
            # in-use start and a synthetic content address.
            "prior_templates": [
                {"label": PRIOR_FORM_2022, "in_use_from": "2022-01", "sha256": "sha256:" + "2" * 64},
            ],
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
                {"id": "insurance", "label": "Insurance", "status": "active",
                 "cuad_origin": "Insurance", "description": "Coverage each party must carry."},
            ],
        },
        "perspective": {"party": "FixtureCorp", "counterparty_type": "Educational Institution"},
        "de_minimis": ["typo fixes", "renumbering with no substantive change"],
        "evidence": {"clauses": _clauses(), "precedent": _date_precedent(_precedent(), _documents())},
        "posture": posture,
        "floor": {} if absent_floor else {"invariants": invariants},
        "corpus": {
            "documents": _documents(),
            "stats": {"documents_total": 5, "documents_in_scope": 5, "versions_total": 12},
        },
        "compiler": {
            "name": "playbook-engine",
            "version": COMPILER_VERSION,
            "run_id": "fixture-run-001",
            "generated_at": "2026-01-01T00:00:00Z",
            "stub_basis_present": False,
        },
    }


def _synthetic_eiaa_body() -> dict:
    """`tests/fixtures/opf/synthetic-eiaa.opf.json`: an OPF 0.6 document with
    NO digest section (schema-optional; a producer may omit it), for the
    no-digest refusal path and the Floor/bind tests that only read posture,
    Floor and agreement_type. Freshly authored for issue #283, migrated to
    0.5 by issue #128 and to 0.6 by issue #177; not copied from any engine
    example."""
    d1, d2 = "synthetic-doc-001", "synthetic-doc-002"
    cap = "Synthetic clause: liability is capped at a fixed dollar amount stated in the agreement."
    notice = "Synthetic clause: either party may terminate for convenience on thirty (30) days' notice."
    return {
        "opf_version": OPF_VERSION,
        "agreement_type": {
            "id": "educational-internship-affiliation",
            "name": "Synthetic Educational Internship & Affiliation Agreement",
            "description": "Synthetic fixture for issue #283 -- freshly authored, not copied from any engine example.",
            "aliases": ["eiaa", "synthetic-generic"],
        },
        "baseline": {"has_canonical_template": False},
        "taxonomy": {
            "source": "synthetic-fixture",
            "entries": [
                {"id": "liability_cap", "label": "Liability Cap", "status": "active"},
                {"id": "termination", "label": "Termination for Convenience", "status": "active"},
            ],
        },
        "evidence": {
            "clauses": [
                {"id": "clause-liability-cap", "taxonomy_id": "liability_cap", "title": "Liability Cap",
                 "our_standard": None, "prior_standards": [], "why_changed": None},
                {"id": "clause-termination", "taxonomy_id": "termination",
                 "title": "Termination for Convenience", "our_standard": None,
                 "prior_standards": [], "why_changed": None},
            ],
            "precedent": [
                {**_prec(d1, "liability_cap", paper="ours", opened_with="non_standard",
                         signed_text=_text(cap, d1, 1, "5.2")), "signed_on": "2025-05"},
                {**_prec(d2, "termination", paper="theirs", opened_with="non_standard",
                         signed_text=_text(notice, d2, 1, "9.1")), "signed_on": None},
            ],
        },
        "posture": {
            "system_prompt": (
                "This is a synthetic posture for a fictional educational internship and affiliation "
                "agreement, authored only for test fixtures. It expresses no real party's negotiation "
                "intent. Treat all monetary and party details in this document as placeholders."
            )
        },
        "floor": {
            "invariants": [
                {
                    "id": "floor-no-uncapped-liability",
                    "statement": (
                        "Our maximum liability under this agreement is a fixed dollar cap stated in the "
                        "agreement; it is never unlimited or tied solely to a multiplier of fees paid."
                    ),
                    "rationale": (
                        "Synthetic placeholder rationale for demonstrating Floor invariant structure; "
                        "not legal advice."
                    ),
                },
                {
                    "id": "floor-termination-notice",
                    "statement": "Either party may terminate for convenience only with at least 30 days written notice.",
                    "rationale": (
                        "Synthetic placeholder rationale for demonstrating Floor invariant structure; "
                        "not legal advice."
                    ),
                },
            ]
        },
        "corpus": {
            "documents": [
                {"document_id": d1, "title": "Synthetic Fixture Document 1", "provenance": "our_paper",
                 "in_scope": True, "versions": 1, "signed_version": 1,
                 "signed_on": "2025-05", "signed_on_basis": "signature_block"},
                {"document_id": d2, "title": "Synthetic Fixture Document 2", "provenance": "counterparty_paper",
                 "in_scope": True, "versions": 1, "signed_version": 1,
                 "signed_on": None, "signed_on_basis": None},
            ],
            "stats": {"document_count": 2},
        },
        "compiler": {
            "name": "synthetic-fixture-author",
            "version": "0.0.1",
            "generated_at": "2026-07-12T00:00:00Z",
        },
    }


# --------------------------------------------------------------------------
# Stamping (engine) and sealing (this repo).
# --------------------------------------------------------------------------
def _stamp_evidence(doc: dict, engine: SimpleNamespace) -> None:
    """Precedent ids, clause counts and standard part ids (engine
    `restamp_evidence`), then point every judgment at its clause's one part."""
    party = (doc.get("perspective") or {}).get("party")
    engine.restamp_evidence(doc["evidence"], doc["agreement_type"]["id"], party=party)
    part_ids = {
        c["taxonomy_id"]: [p["part_id"] for p in engine.standard_parts(c)] for c in doc["evidence"]["clauses"]
    }
    for record in doc["evidence"]["precedent"]:
        entries = [record.get("signed_text"), record.get("opening_text"), *record["refused_asks"]]
        for entry in entries:
            vs = entry.get("vs_standard") if isinstance(entry, dict) else None
            if not isinstance(vs, dict):
                continue
            for part in vs["parts"]:
                if part["part_id"] == _PART:
                    (part["part_id"],) = part_ids[record["taxonomy_id"]]
    # A former form's clause is judged against today's standard, part by part.
    for clause in doc["evidence"]["clauses"]:
        for prior in clause.get("prior_standards") or []:
            vs = prior.get("vs_standard")
            for part in vs["parts"] if isinstance(vs, dict) else []:
                if part["part_id"] == _PART:
                    (part["part_id"],) = part_ids[clause["taxonomy_id"]]
    doc["evidence"]["absence"] = _absence(doc, engine)


def finalize(body: dict, engine: SimpleNamespace, *, with_digest: bool = True) -> dict:
    """Stamp the derived values, attach the digest and the derived sections
    (engine), and seal `identity` (this repo's canonicalizer)."""
    doc = copy.deepcopy(body)
    _stamp_evidence(doc, engine)
    if with_digest:
        doc["digest"] = engine.build_digest_v5(doc)
        doc.update(engine.build_derived_sections(doc))
    identity: dict[str, Any] = {}
    if with_digest:
        identity = {"id": doc["agreement_type"]["id"], "version": "1.0.0", "supersedes": None}
    identity["content_hash"] = opf_canonicalize.content_hash(doc)
    identity["section_digests"] = opf_canonicalize.compute_section_digests(doc)
    doc["identity"] = identity
    return doc


def build_all(engine: SimpleNamespace) -> dict[str, dict]:
    """Every generated document, keyed by its output stem (or path)."""
    return {
        FULL_STEM: finalize(build_body(empty_floor=False), engine),
        EMPTY_FLOOR_STEM: finalize(build_body(empty_floor=True), engine),
        REAL_SHAPE_STEM: finalize(build_body(empty_posture=True, absent_floor=True), engine),
        str(SYNTHETIC_EIAA_PATH): finalize(_synthetic_eiaa_body(), engine, with_digest=False),
    }


def json_text(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def html_text(doc: dict, stem: str) -> str:
    return opf_html.wrap_opf_html(json_text(doc), digest=doc.get("digest"), title=f"OPF playbook — {stem}")


def engine_errors(doc: dict, engine: SimpleNamespace) -> list[str]:
    """Blocking findings of the engine's own validator (value-free paths)."""
    result = engine.validate_document(doc)
    return [f"{e.path}: {e.message}" for e in result.errors if e.blocking]


def _validate(doc: dict, engine: SimpleNamespace) -> None:
    import jsonschema  # noqa: PLC0415

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=doc, schema=schema)
    assert opf_canonicalize.verify_content_hash(doc), "content_hash does not verify"
    errors = engine_errors(doc, engine)
    assert not errors, "the engine's validator refuses this fixture:\n  " + "\n  ".join(errors)


def main() -> int:
    engine = load_engine()
    if engine is None:
        print(
            "ERROR: playbook-engine sibling checkout not found (../playbook-engine or "
            "$PLAYBOOK_ENGINE_REPO). This generator stamps every derived value with the "
            "engine's reference functions and cannot run without it.",
            file=sys.stderr,
        )
        return 1
    docs = build_all(engine)
    for key, doc in docs.items():
        _validate(doc, engine)
        if key == str(SYNTHETIC_EIAA_PATH):
            SYNTHETIC_EIAA_PATH.write_text(json_text(doc), encoding="utf-8")
            print(f"wrote {SYNTHETIC_EIAA_PATH.relative_to(REPO_ROOT)}")
            continue
        (HERE / f"{key}.opf.json").write_text(json_text(doc), encoding="utf-8")
        (HERE / f"{key}.opf.html").write_text(html_text(doc, key), encoding="utf-8")
        print(f"wrote tests/gold-fixtures-opf/{key}.opf.json + .opf.html")
        print(f"  content_hash = {doc['identity']['content_hash']}")

    real_shape = docs[REAL_SHAPE_STEM]
    assert real_shape["posture"] == {}, "real-shape fixture must ship posture == {}"
    assert real_shape["floor"] == {}, "real-shape fixture must ship floor == {}"
    extracted = opf_html.extract_opf_from_html((HERE / f"{FULL_STEM}.opf.html").read_text(encoding="utf-8"))
    assert extracted == docs[FULL_STEM], "HTML round-trip mismatch"
    print("OK: schema-valid, engine-valid, hash verifies, HTML round-trips.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
