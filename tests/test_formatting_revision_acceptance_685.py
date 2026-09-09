#!/usr/bin/env python3
"""
Gate for issue #685: the pre-edit accept-all must accept the counterparty's
pending FORMATTING revisions and tracked MOVES, not just their text edits.

## What this proves

`scripts/extraction_normalization_stage.py::_splice_accept_all` used to
handle `<w:ins>`/`<w:del>` and nothing else, so every `*PrChange`
(`w:rPrChange`, `w:pPrChange`, `w:sectPrChange`, ...) and every tracked-move
element rode through the materializer untouched and into the delivered
redline -- where Word renders them as the counterparty's own
"Formatted: ..." revisions, sitting beside our edits with no way for a
reader to tell them apart (owner-observed on the first production redline of
real counterparty paper, 2026-09-03).

The fixture shapes here are written by Word itself, not by this repo: a user
un-bolding a heading with track changes on produces `<w:rPrChange>`, changing
its indent produces `<w:pPrChange>`, and dragging a clause elsewhere produces
`<w:moveFrom>`/`<w:moveTo>` plus their four range markers. Nothing in
`scripts/` emits any of them -- they arrive in the uploaded document, which
is exactly why the accept-all stage has to dispose of them.

## The hypothesis on the ticket, measured

The ticket asked whether the block map is therefore built over a
not-fully-accepted document -- perturbing the OFFSETS every downstream proof
uses, not merely leaving stray marks. Measured, on this fixture, before the
fix:

  * a `*PrChange` alone perturbs NOTHING. It carries no `<w:t>`, so the
    block map built from a document with pending property changes is
    character-identical to one built from the same document genuinely
    accepted (`test_a_property_change_alone_perturbs_no_block_offsets`).
    For property changes the hypothesis measures FALSE: the defect is the
    cosmetic leak, exactly as first described.
  * a tracked MOVE does perturb it. Before this issue,
    `build_block_map(extract_and_normalize(materialize_accept_all(doc)))`
    was MISSING the moved-in sentence that a genuinely-accepted copy of the
    same document carries, because `<w:moveTo>` survived materialization
    and extraction skips that element
    (`test_block_map_from_materialized_matches_a_genuinely_accepted_document`
    is red without the splice change). Accepting the move closes that half.

## The half this issue did NOT close -- CLOSED SINCE, by issue #686

`_walk_content` (extraction) used to skip `<w:moveFrom>`/`<w:moveTo>`
entirely, while the writer side
(`redline_block_apply._accepted_text_runs`) reads `<w:moveTo>` as visible
text. `scripts/review_spine.py` builds the block map from the ORIGINAL
uploaded bytes, so for a document containing a tracked move the model read a
block whose text was missing the moved-in sentence the delivered document
contains. That divergence predated this issue and was NOT introduced by it
(the writer's accepted view of a `<w:moveTo>` run and of the plain run it
unwraps to are character-identical, so this fix moves no offset). It was
characterized here and left for issue #686, which closed it by making
`<w:moveTo>` a transparent container in `_walk_content` and keeping
`<w:moveFrom>` excluded from both text streams -- deliberately NOT routing
it into `del` mode, which is what would have dropped a moved-away paragraph
to empty text and tripped `normalize_input`'s "no resulting_text --
malformed revision record" refusal. The characterization test that pinned
the open gap (`test_tracked_move_text_is_still_invisible_to_extraction`) is
gone, on its own instructions; `tests/test_tracked_move_extraction_686.py`
now asserts the closed invariant instead.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import block_transcript  # noqa: E402
import extraction_normalization_stage as stage  # noqa: E402
import redline_block_apply  # noqa: E402

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder -- same zipfile-only
# convention as tests/test_accept_all_materializer.py, reproduced locally so
# this file has no import-time dependency on any other test module.
#
# It does have a DEPENDENT: tests/test_tracked_move_extraction_686.py imports
# this module's `_block_map_of` harness and move fixtures rather than
# re-deriving them, on issue #686's explicit instruction (one measurement
# harness for one measurement). Renaming or changing the semantics of
# `_block_map_of`, `_build_docx_bytes`, `_pending_docx`, `_accepted_docx`,
# `_single_block_pending_docx`, `_document_root`, `_w`, `_MOVED_SENTENCE`,
# `_AUTHOR` or `_DATE` therefore moves that file too.
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)

_AUTHOR = "counterparty-counsel"
_DATE = "2026-01-01T00:00:00Z"


def _build_docx_bytes(body_paragraphs_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body_paragraphs_xml}<w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


# --- the pending document -------------------------------------------------
#
# A heading whose run was un-bolded (`w:rPrChange`: the change record holds
# the PREVIOUS `<w:b/>`, the live `<w:rPr>` holds the current italic) and
# whose indent was changed (`w:pPrChange`), then two body paragraphs a
# clause was dragged between (`w:moveFrom` -> `w:moveTo`, bracketed by the
# four range markers Word emits). Synthetic clause text throughout.

_HEADING_TEXT = "Sovereign Immunity"
_MOVED_SENTENCE = "Each party bears its own costs. "

_PENDING_HEADING_P = (
    "<w:p><w:pPr>"
    '<w:pStyle w:val="Heading1"/><w:ind w:left="0"/>'
    f'<w:pPrChange w:id="10" w:author="{_AUTHOR}" w:date="{_DATE}">'
    '<w:pPr><w:pStyle w:val="Heading1"/><w:ind w:left="720"/></w:pPr>'
    "</w:pPrChange>"
    "</w:pPr>"
    "<w:r><w:rPr><w:i/>"
    f'<w:rPrChange w:id="11" w:author="{_AUTHOR}" w:date="{_DATE}">'
    "<w:rPr><w:b/></w:rPr>"
    "</w:rPrChange>"
    f"</w:rPr><w:t>{_HEADING_TEXT}</w:t></w:r></w:p>"
)

_PENDING_BODY_PS = (
    "<w:p><w:r><w:t>Nothing in this Agreement waives any immunity.</w:t></w:r></w:p>"
    f'<w:p><w:moveFromRangeStart w:id="20" w:name="move1" w:author="{_AUTHOR}" w:date="{_DATE}"/>'
    f'<w:moveFrom w:id="21" w:author="{_AUTHOR}" w:date="{_DATE}">'
    f"<w:r><w:delText>{_MOVED_SENTENCE}</w:delText></w:r></w:moveFrom>"
    '<w:moveFromRangeEnd w:id="22"/>'
    "<w:r><w:t>Fees are payable monthly.</w:t></w:r></w:p>"
    f'<w:p><w:moveToRangeStart w:id="23" w:name="move1" w:author="{_AUTHOR}" w:date="{_DATE}"/>'
    f'<w:moveTo w:id="24" w:author="{_AUTHOR}" w:date="{_DATE}">'
    f"<w:r><w:t>{_MOVED_SENTENCE}</w:t></w:r></w:moveTo>"
    '<w:moveToRangeEnd w:id="25"/>'
    "<w:r><w:t>Costs are not reimbursable.</w:t></w:r></w:p>"
)

# --- the same document with those revisions GENUINELY accepted ------------
#
# What Word itself writes out after "Accept All Changes": the current
# properties kept with no change record, the moved-from span gone, the
# moved-to span an ordinary run.

_ACCEPTED_HEADING_P = (
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/><w:ind w:left="0"/></w:pPr>'
    f"<w:r><w:rPr><w:i/></w:rPr><w:t>{_HEADING_TEXT}</w:t></w:r></w:p>"
)

_ACCEPTED_BODY_PS = (
    "<w:p><w:r><w:t>Nothing in this Agreement waives any immunity.</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Fees are payable monthly.</w:t></w:r></w:p>"
    f"<w:p><w:r><w:t>{_MOVED_SENTENCE}</w:t></w:r>"
    "<w:r><w:t>Costs are not reimbursable.</w:t></w:r></w:p>"
)


def _pending_docx() -> bytes:
    return _build_docx_bytes(_PENDING_HEADING_P + _PENDING_BODY_PS)


def _accepted_docx() -> bytes:
    return _build_docx_bytes(_ACCEPTED_HEADING_P + _ACCEPTED_BODY_PS)


def _property_change_only_docx() -> bytes:
    """The formatting half ALONE -- no move anywhere in the document."""
    return _build_docx_bytes(
        _PENDING_HEADING_P
        + "<w:p><w:r><w:t>Nothing in this Agreement waives any immunity.</w:t></w:r></w:p>"
    )


def _property_change_only_accepted_docx() -> bytes:
    return _build_docx_bytes(
        _ACCEPTED_HEADING_P
        + "<w:p><w:r><w:t>Nothing in this Agreement waives any immunity.</w:t></w:r></w:p>"
    )


def _single_block_pending_docx() -> bytes:
    """The same pending markup packed into ONE body paragraph under the
    heading, so a whole-block replace has a single physical `<w:p>` to write
    into (`redline_block_apply` refuses an edit that crosses a physical
    paragraph boundary -- `spans_physical_paragraph`). Used by the
    end-to-end case below."""
    return _build_docx_bytes(
        _PENDING_HEADING_P
        + f'<w:p><w:moveToRangeStart w:id="30" w:name="move2" w:author="{_AUTHOR}" w:date="{_DATE}"/>'
        f'<w:moveTo w:id="31" w:author="{_AUTHOR}" w:date="{_DATE}">'
        f"<w:r><w:t>{_MOVED_SENTENCE}</w:t></w:r></w:moveTo>"
        '<w:moveToRangeEnd w:id="32"/>'
        "<w:r><w:rPr>"
        f'<w:rPrChange w:id="33" w:author="{_AUTHOR}" w:date="{_DATE}">'
        "<w:rPr><w:b/></w:rPr></w:rPrChange>"
        "</w:rPr><w:t>Costs are not reimbursable.</w:t></w:r></w:p>"
    )


def _tracked_cell_insertion_docx() -> bytes:
    """A tracked table-ROW insertion (`<w:trPr><w:ins/></w:trPr>`) whose
    cell carries `<w:cellIns>` -- revision markup this module deliberately
    does NOT apply, because accepting it is a structural table edit rather
    than a marker removal. Word writes exactly this shape when a row is
    added with track changes on."""
    return _build_docx_bytes(
        "<w:tbl><w:tr>"
        f'<w:trPr><w:ins w:id="40" w:author="{_AUTHOR}" w:date="{_DATE}"/></w:trPr>'
        "<w:tc>"
        f'<w:tcPr><w:cellIns w:id="41" w:author="{_AUTHOR}" w:date="{_DATE}"/></w:tcPr>'
        "<w:p><w:r><w:t>Territory: worldwide.</w:t></w:r></w:p>"
        "</w:tc></w:tr></w:tbl>"
        "<w:p><w:r><w:t>The schedule above forms part of this Agreement.</w:t></w:r></w:p>"
    )


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _document_root(docx_bytes: bytes) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return ET.fromstring(zf.read("word/document.xml"))


def _revision_markup_counts(docx_bytes: bytes) -> dict[str, int]:
    """Every revision-tracking element left in the part, by local name --
    read straight out of the delivered bytes, never from the materializer's
    own report about itself."""
    counts: dict[str, int] = {}
    for el in _document_root(docx_bytes).iter():
        local = el.tag.split("}")[-1]
        if el.tag.startswith(f"{{{WORD_NS}}}") and (
            local.endswith("Change")
            or local.startswith("move")
            or local in ("ins", "del", "cellIns", "cellDel", "cellMerge")
        ):
            counts[local] = counts.get(local, 0) + 1
    return counts


def _block_map_of(docx_bytes: bytes) -> dict:
    normalized = stage.extract_and_normalize(docx_bytes)
    if normalized["status"] != "normalized":
        raise AssertionError(
            f"fixture must normalize, got {normalized['status']!r}: "
            f"{normalized.get('analysis_report')}"
        )
    return stage.build_block_map(normalized["paragraphs"])


def _apply_whole_block_replacement(docx_bytes: bytes, new_text: str) -> dict:
    """Replace the FIRST block's entire text through the REAL validate ->
    compile path, proving the transcript against the same bytes the compiler
    is handed (same helper shape as
    tests/test_accept_all_materializer.py)."""
    normalized = stage.extract_and_normalize(docx_bytes)
    block_map = stage.build_block_map(normalized["paragraphs"])
    block_id, block = next(iter(block_map.items()))
    proven = block_transcript.validate_block_patches(
        [
            {
                "block_id": block_id,
                "segments": [
                    {"op": "delete", "text": block["text"], "issue_key": "I1"},
                    {"op": "insert", "text": new_text, "issue_key": "I1"},
                ],
            }
        ],
        [],
        block_map,
    )
    if proven["status"] != "proven":
        return {"proven": False, "applied": [], "failures": proven["failures"], "docx_bytes": None}
    result = redline_block_apply.apply_block_transcript(
        docx_bytes,
        proven,
        author="contract-toaster",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    return dict(result, proven=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_property_changes_and_moves_do_not_survive_materialization(failures: list) -> None:
    """The ticket's verification bar, first half: an operative draft with NO
    `*Change` / `moveFrom` / `moveTo` element left in it."""
    pending = _pending_docx()
    before = _revision_markup_counts(pending)
    if before != {
        "pPrChange": 1,
        "rPrChange": 1,
        "moveFromRangeStart": 1,
        "moveFrom": 1,
        "moveFromRangeEnd": 1,
        "moveToRangeStart": 1,
        "moveTo": 1,
        "moveToRangeEnd": 1,
    }:
        failures.append(
            f"the fixture itself must carry the pending formatting/move "
            f"markup this issue is about; got {before!r}"
        )
        return

    materialized, report = stage.materialize_accept_all_with_report(pending)

    left = _revision_markup_counts(materialized)
    if left:
        failures.append(
            f"the operative draft still carries the counterparty's own "
            f"revision markup: {left!r}"
        )
    if report["accepted"] != before:
        failures.append(
            f"the report must account for every element accepted; "
            f"accepted={report['accepted']!r} vs. present-in-source={before!r}"
        )
    if report["unapplied"]:
        failures.append(
            f"nothing in this fixture is unrecognised, yet the report claims "
            f"{report['unapplied']!r}"
        )


def test_current_formatting_survives_the_accept(failures: list) -> None:
    """Accepting a formatting revision KEEPS the current properties and
    drops the change record -- it must never restore the previous ones the
    `*PrChange` subtree holds (here: the heading stays italic and un-bolded,
    and keeps the current `w:ind`, never the pending change's `720`)."""
    materialized = stage.materialize_accept_all(_pending_docx())
    root = _document_root(materialized)
    heading_p = next(iter(root.iter(_w("p"))))

    rpr = heading_p.find(f"{_w('r')}/{_w('rPr')}")
    if rpr is None or rpr.find(_w("i")) is None:
        failures.append(
            "the run's CURRENT formatting (italic) must survive accepting the rPrChange"
        )
    if rpr is not None and rpr.find(_w("b")) is not None:
        failures.append(
            "accepting an rPrChange must not restore the PREVIOUS formatting "
            "(bold) the change record holds"
        )

    ind = heading_p.find(f"{_w('pPr')}/{_w('ind')}")
    if ind is None or ind.get(_w("left")) != "0":
        failures.append(
            f"the paragraph's CURRENT indent must survive accepting the "
            f"pPrChange; got {None if ind is None else ind.attrib!r}"
        )

    text = "".join(t.text or "" for t in heading_p.iter(_w("t")))
    if text != _HEADING_TEXT:
        failures.append(f"the heading's text must be untouched; got {text!r}")


def test_block_map_from_materialized_matches_a_genuinely_accepted_document(
    failures: list,
) -> None:
    """The ticket comment's assertion 2: the offsets a transcript is proven
    against must be the ACCEPTED-state offsets. Red before this issue --
    `<w:moveTo>` survived materialization, extraction skips that element,
    and the block map was therefore missing the moved-in sentence a
    genuinely-accepted copy of the same document carries."""
    materialized = stage.materialize_accept_all(_pending_docx())
    from_materialized = _block_map_of(materialized)
    from_accepted = _block_map_of(_accepted_docx())

    if from_materialized != from_accepted:
        failures.append(
            f"the block map built from the materialized document must equal "
            f"the one built from the same document genuinely accepted.\n"
            f"  materialized: {from_materialized!r}\n"
            f"  accepted:     {from_accepted!r}"
        )
    if _MOVED_SENTENCE.strip() not in "".join(
        block["text"] for block in from_materialized.values()
    ):
        failures.append(
            "the moved-in sentence must be part of the accepted block text "
            "(an accepted move KEEPS the text at its new location)"
        )


def test_a_property_change_alone_perturbs_no_block_offsets(failures: list) -> None:
    """The hypothesis, measured on the property-change half alone: FALSE.
    A `*PrChange` carries no `<w:t>`, so a document with pending formatting
    revisions and the same document genuinely accepted produce
    character-identical block maps -- with AND without the materializer.
    For formatting revisions the defect is the cosmetic leak into the
    delivered redline, not a perturbed offset. Recorded so the deeper
    version of the hypothesis is not re-litigated from scratch."""
    pending = _property_change_only_docx()
    accepted = _property_change_only_accepted_docx()

    from_source = _block_map_of(pending)
    from_materialized = _block_map_of(stage.materialize_accept_all(pending))
    from_accepted = _block_map_of(accepted)

    if from_source != from_accepted:
        failures.append(
            f"MEASUREMENT CHANGED: a pending property change now perturbs "
            f"the block map built from the source bytes. This test recorded "
            f"the opposite; re-measure before re-asserting.\n"
            f"  source:   {from_source!r}\n  accepted: {from_accepted!r}"
        )
    if from_materialized != from_accepted:
        failures.append(
            f"materializing a property-change-only document must not move a "
            f"single character.\n  materialized: {from_materialized!r}\n"
            f"  accepted:     {from_accepted!r}"
        )


def test_materialization_moves_no_offset_the_writer_can_see(failures: list) -> None:
    """The no-regression invariant behind running the materializer on
    documents it never used to touch: accepting a formatting revision or a
    tracked move must not shift a single character of the WRITER's accepted
    view (`redline_block_apply._accepted_text`, the same view
    `docx_editor`'s text map computes and every applied offset is measured
    against). A `*PrChange` carries no text at all, and a `<w:moveTo>` run
    is already in that view before the wrapper is unwrapped -- so the view
    is character-identical, paragraph for paragraph, before and after."""
    for label, docx_bytes in (
        ("formatting + move", _pending_docx()),
        ("formatting only", _property_change_only_docx()),
        ("single block", _single_block_pending_docx()),
    ):
        before = [
            redline_block_apply._accepted_text(p) for p in _document_root(docx_bytes).iter(_w("p"))
        ]
        after = [
            redline_block_apply._accepted_text(p)
            for p in _document_root(stage.materialize_accept_all(docx_bytes)).iter(_w("p"))
        ]
        if before != after:
            failures.append(
                f"[{label}] materialization changed the writer's accepted "
                f"view -- every offset already computed against it would "
                f"move.\n  before: {before!r}\n  after:  {after!r}"
            )


def test_disclosure_names_the_accepted_kinds(failures: list) -> None:
    """Issue #563's "never silent" rule, extended: the count and kinds of
    accepted formatting revisions are recorded the same way accepted text
    revisions are."""
    _, report = stage.materialize_accept_all_with_report(_pending_docx())
    note = stage.accepted_revision_disclosure(report)

    if not note:
        failures.append("accepting formatting revisions must never be silent")
        return
    for kind in ("rPrChange", "pPrChange", "moveTo", "moveFrom"):
        if kind not in note:
            failures.append(
                f"the disclosure must name what was accepted; {kind!r} missing from {note!r}"
            )
    if "accepted-all into the operative draft." in note:
        failures.append(
            "the disclosure must NOT borrow the literal tail "
            "frontend/src/toaster/receipt.ts::acceptedChangesSummary counts "
            "-- an unparseable extra sentence carrying it makes that "
            "function drop the whole per-edit summary line"
        )


def test_unrecognized_revision_markup_is_reported_and_left_in_place(failures: list) -> None:
    """The fail-closed half: a revision kind the splice has no rule for
    (`w:cellIns` -- accepting it is a structural table edit) must be
    REPORTED, not silently passed through, and must not take the document
    down with it."""
    docx_bytes = _tracked_cell_insertion_docx()
    materialized, report = stage.materialize_accept_all_with_report(docx_bytes)

    if report["unapplied"].get("cellIns") != 1:
        failures.append(
            f"an unrecognised revision kind must be reported; got "
            f"unapplied={report['unapplied']!r}"
        )
    note = stage.accepted_revision_disclosure(report)
    if not note or "cellIns" not in note:
        failures.append(f"the disclosure must name what was left in place; got {note!r}")
    if _revision_markup_counts(materialized).get("cellIns") != 1:
        failures.append(
            "an unrecognised revision kind must be LEFT IN PLACE, not "
            "guessed at -- reporting it is the disposition"
        )
    normalized = stage.extract_and_normalize(materialized)
    if normalized["status"] != "normalized":
        failures.append(
            f"reporting an unapplied revision kind must not fail the "
            f"document closed; got {normalized['status']!r}"
        )


def test_a_document_with_nothing_to_accept_is_returned_byte_identical(failures: list) -> None:
    """The no-op property the spine now relies on: running the materializer
    unconditionally must not touch a document that has nothing pending."""
    clean = _accepted_docx()
    materialized, report = stage.materialize_accept_all_with_report(clean)

    if materialized != clean:
        failures.append("a document with nothing to accept must come back byte-identical")
    if report["accepted"] or report["unapplied"]:
        failures.append(f"a clean document must report nothing accepted; got {report!r}")
    if stage.accepted_revision_disclosure(report) is not None:
        failures.append("a clean document must produce no disclosure sentence")


def test_delivered_redline_carries_no_counterparty_formatting_revision(failures: list) -> None:
    """End to end, on the real block-transcript writer: an edit proven and
    compiled against the materialized bytes delivers a document carrying
    ONLY the toaster's own revisions -- no `Formatted: ...` revision
    attributed to the counterparty's author survives for the attorney's
    reader to mistake for something we asked for."""
    materialized = stage.materialize_accept_all(_single_block_pending_docx())
    result = _apply_whole_block_replacement(
        materialized, "Each party bears its own costs, which are not reimbursable."
    )
    if not result["applied"] or result["docx_bytes"] is None:
        failures.append(
            f"the edit must prove and compile against the materialized "
            f"bytes; applied={result['applied']} failures={result.get('failures')}"
        )
        return

    delivered = result["docx_bytes"]
    left = {
        name: count
        for name, count in _revision_markup_counts(delivered).items()
        if name not in ("ins", "del")
    }
    if left:
        failures.append(f"the delivered redline still carries formatting/move markup: {left!r}")

    authors = {
        el.get(_w("author"))
        for el in _document_root(delivered).iter()
        if el.get(_w("author"))
    }
    if authors != {"contract-toaster"}:
        failures.append(
            f"the delivered redline must carry only the toaster's own "
            f"revisions; got authors={authors!r}"
        )


TESTS = [
    test_property_changes_and_moves_do_not_survive_materialization,
    test_current_formatting_survives_the_accept,
    test_block_map_from_materialized_matches_a_genuinely_accepted_document,
    test_a_property_change_alone_perturbs_no_block_offsets,
    test_materialization_moves_no_offset_the_writer_can_see,
    test_disclosure_names_the_accepted_kinds,
    test_unrecognized_revision_markup_is_reported_and_left_in_place,
    test_a_document_with_nothing_to_accept_is_returned_byte_identical,
    test_delivered_redline_carries_no_counterparty_formatting_revision,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        print(("PASS: " if len(failures) == before else "FAIL: ") + test.__name__)

    if failures:
        print()
        for failure in failures:
            print(f"  - {failure}")
        print(f"\nFAIL: {len(failures)} issue(s) found.")
        return 1
    print("\nPASS: pending formatting revisions and tracked moves are accepted (issue #685).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
