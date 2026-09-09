#!/usr/bin/env python3
"""
End-to-end slice test for issue #626: "block-mode result path in the spine --
dormant wiring, per-issue atomicity, derived replacement text".

## Root problem this proves fixed

Issues #620-#624 landed every PIECE of Candidate E -- the transcript
validator (`scripts/block_transcript.py`), the span/whole-block compiler
(`scripts/redline_block_apply.py`), the projection proofs
(`scripts/redline_projections.py`) and the v3 output contract
(`playbooks/output-schema-v3.json`) -- and nothing joined them to the
review's own result path. `scripts/redline_generate.py` could only turn a
v2 quote-bearing response into a delivered `.docx`; a v3 block transcript
had no route to a document, no leakage coverage over the texts it writes,
no `proposed_replacement_text` for the UI/pen-rules/audit surfaces that
require one, and no per-issue atomicity -- so a batch where one of an
issue's patches failed would have delivered a document carrying HALF a
legal edit.

`scripts/redline_generate.py::generate_redline_from_blocks` does not exist
before this slice and this file FAILS on import until it does.

## What this test asserts (mirrors the issue's Acceptance criteria)

  1. A SCHEMA-PERFECT v3 response -- validated by the real production
     validator, `primary_review_pass.validate_model_response(...,
     schema_path=OUTPUT_SCHEMA_V3_PATH)`, not by a hand-waved dict --
     reconciles and compiles to a delivered `.docx` carrying a MULTI-EDIT
     paragraph (two issues editing one paragraph), an INSERTED clause
     (`insert_block_after`), and one footnote per issue.
  2. Gate parity: the block path returns the SAME status-dict vocabulary
     the quote path returns (`OK` / `MANUAL_REVIEW_REQUIRED` /
     `ERROR_MANUAL_REVIEW_REQUIRED`), with `analysis_report` /
     `flag_only_report` semantics (issue #585) intact.
  3. **Change-set atomicity.** One issue owns two edits; its SECOND is
     sabotaged so it proves but cannot compile (a `delete` span crossing a
     physical `<w:p>` boundary -- `spans_physical_paragraph`). That issue is
     ABSENT from the delivered document (its first edit rolled back too,
     never half-applied), present in the analysis report as
     `issue_changeset_failed` with the per-patch reason, and the OTHER
     issue is still delivered (partial-delivery doctrine, issue #203).
  4. **Derived `proposed_replacement_text`.** Stamped from the proven
     transcript's insert texts in document order, overwriting whatever the
     model supplied, empty for a pure deletion INSIDE a block and
     `[Intentionally omitted.]` for a whole-block strike (issue #646, which
     is the language that strike leaves in the document) -- and the pen rules
     (issue #216) run against the DERIVED text, dropping just that issue's
     edits when it violates them, except for a pure deletion, which proposed
     no language for them to judge.
  5. **Leakage coverage.** A precedent counterparty name planted in an
     `insert` segment, and in an `insert_block_after`'s `new_text`, each
     blocks the whole review at the gate that runs FIRST, with no document
     produced -- the same fail-closed shape a leaked
     `proposed_replacement_text` gets on the quote path.
  6. A flag-only issue (no patches or ops at all) keeps the issue #585
     `flag_only_report` labelling exactly as the quote path produces it.
  7. An edit naming an `issue_key` no issue carries is fail-closed for the
     whole batch -- an unattributed tracked change never reaches the
     counterparty's document.
  8. The spine's stage-5 branch is shape-driven and DORMANT: a v2-shaped
     reconciled result still routes to the quote path, and
     `reconciliation.reconcile` forwards the v3 carriers only when the
     primary pass actually carries them.

Everything here is offline: synthetic python-docx fixtures, no network, no
model client.

Run standalone: `python3 tests/test_block_mode_e2e.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import datetime
import io
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _import_modules():
    missing: list[str] = []
    modules: dict[str, Any] = {}
    for name in (
        "redline_generate",
        "review_spine",
        "reconciliation",
        "leakage_scan",
        "primary_review_pass",
        "extraction_normalization_stage",
        "block_transcript",
    ):
        try:
            modules[name] = __import__(name)
        except ImportError as exc:  # pragma: no cover - reported, not raised
            missing.append(f"MISSING: scripts/{name}.py does not import ({exc}).")
    rg = modules.get("redline_generate")
    if rg is not None and not hasattr(rg, "generate_redline_from_blocks"):
        missing.append(
            "MISSING: scripts/redline_generate.py has no "
            "`generate_redline_from_blocks(...)` -- issue #626's block-mode "
            "result path."
        )
    spine = modules.get("review_spine")
    if spine is not None and not hasattr(spine, "uses_block_mode"):
        missing.append(
            "MISSING: scripts/review_spine.py has no `uses_block_mode(...)` -- "
            "issue #626's stage-5 shape branch."
        )
    return modules, missing


MODULES, IMPORT_ERRORS = _import_modules()

redline_generate = MODULES.get("redline_generate")
review_spine = MODULES.get("review_spine")
reconciliation = MODULES.get("reconciliation")
leakage_scan = MODULES.get("leakage_scan")
primary_review_pass = MODULES.get("primary_review_pass")
extraction_normalization_stage = MODULES.get("extraction_normalization_stage")


AUTHOR = "contract-toaster"
# `generate_redline_from_blocks` takes the same `date` the quote path takes
# (`docx_parts.iso_date` formats it), never a pre-formatted string.
TIMESTAMP = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

# ---------------------------------------------------------------------------
# Synthetic fixture. Nothing here is drawn from any real document or any real
# counterparty: two invented sections of an invented agreement.
#
# The heading paragraphs carry a real Heading style so
# `clause_boundaries.is_boundary_paragraph_ooxml` opens a logical block at
# each; the body paragraphs under one heading are that block's PHYSICAL
# paragraphs, joined with "\n" in the block's text (issue #564).
# ---------------------------------------------------------------------------

TERM_P1 = "The Term shall be sixty (60) days from the Effective Date."
TERM_P2 = "The Fee shall be one hundred dollars per month."
NOTICE_P1 = "Notices shall be sent by prepaid post to the address above."

SECTIONS = [
    ("Section 1. Term and Fee", [TERM_P1, TERM_P2]),
    ("Section 2. Notices", [NOTICE_P1]),
]


def _make_docx(sections: list) -> bytes:
    import docx  # local import: python-docx is a test-only dependency

    document = docx.Document()
    for heading, bodies in sections:
        document.add_paragraph(heading, style="Heading 1")
        for text in bodies:
            paragraph = document.add_paragraph()
            paragraph.add_run(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _block_ids(docx_bytes: bytes) -> list:
    norm = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    assert norm["status"] == "normalized", norm
    return list(extraction_normalization_stage.build_block_map(norm["paragraphs"]))


def _empty_corpus():
    """A corpus that blocks nothing -- the baseline for the delivery tests.
    The leakage tests below build a planted one instead."""
    return leakage_scan.ConfidentialCorpus()


def _document_root(docx_bytes: bytes) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return ET.fromstring(zf.read("word/document.xml"))


def _footnote_texts(docx_bytes: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        if "word/footnotes.xml" not in zf.namelist():
            return []
        root = ET.fromstring(zf.read("word/footnotes.xml"))
    texts = []
    for footnote in root.iter(_qn("footnote")):
        if footnote.get(_qn("type")):
            continue
        texts.append("".join(t.text or "" for t in footnote.iter(_qn("t"))).strip())
    return texts


def _inserted_texts(docx_bytes: bytes) -> list:
    root = _document_root(docx_bytes)
    return [
        "".join(t.text or "" for t in ins.iter(_qn("t")))
        for ins in root.iter(_qn("ins"))
    ]


def _deleted_texts(docx_bytes: bytes) -> list:
    root = _document_root(docx_bytes)
    return [
        "".join(t.text or "" for t in dele.iter(_qn("delText")))
        for dele in root.iter(_qn("del"))
    ]


def _plain_text(docx_bytes: bytes) -> str:
    """Every `<w:t>` in the document, joined -- the accept-all view."""
    root = _document_root(docx_bytes)
    return " ".join(t.text or "" for t in root.iter(_qn("t")))


# ---------------------------------------------------------------------------
# The fake v3 model response. Written the way a MODEL writes one: no
# `schema_version`, no per-issue `provenance` -- the pipeline stamps both
# (`primary_review_pass._stamp_pipeline_envelope`), which is exactly why this
# goes through the real validator instead of being hand-assembled as a dict.
# ---------------------------------------------------------------------------


def _issue(issue_key: str, topic_id: str, *, section: str, note: str | None = None) -> dict:
    issue = {
        "issue_key": issue_key,
        "section_ref": section,
        "section_title": section,
        "counterparty_change_summary": (
            f"The counterparty altered {section} relative to the standard form."
        ),
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": (
            f"{section} departs from the standard form position."
        ),
        "playbook_topic_id": topic_id,
        "internal_precedent_citation": None,
        # Deliberately populated and deliberately WRONG: block mode ignores a
        # model-supplied value and derives this field from the transcript.
        "proposed_replacement_text": "MODEL SUPPLIED TEXT THAT MUST BE IGNORED",
    }
    if note is not None:
        issue["replacement_scope_note"] = note
    return issue


def _validate_v3(response: dict) -> dict:
    """Run the REAL validator over the fake response, against the v3
    artifact. Returns the parsed, envelope-stamped dict -- the exact object
    `run_primary_pass` hands to reconciliation on the flipped contract."""
    ok, parsed = primary_review_pass.validate_model_response(
        json.dumps(response),
        schema_path=primary_review_pass.OUTPUT_SCHEMA_V3_PATH,
    )
    assert ok, f"fake v3 response is not schema-perfect: {parsed}"
    return parsed


def _reconciled(response: dict) -> dict:
    """Through the real reconciler, so the block carriers reach stage 5 the
    way production would deliver them."""
    return reconciliation.reconcile(primary_result=_validate_v3(response))


def _multi_edit_response(block_ids: list) -> dict:
    """Two issues editing ONE paragraph of block 0, plus a third supplying a
    whole new clause after block 1."""
    term_block, notice_block = block_ids[0], block_ids[1]
    return {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "verdict_summary": "Three departures from the standard form.",
        "issues": [
            _issue("I1", "term-length", section="Section 1. Term and Fee"),
            _issue("I2", "fee-cap", section="Section 1. Term and Fee"),
            _issue(
                "I3",
                "notice-method",
                section="Section 2. Notices",
                note="The clause is absent entirely, so it is supplied whole.",
            ),
        ],
        "block_patches": [
            {
                "block_id": term_block,
                "segments": [
                    {"op": "keep", "text": "The Term shall be "},
                    {"op": "delete", "text": "sixty (60)", "issue_key": "I1"},
                    {"op": "insert", "text": "thirty (30)", "issue_key": "I1"},
                    {
                        "op": "keep",
                        "text": " days from the Effective Date.\nThe Fee shall be ",
                    },
                    {"op": "delete", "text": "one hundred dollars", "issue_key": "I2"},
                    {"op": "insert", "text": "fifty dollars", "issue_key": "I2"},
                    {"op": "keep", "text": " per month."},
                ],
            }
        ],
        "block_ops": [
            {
                "op": "insert_block_after",
                "anchor_block_id": notice_block,
                "new_text": "Notices may also be sent by electronic mail.",
                "issue_key": "I3",
            }
        ],
    }


def _atomicity_response(block_ids: list) -> dict:
    """Issue I1 owns TWO edits in block 0; the second `delete` deliberately
    spans the physical `<w:p>` join inside that block, which PROVES (the
    transcript still tiles the block's real text) but cannot COMPILE --
    `docx_editor` edits physical paragraphs, so there is nowhere to write one
    tracked change across the join (`spans_physical_paragraph`). Issue I2
    edits a different block and must still be delivered."""
    term_block, notice_block = block_ids[0], block_ids[1]
    return {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "verdict_summary": "Two departures from the standard form.",
        "issues": [
            _issue("I1", "term-length", section="Section 1. Term and Fee"),
            _issue("I2", "notice-method", section="Section 2. Notices"),
        ],
        "block_patches": [
            {
                "block_id": term_block,
                "segments": [
                    {"op": "keep", "text": "The Term shall be "},
                    {"op": "delete", "text": "sixty (60)", "issue_key": "I1"},
                    {"op": "insert", "text": "thirty (30)", "issue_key": "I1"},
                    {"op": "keep", "text": " days from the"},
                    # Sabotage: this span crosses the "\n" join between the
                    # block's two physical paragraphs.
                    {
                        "op": "delete",
                        "text": " Effective Date.\nThe Fee",
                        "issue_key": "I1",
                    },
                    {"op": "insert", "text": " Commencement Date.\nThe Charge", "issue_key": "I1"},
                    {"op": "keep", "text": " shall be one hundred dollars per month."},
                ],
            },
            {
                "block_id": notice_block,
                "segments": [
                    {"op": "keep", "text": "Notices shall be sent by "},
                    {"op": "delete", "text": "prepaid post", "issue_key": "I2"},
                    {"op": "insert", "text": "electronic mail", "issue_key": "I2"},
                    {"op": "keep", "text": " to the address above."},
                ],
            },
        ],
    }


def _run_block_mode(
    reconciled, docx_bytes, *, corpus=None, notes_mode="external", pen_rules_bundle=None
):
    return redline_generate.generate_redline_from_blocks(
        reconciled_result=reconciled,
        corpus=corpus if corpus is not None else _empty_corpus(),
        normalized_docx_bytes=docx_bytes,
        review_id="review-block-e2e",
        current_counterparty_name="Northwind Widgets LLC",
        author=AUTHOR,
        date=TIMESTAMP,
        notes_mode=notes_mode,
        pen_rules_bundle=pen_rules_bundle,
    )


# ---------------------------------------------------------------------------
# 1 + 2: schema-perfect v3 response -> delivered document
# ---------------------------------------------------------------------------


def test_multi_edit_paragraph_inserted_clause_and_per_issue_footnotes(failures: list) -> None:
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    reconciled = _reconciled(_multi_edit_response(block_ids))

    result = _run_block_mode(reconciled, docx_bytes)

    if result["status"] != "OK":
        failures.append(
            f"block mode did not deliver: status={result['status']!r} "
            f"reason={result.get('reason')!r} detail={result.get('detail')!r} "
            f"report={result.get('analysis_report')!r}"
        )
        return
    if result.get("decision") != "REQUEST_CHANGE":
        failures.append(f"expected decision=REQUEST_CHANGE, got {result.get('decision')!r}")
    delivered = result["docx_bytes"]
    if not delivered:
        failures.append("status=OK but no document was delivered")
        return

    redline_generate.verify_docx_round_trip(delivered)

    inserted = _inserted_texts(delivered)
    deleted = _deleted_texts(delivered)
    for want in ("thirty (30)", "fifty dollars"):
        if want not in inserted:
            failures.append(f"delivered document has no <w:ins> carrying {want!r}: {inserted}")
    for want in ("sixty (60)", "one hundred dollars"):
        if want not in deleted:
            failures.append(f"delivered document has no <w:del> carrying {want!r}: {deleted}")

    # Multi-edit paragraph: BOTH of block 0's edits are tracked changes, and
    # they land in the two physical paragraphs of the SAME logical block --
    # the shape the quote path could not express.
    root = _document_root(delivered)
    paragraphs = list(root.iter(_qn("p")))
    edited_paragraph_count = sum(
        1
        for p in paragraphs
        if any(child.tag in (_qn("ins"), _qn("del")) for child in p)
    )
    if edited_paragraph_count < 3:
        failures.append(
            "expected at least 3 edited paragraphs (two in the Term block, one "
            f"inserted clause); found {edited_paragraph_count}"
        )

    # Inserted clause (issue #622's `insert_block_after`).
    if "Notices may also be sent by electronic mail." not in " ".join(inserted):
        failures.append(f"the inserted clause is not in the document: {inserted}")

    # One footnote per issue.
    notes = _footnote_texts(delivered)
    if len(notes) != 3:
        failures.append(f"expected one footnote per issue (3), got {len(notes)}: {notes}")
    for section in ("Section 1. Term and Fee", "Section 2. Notices"):
        if not any(section in note for note in notes):
            failures.append(f"no footnote names {section!r}: {notes}")

    # Gate parity: nothing flag-only, so no report at all.
    if result.get("analysis_report") is not None:
        failures.append(
            f"expected analysis_report=None with nothing flag-only, got "
            f"{result['analysis_report']!r}"
        )
    if result.get("flag_only"):
        failures.append(f"expected an empty flag_only list, got {result['flag_only']!r}")


def test_derived_replacement_text_overwrites_the_model_supplied_value(failures: list) -> None:
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    reconciled = _reconciled(_multi_edit_response(block_ids))

    _run_block_mode(reconciled, docx_bytes)

    derived = {
        issue["issue_key"]: issue.get("proposed_replacement_text")
        for issue in reconciled["issues"]
    }
    expected = {
        "I1": "thirty (30)",
        "I2": "fifty dollars",
        "I3": "Notices may also be sent by electronic mail.",
    }
    for issue_key, want in expected.items():
        if derived.get(issue_key) != want:
            failures.append(
                f"issue {issue_key}: derived proposed_replacement_text is "
                f"{derived.get(issue_key)!r}, expected {want!r}"
            )
    if any(value == "MODEL SUPPLIED TEXT THAT MUST BE IGNORED" for value in derived.values()):
        failures.append(
            "a model-supplied proposed_replacement_text survived into the result: "
            f"{derived!r}"
        )


def test_pure_deletion_derives_the_empty_string(failures: list) -> None:
    """A delete-only issue derives `""` -- and is NOT thereby treated as
    flag-only: it has real edits and they are delivered."""
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    response = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [_issue("I1", "term-length", section="Section 1. Term and Fee")],
        "block_patches": [
            {
                "block_id": block_ids[0],
                "segments": [
                    {"op": "keep", "text": "The Term shall be sixty (60) days"},
                    {"op": "delete", "text": " from the Effective Date", "issue_key": "I1"},
                    {
                        "op": "keep",
                        "text": ".\nThe Fee shall be one hundred dollars per month.",
                    },
                ],
            }
        ],
    }
    reconciled = _reconciled(response)
    result = _run_block_mode(reconciled, docx_bytes)

    if result["status"] != "OK" or not result.get("docx_bytes"):
        failures.append(
            f"a pure deletion was not delivered: status={result['status']!r} "
            f"reason={result.get('reason')!r}"
        )
        return
    if reconciled["issues"][0].get("proposed_replacement_text") != "":
        failures.append(
            "a pure-deletion issue should derive the empty string, got "
            f"{reconciled['issues'][0].get('proposed_replacement_text')!r}"
        )
    if result.get("analysis_report") is not None:
        failures.append(
            "a pure-deletion issue must not be reported flag-only: "
            f"{result['analysis_report']!r}"
        )
    if " from the Effective Date" not in _deleted_texts(result["docx_bytes"]):
        failures.append(
            f"the deletion is not in the document: {_deleted_texts(result['docx_bytes'])}"
        )


def test_a_whole_block_strike_derives_the_placeholder_and_skips_pen_rules(
    failures: list,
) -> None:
    """Issue #646: a `delete_block` leaves `[Intentionally omitted.]` in the
    document, so that -- and not `""` -- is what the derived
    `proposed_replacement_text` says.

    And because the compiler authored that string and no model drafted it, it
    must never be judged as a proposal. The bundle here makes the exemption
    load-bearing rather than decorative: `mode: "none"` on the struck issue's
    topic is a pen rule saying "no replacement text is permitted for this
    topic", which would burn the whole striking over language nobody wrote.
    The OTHER issue in the same batch is on the same `mode: "none"` topic and
    DOES propose language, so the rule is proven still live -- an exemption
    that swallowed it would be a blanket, not a filter.
    """
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    # The `pen_rules` BLOCK shape `resolve_pen_rules` consumes (issue #293):
    # `per_topic` > `default` > the toaster-global defaults artifact.
    bundle = {"per_topic": {"notice-method": {"mode": "none"}}}
    response = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [
            _issue("I1", "notice-method", section="Section 2. Notices"),
            _issue("I2", "term-length", section="Section 1. Term and Fee"),
        ],
        "block_patches": [
            {
                "block_id": block_ids[0],
                "segments": [
                    {"op": "keep", "text": "The Term shall be "},
                    {"op": "delete", "text": "sixty (60) days", "issue_key": "I2"},
                    {"op": "insert", "text": "thirty (30) days", "issue_key": "I2"},
                    {
                        "op": "keep",
                        "text": " from the Effective Date.\nThe Fee shall be "
                        "one hundred dollars per month.",
                    },
                ],
            }
        ],
        "block_ops": [
            {"op": "delete_block", "block_id": block_ids[1], "issue_key": "I1"}
        ],
    }
    reconciled = _reconciled(response)
    result = _run_block_mode(reconciled, docx_bytes, pen_rules_bundle=bundle)

    if result["status"] != "OK" or not result.get("docx_bytes"):
        failures.append(
            f"a whole-block strike was not delivered: status={result['status']!r} "
            f"reason={result.get('reason')!r} report={result.get('analysis_report')!r}"
        )
        return

    placeholder = MODULES["block_transcript"].OMITTED_CLAUSE_PLACEHOLDER
    struck = reconciled["issues"][0]
    if struck.get("proposed_replacement_text") != placeholder:
        failures.append(
            f"a whole-block strike derived "
            f"{struck.get('proposed_replacement_text')!r}, expected {placeholder!r}"
        )
    if placeholder not in " ".join(_inserted_texts(result["docx_bytes"])):
        failures.append(
            f"the delivered document does not carry {placeholder!r}: "
            f"{_inserted_texts(result['docx_bytes'])!r}"
        )
    if NOTICE_P1 not in " ".join(_deleted_texts(result["docx_bytes"])):
        failures.append(
            f"the struck clause is not deleted in the document: "
            f"{_deleted_texts(result['docx_bytes'])!r}"
        )
    if result.get("analysis_report") is not None:
        failures.append(
            f"the whole-block strike was reported not-applied: "
            f"{result['analysis_report']!r}"
        )

    # The same `mode: "none"` rule, on an issue that DID propose language,
    # still bites -- so the exemption above is a filter and not a blanket.
    proposing = dict(response)
    proposing["issues"] = [_issue("I1", "notice-method", section="Section 2. Notices")]
    proposing["block_patches"] = [
        {
            "block_id": block_ids[1],
            "segments": [
                {"op": "keep", "text": "Notices shall be sent by "},
                {"op": "delete", "text": "prepaid post", "issue_key": "I1"},
                {"op": "insert", "text": "electronic mail", "issue_key": "I1"},
                {"op": "keep", "text": " to the address above."},
            ],
        }
    ]
    proposing.pop("block_ops", None)
    control = _run_block_mode(_reconciled(proposing), docx_bytes, pen_rules_bundle=bundle)
    control_reasons = [
        entry["reason"]
        for entry in (control.get("analysis_report") or {}).get("changes_not_applied", [])
    ]
    if control_reasons != [redline_generate.REASON_DERIVED_REPLACEMENT_TEXT_REJECTED]:
        failures.append(
            f"the pen rule that must still bite did not: {control_reasons!r} "
            f"(status={control['status']!r}) -- if `mode: \"none\"` no longer rejects a "
            f"proposal, the exemption above proves nothing"
        )


# ---------------------------------------------------------------------------
# 3: change-set atomicity
# ---------------------------------------------------------------------------


def test_partial_change_set_is_rolled_back_and_the_other_issue_delivered(
    failures: list,
) -> None:
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    response = _atomicity_response(block_ids)
    reconciled = _reconciled(response)

    # Guard the fixture itself: the sabotaged transcript must PROVE, or this
    # test would be exercising the validator's rejection path instead of the
    # compiler's atomicity.
    block_transcript = MODULES["block_transcript"]
    norm = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    proven = block_transcript.validate_block_patches(
        response["block_patches"],
        [],
        extraction_normalization_stage.build_block_map(norm["paragraphs"]),
    )
    if proven["status"] != "proven":
        failures.append(
            "the atomicity fixture no longer proves, so it cannot exercise the "
            f"compiler's rollback: {proven['failures']}"
        )
        return

    result = _run_block_mode(reconciled, docx_bytes)

    if result["status"] != "OK" or not result.get("docx_bytes"):
        failures.append(
            "the surviving issue was not delivered (partial delivery, issue "
            f"#203): status={result['status']!r} reason={result.get('reason')!r}"
        )
        return
    delivered = result["docx_bytes"]

    # I1 is absent from the document ENTIRELY -- including the edit that
    # would have applied on its own. That is the whole point of atomicity.
    inserted = " ".join(_inserted_texts(delivered))
    deleted = " ".join(_deleted_texts(delivered))
    if "thirty (30)" in inserted:
        failures.append(
            "issue I1's FIRST edit survived even though its change set failed -- "
            f"the document carries half a legal edit: {inserted!r}"
        )
    if "sixty (60)" in deleted:
        failures.append(
            f"issue I1's deletion survived its own change-set rollback: {deleted!r}"
        )
    if "sixty (60)" not in _plain_text(delivered):
        failures.append("the rolled-back issue's original text was not left intact")

    # I2 is delivered.
    if "electronic mail" not in inserted:
        failures.append(f"issue I2 was not delivered alongside the rollback: {inserted!r}")

    # I1 is in the report, labelled, with the per-patch reason.
    report = result.get("analysis_report")
    if not report:
        failures.append("a rolled-back change set produced no analysis_report")
        return
    if report["report_type"] != "analysis_report":
        failures.append(
            f"expected report_type='analysis_report' for a real apply failure, "
            f"got {report['report_type']!r}"
        )
    if report["reason"] != redline_generate.REASON_BLOCK_EDITS_NOT_APPLIED:
        failures.append(f"unexpected top-level report reason {report['reason']!r}")
    entries = report["changes_not_applied"]
    if len(entries) != 1:
        failures.append(f"expected exactly one not-applied entry, got {entries!r}")
        return
    entry = entries[0]
    if entry["reason"] != redline_generate.REASON_ISSUE_CHANGESET_FAILED:
        failures.append(
            f"expected reason={redline_generate.REASON_ISSUE_CHANGESET_FAILED!r}, "
            f"got {entry['reason']!r}"
        )
    patch_reasons = [item.get("reason") for item in entry.get("patch_reasons") or []]
    if "spans_physical_paragraph" not in patch_reasons:
        failures.append(
            "the rollback did not name the per-patch reason that caused it: "
            f"{patch_reasons!r}"
        )
    redline_generate.verify_docx_round_trip(delivered)


def test_a_wholly_failed_issue_keeps_its_own_reason(failures: list) -> None:
    """An issue whose edits ALL fail is not a rollback -- nothing of it
    landed -- so it must keep its own per-edit reason rather than be
    mislabelled `issue_changeset_failed`."""
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    response = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [
            _issue("I1", "term-length", section="Section 1. Term and Fee"),
            _issue("I2", "notice-method", section="Section 2. Notices"),
        ],
        "block_patches": [
            {
                "block_id": block_ids[0],
                "segments": [
                    {"op": "keep", "text": "The Term shall be sixty (60) days from the"},
                    {
                        "op": "delete",
                        "text": " Effective Date.\nThe Fee",
                        "issue_key": "I1",
                    },
                    {"op": "insert", "text": " Commencement Date.\nThe Charge", "issue_key": "I1"},
                    {"op": "keep", "text": " shall be one hundred dollars per month."},
                ],
            },
            {
                "block_id": block_ids[1],
                "segments": [
                    {"op": "keep", "text": "Notices shall be sent by "},
                    {"op": "delete", "text": "prepaid post", "issue_key": "I2"},
                    {"op": "insert", "text": "electronic mail", "issue_key": "I2"},
                    {"op": "keep", "text": " to the address above."},
                ],
            },
        ],
    }
    reconciled = _reconciled(response)
    result = _run_block_mode(reconciled, docx_bytes)

    if result["status"] != "OK" or not result.get("docx_bytes"):
        failures.append(
            f"expected partial delivery, got status={result['status']!r} "
            f"reason={result.get('reason')!r}"
        )
        return
    entries = result["analysis_report"]["changes_not_applied"]
    reasons = [entry["reason"] for entry in entries]
    if reasons != ["spans_physical_paragraph"]:
        failures.append(
            "a wholly-failed issue should keep its own per-edit reason, got "
            f"{reasons!r}"
        )


# ---------------------------------------------------------------------------
# 4: pen rules over the DERIVED text
# ---------------------------------------------------------------------------


def test_pen_rules_run_against_the_derived_text(failures: list) -> None:
    """`playbooks/pen-rules.defaults.json`'s `default.must_not_introduce`
    forbids "perpetual". The model never wrote a `proposed_replacement_text`
    containing it -- the DERIVED text does, assembled from the insert
    segments -- so this only fires if the pen rules really do run against
    the derived value."""
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    response = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [
            _issue("I1", "term-length", section="Section 1. Term and Fee"),
            _issue("I2", "notice-method", section="Section 2. Notices"),
        ],
        "block_patches": [
            {
                "block_id": block_ids[0],
                "segments": [
                    {"op": "keep", "text": "The Term shall be "},
                    {"op": "delete", "text": "sixty (60) days", "issue_key": "I1"},
                    {"op": "insert", "text": "perpetual", "issue_key": "I1"},
                    {
                        "op": "keep",
                        "text": " from the Effective Date.\nThe Fee shall be "
                        "one hundred dollars per month.",
                    },
                ],
            },
            {
                "block_id": block_ids[1],
                "segments": [
                    {"op": "keep", "text": "Notices shall be sent by "},
                    {"op": "delete", "text": "prepaid post", "issue_key": "I2"},
                    {"op": "insert", "text": "electronic mail", "issue_key": "I2"},
                    {"op": "keep", "text": " to the address above."},
                ],
            },
        ],
    }
    reconciled = _reconciled(response)
    result = _run_block_mode(reconciled, docx_bytes)

    if result["status"] != "OK" or not result.get("docx_bytes"):
        failures.append(
            f"expected partial delivery, got status={result['status']!r} "
            f"reason={result.get('reason')!r}"
        )
        return
    if "perpetual" in " ".join(_inserted_texts(result["docx_bytes"])):
        failures.append("pen-rules-forbidden text reached the delivered document")
    if "electronic mail" not in " ".join(_inserted_texts(result["docx_bytes"])):
        failures.append("the compliant issue was not delivered alongside the rejection")
    entries = result["analysis_report"]["changes_not_applied"]
    reasons = [entry["reason"] for entry in entries]
    if reasons != [redline_generate.REASON_DERIVED_REPLACEMENT_TEXT_REJECTED]:
        failures.append(
            "expected the pen-rules rejection reason, got "
            f"{reasons!r} (report={result['analysis_report']!r})"
        )


# ---------------------------------------------------------------------------
# 5: leakage coverage over the texts block mode writes
# ---------------------------------------------------------------------------


def _planted_corpus():
    """A corpus naming a PRECEDENT counterparty. Synthetic, invented for this
    test; the review's own counterparty is a different invented name, so the
    issue #208 self-block exemption cannot mask the detection."""
    return leakage_scan.ConfidentialCorpus(counterparty_names=["Grimsby Holdings PLC"])


def test_leaked_insert_segment_blocks_the_review(failures: list) -> None:
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    response = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [_issue("I1", "term-length", section="Section 1. Term and Fee")],
        "block_patches": [
            {
                "block_id": block_ids[0],
                "segments": [
                    {"op": "keep", "text": "The Term shall be "},
                    {"op": "delete", "text": "sixty (60)", "issue_key": "I1"},
                    {
                        "op": "insert",
                        "text": "thirty (30), as agreed with Grimsby Holdings PLC",
                        "issue_key": "I1",
                    },
                    {
                        "op": "keep",
                        "text": " days from the Effective Date.\nThe Fee shall be "
                        "one hundred dollars per month.",
                    },
                ],
            }
        ],
    }
    result = _run_block_mode(
        _reconciled(response), docx_bytes, corpus=_planted_corpus()
    )
    if result["status"] != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            "a precedent counterparty name in an insert segment did not block the "
            f"review: status={result['status']!r}"
        )
        return
    if result.get("reason") != "leakage_detected":
        failures.append(f"unexpected block reason {result.get('reason')!r}")
    if result.get("field_name") != leakage_scan.BLOCK_SEGMENT_INSERT_FIELD:
        failures.append(
            f"expected field_name={leakage_scan.BLOCK_SEGMENT_INSERT_FIELD!r}, got "
            f"{result.get('field_name')!r}"
        )
    if result.get("docx_bytes") is not None:
        failures.append("a leakage-blocked review produced a document")


def test_leaked_insert_block_new_text_blocks_the_review(failures: list) -> None:
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    response = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [_issue("I3", "notice-method", section="Section 2. Notices")],
        "block_ops": [
            {
                "op": "insert_block_after",
                "anchor_block_id": block_ids[1],
                "new_text": "Notices shall follow the Grimsby Holdings PLC precedent.",
                "issue_key": "I3",
            }
        ],
    }
    result = _run_block_mode(
        _reconciled(response), docx_bytes, corpus=_planted_corpus()
    )
    if result["status"] != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            "a precedent counterparty name in an insert_block_after new_text did "
            f"not block the review: status={result['status']!r}"
        )
        return
    if result.get("field_name") != leakage_scan.BLOCK_OP_NEW_TEXT_FIELD:
        failures.append(
            f"expected field_name={leakage_scan.BLOCK_OP_NEW_TEXT_FIELD!r}, got "
            f"{result.get('field_name')!r}"
        )
    if result.get("docx_bytes") is not None:
        failures.append("a leakage-blocked review produced a document")


def test_replacement_scope_note_is_scanned(failures: list) -> None:
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    response = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [
            _issue(
                "I1",
                "term-length",
                section="Section 1. Term and Fee",
                note="Replaced whole, per the Grimsby Holdings PLC deal.",
            )
        ],
        "block_patches": [
            {
                "block_id": block_ids[0],
                "segments": [
                    {"op": "keep", "text": "The Term shall be "},
                    {"op": "delete", "text": "sixty (60)", "issue_key": "I1"},
                    {"op": "insert", "text": "thirty (30)", "issue_key": "I1"},
                    {
                        "op": "keep",
                        "text": " days from the Effective Date.\nThe Fee shall be "
                        "one hundred dollars per month.",
                    },
                ],
            }
        ],
    }
    result = _run_block_mode(
        _reconciled(response), docx_bytes, corpus=_planted_corpus()
    )
    if result["status"] != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            "a precedent counterparty name in replacement_scope_note did not block "
            f"the review: status={result['status']!r}"
        )
        return
    if result.get("field_name") != "replacement_scope_note":
        failures.append(
            f"expected field_name='replacement_scope_note', got "
            f"{result.get('field_name')!r}"
        )


# ---------------------------------------------------------------------------
# 6: flag-only labelling parity with the quote path (issue #585)
# ---------------------------------------------------------------------------


def test_flag_only_issue_keeps_the_585_labelling(failures: list) -> None:
    docx_bytes = _make_docx(SECTIONS)
    response = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [_issue("I1", "term-length", section="Section 1. Term and Fee")],
    }
    reconciled = _reconciled(response)
    # The label a v3 flag-only issue arrives with is set upstream by
    # `replacement_text_enforcement.check_issues_replacement_text` /
    # `demote_issue_to_flag_only` -- the same two writers the quote path
    # reads it from. Drive the REAL producer rather than hand-stamping it.
    import replacement_text_enforcement as rte

    rte.demote_issue_to_flag_only(reconciled["issues"][0])

    result = _run_block_mode(reconciled, docx_bytes)
    if result["status"] != "OK":
        failures.append(f"a flag-only-only batch is not a failure; got {result['status']!r}")
        return
    if result.get("docx_bytes") is not None:
        failures.append("a flag-only-only batch produced a document")
    report = result.get("analysis_report")
    if not report:
        failures.append("a flag-only issue produced no report")
        return
    if report["report_type"] != "flag_only_report":
        failures.append(f"expected report_type='flag_only_report', got {report['report_type']!r}")
    if report["reason"] != redline_generate.REASON_FLAG_ONLY_ISSUES_PRESENT:
        failures.append(f"unexpected report reason {report['reason']!r}")
    entry_reasons = [entry["reason"] for entry in report["changes_not_applied"]]
    if entry_reasons != [rte.FLAG_ONLY_RETRY_EXHAUSTED]:
        failures.append(
            f"expected the upstream #585 label to survive, got {entry_reasons!r}"
        )


def test_transcript_rejection_is_terminal_and_names_the_failures(failures: list) -> None:
    docx_bytes = _make_docx(SECTIONS)
    response = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [_issue("I1", "term-length", section="Section 1. Term and Fee")],
        "block_patches": [
            {
                "block_id": "p9999",  # not in this document's block map
                "segments": [
                    {"op": "keep", "text": "The Term shall be "},
                    {"op": "delete", "text": "sixty (60)", "issue_key": "I1"},
                    {"op": "insert", "text": "thirty (30)", "issue_key": "I1"},
                ],
            }
        ],
    }
    result = _run_block_mode(_reconciled(response), docx_bytes)
    if result["status"] != "MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"a rejected transcript should be MANUAL_REVIEW_REQUIRED, got "
            f"{result['status']!r}"
        )
        return
    if result.get("reason") != redline_generate.REASON_BLOCK_TRANSCRIPT_REJECTED:
        failures.append(f"unexpected reason {result.get('reason')!r}")
    if result.get("docx_bytes") is not None:
        failures.append("a rejected transcript produced a document")
    reasons = [
        entry.get("reason")
        for entry in result["analysis_report"].get("transcript_failures") or []
    ]
    if "unknown_block_id" not in reasons:
        failures.append(
            f"the report does not name WHICH check rejected the transcript: {reasons!r}"
        )


# ---------------------------------------------------------------------------
# 7: the branch is shape-driven and dormant
# ---------------------------------------------------------------------------


def test_stage_five_branch_is_shape_driven_and_live(failures: list) -> None:
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    v3_reconciled = _reconciled(_multi_edit_response(block_ids))
    if not review_spine.uses_block_mode(v3_reconciled):
        failures.append("a v3 reconciled result does not route to block mode")

    # Issue #627 flipped the ACTIVE validator to v3, so this branch is no
    # longer dormant -- but `uses_block_mode` must still be SHAPE-driven
    # rather than version-driven, which is what the v2 control below proves.
    # It is validated against `OUTPUT_SCHEMA_V2_PATH` EXPLICITLY: after the
    # flip the active artifact rejects it (no `issue_key`), which is the
    # cutover working, not a control worth deleting. What it pins is that
    # `uses_block_mode` routes on the CARRIERS, not on the schema version --
    # a result with no `block_patches`/`block_ops` takes the no-document
    # path (`generate_redline`), and `reconcile()` never invents carriers
    # for one.
    v2_raw = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [
            {
                "section_ref": "Section 1. Term and Fee",
                "section_title": "Section 1. Term and Fee",
                "counterparty_change_summary": "The term was extended.",
                "decision": "REQUEST_CHANGE",
                "source_quote": "sixty (60)",
                "proposed_replacement_text": "thirty (30)",
                "external_rationale_for_footnote": "The standard term is thirty days.",
                "playbook_topic_id": "term-length",
                "internal_precedent_citation": None,
            }
        ],
    }
    ok, v2_parsed = primary_review_pass.validate_model_response(
        json.dumps(v2_raw), schema_path=primary_review_pass.OUTPUT_SCHEMA_V2_PATH
    )
    if not ok:
        failures.append(f"the v2 control response is not schema-valid: {v2_parsed}")
        return
    v2_reconciled = reconciliation.reconcile(primary_result=v2_parsed)
    if review_spine.uses_block_mode(v2_reconciled):
        failures.append("a v2 reconciled result was routed to block mode")
    for key in ("block_patches", "block_ops"):
        if key in v2_reconciled:
            failures.append(
                f"reconcile() added {key!r} to a v2 result; the v2 shape must be "
                "byte-identical to before issue #626"
            )

    # And the carriers really are forwarded for v3, or the branch above could
    # never fire in production.
    if v3_reconciled.get("block_patches") != _multi_edit_response(block_ids)["block_patches"]:
        failures.append("reconcile() did not forward block_patches verbatim")
    if v3_reconciled.get("block_ops") != _multi_edit_response(block_ids)["block_ops"]:
        failures.append("reconcile() did not forward block_ops verbatim")


def test_accept_path_produces_no_document(failures: list) -> None:
    docx_bytes = _make_docx(SECTIONS)
    response = {
        "decision": "ACCEPT",
        "confidence_state": "OK",
        "verdict_summary": "No requested changes identified by tool.",
        "issues": [],
    }
    reconciled = _reconciled(response)
    # `reconcile` only forces REQUEST_CHANGE when issues survive; an ACCEPT
    # with none stays ACCEPT.
    result = _run_block_mode(reconciled, docx_bytes)
    if result["status"] != "OK" or result.get("decision") != "ACCEPT":
        failures.append(
            f"ACCEPT short-circuit broken: status={result['status']!r} "
            f"decision={result.get('decision')!r}"
        )
        return
    if result.get("docx_bytes") is not None:
        failures.append("the ACCEPT path produced a document")


def test_notes_mode_none_writes_no_footnotes(failures: list) -> None:
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    reconciled = _reconciled(_multi_edit_response(block_ids))
    result = _run_block_mode(reconciled, docx_bytes, notes_mode="none")
    if result["status"] != "OK" or not result.get("docx_bytes"):
        failures.append(f"notes_mode='none' broke delivery: {result['status']!r}")
        return
    notes = _footnote_texts(result["docx_bytes"])
    if notes:
        failures.append(f"notes_mode='none' still wrote footnotes: {notes!r}")


def test_notes_mode_internal_marks_the_document(failures: list) -> None:
    """Issue #513's invariant on the block path: the export marker is present
    iff internal-audience content is actually included, and the internal note
    carries its unmissable prefix."""
    import footnote_audience

    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    response = _multi_edit_response(block_ids)
    for issue in response["issues"]:
        issue["internal_rationale_for_footnote"] = (
            "Our standing position is thirty days; do not concede past that."
        )
    reconciled = _reconciled(response)
    result = _run_block_mode(reconciled, docx_bytes, notes_mode="internal")
    if result["status"] != "OK" or not result.get("docx_bytes"):
        failures.append(f"notes_mode='internal' broke delivery: {result['status']!r}")
        return
    delivered = result["docx_bytes"]
    notes = _footnote_texts(delivered)
    if not notes or not all(
        footnote_audience.INTERNAL_FOOTNOTE_PREFIX.strip() in note for note in notes
    ):
        failures.append(f"internal footnotes are not marked: {notes!r}")
    with zipfile.ZipFile(io.BytesIO(delivered)) as zf:
        names = set(zf.namelist())
        marked = any(
            redline_generate.MARKER_TEXT in zf.read(name).decode("utf-8", "replace")
            for name in names
            if name.startswith("word/header") or name.startswith("word/footer")
        )
    if not marked:
        failures.append(
            "notes_mode='internal' delivered internal content with no export marker "
            f"(parts: {sorted(n for n in names if 'head' in n or 'foot' in n)})"
        )
    redline_generate.verify_docx_round_trip(delivered)



def test_an_edit_naming_no_issue_is_terminal(failures: list) -> None:
    """An edit whose `issue_key` resolves to no issue in the response is an
    UNATTRIBUTED change. The v3 schema cannot express that cross-array
    foreign key (draft-07 has no such construct) and
    `block_transcript.validate_block_patches` is never shown the issue list,
    so this is the only place it can be caught -- and it must fail closed,
    not write an unexplained tracked change into the counterparty's
    document."""
    docx_bytes = _make_docx(SECTIONS)
    block_ids = _block_ids(docx_bytes)
    response = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [_issue("I1", "term-length", section="Section 1. Term and Fee")],
        "block_patches": [
            {
                "block_id": block_ids[0],
                "segments": [
                    {"op": "keep", "text": "The Term shall be "},
                    # I9 is on no issue in this response.
                    {"op": "delete", "text": "sixty (60)", "issue_key": "I9"},
                    {"op": "insert", "text": "thirty (30)", "issue_key": "I9"},
                    {
                        "op": "keep",
                        "text": " days from the Effective Date.\nThe Fee shall be "
                        "one hundred dollars per month.",
                    },
                ],
            }
        ],
    }
    result = _run_block_mode(_reconciled(response), docx_bytes)
    if result["status"] != "MANUAL_REVIEW_REQUIRED":
        failures.append(
            "an unattributed edit was not fail-closed: status="
            f"{result['status']!r}"
        )
        return
    if result.get("docx_bytes") is not None:
        failures.append("an unattributed edit still produced a document")
    reasons = [
        entry.get("reason")
        for entry in result["analysis_report"].get("transcript_failures") or []
    ]
    if redline_generate.REASON_UNATTRIBUTED_BLOCK_EDIT not in reasons:
        failures.append(f"the report does not name the unattributed edit: {reasons!r}")


TESTS = [
    test_multi_edit_paragraph_inserted_clause_and_per_issue_footnotes,
    test_derived_replacement_text_overwrites_the_model_supplied_value,
    test_pure_deletion_derives_the_empty_string,
    test_a_whole_block_strike_derives_the_placeholder_and_skips_pen_rules,
    test_partial_change_set_is_rolled_back_and_the_other_issue_delivered,
    test_a_wholly_failed_issue_keeps_its_own_reason,
    test_pen_rules_run_against_the_derived_text,
    test_leaked_insert_segment_blocks_the_review,
    test_leaked_insert_block_new_text_blocks_the_review,
    test_replacement_scope_note_is_scanned,
    test_flag_only_issue_keeps_the_585_labelling,
    test_transcript_rejection_is_terminal_and_names_the_failures,
    test_an_edit_naming_no_issue_is_terminal,
    test_stage_five_branch_is_shape_driven_and_live,
    test_accept_path_produces_no_document,
    test_notes_mode_none_writes_no_footnotes,
    test_notes_mode_internal_marks_the_document,
]


def main() -> int:
    if IMPORT_ERRORS:
        for problem in IMPORT_ERRORS:
            print(f"FAIL: {problem}")
        return 1

    failures: list = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for problem in failures[before:]:
                print(f"FAIL: {problem}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: all block-mode end-to-end (issue #626) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
