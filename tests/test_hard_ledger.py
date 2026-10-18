#!/usr/bin/env python3
"""
Issue #147: hard-rejection recall enforced outside both models.

The three acceptance criteria, end to end through the real
`review_spine.run_review` with `FakeBedrockClient` fixtures:

  [1] A synthetic document with the indemnification clause deleted wholesale,
      both passes silent about it (neither raises an issue; the critic can
      only ASSERT the ledger entry it cannot find): the redline carries the
      fallback insertion, attributed to an issue with provenance
      `ledger:IND-004`, decision REQUEST_CHANGE, status OK (DONE on the row).
  [2] The clause present and the critic citing a valid span that satisfies
      the predicate: the entry is CLEARED and nothing is inserted.
  [3] A critic citing a span whose hash does not validate: the entry stays
      asserted and the fallback language is inserted.

Plus the rest of the contract: a missing ledger disposition fails the review
closed (critic pass retry, then reconcile), the Floor judge no longer judges a
deterministic presence rule and only recommends on a judged one, every
predicate kind, the insertion anchor's branches (a block the critic names
outside the clause never places the insertion, whether it CLEARED or
ASSERTed), the counts-only sentences with a count of one and of two, and the
internal footnote. And stage 5's say (issue #164): the OPF pen-rule
defaults (`max_chars`, `must_not_introduce`) do not gate the playbook's own
standard, so a ledger-only review whose standard is over 1,500 characters or
carries a banned phrase still delivers it, while model-drafted text in the
same review is still refused; a critic restoration that copies the standard
word for word shares that exemption when it stands in for the ledger's own
insertion, and never stands in beside language the critic drafted itself; an
insertion stage 5 rolls out through another gate is noted as not inserted.
And issue #165: an insertion that fails to compile (a table-cell anchor) is
rolled out so its rule id re-hosts on another insertion's internal footnote,
or, with no insertion left to host it (no other edit, or deletions only) in a
notes mode with internal content, the review fails closed -- whether that
insertion was the ledger's own or the critic's copy standing in for it.
And issue #169: that refused insertion's `analysis_report.changes_not_applied`
row says in fixed copy that the standard language could not be inserted,
never the issue's own "has been inserted".
And issue #168 (did not reproduce; regression test only): a critic copy of
the standard that the pen rules refuse with its change set never left the
clause delivered by neither path -- the ledger's fallback is inserted and
its rule id reaches the internal footnotes.

## Fixture reachability

  * The OPF document is the committed synthetic gold fixture
    (`tests/gold-fixtures-opf/acme-university-0.5.opf.json`) with one more
    signed Floor invariant, `IND-004`, carrying the vendor keys
    `playbook floor sign --clause indemnification --requires-presence
    --condition ...` writes (`x_taxonomy_id`, `x_required_presence`,
    `x_condition`). Its manifest is rebuilt by `_build_manifest`, a mirror of
    the engine's `playbook_engine.dossiers.build_manifest` (OPF-SPEC
    §3.12.3) -- and `test_fixture_manifest_is_what_the_engine_builds`
    compares the two whenever the sibling engine checkout is importable.
  * Every block id is the production `build_block_map` id of a real `.docx`.
  * `critic_delta.ledger_dispositions` is the shape `playbooks/output-schema-v3
    .json` admits and the critic's tasking (`hard_ledger
    .render_critic_ledger_block`) asks for; every response below passes the
    pass's own full-schema validation, or the review would ERROR.

Run standalone: `.venv/bin/python tests/test_hard_ledger.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src", REPO_ROOT / "tests"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import extraction_normalization_stage as ens  # noqa: E402
import hard_ledger as hl  # noqa: E402
import model_client  # noqa: E402
import reconciliation as recon  # noqa: E402
import review_spine  # noqa: E402
from test_review_spine import _body_p, _build_docx_bytes, _heading_p, _load_bundle  # noqa: E402

REVIEW_ID = "00000000-0000-4000-a000-000000000147"
RULE_ID = "IND-004"
JUDGED_RULE = "no-uncapped-liability"
OPF_FIXTURE = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.5.opf.json"
ENGINE_CHECKOUT = Path(
    os.environ.get("PLAYBOOK_ENGINE_CHECKOUT", REPO_ROOT.parent / "playbook-engine")
)

INDEMNITY_CONDITION = {
    "type": "required_phrases",
    "phrases": ["indemnify", "third-party claims"],
    "match": "all",
}

DEFINITIONS = "Confidential Information means any non-public information disclosed by a party."
CONFIDENTIALITY = "Confidential Information shall be protected for three (3) years after disclosure."
TERM = "Either party may terminate for convenience on thirty (30) days' written notice."
GOVERNING_LAW = "This Agreement is governed by the laws of the State of Fixtureland."


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_manifest(opf_doc: dict[str, Any]) -> dict[str, Any]:
    """Mirror of the engine's `build_manifest` (OPF-SPEC §3.12.3) over a
    valid Floor: one rule per invariant, in Floor order."""
    by_taxonomy = {
        clause.get("taxonomy_id"): clause for clause in opf_doc["evidence"]["clauses"]
    }
    rules = []
    for invariant in opf_doc["floor"]["invariants"]:
        taxonomy_id = invariant.get("x_taxonomy_id")
        clause = by_taxonomy.get(taxonomy_id) if taxonomy_id else None
        standard = (clause or {}).get("our_standard") or {}
        rules.append(
            {
                "rule_id": invariant["id"],
                "clause_id": clause.get("id") if clause else None,
                "taxonomy_id": taxonomy_id,
                "statement": invariant["statement"],
                "required_presence": bool(invariant.get("x_required_presence", False)),
                "condition": invariant.get("x_condition", "judged"),
                "permissible_proof": list(invariant.get("x_permissible_proof") or []),
                "fallback_language": standard.get("text"),
            }
        )
    return {"hard_rules": rules}


def _invariant(
    rule_id: str, condition: Any = None, taxonomy_id: str = "indemnification"
) -> dict[str, Any]:
    invariant: dict[str, Any] = {
        "id": rule_id,
        "statement": "The agreement must keep the mutual indemnification clause.",
        "x_signed_by": "Legal Owner",
        "x_signed_at": "2026-01-01T00:00:00+00:00",
        "x_taxonomy_id": taxonomy_id,
        "x_required_presence": True,
    }
    if condition is not None:
        invariant["x_condition"] = condition
    return invariant


def _opf_doc(
    condition: Any = None,
    extra: tuple[dict[str, Any], ...] = (),
    standard: str | None = None,
) -> dict[str, Any]:
    """The gold fixture plus the signed `IND-004` invariant (and `extra`
    invariants after it), its manifest rebuilt as the engine builds it.
    `standard` replaces the Indemnification clause's `our_standard.text` --
    and so the manifest's `fallback_language` -- keeping its `char_span`
    true to the new text."""
    doc = json.loads(OPF_FIXTURE.read_text(encoding="utf-8"))
    if standard is not None:
        (clause,) = [c for c in doc["evidence"]["clauses"] if c["id"] == "clause.indemnification"]
        clause["our_standard"]["text"] = standard
        clause["our_standard"]["source_ref"]["char_span"] = [0, len(standard)]
    doc["floor"]["invariants"].append(_invariant(RULE_ID, condition))
    doc["floor"]["invariants"].extend(extra)
    doc["manifest"] = _build_manifest(doc)
    return doc


#: Two synthetic standards the OPF pen-rule defaults
#: (`playbooks/pen-rules.defaults.json`, applied to every OPF review, whose
#: pen-rules bundle is None) refuse: one over `max_chars` (1500), and one
#: short one carrying a `must_not_introduce` phrase ("perpetual").
LONG_STANDARD = " ".join(
    f"{n}. Each party shall indemnify, defend and hold harmless the other party and "
    "its officers against third-party claims arising from its breach of this Agreement."
    for n in range(1, 13)
)
BANNED_PHRASE_STANDARD = (
    "Each party shall indemnify the other against third-party claims arising from "
    "its breach, and this obligation is perpetual."
)


def _fallback(doc: dict[str, Any]) -> str:
    return next(r for r in doc["manifest"]["hard_rules"] if r["rule_id"] == RULE_ID)[
        "fallback_language"
    ]


def _docx(
    with_indemnity: bool,
    indemnity_text: str | list[str] | None = None,
    extra: tuple[tuple[str, str], ...] = (),
) -> bytes:
    """A synthetic agreement; `indemnity_text` may be several paragraphs
    under the one heading, and `extra` sections follow Confidentiality."""
    sections: list[tuple[str, str | list[str]]] = [
        ("Definitions", DEFINITIONS),
        ("Confidentiality", CONFIDENTIALITY),
        *extra,
    ]
    if with_indemnity:
        sections.append(("Indemnification", indemnity_text or _fallback(_opf_doc())))
    sections += [("Term and Termination", TERM), ("Governing Law", GOVERNING_LAW)]
    body = "".join(
        _heading_p(head) + "".join(_body_p(t) for t in ([text] if isinstance(text, str) else text))
        for head, text in sections
    )
    return _build_docx_bytes(body)


def _block_map(docx_bytes: bytes) -> dict[str, Any]:
    normalized = ens.extract_and_normalize(docx_bytes)
    assert normalized["status"] == "normalized", normalized
    return ens.build_block_map(normalized["paragraphs"])


def _block_id(docx_bytes: bytes, heading: str) -> str:
    for block_id, block in _block_map(docx_bytes).items():
        if block["heading"] == heading:
            return block_id
    raise AssertionError(f"setup: no block under heading {heading!r}")


def _response(
    *,
    ledger: list[dict[str, Any]] | None = None,
    critic: bool = False,
    summary: str = "No changes identified.",
) -> str:
    body: dict[str, Any] = {
        "decision": "ACCEPT",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "block_patches": [],
        "block_ops": [],
        "verdict_summary": summary,
    }
    if critic:
        body["critic_delta"] = {
            "dispositions": [],
            "overrides": [],
            **({"ledger_dispositions": ledger} if ledger is not None else {}),
        }
    return json.dumps(body)


def _assert(rule_id: str = RULE_ID, block_id: str = "") -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "disposition": "ASSERT",
        "block_id": block_id,
        "span": "",
        "reason": "The agreement has no indemnification clause.",
    }


def _cleared(block_id: str, span: str, rule_id: str = RULE_ID) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "disposition": "CLEARED",
        "block_id": block_id,
        "span": span,
        "reason": "The mutual indemnity is in the indemnification clause.",
    }


def _verdict(invariant_id: str, violated: bool = False) -> str:
    return json.dumps({"invariant_id": invariant_id, "violated": violated, "evidence_quote": ""})


def _run(
    docx_bytes: bytes,
    critic_responses: list[str],
    *,
    doc: dict[str, Any] | None = None,
    notes_mode: str = "external",
) -> tuple[dict[str, Any], model_client.FakeBedrockClient]:
    metadata = _load_bundle()["playbook"]["metadata"]
    bundle = {
        "opf_bundle_v2": {"opf": doc or _opf_doc(INDEMNITY_CONDITION), "overrides": None},
        "playbook": {"metadata": dict(metadata)},
    }
    client = model_client.FakeBedrockClient(
        {
            metadata["primary_model_id"]: [_response()],
            metadata["critic_model_id"]: critic_responses,
        }
    )
    result = review_spine.run_review(
        docx_bytes, bundle, client, review_id=REVIEW_ID, notes_mode=notes_mode
    )
    return result, client


def _part(docx_bytes: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
        return archive.read(name).decode("utf-8") if name in archive.namelist() else ""


def _inserted_texts(docx_bytes: bytes) -> str:
    """Every run of text inside a `<w:ins>` of the delivered document."""
    import re

    xml = _part(docx_bytes, "word/document.xml")
    return " ".join(
        "".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", chunk))
        for chunk in re.findall(r"<w:ins\b.*?</w:ins>", xml, flags=re.S)
    )


def _entry(result: dict[str, Any]) -> dict[str, Any]:
    entries = (result.get("hard_ledger") or {}).get("entries") or []
    assert [e["rule_id"] for e in entries] == [RULE_ID], entries
    return entries[0]


def _texts(docx_bytes: bytes, part: str) -> str:
    """Every `<w:t>` of a part, in document order, joined and unescaped --
    inserted runs included -- so text order is document order."""
    import html
    import re

    return html.unescape(
        "".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", _part(docx_bytes, part)))
    )


def _in_order(text: str, *needles: str) -> bool:
    positions = [text.find(needle) for needle in needles]
    return all(position >= 0 for position in positions) and positions == sorted(positions)


TERM_SUMMARY = "The notice period must be sixty days."


def _term_edit_response(docx_bytes: bytes, ledger: list[dict[str, Any]]) -> str:
    """A critic final result making one ordinary edit -- thirty days' notice
    to sixty, on the Term and Termination paragraph -- beside its ledger
    dispositions. A v3 transcript: its segments tile that block's text."""
    issue = {
        "issue_key": "I1",
        "section_ref": "Term and Termination",
        "section_title": "Term and Termination",
        "counterparty_change_summary": "The termination notice period is shorter than our standard.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "Termination for convenience needs sixty days' notice.",
        "playbook_topic_id": "clause.term-termination",
        "internal_precedent_citation": None,
        "provenance": "critic-added",
    }
    patch = {
        "block_id": _block_id(docx_bytes, "Term and Termination"),
        "segments": [
            {"op": "keep", "text": "Either party may terminate for convenience on "},
            {"op": "delete", "text": "thirty (30)", "issue_key": "I1"},
            {"op": "insert", "text": "sixty (60)", "issue_key": "I1"},
            {"op": "keep", "text": " days' written notice."},
        ],
    }
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [issue],
            "block_patches": [patch],
            "block_ops": [],
            "verdict_summary": TERM_SUMMARY,
            "critic_delta": {"dispositions": [], "overrides": [], "ledger_dispositions": ledger},
        }
    )


# ---------------------------------------------------------------------------
# [1] A deleted clause cannot vanish: the redline asserts it
# ---------------------------------------------------------------------------


def test_a_deleted_clause_is_asserted_into_the_redline() -> None:
    docx_bytes = _docx(with_indemnity=False)
    result, _client = _run(
        docx_bytes, [_response(critic=True, ledger=[_assert()]), _verdict(JUDGED_RULE)]
    )
    assert result["status"] == "OK", (result.get("status"), result.get("reason"))
    assert result["decision"] == "REQUEST_CHANGE"
    provenances = [finding.get("provenance") for finding in result["findings"]]
    assert provenances == [f"ledger:{RULE_ID}"], provenances
    redline = result.get("redline_bytes")
    assert redline, "no redline was delivered"
    assert _fallback(_opf_doc()) in _inserted_texts(redline), "fallback language is not a tracked insertion"
    entry = _entry(result)
    assert entry["status"] == hl.STATUS_ASSERTED
    assert entry["reason"] == hl.REASON_CRITIC_ASSERTED
    assert entry["inserted"] is True
    # Indemnification (template 8) is absent and no present section precedes
    # it in template order, so it lands just ahead of the first one after it
    # (Confidentiality, 9): after the Definitions block.
    assert entry["anchor_block_id"] == _block_id(docx_bytes, "Definitions"), entry
    assert result["hard_ledger"]["asserted"] == 1 and result["hard_ledger"]["cleared"] == 0
    assert result["summary"] == (
        "Changes are requested: 1 required protection was not found in the agreement "
        "and is asserted in the redline."
    ), result["summary"]


def test_the_status_is_done_on_the_row() -> None:
    """`OK` from the spine is `DONE` on the reviews row (issue #133):
    `pipeline_runner` maps exactly that."""
    import pipeline_runner

    source = Path(pipeline_runner.__file__).read_text(encoding="utf-8")
    assert 'terminal = "DONE" if status_value == "OK" else status_value' in source


def test_a_cleared_claim_the_document_does_not_support_is_still_asserted() -> None:
    """The critic CLEARs the deleted clause by citing a real span from another
    paragraph: the span validates, but it is not in the clause the rule
    names, so the entry stays asserted -- and the refuted citation does not
    decide where the standard language goes. It lands where the
    deterministic placement puts it for this document (after Definitions,
    ahead of Confidentiality), never after the Governing Law paragraph the
    critic named."""
    docx_bytes = _docx(with_indemnity=False)
    elsewhere = _block_id(docx_bytes, "Governing Law")
    result, _client = _run(
        docx_bytes,
        [_response(critic=True, ledger=[_cleared(elsewhere, GOVERNING_LAW)]), _verdict(JUDGED_RULE)],
    )
    entry = _entry(result)
    assert entry["reason"] == hl.REASON_SPAN_OUTSIDE_CLAUSE, entry
    assert entry["status"] == hl.STATUS_ASSERTED and entry["inserted"] is True
    assert result["decision"] == "REQUEST_CHANGE"
    # Recorded as cited; not where the insertion goes.
    assert entry["block_id"] == elsewhere, entry
    assert entry["anchor_block_id"] == _block_id(docx_bytes, "Definitions"), entry
    text = _texts(result["redline_bytes"], "word/document.xml")
    assert _in_order(text, DEFINITIONS, _fallback(_opf_doc()), CONFIDENTIALITY, GOVERNING_LAW), text


def test_an_assert_naming_another_clause_does_not_place_the_insertion() -> None:
    """An ASSERT may name "the paragraph where it belongs"; code honours that
    only for a paragraph under the clause's own heading. Naming Governing Law
    for a deleted Indemnification clause puts nothing there (the
    nearest-section placement: after Definitions); naming Term and
    Termination when the clause IS present puts the standard after the
    clause's own block instead."""
    absent = _docx(with_indemnity=False)
    governing = _block_id(absent, "Governing Law")
    result, _client = _run(
        absent, [_response(critic=True, ledger=[_assert(block_id=governing)]), _verdict(JUDGED_RULE)]
    )
    assert result["status"] == "OK", result.get("reason")
    entry = _entry(result)
    assert entry["reason"] == hl.REASON_CRITIC_ASSERTED and entry["inserted"] is True, entry
    assert entry["block_id"] == governing, entry
    assert entry["anchor_block_id"] == _block_id(absent, "Definitions"), entry
    text = _texts(result["redline_bytes"], "word/document.xml")
    assert _in_order(text, DEFINITIONS, _fallback(_opf_doc()), CONFIDENTIALITY), text

    waived = "The indemnity obligations of both parties are waived."
    present = _docx(with_indemnity=True, indemnity_text=waived)
    term = _block_id(present, "Term and Termination")
    result, _client = _run(
        present, [_response(critic=True, ledger=[_assert(block_id=term)]), _verdict(JUDGED_RULE)]
    )
    assert result["status"] == "OK", result.get("reason")
    entry = _entry(result)
    assert entry["block_id"] == term, entry
    assert entry["anchor_block_id"] == _block_id(present, "Indemnification"), entry
    text = _texts(result["redline_bytes"], "word/document.xml")
    assert _in_order(text, waived, _fallback(_opf_doc()), TERM), text


# ---------------------------------------------------------------------------
# A clear is bound to the clause the rule names, evaluated whole
# ---------------------------------------------------------------------------

LIABILITY = (
    "Neither party's liability is capped where it must indemnify the other "
    "against third-party claims arising from its breach."
)


def test_a_satisfying_span_from_another_clause_does_not_clear_a_predicate_rule() -> None:
    """Clause deleted wholesale; the critic cites words that DO satisfy the
    required_phrases test, genuinely, from the Limitation of Liability
    paragraph. They are not the Indemnification clause: asserted."""
    docx_bytes = _docx(with_indemnity=False, extra=(("Limitation of Liability", LIABILITY),))
    elsewhere = _block_id(docx_bytes, "Limitation of Liability")
    span = "indemnify the other against third-party claims"
    assert hl.evaluate_predicate(hl.build_ledger(_opf_doc(INDEMNITY_CONDITION))[0], span)
    result, _client = _run(
        docx_bytes,
        [_response(critic=True, ledger=[_cleared(elsewhere, span)]), _verdict(JUDGED_RULE)],
    )
    assert result["status"] == "OK", result.get("reason")
    entry = _entry(result)
    assert entry["status"] == hl.STATUS_ASSERTED, entry
    assert entry["reason"] == hl.REASON_SPAN_OUTSIDE_CLAUSE, entry
    assert entry["inserted"] is True and result["decision"] == "REQUEST_CHANGE"
    assert _fallback(_opf_doc()) in _inserted_texts(result["redline_bytes"])


def test_a_genuine_span_from_another_clause_does_not_clear_a_judged_rule() -> None:
    """A `"judged"` rule is cleared only by the deterministic check: a
    validated span INSIDE the clause's own section. The verbatim Governing
    Law sentence is not that."""
    doc = _opf_doc()  # no x_condition: "judged"
    docx_bytes = _docx(with_indemnity=False)
    elsewhere = _block_id(docx_bytes, "Governing Law")
    result, _client = _run(
        docx_bytes,
        [
            _response(critic=True, ledger=[_cleared(elsewhere, GOVERNING_LAW)]),
            _verdict(JUDGED_RULE),
            _verdict(RULE_ID),
        ],
        doc=doc,
    )
    assert result["status"] == "OK", result.get("reason")
    entry = _entry(result)
    assert entry["status"] == hl.STATUS_ASSERTED, entry
    assert entry["reason"] == hl.REASON_SPAN_OUTSIDE_CLAUSE, entry
    assert entry["predicate"] == hl.PREDICATE_PRESENCE and entry["inserted"] is True
    assert result["decision"] == "REQUEST_CHANGE"


def test_an_entry_with_no_clause_title_is_never_cleared() -> None:
    """Nothing to bind the citation to: fail closed."""
    docx_bytes = _docx(with_indemnity=True)
    block_map = _block_map(docx_bytes)
    (entry,) = hl.build_ledger(_opf_doc(INDEMNITY_CONDITION))
    critic = {
        "critic_delta": {
            "ledger_dispositions": [
                _cleared(_block_id(docx_bytes, "Indemnification"), "Each party shall indemnify")
            ]
        }
    }
    (result,) = hl.evaluate_ledger([{**entry, "title": None}], critic, block_map)
    assert result["status"] == hl.STATUS_ASSERTED, result
    assert result["reason"] == hl.REASON_SPAN_OUTSIDE_CLAUSE, result


YEARS_CAP = {"type": "numeric_bound", "pattern": "([0-9]+) years", "max": 5, "unit": "years"}


def test_a_cherry_picked_numeric_bound_span_stays_asserted() -> None:
    """The predicate runs over the WHOLE clause, never the critic's chosen
    sub-span: quoting only the compliant "3 years" does not hide the "9
    years" the same clause states -- in the same paragraph or the next."""
    doc = _opf_doc(YEARS_CAP)
    (entry,) = hl.build_ledger(doc)
    assert hl.evaluate_predicate(entry, "runs for 3 years") is True
    for clause in (
        "The indemnity runs for 3 years, renewable once for 9 years.",
        ["The indemnity runs for 3 years.", "It is renewable once for 9 years."],
    ):
        docx_bytes = _docx(with_indemnity=True, indemnity_text=clause)
        block_id = _block_id(docx_bytes, "Indemnification")
        result, _client = _run(
            docx_bytes,
            [_response(critic=True, ledger=[_cleared(block_id, "runs for 3 years")]), _verdict(JUDGED_RULE)],
            doc=doc,
        )
        assert result["status"] == "OK", result.get("reason")
        ledger_entry = _entry(result)
        assert ledger_entry["status"] == hl.STATUS_ASSERTED, (clause, ledger_entry)
        assert ledger_entry["reason"] == hl.REASON_PREDICATE_UNSATISFIED, ledger_entry
        assert result["decision"] == "REQUEST_CHANGE"
    # The same citation clears a clause whose every stated term is in bounds.
    docx_bytes = _docx(with_indemnity=True, indemnity_text="The indemnity runs for 3 years.")
    block_id = _block_id(docx_bytes, "Indemnification")
    (cleared,) = hl.evaluate_ledger(
        [entry],
        {"critic_delta": {"ledger_dispositions": [_cleared(block_id, "runs for 3 years")]}},
        _block_map(docx_bytes),
    )
    assert cleared["status"] == hl.STATUS_CLEARED, cleared


# ---------------------------------------------------------------------------
# [2] A valid span with a satisfied predicate clears the entry
# ---------------------------------------------------------------------------


def test_a_valid_span_with_a_satisfied_predicate_clears_the_entry() -> None:
    docx_bytes = _docx(with_indemnity=True)
    block_id = _block_id(docx_bytes, "Indemnification")
    span = "Each party shall indemnify the other against third-party claims"
    result, _client = _run(
        docx_bytes,
        [_response(critic=True, ledger=[_cleared(block_id, span)]), _verdict(JUDGED_RULE)],
    )
    assert result["status"] == "OK", result.get("reason")
    assert result["decision"] == "ACCEPT", result["decision"]
    assert result["redline_bytes"] is None
    assert result["findings"] == []
    entry = _entry(result)
    assert entry["status"] == hl.STATUS_CLEARED and entry["reason"] == hl.REASON_CLEARED
    assert entry["inserted"] is False and entry["issue_key"] is None
    assert entry["span_sha256"] == hashlib.sha256(span.encode("utf-8")).hexdigest()
    assert result["hard_ledger"]["cleared"] == 1 and result["hard_ledger"]["asserted"] == 0
    assert result["summary"] == "No changes identified."


def test_a_span_differing_only_by_transcription_noise_still_validates() -> None:
    """Curly quotes and a collapsed whitespace run are transcription noise
    (`text_fold`); the hash is of the DOCUMENT's characters, not the copy."""
    text = "Each party shall  indemnify the other against third-party claims \u2018as defined\u2019."
    docx_bytes = _docx(with_indemnity=True, indemnity_text=text)
    block_id = _block_id(docx_bytes, "Indemnification")
    cited = "Each party shall indemnify the other against third-party claims 'as defined'"
    result, _client = _run(
        docx_bytes,
        [_response(critic=True, ledger=[_cleared(block_id, cited)]), _verdict(JUDGED_RULE)],
    )
    entry = _entry(result)
    assert entry["status"] == hl.STATUS_CLEARED, entry
    real = "Each party shall  indemnify the other against third-party claims \u2018as defined\u2019"
    assert entry["span_sha256"] == hashlib.sha256(real.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# [3] A span whose hash does not validate leaves the entry asserted
# ---------------------------------------------------------------------------


def test_a_span_whose_hash_does_not_validate_stays_asserted() -> None:
    docx_bytes = _docx(with_indemnity=True)
    block_id = _block_id(docx_bytes, "Indemnification")
    # One word the document does not say: the span is not the contract's.
    forged = "Each party shall indemnify the other against all third-party claims"
    result, _client = _run(
        docx_bytes,
        [_response(critic=True, ledger=[_cleared(block_id, forged)]), _verdict(JUDGED_RULE)],
    )
    assert result["status"] == "OK", result.get("reason")
    assert result["decision"] == "REQUEST_CHANGE"
    entry = _entry(result)
    assert entry["status"] == hl.STATUS_ASSERTED
    assert entry["reason"] == hl.REASON_SPAN_HASH_MISMATCH, entry
    assert entry["span_sha256"] is None
    # The critic's block resolves, so the fallback goes after it.
    assert entry["anchor_block_id"] == block_id and entry["inserted"] is True
    assert [f["provenance"] for f in result["findings"]] == [f"ledger:{RULE_ID}"]
    assert _fallback(_opf_doc()) in _inserted_texts(result["redline_bytes"])


def test_a_copied_block_marker_is_not_part_of_the_span() -> None:
    docx_bytes = _docx(with_indemnity=True)
    block_id = _block_id(docx_bytes, "Indemnification")
    ledger = hl.build_ledger(_opf_doc(INDEMNITY_CONDITION))
    span = f"[{block_id}] Each party shall indemnify the other against third-party claims"
    critic = {"critic_delta": {"ledger_dispositions": [_cleared(block_id, span)]}}
    (entry,) = hl.evaluate_ledger(ledger, critic, _block_map(docx_bytes))
    assert entry["status"] == hl.STATUS_CLEARED, entry


def test_an_unknown_block_id_or_missing_span_stays_asserted() -> None:
    docx_bytes = _docx(with_indemnity=True)
    block_map = _block_map(docx_bytes)
    ledger = hl.build_ledger(_opf_doc(INDEMNITY_CONDITION))
    for disposition, reason in (
        (_cleared("p9999", "Each party shall indemnify"), hl.REASON_UNKNOWN_BLOCK_ID),
        (_cleared(_block_id(docx_bytes, "Indemnification"), "  "), hl.REASON_SPAN_MISSING),
    ):
        critic = {"critic_delta": {"ledger_dispositions": [disposition]}}
        (entry,) = hl.evaluate_ledger(ledger, critic, block_map)
        assert entry["status"] == hl.STATUS_ASSERTED and entry["reason"] == reason, entry


# ---------------------------------------------------------------------------
# A missing disposition is a schema failure: the review fails closed
# ---------------------------------------------------------------------------


def test_a_missing_ledger_disposition_fails_the_review_closed() -> None:
    docx_bytes = _docx(with_indemnity=False)
    silent = _response(critic=True, ledger=[])
    # The Floor judge runs after the critic pass whatever its outcome, so it
    # is seeded too; the critic's failure is what the review reports.
    result, client = _run(docx_bytes, [silent, silent, _verdict(JUDGED_RULE)])
    assert result["status"] == "ERROR", result.get("status")
    assert result["reason"] == "critic_schema_invalid", result.get("reason")
    assert result["decision"] is None and result["redline_bytes"] is None
    critic_calls = [
        call
        for call in client.calls
        if call["model_id"] == _load_bundle()["playbook"]["metadata"]["critic_model_id"]
    ]
    assert len(critic_calls) == 3, "two critic attempts (one informed retry), then the judge"
    retry_prompt = json.dumps(critic_calls[1]["user_prompt"])
    assert "ledger_dispositions has no disposition" in retry_prompt


def test_reconcile_refuses_a_missing_or_repeated_ledger_disposition() -> None:
    primary = json.loads(_response())
    for ledger in ([], [_assert(), _assert()]):
        critic = json.loads(_response(critic=True, ledger=ledger))
        try:
            recon.reconcile(
                primary_result=primary, critic_result=critic, hard_ledger_rule_ids=[RULE_ID]
            )
        except recon.ReconciliationRejected as exc:
            assert exc.reason == "critic_schema_invalid"
            assert RULE_ID in exc.last_error
        else:
            raise AssertionError(f"reconcile accepted ledger dispositions {ledger!r}")
    # One disposition each: accepted, and the raw dispositions are not forwarded.
    critic = json.loads(_response(critic=True, ledger=[_assert()]))
    merged = recon.reconcile(
        primary_result=primary, critic_result=critic, hard_ledger_rule_ids=[RULE_ID]
    )
    assert "ledger_dispositions" not in (merged.get("critic_delta") or {})


def test_the_critic_is_told_the_ledger_and_the_primary_is_not() -> None:
    docx_bytes = _docx(with_indemnity=False)
    _result, client = _run(
        docx_bytes, [_response(critic=True, ledger=[_assert()]), _verdict(JUDGED_RULE)]
    )
    metadata = _load_bundle()["playbook"]["metadata"]
    primary_prompt = json.dumps(
        [c["user_prompt"] for c in client.calls if c["model_id"] == metadata["primary_model_id"]]
    )
    critic_prompt = json.dumps(client.calls[1]["user_prompt"])
    assert "HARD-REJECTION LEDGER" in critic_prompt and RULE_ID in critic_prompt
    assert "HARD-REJECTION LEDGER" not in primary_prompt
    block = hl.render_critic_ledger_block(hl.build_ledger(_opf_doc(INDEMNITY_CONDITION)))
    assert block.endswith(
        f'  - {RULE_ID} ("Indemnification"): the clause must contain all of: '
        '"indemnify", "third-party claims".'
    ), block
    assert hl.render_critic_ledger_block([]) == ""


# ---------------------------------------------------------------------------
# The Floor judge: presence rules with a predicate leave it; judged ones only
# recommend
# ---------------------------------------------------------------------------


def test_a_deterministic_presence_rule_leaves_the_judged_floor() -> None:
    docx_bytes = _docx(with_indemnity=False)
    result, client = _run(
        docx_bytes, [_response(critic=True, ledger=[_assert()]), _verdict(JUDGED_RULE)]
    )
    judged = [v["invariant_id"] for v in result["floor_judgment"]["verdicts"]]
    assert judged == [JUDGED_RULE], judged
    assert len(client.calls) == 3, "primary, critic and ONE judge call"


def test_a_judged_presence_rule_is_recommended_on_never_cleared_by_the_judge() -> None:
    doc = _opf_doc()  # no x_condition: the manifest says "judged"
    docx_bytes = _docx(with_indemnity=False)
    result, _client = _run(
        docx_bytes,
        [
            _response(critic=True, ledger=[_assert()]),
            _verdict(JUDGED_RULE),
            _verdict(RULE_ID, violated=False),  # the judge says it is fine
        ],
        doc=doc,
    )
    entry = _entry(result)
    assert entry["judge_recommendation"] == "satisfied"
    assert entry["status"] == hl.STATUS_ASSERTED and entry["inserted"] is True
    assert result["decision"] == "REQUEST_CHANGE"
    # ...and the deterministic check that IS available -- a validated span --
    # clears a judged presence rule.
    present = _docx(with_indemnity=True)
    block_id = _block_id(present, "Indemnification")
    cleared, _client = _run(
        present,
        [
            _response(critic=True, ledger=[_cleared(block_id, "Each party shall indemnify")]),
            _verdict(JUDGED_RULE),
            _verdict(RULE_ID),
        ],
        doc=doc,
    )
    entry = _entry(cleared)
    assert entry["status"] == hl.STATUS_CLEARED and entry["predicate"] == hl.PREDICATE_PRESENCE
    assert cleared["decision"] == "ACCEPT"


def test_judged_floor_invariants_keeps_everything_else() -> None:
    doc = _opf_doc(INDEMNITY_CONDITION)
    ledger = hl.build_ledger(doc)
    floor = doc["floor"]["invariants"]
    assert [i["id"] for i in hl.judged_floor_invariants(floor, ledger)] == [JUDGED_RULE]
    judged_ledger = hl.build_ledger(_opf_doc())
    assert [i["id"] for i in hl.judged_floor_invariants(floor, judged_ledger)] == [
        JUDGED_RULE,
        RULE_ID,
    ]
    assert hl.judged_floor_invariants(floor, []) == floor


# ---------------------------------------------------------------------------
# Building the ledger
# ---------------------------------------------------------------------------


def test_only_presence_rules_open_ledger_entries() -> None:
    doc = _opf_doc(INDEMNITY_CONDITION)
    ledger = hl.build_ledger(doc)
    assert [(e["rule_id"], e["status"], e["clause_id"]) for e in ledger] == [
        (RULE_ID, hl.STATUS_UNPROVEN, "clause.indemnification")
    ]
    assert ledger[0]["title"] == "Indemnification" and ledger[0]["order"] == (8,)
    assert hl.build_ledger(json.loads(OPF_FIXTURE.read_text(encoding="utf-8"))) == []
    assert hl.build_ledger({}) == [] and hl.build_ledger(None) == []


def test_fixture_manifest_is_what_the_engine_builds() -> None:
    """The FIXTURE RULE: the manifest this suite reviews against is the one
    the engine's own builder produces for the same Floor. Runs wherever the
    read-only sibling engine checkout is importable; the mirror above is the
    stand-in everywhere else."""
    if not (ENGINE_CHECKOUT / "playbook_engine" / "dossiers.py").is_file():
        print("  (engine checkout not present; manifest cross-check skipped)")
        return
    sys.path.insert(0, str(ENGINE_CHECKOUT))
    try:
        from playbook_engine.dossiers import build_manifest  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        print(f"  (engine not importable: {type(exc).__name__}; cross-check skipped)")
        return
    finally:
        sys.path.remove(str(ENGINE_CHECKOUT))
    for condition in (INDEMNITY_CONDITION, None):
        doc = _opf_doc(condition)
        assert build_manifest(doc) == doc["manifest"], condition
    for standard in (LONG_STANDARD, BANNED_PHRASE_STANDARD):
        doc = _opf_doc(INDEMNITY_CONDITION, standard=standard)
        assert build_manifest(doc) == doc["manifest"], standard[:40]


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def _entry_with(condition: Any, reference_title: str | None = None) -> dict[str, Any]:
    return {"rule_id": "R-1", "condition": condition, "reference_title": reference_title}


def test_required_phrases_any_and_all() -> None:
    all_of = _entry_with({"type": "required_phrases", "phrases": ["State of Delaware", "exclusive"]})
    any_of = _entry_with(
        {"type": "required_phrases", "phrases": ["State of Delaware", "State of New York"], "match": "any"}
    )
    text = "The courts of the  STATE OF DELAWARE have exclusive jurisdiction."
    assert hl.evaluate_predicate(all_of, text) is True
    assert hl.evaluate_predicate(all_of, "The courts of the State of Delaware.") is False
    assert hl.evaluate_predicate(any_of, "The courts of the State of New York.") is True
    assert hl.evaluate_predicate(any_of, "The courts of the State of Texas.") is False
    assert hl.evaluate_predicate(_entry_with({"type": "required_phrases", "phrases": []}), text) is None


def test_numeric_bound() -> None:
    cap = _entry_with({"type": "numeric_bound", "pattern": "([0-9]+) years", "max": 5, "unit": "years"})
    assert hl.evaluate_predicate(cap, "This Agreement runs for 3 years.") is True
    assert hl.evaluate_predicate(cap, "This Agreement runs for 7 years.") is False
    assert hl.evaluate_predicate(cap, "3 years, renewable once for 9 years.") is False
    assert hl.evaluate_predicate(cap, "This Agreement runs indefinitely.") is False
    floor = _entry_with({"type": "numeric_bound", "pattern": "\\$([0-9,]+)", "min": 100000})
    assert hl.evaluate_predicate(floor, "capped at $150,000 per year") is True
    assert hl.evaluate_predicate(floor, "capped at $50,000 per year") is False
    for broken in ("([0-9]+", "[0-9]+ years", "([0-9]+) (years)"):
        bad = _entry_with({"type": "numeric_bound", "pattern": broken, "max": 5})
        assert hl.evaluate_predicate(bad, "3 years") is None, broken
    # A clause too long to run the pattern over is not evaluable -- never
    # truncated, which would hide every number past the cut.
    long_clause = "x" * hl.MAX_CLAUSE_CHARS + " 9 years"
    assert hl.evaluate_predicate(cap, long_clause) is None


def test_cross_reference() -> None:
    entry = _entry_with({"type": "cross_reference", "clause_id": "clause.term"}, "Term")
    assert hl.evaluate_predicate(entry, "Sections survive the end of the Term.") is True
    assert hl.evaluate_predicate(entry, "Sections survive the termination.") is False
    assert hl.evaluate_predicate(_entry_with({"type": "cross_reference", "clause_id": "x"}), "Term") is None


def test_judged_is_presence_and_unknown_kinds_fail_closed() -> None:
    assert hl.evaluate_predicate(_entry_with("judged"), "Any present words.") is True
    assert hl.evaluate_predicate(_entry_with({"type": "regex_soup"}), "words") is None


# ---------------------------------------------------------------------------
# Enforcement: anchors, de-duplication, sentences
# ---------------------------------------------------------------------------


def test_insertion_anchor_branches() -> None:
    docx_bytes = _docx(with_indemnity=True)
    block_map = _block_map(docx_bytes)
    (entry,) = hl.build_ledger(_opf_doc(INDEMNITY_CONDITION))
    indemnity = _block_id(docx_bytes, "Indemnification")
    # 1. the critic's resolvable block -- only a block of the clause itself.
    #    Two blocks under the Indemnification heading: naming the FIRST puts
    #    the insertion after it, not at the section's end...
    two = {
        "p0001": {"index": 0, "heading": "Indemnification", "text": "First paragraph."},
        "p0002": {"index": 1, "heading": "Indemnification", "text": "Second paragraph."},
        "p0003": {"index": 2, "heading": "Governing Law", "text": GOVERNING_LAW},
    }
    assert hl.insertion_anchor({**entry, "block_id": "p0001"}, two) == "p0001"
    #    ...while a block of any other clause is ignored: the clause's own
    #    section end, exactly as if the critic had named nothing.
    assert hl.insertion_anchor({**entry, "block_id": "p0003"}, two) == "p0002"
    assert hl.insertion_anchor({**entry, "block_id": None}, two) == "p0002"
    assert hl.insertion_anchor({**entry, "block_id": _block_id(docx_bytes, "Term and Termination")}, block_map) == indemnity
    #    An entry with no title binds no block: the critic's is never honoured.
    assert hl.insertion_anchor({**entry, "title": None, "block_id": "p0001"}, two) == "p0002"
    # 2. the clause's own section
    assert hl.insertion_anchor({**entry, "block_id": None}, block_map) == indemnity
    # 3a. absent, present sections precede it in template order: the nearest (Confidentiality, 9) wins
    term_like = {**entry, "block_id": None, "title": "Missing", "clause_id": "clause.missing", "order": (10,)}
    assert hl.insertion_anchor(term_like, block_map) == _block_id(docx_bytes, "Confidentiality")
    # 3b. absent, nothing precedes it: just ahead of the first section after it
    absent = _block_map(_docx(with_indemnity=False))
    earliest = {**term_like, "order": (8,)}
    assert hl.insertion_anchor(earliest, absent) == _block_id(_docx(with_indemnity=False), "Definitions")
    only_confidentiality = {
        block_id: block for block_id, block in absent.items() if block["heading"] != "Definitions"
    }
    assert hl.insertion_anchor(earliest, only_confidentiality) == "start"
    # 4. no order and no heading: the document's last block
    assert hl.insertion_anchor({**term_like, "order": None}, block_map) == _block_id(docx_bytes, "Governing Law")
    assert hl.insertion_anchor(term_like, {}) is None


def _asserted(rule_id: str, clause_id: str = "clause.indemnification", reason: str = hl.REASON_CRITIC_ASSERTED) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "clause_id": clause_id,
        "taxonomy_id": "indemnification",
        "title": "Indemnification",
        "condition": "judged",
        "fallback_language": "Each party shall indemnify the other.",
        "order": (8,),
        "sections": [],
        "status": hl.STATUS_ASSERTED,
        "reason": reason,
        "block_id": None,
        "span_sha256": None,
        "predicate": hl.PREDICATE_PRESENCE,
    }


def test_two_asserted_rules_on_one_clause_insert_once_and_count_two() -> None:
    block_map = _block_map(_docx(with_indemnity=False))
    reconciled = {
        "decision": "REQUEST_CHANGE",
        "issues": [{"issue_key": "I1", "provenance": "model"}],
        "block_ops": [],
        "verdict_summary": "One clause needs work.",
    }
    result, record = hl.enforce(
        reconciled,
        [_asserted("IND-004"), _asserted("IND-005", reason=hl.REASON_SPAN_HASH_MISMATCH)],
        block_map,
    )
    assert record["asserted"] == 2 and record["inserted"] == 1, record
    keys = [issue["issue_key"] for issue in result["issues"]]
    assert keys == ["I1", "I2", "I3"], keys
    assert [op["issue_key"] for op in result["block_ops"]] == ["I2"]
    assert [i["provenance"] for i in result["issues"][1:]] == ["ledger:IND-004", "ledger:IND-005"]
    assert result["issues"][2]["external_rationale_for_footnote"] == (
        "This protection was not found in the agreement and must be added."
    )
    assert result["verdict_summary"] == (
        "One clause needs work. 2 required protections were not found in the agreement: "
        "1 is asserted in the redline and 1 is flagged only."
    ), result["verdict_summary"]
    assert result["issues"][2]["internal_rationale_for_footnote"] == (
        "Hard-rejection ledger rule IND-005 was not proven present: the contract text the "
        "second review cited does not match the agreement; its clause's standard language "
        "is already proposed by another edit in this review, so it is flagged only."
    )
    assert reconciled["issues"] == [{"issue_key": "I1", "provenance": "model"}], "input mutated"


def test_the_summary_sentences_are_exact() -> None:
    assert hl.ledger_summary_note(0) is None
    assert hl.ledger_summary_note(1, 1) == (
        "1 required protection was not found in the agreement and is asserted in the redline."
    )
    assert hl.ledger_summary_note(3, 3) == (
        "3 required protections were not found in the agreement and are asserted in the redline."
    )
    # Nothing inserted: the summary never claims a redline insertion.
    assert hl.ledger_summary_note(1, 0) == (
        "1 required protection was not found in the agreement and is flagged only; no "
        "standard language was inserted for it."
    )
    assert hl.ledger_summary_note(2) == (
        "2 required protections were not found in the agreement and are flagged only; no "
        "standard language was inserted for them."
    )
    # Mixed, with counts of one and of two or more on each side.
    assert hl.ledger_summary_note(2, 1) == (
        "2 required protections were not found in the agreement: 1 is asserted in the "
        "redline and 1 is flagged only."
    )
    assert hl.ledger_summary_note(5, 2) == (
        "5 required protections were not found in the agreement: 2 are asserted in the "
        "redline and 3 are flagged only."
    )
    assert hl.internal_note("IND-004", hl.REASON_CRITIC_ASSERTED, hl.OUTCOME_INSERTED) == (
        "Hard-rejection ledger rule IND-004 was not proven present: the second review did "
        "not find it in the agreement; its standard language was inserted."
    )
    assert hl.internal_note("IND-004", hl.REASON_SPAN_OUTSIDE_CLAUSE, hl.OUTCOME_FLAG_ONLY) == (
        "Hard-rejection ledger rule IND-004 was not proven present: the contract text the "
        "second review cited is not in the clause the rule names; it has no standard "
        "language to insert, so it is flagged only."
    )


def test_a_critic_insertion_of_the_same_language_is_not_duplicated() -> None:
    block_map = _block_map(_docx(with_indemnity=False))
    reconciled = {
        "decision": "REQUEST_CHANGE",
        "issues": [{"issue_key": "I1"}],
        "block_ops": [
            {
                "op": "insert_block_after",
                "anchor_block_id": "start",
                "new_text": "Each party shall  indemnify the other.",
                "issue_key": "I1",
            }
        ],
        "verdict_summary": "Restored the indemnity.",
    }
    result, record = hl.enforce(reconciled, [_asserted("IND-004")], block_map)
    assert record["asserted"] == 1 and record["inserted"] == 0
    assert len(result["block_ops"]) == 1


def test_a_cleared_ledger_changes_nothing() -> None:
    reconciled = {"decision": "ACCEPT", "issues": [], "verdict_summary": "Fine."}
    cleared = {**_asserted("IND-004"), "status": hl.STATUS_CLEARED, "reason": hl.REASON_CLEARED}
    result, record = hl.enforce(reconciled, [cleared], {})
    assert result is reconciled and record["cleared"] == 1 and record["asserted"] == 0
    assert hl.enforce(reconciled, [], {}) == (reconciled, None)


def test_the_internal_footnote_names_the_rule_and_the_reason() -> None:
    docx_bytes = _docx(with_indemnity=False)
    result, _client = _run(
        docx_bytes,
        [_response(critic=True, ledger=[_assert()]), _verdict(JUDGED_RULE)],
        notes_mode="internal",
    )
    assert result["status"] == "OK", result.get("reason")
    footnotes = _part(result["redline_bytes"], "word/footnotes.xml")
    assert f"Hard-rejection ledger rule {RULE_ID} was not proven present" in footnotes, footnotes[:400]
    assert _opf_doc(INDEMNITY_CONDITION)["floor"]["invariants"][-1]["statement"] not in footnotes


def test_a_rule_with_no_standard_language_never_claims_an_insertion() -> None:
    """End to end: a manifest rule whose `fallback_language` is null (its
    taxonomy has no evidence clause, so `clause_id` is null too -- OPF-SPEC
    §3.12.3 allows both). Nothing is inserted, so no redline is delivered,
    and the summary says the protection is flagged, not asserted in a
    redline that does not exist."""
    doc = _opf_doc(
        INDEMNITY_CONDITION,
        extra=(_invariant("LOL-001", INDEMNITY_CONDITION, "limitation_of_liability"),),
    )
    doc["floor"]["invariants"] = [
        i for i in doc["floor"]["invariants"] if i["id"] != RULE_ID
    ]
    doc["manifest"] = _build_manifest(doc)
    (rule,) = [r for r in doc["manifest"]["hard_rules"] if r["rule_id"] == "LOL-001"]
    assert rule["fallback_language"] is None and rule["clause_id"] is None, rule
    docx_bytes = _docx(with_indemnity=True)
    result, _client = _run(
        docx_bytes,
        [_response(critic=True, ledger=[_assert("LOL-001")]), _verdict(JUDGED_RULE)],
        doc=doc,
        notes_mode="internal",
    )
    assert result["status"] == "OK", (result.get("status"), result.get("reason"))
    assert result["decision"] == "REQUEST_CHANGE"
    assert result["redline_bytes"] is None
    record = result["hard_ledger"]
    assert record["asserted"] == 1 and record["inserted"] == 0, record
    assert result["summary"] == (
        "Changes are requested: 1 required protection was not found in the agreement and "
        "is flagged only; no standard language was inserted for it."
    ), result["summary"]
    assert "asserted in the redline" not in result["summary"]
    assert [f["provenance"] for f in result["findings"]] == ["ledger:LOL-001"]


def test_every_asserted_rule_on_one_clause_reaches_an_internal_footnote() -> None:
    """End to end: two presence rules on the Indemnification clause, both
    asserted. One insertion (one clause's standard is never inserted twice),
    and BOTH rule ids and reasons reach the delivered document's internal
    footnotes -- the second rides on the first's footnote. A third rule with
    no standard language rides there too."""
    doc = _opf_doc(
        INDEMNITY_CONDITION,
        extra=(
            _invariant("IND-005", INDEMNITY_CONDITION),
            _invariant("LOL-001", INDEMNITY_CONDITION, "limitation_of_liability"),
        ),
    )
    docx_bytes = _docx(with_indemnity=False)
    forged = _cleared(_block_id(docx_bytes, "Governing Law"), GOVERNING_LAW, rule_id="IND-005")
    result, _client = _run(
        docx_bytes,
        [
            _response(critic=True, ledger=[_assert(), forged, _assert("LOL-001")]),
            _verdict(JUDGED_RULE),
        ],
        doc=doc,
        notes_mode="internal",
    )
    assert result["status"] == "OK", (result.get("status"), result.get("reason"))
    record = result["hard_ledger"]
    assert [(e["rule_id"], e["inserted"]) for e in record["entries"]] == [
        (RULE_ID, True),
        ("IND-005", False),
        ("LOL-001", False),
    ], record
    assert record["asserted"] == 3 and record["inserted"] == 1
    assert result["summary"] == (
        "Changes are requested: 3 required protections were not found in the agreement: "
        "1 is asserted in the redline and 2 are flagged only."
    ), result["summary"]
    redline = result["redline_bytes"]
    assert _inserted_texts(redline).count(_fallback(_opf_doc())) == 1
    footnotes = _part(redline, "word/footnotes.xml")
    for rule_id in (RULE_ID, "IND-005", "LOL-001"):
        assert f"Hard-rejection ledger rule {rule_id} was not proven present" in footnotes, (
            rule_id,
            footnotes[:600],
        )
    assert "is not in the clause the rule names" in footnotes
    assert "already proposed by another edit in this review" in footnotes
    # External mode carries no internal note at all.
    external, _client = _run(
        docx_bytes,
        [
            _response(critic=True, ledger=[_assert(), forged, _assert("LOL-001")]),
            _verdict(JUDGED_RULE),
        ],
        doc=doc,
    )
    assert "Hard-rejection ledger rule" not in _part(external["redline_bytes"], "word/footnotes.xml")


# ---------------------------------------------------------------------------
# Issue #164: the pen rules do not gate the playbook's own standard language
# ---------------------------------------------------------------------------


def _long_term_edit_response(docx_bytes: bytes, ledger: list[dict[str, Any]], insert: str) -> str:
    """`_term_edit_response` with a MODEL-DRAFTED insertion of `insert` in
    place of "sixty (60)" -- the critic's own language, which the pen rules
    still judge."""
    body = json.loads(_term_edit_response(docx_bytes, ledger))
    (segment,) = [s for s in body["block_patches"][0]["segments"] if s["op"] == "insert"]
    segment["text"] = insert
    return json.dumps(body)


def _pen_refusal(result: dict[str, Any], section: str) -> dict[str, Any]:
    """The one `changes_not_applied` entry, for a pen-refused issue in `section`."""
    not_applied = (result.get("analysis_report") or {}).get("changes_not_applied") or []
    assert [(c["section_ref"], c["reason"]) for c in not_applied] == [
        (section, "derived_replacement_text_rejected")
    ], not_applied
    return not_applied[0]


def test_a_ledger_only_review_inserts_a_standard_the_pen_rules_would_refuse() -> None:
    """Issue #164. An OPF review's pen rules are the toaster defaults
    (`max_chars` 1500, `must_not_introduce` "perpetual" ...), which bound
    language a MODEL drafts. The ledger's fallback is the playbook's own
    `our_standard.text`, inserted by code, so they do not gate it: a review
    whose only edit is that insertion -- a standard over 1,500 characters, or
    one carrying a banned phrase -- delivers it, DONE / REQUEST_CHANGE,
    counted inserted by the record and the summary. (Before #164 the pen
    rules refused it and the review ended ERROR / block_edits_not_applied
    with the protection undelivered.)"""
    import replacement_text_enforcement as rte

    defaults = rte.resolve_pen_rules(None, "clause.indemnification")
    assert len(LONG_STANDARD) > defaults["max_chars"] == 1500, defaults
    assert "perpetual" in [entry["phrase"] for entry in defaults["must_not_introduce"]], defaults
    for standard in (LONG_STANDARD, BANNED_PHRASE_STANDARD):
        doc = _opf_doc(INDEMNITY_CONDITION, standard=standard)
        assert _fallback(doc) == standard
        docx_bytes = _docx(with_indemnity=False)
        result, _client = _run(
            docx_bytes,
            [_response(critic=True, ledger=[_assert()]), _verdict(JUDGED_RULE)],
            doc=doc,
            notes_mode="internal",
        )
        assert result["status"] == "OK", (result.get("status"), result.get("reason"))
        assert result["decision"] == "REQUEST_CHANGE"
        assert [f["provenance"] for f in result["findings"]] == [f"ledger:{RULE_ID}"]
        redline = result["redline_bytes"]
        assert redline, "the ledger's insertion was not delivered"
        assert _in_order(_texts(redline, "word/document.xml"), DEFINITIONS, standard, CONFIDENTIALITY)
        assert standard.split(". ")[0] in _inserted_texts(redline), "standard is not a tracked insertion"
        not_applied = (result.get("analysis_report") or {}).get("changes_not_applied") or []
        assert not_applied == [], not_applied
        record = result["hard_ledger"]
        assert record["asserted"] == 1 and record["inserted"] == 1, record
        entry = _entry(result)
        assert entry["inserted"] is True, entry
        assert entry["anchor_block_id"] == _block_id(docx_bytes, "Definitions"), entry
        assert result["summary"] == (
            "Changes are requested: 1 required protection was not found in the agreement "
            "and is asserted in the redline."
        ), result["summary"]
        footnotes = _texts(redline, "word/footnotes.xml")
        assert "its standard language was inserted" in footnotes, footnotes
        assert "did not pass the redline's checks" not in footnotes, footnotes


def test_model_drafted_text_is_still_pen_checked_beside_a_ledger_insertion() -> None:
    """Issue #164's other half: the exemption is the ledger's alone. In the
    same review as a ledger insertion of an over-long standard, the critic's
    own edit -- over 1,500 characters, or carrying "perpetual" -- is still
    refused by the pen rules (`max_chars_exceeded` /
    `must_not_introduce_violation`) and never reaches the document, while the
    ledger's standard is delivered."""
    import replacement_text_enforcement as rte

    long_insert = " ".join(
        f"sixty (60) days' written notice, clause {n}, delivered to the other party's notice address"
        for n in range(1, 25)
    )
    assert len(long_insert) > 1500
    for insert, failure in (
        (long_insert, rte.MAX_CHARS_EXCEEDED),
        ("sixty (60), and this right is perpetual,", rte.MUST_NOT_INTRODUCE_VIOLATION),
    ):
        doc = _opf_doc(INDEMNITY_CONDITION, standard=LONG_STANDARD)
        docx_bytes = _docx(with_indemnity=False)
        result, _client = _run(
            docx_bytes,
            [_long_term_edit_response(docx_bytes, [_assert()], insert), _verdict(JUDGED_RULE)],
            doc=doc,
            notes_mode="internal",
        )
        assert result["status"] == "OK", (failure, result.get("status"), result.get("reason"))
        assert result["decision"] == "REQUEST_CHANGE"
        redline = result["redline_bytes"]
        assert redline, "the ledger's insertion was not delivered"
        document = _texts(redline, "word/document.xml")
        assert LONG_STANDARD.split(". ")[0] in _inserted_texts(redline)
        assert "sixty (60)" not in document, "the refused model-drafted edit was delivered"
        assert TERM in document, "the refused edit's deletion was not rolled back"
        assert _pen_refusal(result, "Term and Termination")["patch_reasons"][0]["reason"] == failure
        record = result["hard_ledger"]
        assert record["asserted"] == 1 and record["inserted"] == 1, record
        assert _entry(result)["issue_key"] == "I2"


def test_only_ledger_provenance_is_exempt_from_the_pen_rules() -> None:
    """`redline_generate.ledger_issue_keys` names exactly the issues whose
    provenance is the ledger's -- never a model's, a detector's, the Floor's
    or a retained primary's, and never an issue without a key."""
    import redline_generate as rg

    issues = [
        {"issue_key": "I1", "provenance": "model"},
        {"issue_key": "I2", "provenance": f"ledger:{RULE_ID}"},
        {"issue_key": "I3", "provenance": "critic-added"},
        {"issue_key": "I4", "provenance": "detector:no-uncapped-liability"},
        {"issue_key": "I5", "provenance": "floor:no-uncapped-liability"},
        {"issue_key": "I6", "provenance": "primary-retained"},
        {"issue_key": "I7"},
        {"issue_key": "", "provenance": "ledger:OTHER"},
        {"issue_key": "I8", "provenance": "ledger:OTHER-001"},
        "not an issue",
    ]
    assert rg.ledger_issue_keys(issues) == {"I2", "I8"}
    # No model can carry the marker: the output schema's provenance pattern
    # admits no `ledger:` value.
    import re

    schema = json.loads((REPO_ROOT / "playbooks" / "output-schema-v3.json").read_text(encoding="utf-8"))
    pattern = schema["definitions"]["Issue"]["properties"]["provenance"]["pattern"]
    assert re.match(pattern, f"ledger:{RULE_ID}") is None, pattern
    assert re.match(pattern, "ledger:ind-004") is None, pattern


def test_a_rolled_back_ledger_insertion_is_noted_as_not_inserted() -> None:
    """Stage 5 can still refuse the ledger's insertion through a gate other
    than the pen rules (a compile failure of its op, after which it is rolled
    out of the compile). Its hosted internal note then
    says it did not pass the redline's checks -- never that it was inserted."""
    import redline_generate as rg

    note = hl.internal_note(RULE_ID, hl.REASON_CRITIC_ASSERTED, hl.OUTCOME_INSERTED)
    issues = [
        {
            "issue_key": "I2",
            "provenance": f"ledger:{RULE_ID}",
            "section_ref": "Indemnification",
            rg.INTERNAL_RATIONALE_FIELD: note,
        }
    ]
    insertion = {
        "op": "insert_block_after",
        "anchor_block_id": "b0001",
        "new_text": LONG_STANDARD,
        "issue_key": "I2",
    }
    proven = {"blocks": [], "block_ops": [insertion], "by_issue": {"I2": {}}}
    working = {"blocks": [], "block_ops": [], "by_issue": {}}
    assert rg._ledger_flag_only_notes(issues, proven, proven) == []
    (rolled_back,) = rg._ledger_flag_only_notes(issues, working, proven)
    assert rolled_back["text"].endswith(rg._LEDGER_REFUSED_OUTCOME), rolled_back
    assert rg._LEDGER_INSERTED_OUTCOME not in rolled_back["text"], rolled_back


TABLE_INDEMNITY = "Indemnity obligations are set out in the schedule."


def _table_indemnity_docx() -> bytes:
    """A synthetic agreement whose Indemnification section is a one-cell
    table that does not meet the rule's test. `insertion_anchor` places the
    fallback after the section's last block -- the cell's paragraph, which
    the production block map heads "Indemnification" -- and a whole-block op
    on a table-cell paragraph is a per-edit compile failure
    (`redline_block_apply.REASON_BLOCK_OP_UNSUPPORTED_IN_TABLE`)."""
    table = (
        "<w:tbl><w:tr><w:tc>"
        f"<w:p><w:r><w:t>{TABLE_INDEMNITY}</w:t></w:r></w:p>"
        "</w:tc></w:tr></w:tbl>"
    )
    body = (
        _heading_p("Definitions")
        + _body_p(DEFINITIONS)
        + _heading_p("Confidentiality")
        + _body_p(CONFIDENTIALITY)
        + _heading_p("Indemnification")
        + table
        + _heading_p("Term and Termination")
        + _body_p(TERM)
        + _heading_p("Governing Law")
        + _body_p(GOVERNING_LAW)
    )
    return _build_docx_bytes(body)


def test_a_ledger_insertion_that_fails_to_compile_is_re_hosted_or_fails_closed() -> None:
    """Issue #165. The ledger's insertion hits a per-edit compile failure
    (its anchor is a table-cell paragraph), so it lands nothing and has no
    `<w:ins>` for its own internal footnote. It is rolled out of the compile
    like a stranded host, and its note -- rule id, reason, and that the
    standard language was NOT inserted -- re-hosts on the critic's own
    insertion. Before #165 it hosted nothing, so it was never rolled out and
    its rule id reached no footnote. With no insertion left to host it --
    no other edit, or only pure deletions -- the review fails closed in a
    notes mode with internal content (ERROR / block_edits_not_applied), the
    record still naming the rule; external mode renders no internal note,
    so it delivers the deletions. The same holds when the protection's
    carrier is the critic's copy standing in for the ledger's insertion
    (`OUTCOME_COVERED`) and that copy is the op refused at the cell."""
    import redline_block_apply

    docx_bytes = _table_indemnity_docx()
    cell = _block_id(docx_bytes, "Indemnification")
    assert _block_map(docx_bytes)[cell]["text"] == TABLE_INDEMNITY
    refused_note = hl.internal_note(RULE_ID, hl.REASON_CRITIC_ASSERTED, hl.OUTCOME_REFUSED)

    result, _client = _run(
        docx_bytes,
        [_term_edit_response(docx_bytes, [_assert()]), _verdict(JUDGED_RULE)],
        notes_mode="internal",
    )
    assert result["status"] == "OK", (result.get("status"), result.get("reason"))
    assert result["decision"] == "REQUEST_CHANGE"
    redline = result["redline_bytes"]
    assert "sixty (60)" in _inserted_texts(redline), "the critic's own edit was not delivered"
    assert _fallback(_opf_doc()) not in _texts(redline, "word/document.xml")
    footnotes = _texts(redline, "word/footnotes.xml")
    assert f"Hard-rejection ledger entry for Indemnification: {refused_note}" in footnotes, footnotes
    assert "its standard language was inserted" not in footnotes, footnotes
    entry = _entry(result)
    assert (entry["inserted"], entry["anchor_block_id"], entry["issue_key"]) == (False, None, "I2")
    assert result["hard_ledger"]["inserted"] == 0
    not_applied = (result.get("analysis_report") or {}).get("changes_not_applied") or []
    assert [(c["section_ref"], c["reason"]) for c in not_applied] == [
        ("Indemnification", redline_block_apply.REASON_BLOCK_OP_UNSUPPORTED_IN_TABLE)
    ], not_applied
    assert result["summary"] == (
        f"{TERM_SUMMARY} 1 required protection was not found in the agreement and is "
        "flagged only; no standard language was inserted for it."
    ), result["summary"]

    # External mode renders no internal note at all; the document is the same.
    external, _client = _run(
        docx_bytes,
        [_term_edit_response(docx_bytes, [_assert()]), _verdict(JUDGED_RULE)],
    )
    assert external["status"] == "OK", (external.get("status"), external.get("reason"))
    assert "Hard-rejection ledger" not in _texts(external["redline_bytes"], "word/footnotes.xml")
    assert "sixty (60)" in _inserted_texts(external["redline_bytes"])

    # The dedupe path: the critic's faithful copy of the standard covers the
    # ledger's own insertion (`OUTCOME_COVERED`) and then fails to compile
    # at the same cell. The covered entry's note still reaches the footnotes
    # on the Term edit.
    body = json.loads(_term_edit_response(docx_bytes, [_assert()]))
    copy = json.loads(_restoration_response(docx_bytes, [_assert()], _fallback(_opf_doc())))
    (copy_issue,) = copy["issues"]
    (copy_op,) = copy["block_ops"]
    copy_issue["issue_key"] = copy_op["issue_key"] = "I3"
    copy_op["anchor_block_id"] = cell
    body["issues"].append(copy_issue)
    body["block_ops"].append(copy_op)
    covered, _client = _run(
        docx_bytes, [json.dumps(body), _verdict(JUDGED_RULE)], notes_mode="internal"
    )
    assert covered["status"] == "OK", (covered.get("status"), covered.get("reason"))
    covered_note = hl.internal_note(RULE_ID, hl.REASON_CRITIC_ASSERTED, hl.OUTCOME_COVERED)
    assert covered_note in _texts(covered["redline_bytes"], "word/footnotes.xml")
    assert _entry(covered)["inserted"] is False

    # The dedupe path beside deletions alone: the critic's copy stands in
    # for the ledger's insertion and is refused at the cell, and the only
    # other edit is a pure deletion. The ledger's own issue never failed --
    # it authored no edit -- yet no insertion is left to host the covered
    # entry's note, so a notes mode with internal content fails closed
    # rather than deliver the deletion with the rule id in no footnote.
    covered_deletion = json.loads(_term_edit_response(docx_bytes, [_assert()]))
    (covered_patch,) = covered_deletion["block_patches"]
    covered_patch["segments"] = [
        segment for segment in covered_patch["segments"] if segment["op"] != "insert"
    ]
    covered_deletion["issues"].append(copy_issue)
    covered_deletion["block_ops"].append(copy_op)
    for mode in ("internal", "both"):
        refused_copy, _client = _run(
            docx_bytes, [json.dumps(covered_deletion), _verdict(JUDGED_RULE)], notes_mode=mode
        )
        assert (refused_copy["status"], refused_copy.get("reason")) == (
            "ERROR",
            "block_edits_not_applied",
        ), (mode, refused_copy.get("status"), refused_copy.get("reason"))
        assert not refused_copy.get("redline_bytes"), mode
        entry = _entry(refused_copy)
        assert (entry["status"], entry["inserted"]) == (hl.STATUS_ASSERTED, False), (mode, entry)
        assert refused_copy["hard_ledger"]["inserted"] == 0, mode
        not_applied = (refused_copy.get("analysis_report") or {}).get("changes_not_applied") or []
        assert (
            "Indemnification",
            redline_block_apply.REASON_BLOCK_OP_UNSUPPORTED_IN_TABLE,
        ) in [(c["section_ref"], c["reason"]) for c in not_applied], (mode, not_applied)
    # External mode carries no internal note to lose: the deletion ships.
    covered_external, _client = _run(
        docx_bytes, [json.dumps(covered_deletion), _verdict(JUDGED_RULE)]
    )
    assert covered_external["status"] == "OK", (
        covered_external.get("status"),
        covered_external.get("reason"),
    )
    assert covered_external["redline_bytes"]
    assert _fallback(_opf_doc()) not in _texts(covered_external["redline_bytes"], "word/document.xml")
    assert _entry(covered_external)["inserted"] is False

    # Deletions only beside the refusal: a footnote hangs only on a
    # `<w:ins>`, so nothing can host the note and the review fails closed
    # rather than deliver the deletion with the rule id in no footnote.
    deletion = json.loads(_term_edit_response(docx_bytes, [_assert()]))
    (term_patch,) = deletion["block_patches"]
    term_patch["segments"] = [
        segment for segment in term_patch["segments"] if segment["op"] != "insert"
    ]
    deletions_only, _client = _run(
        docx_bytes, [json.dumps(deletion), _verdict(JUDGED_RULE)], notes_mode="internal"
    )
    assert (deletions_only["status"], deletions_only.get("reason")) == (
        "ERROR",
        "block_edits_not_applied",
    ), deletions_only
    assert not deletions_only.get("redline_bytes")
    entry = _entry(deletions_only)
    assert (entry["status"], entry["inserted"]) == (hl.STATUS_ASSERTED, False), entry
    assert deletions_only["hard_ledger"]["inserted"] == 0
    not_applied = (deletions_only.get("analysis_report") or {}).get("changes_not_applied") or []
    assert [(c["section_ref"], c["reason"]) for c in not_applied] == [
        ("Indemnification", redline_block_apply.REASON_BLOCK_OP_UNSUPPORTED_IN_TABLE)
    ], not_applied
    # External mode carries no internal note to lose: the deletion ships.
    deletions_external, _client = _run(
        docx_bytes, [json.dumps(deletion), _verdict(JUDGED_RULE)]
    )
    assert deletions_external["status"] == "OK", (
        deletions_external.get("status"),
        deletions_external.get("reason"),
    )
    assert deletions_external["redline_bytes"]
    assert _entry(deletions_external)["inserted"] is False

    # Nothing else to host it: the review fails closed, the rule recorded.
    alone, _client = _run(
        docx_bytes,
        [_response(critic=True, ledger=[_assert()]), _verdict(JUDGED_RULE)],
        notes_mode="internal",
    )
    assert (alone["status"], alone.get("reason")) == ("ERROR", "block_edits_not_applied"), alone
    assert not alone.get("redline_bytes")
    entry = _entry(alone)
    assert (entry["status"], entry["inserted"]) == (hl.STATUS_ASSERTED, False), entry
    assert alone["hard_ledger"]["asserted"] == 1 and alone["hard_ledger"]["inserted"] == 0


def _ledger_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The `changes_not_applied` rows for the ledger's own issue (`I2`, the
    one fresh key `hard_ledger.enforce` gives it beside the critic's `I1`),
    found by the section and the fixed summary only a ledger issue carries."""
    not_applied = (result.get("analysis_report") or {}).get("changes_not_applied") or []
    return [
        row
        for row in not_applied
        if row["section_ref"] == "Indemnification"
        and row["counterparty_change_summary"] == hl._CHANGE_SUMMARY
    ]


def test_a_ledger_insertion_that_fails_to_compile_is_not_reported_as_inserted() -> None:
    """Issue #169 (reproduced before the fix). The ledger issue's own
    `external_rationale_for_footnote` says its standard language "has been
    inserted". When stage 5 refuses that insertion (a table-cell anchor,
    `block_op_unsupported_in_table`), the issue is listed under
    `analysis_report.changes_not_applied` -- and before #169 that row kept
    the claim, so the report stated a change the document does not carry.
    The row now carries fixed copy saying the standard language could not
    be inserted, in every notes mode, whether the document is delivered
    beside another edit or the review ends ERROR with nothing to deliver.
    A non-ledger issue refused the same way keeps its own rationale, and a
    delivered ledger insertion lists no row at all."""
    import redline_block_apply
    import redline_generate as rg

    assert rg._LEDGER_NOT_INSERTED_RATIONALE == hl._EXTERNAL_RATIONALE_NOT_INSERTED
    assert "could not be inserted" in hl._EXTERNAL_RATIONALE_NOT_INSERTED
    assert "has been inserted" in hl._EXTERNAL_RATIONALE_INSERTED

    docx_bytes = _table_indemnity_docx()
    cell = _block_id(docx_bytes, "Indemnification")
    beside_term = [_term_edit_response(docx_bytes, [_assert()]), _verdict(JUDGED_RULE)]
    alone = [_response(critic=True, ledger=[_assert()]), _verdict(JUDGED_RULE)]
    cases = [
        ("external, beside the Term edit", beside_term, {}, "OK"),
        ("internal, beside the Term edit", beside_term, {"notes_mode": "internal"}, "OK"),
        ("both, beside the Term edit", beside_term, {"notes_mode": "both"}, "OK"),
        ("internal, the only edit", alone, {"notes_mode": "internal"}, "ERROR"),
        ("external, the only edit", alone, {}, "ERROR"),
    ]
    for label, responses, kwargs, status in cases:
        result, _client = _run(docx_bytes, list(responses), **kwargs)
        assert result["status"] == status, (label, result.get("status"), result.get("reason"))
        assert _entry(result)["inserted"] is False, label
        rows = _ledger_rows(result)
        assert [row["reason"] for row in rows] == [
            redline_block_apply.REASON_BLOCK_OP_UNSUPPORTED_IN_TABLE
        ], (label, rows)
        (row,) = rows
        assert row["external_rationale_for_footnote"] == hl._EXTERNAL_RATIONALE_NOT_INSERTED, (
            label,
            row,
        )
        assert "has been inserted" not in json.dumps(
            result.get("analysis_report")
        ), (label, result.get("analysis_report"))
        # The issue itself is unchanged: only the not-applied row is reworded.
        (ledger_finding,) = [
            f for f in result["findings"] if f["provenance"] == f"ledger:{RULE_ID}"
        ]
        assert ledger_finding["external_rationale_for_footnote"] == hl._EXTERNAL_RATIONALE_INSERTED

    # A model issue refused at the same cell keeps its own rationale: the
    # critic's copy of the standard (`OUTCOME_COVERED`, so the ledger's own
    # issue authors no edit and its row, if any, is the flag-only wording).
    body = json.loads(_term_edit_response(docx_bytes, [_assert()]))
    copy = json.loads(_restoration_response(docx_bytes, [_assert()], _fallback(_opf_doc())))
    (copy_issue,) = copy["issues"]
    (copy_op,) = copy["block_ops"]
    copy_issue["issue_key"] = copy_op["issue_key"] = "I3"
    copy_op["anchor_block_id"] = cell
    body["issues"].append(copy_issue)
    body["block_ops"].append(copy_op)
    covered, _client = _run(
        docx_bytes, [json.dumps(body), _verdict(JUDGED_RULE)], notes_mode="internal"
    )
    assert covered["status"] == "OK", (covered.get("status"), covered.get("reason"))
    not_applied = (covered.get("analysis_report") or {}).get("changes_not_applied") or []
    model_rows = [
        row for row in not_applied if row["counterparty_change_summary"] != hl._CHANGE_SUMMARY
    ]
    assert [row["external_rationale_for_footnote"] for row in model_rows] == [
        copy_issue["external_rationale_for_footnote"]
    ], not_applied
    for row in _ledger_rows(covered):
        assert row["external_rationale_for_footnote"] == hl._EXTERNAL_RATIONALE_FLAG_ONLY, row

    # A delivered ledger insertion is applied, so it lists no row at all.
    delivered, _client = _run(
        _docx(with_indemnity=False),
        [_response(critic=True, ledger=[_assert()]), _verdict(JUDGED_RULE)],
        notes_mode="internal",
    )
    assert delivered["status"] == "OK" and _entry(delivered)["inserted"] is True
    assert _ledger_rows(delivered) == [], delivered.get("analysis_report")


def _restoration_response(
    docx_bytes: bytes,
    ledger: list[dict[str, Any]],
    standard: str,
    drafted: str | None = None,
) -> str:
    """A critic final result whose own issue `I1` restores the clause's
    standard WORD FOR WORD -- an `insert_block_after` under Confidentiality
    carrying `standard` -- beside its ledger dispositions. With `drafted`,
    the same issue also drafts language of its own: it replaces "thirty
    (30)" in the Term paragraph with `drafted`."""
    issue = {
        "issue_key": "I1",
        "section_ref": "Indemnification",
        "section_title": "Indemnification",
        "counterparty_change_summary": "The mutual indemnification clause was deleted.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "Our standard indemnification clause must be restored.",
        "playbook_topic_id": "clause.indemnification",
        "internal_precedent_citation": None,
        "provenance": "critic-added",
    }
    patches = []
    if drafted is not None:
        patches.append(
            {
                "block_id": _block_id(docx_bytes, "Term and Termination"),
                "segments": [
                    {"op": "keep", "text": "Either party may terminate for convenience on "},
                    {"op": "delete", "text": "thirty (30)", "issue_key": "I1"},
                    {"op": "insert", "text": drafted, "issue_key": "I1"},
                    {"op": "keep", "text": " days' written notice."},
                ],
            }
        )
    restoration = {
        "op": "insert_block_after",
        "anchor_block_id": _block_id(docx_bytes, "Confidentiality"),
        "new_text": standard,
        "issue_key": "I1",
    }
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [issue],
            "block_patches": patches,
            "block_ops": [restoration],
            "verdict_summary": "The indemnification clause must be restored.",
            "critic_delta": {"dispositions": [], "overrides": [], "ledger_dispositions": ledger},
        }
    )


def test_a_critic_copy_of_a_standard_the_pen_rules_would_refuse_is_delivered() -> None:
    """Issue #164, through the dedupe. The critic ASSERTs the entry and
    restores the playbook's own standard word for word; `enforce` lets that
    copy stand in for its own insertion (`OUTCOME_COVERED`), so the copy must
    share the ledger's pen-rule exemption -- otherwise a standard over 1,500
    characters, or one carrying "perpetual", is refused on the critic's
    issue and a review with no other edit ends ERROR /
    block_edits_not_applied with the protection undelivered. It is
    delivered, once, and no pen refusal is listed: the one entry in
    `changes_not_applied` is the ledger's own issue, which authors no edit
    of its own because the critic's copy covered it."""
    import html

    for standard in (LONG_STANDARD, BANNED_PHRASE_STANDARD):
        doc = _opf_doc(INDEMNITY_CONDITION, standard=standard)
        docx_bytes = _docx(with_indemnity=False)
        result, _client = _run(
            docx_bytes,
            [_restoration_response(docx_bytes, [_assert()], standard), _verdict(JUDGED_RULE)],
            doc=doc,
            notes_mode="internal",
        )
        assert result["status"] == "OK", (result.get("status"), result.get("reason"))
        assert result["decision"] == "REQUEST_CHANGE"
        not_applied = (result.get("analysis_report") or {}).get("changes_not_applied") or []
        assert [(c["section_ref"], c["reason"]) for c in not_applied] == [
            ("Indemnification", "legacy_unlabelled_flag_only")
        ], not_applied
        assert not_applied[0]["external_rationale_for_footnote"] == (
            "This protection was not found in the agreement and must be added."
        ), not_applied
        redline = result["redline_bytes"]
        assert redline, "the restored standard was not delivered"
        assert html.unescape(_inserted_texts(redline)).count(standard) == 1, "standard not delivered once"
        assert _in_order(_texts(redline, "word/document.xml"), CONFIDENTIALITY, standard, TERM)
        record = result["hard_ledger"]
        entry = _entry(result)
        assert entry["status"] == hl.STATUS_ASSERTED and entry["issue_key"] == "I2", entry
        # The ledger inserted nothing of its own: the critic's copy covered it.
        assert record["asserted"] == 1 and record["inserted"] == 0, record
        assert entry["inserted"] is False, entry
        assert "already proposed by another edit in this review" in _texts(
            redline, "word/footnotes.xml"
        )


def test_a_copy_beside_drafted_language_never_stands_in_for_the_ledger() -> None:
    """A critic copy of the standard whose issue ALSO drafts language of its
    own could be rolled back over that language -- here a "perpetual" the pen
    rules refuse, which takes the whole change set, copy included. Such a
    copy never covers: the ledger inserts its own (exempt) copy, so the
    protection is delivered, once, and the review is DONE while the
    critic's issue is listed as not applied."""
    import html

    import replacement_text_enforcement as rte

    for standard in (LONG_STANDARD, BANNED_PHRASE_STANDARD):
        doc = _opf_doc(INDEMNITY_CONDITION, standard=standard)
        docx_bytes = _docx(with_indemnity=False)
        response = _restoration_response(
            docx_bytes, [_assert()], standard, drafted="sixty (60), and this right is perpetual,"
        )
        result, _client = _run(
            docx_bytes, [response, _verdict(JUDGED_RULE)], doc=doc, notes_mode="internal"
        )
        assert result["status"] == "OK", (result.get("status"), result.get("reason"))
        assert result["decision"] == "REQUEST_CHANGE"
        refusal = _pen_refusal(result, "Indemnification")
        assert refusal["patch_reasons"][0]["reason"] == rte.MUST_NOT_INTRODUCE_VIOLATION, refusal
        redline = result["redline_bytes"]
        assert redline, "the ledger's insertion was not delivered"
        assert html.unescape(_inserted_texts(redline)).count(standard) == 1
        document = _texts(redline, "word/document.xml")
        assert TERM in document and "perpetual," not in document.replace(standard, "")
        record = result["hard_ledger"]
        assert record["asserted"] == 1 and record["inserted"] == 1, record
        assert _entry(result)["issue_key"] == "I2"


def test_a_pen_refused_critic_copy_leaves_the_fallback_inserted_and_noted() -> None:
    """Issue #168 (reported unverified; it does not reproduce on this tree).
    The report: the critic's own edit proposes our standard, the ledger
    counts the entry covered and inserts nothing, the pen rules then refuse
    the critic's edit -- and the required clause is delivered by neither.

    The critic copy here is word for word the standard, so on its own it
    WOULD cover (the control run: nothing inserted by the ledger, the
    "already proposed" note). Beside language the critic drafted that the
    pen rules refuse -- a banned phrase, or text over `max_chars` -- the
    pen rules roll the whole change set back, copy included. Never both,
    never neither: the ledger's own fallback is the one copy delivered, the
    record counts it inserted, and the rule id reaches the internal
    footnotes with the INSERTED sentence, never the COVERED or REFUSED
    one. Seeded with a standard the pen rules pass and the two they would
    refuse, against both pen-rule failures."""
    import html

    import footnote_audience
    import replacement_text_enforcement as rte

    long_drafted = " ".join(
        f"sixty (60) days' written notice, clause {n}, delivered to the other party's notice address"
        for n in range(1, 25)
    )
    assert len(long_drafted) > 1500
    drafted_cases = (
        ("sixty (60), and this right is perpetual,", rte.MUST_NOT_INTRODUCE_VIOLATION),
        (long_drafted, rte.MAX_CHARS_EXCEEDED),
    )
    inserted_note = hl.internal_note(RULE_ID, hl.REASON_CRITIC_ASSERTED, hl.OUTCOME_INSERTED)
    covered_note = hl.internal_note(RULE_ID, hl.REASON_CRITIC_ASSERTED, hl.OUTCOME_COVERED)
    refused_note = hl.internal_note(RULE_ID, hl.REASON_CRITIC_ASSERTED, hl.OUTCOME_REFUSED)
    default_standard = _fallback(_opf_doc(INDEMNITY_CONDITION))

    # Control: the same copy alone on its issue covers the entry.
    docx_bytes = _docx(with_indemnity=False)
    alone, _client = _run(
        docx_bytes,
        [_restoration_response(docx_bytes, [_assert()], default_standard), _verdict(JUDGED_RULE)],
        notes_mode="internal",
    )
    assert alone["status"] == "OK", (alone.get("status"), alone.get("reason"))
    assert alone["hard_ledger"]["inserted"] == 0, alone["hard_ledger"]
    assert covered_note in _texts(alone["redline_bytes"], "word/footnotes.xml")

    for standard in (default_standard, LONG_STANDARD, BANNED_PHRASE_STANDARD):
        doc = _opf_doc(INDEMNITY_CONDITION, standard=standard)
        for drafted, failure in drafted_cases:
            label = (len(standard), failure)
            docx_bytes = _docx(with_indemnity=False)
            response = _restoration_response(docx_bytes, [_assert()], standard, drafted=drafted)
            result, _client = _run(
                docx_bytes, [response, _verdict(JUDGED_RULE)], doc=doc, notes_mode="internal"
            )
            assert result["status"] == "OK", (label, result.get("status"), result.get("reason"))
            assert result["decision"] == "REQUEST_CHANGE", label
            # The critic's covering-shaped edit was refused by the pen rules ...
            refusal = _pen_refusal(result, "Indemnification")
            assert refusal["patch_reasons"][0]["reason"] == failure, (label, refusal)
            redline = result["redline_bytes"]
            assert redline, (label, "neither the critic nor the ledger delivered the clause")
            document = _texts(redline, "word/document.xml")
            assert TERM in document and "sixty (60)" not in document, label
            # ... and the ledger's fallback is delivered instead: once, never both.
            assert html.unescape(_inserted_texts(redline)).count(standard) == 1, label
            entry = _entry(result)
            assert (entry["status"], entry["inserted"], entry["issue_key"]) == (
                hl.STATUS_ASSERTED,
                True,
                "I2",
            ), (label, entry)
            assert result["hard_ledger"]["inserted"] == 1, (label, result["hard_ledger"])
            (ledger_finding,) = [
                f for f in result["findings"] if f.get("provenance") == f"ledger:{RULE_ID}"
            ]
            assert ledger_finding["internal_rationale_for_footnote"] == inserted_note, label
            footnotes = _texts(redline, "word/footnotes.xml")
            assert footnote_audience.INTERNAL_FOOTNOTE_PREFIX + inserted_note in footnotes, (
                label,
                footnotes[:600],
            )
            assert covered_note not in footnotes and refused_note not in footnotes, label


def test_only_a_pure_copy_of_a_standard_covers_and_escapes_the_pen_rules() -> None:
    """The unit view of the two tests above. `enforce` lets a critic
    insertion stand in for its own only when it folds equal to the standard
    AND its issue inserts nothing but ledger standards; and
    `redline_generate.pen_checked_text_by_issue` leaves exactly such copies
    out of an issue's pen-checked text, judging everything else it drafts."""
    import redline_generate as rg

    block_map = _block_map(_docx(with_indemnity=False))
    standard = "Each party shall indemnify the other."
    copy = {"op": "insert_block_after", "anchor_block_id": "b0001", "issue_key": "I1"}

    def enforce(*ops: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        reconciled = {
            "decision": "REQUEST_CHANGE",
            "issues": [{"issue_key": "I1", "provenance": "model"}],
            "block_ops": list(ops),
            "verdict_summary": "One clause needs work.",
        }
        result, record = hl.enforce(reconciled, [_asserted("IND-004")], block_map)
        assert record is not None
        return result, record

    # A faithful copy (whitespace and quotes fold) on an issue of its own: covered.
    _result, record = enforce({**copy, "new_text": "  Each party  shall indemnify the other. "})
    assert record["inserted"] == 0, record
    # The same copy beside language the issue drafts itself: not covered.
    result, record = enforce(
        {**copy, "new_text": standard},
        {**copy, "new_text": "And this right is perpetual."},
    )
    assert record["inserted"] == 1, record
    assert [op["issue_key"] for op in result["block_ops"]] == ["I1", "I1", "I2"]
    # A near copy is the critic's own language: not covered.
    _result, record = enforce({**copy, "new_text": standard + " Forever."})
    assert record["inserted"] == 1, record
    assert hl.standard_texts([_asserted("IND-004"), {**_asserted("X"), "status": hl.STATUS_CLEARED}]) == [
        standard
    ]

    proven = {
        "blocks": [],
        "block_ops": [
            {**copy, "new_text": standard},
            {**copy, "new_text": "And this right is perpetual.", "issue_key": "I2"},
            {**copy, "new_text": standard, "issue_key": "I2"},
            {**copy, "new_text": standard + " Forever.", "issue_key": "I3"},
        ],
    }
    assert rg.pen_checked_text_by_issue(proven, [standard]) == {
        "I1": "",
        "I2": "And this right is perpetual.",
        "I3": standard + " Forever.",
    }
    # With no standards, the pen rules judge the derived text unchanged.
    assert rg.pen_checked_text_by_issue(proven) == rg.derived_replacement_text_by_issue(proven)


def test_settle_keeps_only_delivered_insertions() -> None:
    record = {
        "entries": [
            {"rule_id": "A", "status": hl.STATUS_ASSERTED, "inserted": True, "anchor_block_id": "p0001", "issue_key": "I2"},
            {"rule_id": "B", "status": hl.STATUS_ASSERTED, "inserted": True, "anchor_block_id": "p0003", "issue_key": "I3"},
            {"rule_id": "C", "status": hl.STATUS_ASSERTED, "inserted": False, "anchor_block_id": None, "issue_key": "I4"},
            {"rule_id": "D", "status": hl.STATUS_CLEARED, "inserted": False, "anchor_block_id": None, "issue_key": None},
        ],
        "cleared": 1,
        "asserted": 3,
        "inserted": 2,
    }
    settled = hl.settle(record, {"I1", "I3", "I4"})
    assert [(e["rule_id"], e["inserted"], e["anchor_block_id"]) for e in settled["entries"]] == [
        ("A", False, None),
        ("B", True, "p0003"),
        ("C", False, None),
        ("D", False, None),
    ], settled
    assert (settled["cleared"], settled["asserted"], settled["inserted"]) == (1, 3, 1)
    assert record["inserted"] == 2 and record["entries"][0]["inserted"] is True, "input mutated"
    nothing = hl.settle(record, set())
    assert nothing["inserted"] == 0
    assert hl.settled_verdict_summary(
        {"decision": "REQUEST_CHANGE", "verdict_summary": "One clause needs work."}, settled
    ) == (
        "One clause needs work. 3 required protections were not found in the agreement: "
        "1 is asserted in the redline and 2 are flagged only."
    )
    assert hl.settled_verdict_summary({"decision": "ACCEPT", "verdict_summary": "Fine."}, nothing) == (
        "Changes are requested: 3 required protections were not found in the agreement and "
        "are flagged only; no standard language was inserted for them."
    )
    # Nothing asserted: the reconciled summary is untouched.
    assert hl.settled_verdict_summary(
        {"decision": "ACCEPT", "verdict_summary": "Fine."}, {"asserted": 0, "inserted": 0}
    ) == "Fine."


def test_delivered_issue_keys_are_only_those_of_a_delivered_document() -> None:
    import redline_generate as rg

    key = rg.DELIVERED_ISSUE_KEYS_KEY
    assert rg.delivered_issue_keys({"docx_bytes": b"PK", key: ["I1", "I3"]}) == {"I1", "I3"}
    assert rg.delivered_issue_keys({"docx_bytes": None, key: ["I1"]}) == set()
    assert rg.delivered_issue_keys({"status": "ERROR", "docx_bytes": None}) == set()
    assert rg.delivered_issue_keys({"docx_bytes": b"PK"}) == set()


def test_redline_generate_restates_the_ledger_sentences_exactly() -> None:
    """`redline_generate` restates the ledger's provenance prefix and the two
    outcome sentences it swaps, to stay free of this module's import; a
    drift would let a hosted note claim an insertion again."""
    import redline_generate as rg

    assert rg._LEDGER_PROVENANCE_PREFIX == hl.PROVENANCE_PREFIX
    inserted = hl.internal_note(RULE_ID, hl.REASON_CRITIC_ASSERTED, hl.OUTCOME_INSERTED)
    refused = hl.internal_note(RULE_ID, hl.REASON_CRITIC_ASSERTED, hl.OUTCOME_REFUSED)
    assert inserted.endswith(" " + rg._LEDGER_INSERTED_OUTCOME), inserted
    assert refused.endswith(" " + rg._LEDGER_REFUSED_OUTCOME), refused
    assert inserted[: -len(rg._LEDGER_INSERTED_OUTCOME)] == refused[: -len(rg._LEDGER_REFUSED_OUTCOME)]
    assert refused == (
        f"Hard-rejection ledger rule {RULE_ID} was not proven present: the second review did "
        "not find it in the agreement; its standard language did not pass the redline's "
        "checks and was not inserted, so it is flagged only."
    )
    assert hl.internal_note(RULE_ID, hl.REASON_CRITIC_ASSERTED, hl.OUTCOME_COVERED) == (
        f"Hard-rejection ledger rule {RULE_ID} was not proven present: the second review did "
        "not find it in the agreement; its clause's standard language is already proposed by "
        "another edit in this review, so it is flagged only."
    )


def test_a_v1_review_has_no_ledger() -> None:
    from test_critic_wins_reconciliation import _draft
    from test_critic_wins_reconciliation import _response as v1_response
    from test_critic_wins_reconciliation import _run as v1_run

    docx_bytes, _block = _draft()
    result = v1_run(docx_bytes, v1_response([], []), [v1_response([], [], summary="Fine.")])
    assert result["status"] == "OK", result.get("reason")
    assert "hard_ledger" not in result


TESTS = [
    test_a_deleted_clause_is_asserted_into_the_redline,
    test_the_status_is_done_on_the_row,
    test_a_cleared_claim_the_document_does_not_support_is_still_asserted,
    test_an_assert_naming_another_clause_does_not_place_the_insertion,
    test_a_satisfying_span_from_another_clause_does_not_clear_a_predicate_rule,
    test_a_genuine_span_from_another_clause_does_not_clear_a_judged_rule,
    test_an_entry_with_no_clause_title_is_never_cleared,
    test_a_cherry_picked_numeric_bound_span_stays_asserted,
    test_a_valid_span_with_a_satisfied_predicate_clears_the_entry,
    test_a_span_differing_only_by_transcription_noise_still_validates,
    test_a_span_whose_hash_does_not_validate_stays_asserted,
    test_a_copied_block_marker_is_not_part_of_the_span,
    test_an_unknown_block_id_or_missing_span_stays_asserted,
    test_a_missing_ledger_disposition_fails_the_review_closed,
    test_reconcile_refuses_a_missing_or_repeated_ledger_disposition,
    test_the_critic_is_told_the_ledger_and_the_primary_is_not,
    test_a_deterministic_presence_rule_leaves_the_judged_floor,
    test_a_judged_presence_rule_is_recommended_on_never_cleared_by_the_judge,
    test_judged_floor_invariants_keeps_everything_else,
    test_only_presence_rules_open_ledger_entries,
    test_fixture_manifest_is_what_the_engine_builds,
    test_required_phrases_any_and_all,
    test_numeric_bound,
    test_cross_reference,
    test_judged_is_presence_and_unknown_kinds_fail_closed,
    test_insertion_anchor_branches,
    test_two_asserted_rules_on_one_clause_insert_once_and_count_two,
    test_the_summary_sentences_are_exact,
    test_a_critic_insertion_of_the_same_language_is_not_duplicated,
    test_a_cleared_ledger_changes_nothing,
    test_the_internal_footnote_names_the_rule_and_the_reason,
    test_a_rule_with_no_standard_language_never_claims_an_insertion,
    test_every_asserted_rule_on_one_clause_reaches_an_internal_footnote,
    test_a_ledger_only_review_inserts_a_standard_the_pen_rules_would_refuse,
    test_model_drafted_text_is_still_pen_checked_beside_a_ledger_insertion,
    test_only_ledger_provenance_is_exempt_from_the_pen_rules,
    test_a_rolled_back_ledger_insertion_is_noted_as_not_inserted,
    test_a_ledger_insertion_that_fails_to_compile_is_re_hosted_or_fails_closed,
    test_a_ledger_insertion_that_fails_to_compile_is_not_reported_as_inserted,
    test_a_critic_copy_of_a_standard_the_pen_rules_would_refuse_is_delivered,
    test_a_copy_beside_drafted_language_never_stands_in_for_the_ledger,
    test_a_pen_refused_critic_copy_leaves_the_fallback_inserted_and_noted,
    test_only_a_pure_copy_of_a_standard_covers_and_escapes_the_pen_rules,
    test_settle_keeps_only_delivered_insertions,
    test_delivered_issue_keys_are_only_those_of_a_delivered_document,
    test_redline_generate_restates_the_ledger_sentences_exactly,
    test_a_v1_review_has_no_ledger,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL: {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS: {test.__name__}")
    print()
    if failed:
        print(f"FAIL: {failed} test(s) failed (issue #147).")
        return 1
    print("PASS: hard-rejection recall is enforced outside both models (issue #147).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
