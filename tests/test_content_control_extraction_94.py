#!/usr/bin/env python3
"""
Gate for issue #94: extraction must not silently drop text a reader SEES
just because Word wrapped it in a content control (`w:sdt`/`w:sdtContent`)
or a smart tag (`w:smartTag`) -- and the three views of a paragraph's text
(this repo's extractor, this repo's writer-side `_accepted_text_runs`, and
`docx_editor`, the library that actually writes the tracked change) must
line up the way the section below spells out, not merely two of the three.

## The divergence this closes

`extraction_normalization_stage._walk_content` (inline content) and
`_iter_body_paragraphs` (block-level content) used to skip `<w:sdt>` and
`<w:smartTag>` WITHOUT recursion, exactly like the textbox/drawing wrappers
that stay excluded on purpose. That is wrong for the overwhelming majority
of real `<w:sdt>` elements: a Word template's Cover Page / Quick Parts /
Date Picker controls, and any document-assembly tool's merge fields, are
FILLED IN with the actual party name, date, or address before the document
is sent -- Word renders that fill-in as ordinary visible text, exactly like
a `<w:hyperlink>` (issue #663) or a tracked `<w:moveTo>` (issue #686). A
content control around a whole clause is the worst case: the model reviewed
"This Agreement is between  and Buyer." with the party name gone from the
middle, or lost an entire controlled clause outright, silently -- no
normalization note said anything was skipped.

`redline_block_apply._accepted_text_runs` (the writer's "what does a reader
see" walk) already recursed into every container except `<w:del>`/
`<w:moveFrom>`, so a filled-in control's text was already reachable on the
WRITER side while invisible to extraction -- the same shape of divergence
issues #663 and #686 each closed for one container at a time.

## The one shape that stays excluded, and the THREE views that must line up

ARCHITECTURE.md's OOXML part allowlist has always scoped the exclusion
narrowly: "Content-control placeholders (`w:sdt` with display-only
content)". That is `<w:sdtPr><w:showingPlcHdr/></w:sdtPr>` -- Word's own
marker that a control is currently showing its DISPLAY-ONLY guidance text
("Click here to enter a date"), because nobody has filled it in. That is
never something a reader would take as part of the agreement, so it is
excluded from extraction exactly as before -- `extraction_normalization_
stage.sdt_is_placeholder` is the one place that flag is read.

That exclusion is EXTRACTION-ONLY, and the reason is a third view this file
tests against directly: `docx_editor.xml_editor.build_text_map`, the map
the library that actually writes a tracked change counts `occurrence=`
over. It keeps an unfilled control's display copy, so
`redline_block_apply._accepted_text_runs` -- which exists to mirror it
character for character -- keeps it too. Teaching the writer the
extractor's carve-out instead makes this repo's two walks agree with each
other and both disagree with `docx_editor`: a placeholder-bearing paragraph
then RESOLVES, and an `occurrence=` counted over placeholder-free text
addresses the display copy, so the tracked change is written inside the
empty control and the projection check voids the whole batch.

What the three views actually do, therefore:

- FILLED-IN control / smart tag: all three read the paragraph identically,
  edits resolve and land (`test_all_three_views_agree_character_for_
  character`, `test_repeated_word_content_control_edit_lands_in_the_
  operative_text`).
- UNFILLED control: extraction drops the display copy, the two writer-side
  views keep it, so the paragraph does not resolve and the edits touching
  it -- and only those -- fail closed as `paragraph_not_resolved` while the
  rest of the batch still lands
  (`test_placeholder_control_edit_fails_closed_without_voiding_the_batch`).
  That limitation is recorded in ARCHITECTURE.md's OOXML part allowlist.

## Fixture provenance

Every fixture below is hand-built raw OOXML (`zipfile` + `ElementTree`, no
python-docx), matching this repo's dependency-free `scripts/docx_parts.py`
/ `tests/test_extraction_normalization_stage_80.py` convention. Clause text
is entirely synthetic -- no real party names, no vendored third-party
paper.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import block_transcript  # noqa: E402
import docx_editor  # noqa: E402
import extraction_normalization_stage as stage  # noqa: E402
import redline_block_apply  # noqa: E402

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder -- reproduced locally (same
# convention as test_formatting_revision_acceptance_685.py) so this file has
# no import-time dependency on any other test module.
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


def _document_root(docx_bytes: bytes) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return ET.fromstring(zf.read("word/document.xml"))  # noqa: S314


def _heading_p(text: str) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _r(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def _block_map_of(docx_bytes: bytes) -> dict:
    normalized = stage.extract_and_normalize(docx_bytes)
    if normalized["status"] != "normalized":
        raise AssertionError(
            f"fixture must normalize, got {normalized['status']!r}: {normalized.get('analysis_report')}"
        )
    return stage.build_block_map(normalized["paragraphs"])


# ---------------------------------------------------------------------------
# Fixtures -- the exact shapes this issue's Evidence section names
# ---------------------------------------------------------------------------

_PARTIES_HEADING = "Parties"
_FEES_HEADING = "Fees"
_CONTROLLED_HEADING = "Controlled Clause"
_SCHEDULE_HEADING = "Schedule"

_FILLED_PARTY_NAME = "Acme Corp"
_CONTROLLED_CLAUSE_TEXT = "Controlled clause text."
_FOLLOWING_PARAGRAPH_TEXT = "Following paragraph."
_FILLED_DATE = "January 1, 2027"

# The placeholder's display copy deliberately REPEATS a word ("Buyer") that
# also occurs in the operative sentence outside the control. That is the
# shape that turns an offset/coordinate disagreement between
# `_accepted_text_runs` and `docx_editor` into a wrong-span write rather
# than a harmless one -- see
# `test_placeholder_control_edit_fails_closed_without_voiding_the_batch`.
_PLACEHOLDER_DISPLAY_COPY = "[Enter Buyer legal name]"
_CLEAN_CLAUSE_TEXT = "Fees are due within 30 days."
_ROW_CONTROLLED_TEXT = "Supplier delivers the first milestone."
_CELL_CONTROLLED_TEXT = "Acceptance testing runs for ten business days."


def _inline_sdt_party_name_docx() -> bytes:
    """A filled-in inline content control wraps the party name -- the
    issue's exact evidence: 'This Agreement is between Acme Corp and
    Buyer.'"""
    body = _heading_p(_PARTIES_HEADING) + (
        "<w:p>"
        + _r("This Agreement is between ")
        + "<w:sdt><w:sdtPr/><w:sdtContent>"
        + _r(_FILLED_PARTY_NAME)
        + "</w:sdtContent></w:sdt>"
        + _r(" and Buyer.")
        + "</w:p>"
    )
    return _build_docx_bytes(body)


def _inline_sdt_repeated_word_docx() -> bytes:
    """A FILLED-IN inline content control whose text repeats a word that
    also occurs in the operative sentence OUTSIDE the control ("Acme Corp"
    twice). All three views -- extractor, `redline_block_apply.
    _accepted_text_runs`, `docx_editor` -- must read this paragraph
    identically, or the `occurrence=` computed from the block map addresses
    the wrong one of the two copies and the tracked change lands inside the
    control."""
    body = _heading_p(_PARTIES_HEADING) + (
        "<w:p>"
        + _r("This Agreement is between ")
        + "<w:sdt><w:sdtPr/><w:sdtContent>"
        + _r(_FILLED_PARTY_NAME)
        + "</w:sdtContent></w:sdt>"
        + _r(f" and Buyer; {_FILLED_PARTY_NAME} shall invoice Buyer monthly.")
        + "</w:p>"
    )
    return _build_docx_bytes(body)


def _inline_sdt_placeholder_docx() -> bytes:
    """The SAME shape, but the control is genuinely showing its
    display-only placeholder -- `w:sdtPr/w:showingPlcHdr` -- because nobody
    filled it in. Must stay excluded from EXTRACTION.

    Two deliberate properties beyond that: the display copy repeats a word
    ("Buyer") the operative sentence also uses, and the document carries a
    SECOND, control-free clause. Together they let the end-to-end test
    below distinguish the three possible outcomes of a batch that touches
    this paragraph -- the tracked change lands in the operative text, it
    lands inside the empty control, or the whole batch is voided -- rather
    than only proving "something failed"."""
    body = (
        _heading_p(_PARTIES_HEADING)
        + (
            "<w:p>"
            + _r("This Agreement is between ")
            + "<w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent>"
            + _r(_PLACEHOLDER_DISPLAY_COPY)
            + "</w:sdtContent></w:sdt>"
            + _r(" and Buyer.")
            + "</w:p>"
        )
        + _heading_p(_FEES_HEADING)
        + f"<w:p>{_r(_CLEAN_CLAUSE_TEXT)}</w:p>"
    )
    return _build_docx_bytes(body)


def _block_level_sdt_docx() -> bytes:
    """A block-level content control wraps a WHOLE controlled clause
    paragraph -- the issue's other exact evidence: dropping this removed
    the controlled paragraph from the review entirely."""
    body = (
        _heading_p(_CONTROLLED_HEADING)
        + "<w:sdt><w:sdtPr/><w:sdtContent>"
        + f"<w:p>{_r(_CONTROLLED_CLAUSE_TEXT)}</w:p>"
        + "</w:sdtContent></w:sdt>"
        + f"<w:p>{_r(_FOLLOWING_PARAGRAPH_TEXT)}</w:p>"
    )
    return _build_docx_bytes(body)


def _block_level_sdt_placeholder_docx() -> bytes:
    """Same shape, but the block-level control is a genuine, unfilled
    placeholder. The controlled paragraph must stay excluded, same as
    ARCHITECTURE.md has always documented -- only now for the RIGHT
    reason (a real placeholder flag), not merely because it is wrapped
    in `w:sdt` at all."""
    body = (
        _heading_p(_CONTROLLED_HEADING)
        + "<w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent>"
        + f"<w:p>{_r('[Insert controlled clause text here]')}</w:p>"
        + "</w:sdtContent></w:sdt>"
        + f"<w:p>{_r(_FOLLOWING_PARAGRAPH_TEXT)}</w:p>"
    )
    return _build_docx_bytes(body)


def _table_sdt_docx(*, level: str, placeholder: bool) -> bytes:
    """A content control at a table's ROW level
    (`<w:tbl><w:sdt><w:sdtContent><w:tr>`, what Word's repeating-section
    control emits) or CELL level (`<w:tr><w:sdt><w:sdtContent><w:tc>`).
    Both are containers `_iter_table_paragraphs` has to descend through
    transparently, or every paragraph inside them drops out of the review
    exactly as a block-level control's did."""
    controlled_row = (
        "<w:tr><w:tc><w:p>"
        + _r(_ROW_CONTROLLED_TEXT)
        + "</w:p></w:tc><w:tc><w:p>"
        + _r(_FILLED_DATE)
        + "</w:p></w:tc></w:tr>"
    )
    controlled_cell = f"<w:tc><w:p>{_r(_CELL_CONTROLLED_TEXT)}</w:p></w:tc>"
    sdt_pr = "<w:sdtPr><w:showingPlcHdr/></w:sdtPr>" if placeholder else "<w:sdtPr/>"
    if level == "row":
        rows = (
            f"<w:tr><w:tc><w:p>{_r('Milestone')}</w:p></w:tc>"
            f"<w:tc><w:p>{_r('Date')}</w:p></w:tc></w:tr>"
            f"<w:sdt>{sdt_pr}<w:sdtContent>{controlled_row}</w:sdtContent></w:sdt>"
        )
    else:
        rows = (
            f"<w:tr><w:sdt>{sdt_pr}<w:sdtContent>{controlled_cell}</w:sdtContent></w:sdt>"
            f"<w:tc><w:p>{_r(_FILLED_DATE)}</w:p></w:tc></w:tr>"
        )
    return _build_docx_bytes(_heading_p(_SCHEDULE_HEADING) + f"<w:tbl>{rows}</w:tbl>")


def _smart_tag_date_docx() -> bytes:
    """A smart tag (Word's own auto-detected date markup) wraps a date --
    the issue's exact evidence: 'Pay January 1, 2027.' -> 'Pay .'"""
    body = _heading_p(_FEES_HEADING) + (
        "<w:p>"
        + _r("Pay ")
        + '<w:smartTag w:uri="urn:schemas-microsoft-com:office:smarttags" w:element="date">'
        + _r(_FILLED_DATE)
        + "</w:smartTag>"
        + _r(".")
        + "</w:p>"
    )
    return _build_docx_bytes(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_filled_in_content_control_reaches_extraction(failures: list) -> None:
    result = stage.extract_and_normalize(_inline_sdt_party_name_docx())
    if result.get("status") != "normalized":
        failures.append(f"fixture must normalize, got: {result}")
        return
    text = result["paragraphs"][0]["text"]
    expected = f"This Agreement is between {_FILLED_PARTY_NAME} and Buyer."
    if text != expected:
        failures.append(
            f"a filled-in inline content control must be a transparent "
            f"wrapper -- expected {expected!r}, got {text!r}"
        )


def test_placeholder_content_control_still_excluded(failures: list) -> None:
    result = stage.extract_and_normalize(_inline_sdt_placeholder_docx())
    if result.get("status") != "normalized":
        failures.append(f"fixture must normalize, got: {result}")
        return
    text = result["paragraphs"][0]["text"]
    if _PLACEHOLDER_DISPLAY_COPY in text:
        failures.append(
            f"a genuine placeholder (w:sdtPr/w:showingPlcHdr) must stay "
            f"excluded from extraction, per ARCHITECTURE.md's OOXML part "
            f"allowlist. Got: {text!r}"
        )
    if "This Agreement is between" not in text or "and Buyer." not in text:
        failures.append(f"the surrounding, non-placeholder text must still extract. Got: {text!r}")


def test_block_level_content_control_reaches_extraction(failures: list) -> None:
    result = stage.extract_and_normalize(_block_level_sdt_docx())
    if result.get("status") != "normalized":
        failures.append(f"fixture must normalize, got: {result}")
        return
    headings = [p["heading"] for p in result["paragraphs"]]
    if _CONTROLLED_HEADING not in headings:
        failures.append(f"the controlled clause's own heading must survive. Got headings: {headings!r}")
        return
    controlled = next(p for p in result["paragraphs"] if p["heading"] == _CONTROLLED_HEADING)
    if _CONTROLLED_CLAUSE_TEXT not in controlled["text"]:
        failures.append(
            f"a block-level content control wrapping a whole clause "
            f"paragraph must be transparent -- the controlled clause must "
            f"not be dropped from the review. Got: {controlled['text']!r}"
        )


def test_block_level_placeholder_control_still_excluded(failures: list) -> None:
    result = stage.extract_and_normalize(_block_level_sdt_placeholder_docx())
    if result.get("status") != "normalized":
        failures.append(f"fixture must normalize, got: {result}")
        return
    serialized = repr(result)
    if "Insert controlled clause text here" in serialized:
        failures.append(
            "a genuinely unfilled block-level placeholder control must "
            "stay excluded, same as an inline one."
        )


def test_smart_tag_wrapped_text_reaches_extraction(failures: list) -> None:
    result = stage.extract_and_normalize(_smart_tag_date_docx())
    if result.get("status") != "normalized":
        failures.append(f"fixture must normalize, got: {result}")
        return
    text = result["paragraphs"][0]["text"]
    if text != f"Pay {_FILLED_DATE}.":
        failures.append(
            f"a smart tag must be a transparent wrapper, exactly like an "
            f"unfilled content control is not -- expected 'Pay "
            f"{_FILLED_DATE}.', got {text!r}"
        )


def _docx_editor_paragraph_texts(docx_bytes: bytes) -> list[str]:
    """The THIRD view: what `docx_editor` -- the library that actually
    writes the tracked change -- reads each `<w:p>` as, in the same
    preorder numbering `redline_block_apply._body_paragraph_elements`
    uses (`Document.get_paragraph` is 1-based over that same order).

    `Paragraph.text` is `xml_editor.build_text_map(view="accepted")`, the
    map `doc.replace(..., occurrence=N)` counts occurrences in. Reading it
    here is the whole point of `test_all_three_views_agree_character_for_
    character` below: extraction and `_accepted_text_runs` can be edited in
    lockstep and agree with each other while both disagree with the
    consumer that writes."""
    with tempfile.TemporaryDirectory(prefix="ct-94-third-view-") as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / "input.docx"
        input_path.write_bytes(docx_bytes)
        doc = docx_editor.Document.open(
            input_path, author="issue-94-probe", workspace_dir=str(tmp_path / "workspace")
        )
        count = len(list(_document_root(docx_bytes).iter(_w("p"))))
        return [doc.get_paragraph(index + 1).text for index in range(count)]


def _paragraphs_containing_a_placeholder(docx_bytes: bytes) -> set:
    """The `<w:p>` indexes (preorder, 0-based) whose accepted view carries
    an unfilled control's display-only copy -- the paragraphs extraction
    deliberately reads differently from the two writer-side views."""
    return {
        index
        for index, p_el in enumerate(_document_root(docx_bytes).iter(_w("p")))
        if any(stage.sdt_is_placeholder(sdt) for sdt in p_el.iter(_w("sdt")))
    }


# Every fixture, labelled with whether it carries an unfilled control.
_ALL_FIXTURES = (
    ("inline filled-in", _inline_sdt_party_name_docx),
    ("inline filled-in, repeated word", _inline_sdt_repeated_word_docx),
    ("inline placeholder", _inline_sdt_placeholder_docx),
    ("block-level filled-in", _block_level_sdt_docx),
    ("block-level placeholder", _block_level_sdt_placeholder_docx),
    ("smart tag", _smart_tag_date_docx),
    ("table row-level control", lambda: _table_sdt_docx(level="row", placeholder=False)),
    ("table cell-level control", lambda: _table_sdt_docx(level="cell", placeholder=False)),
    ("table row-level placeholder", lambda: _table_sdt_docx(level="row", placeholder=True)),
    ("table cell-level placeholder", lambda: _table_sdt_docx(level="cell", placeholder=True)),
)


def test_all_three_views_agree_character_for_character(failures: list) -> None:
    """`redline_block_apply._accepted_text_runs` says it "mirrors
    `docx_editor.xml_editor.build_text_map(view="accepted")` exactly", and
    every offset and `occurrence=` this repo hands that library means what
    it means only because that claim holds. Pin it against the library
    itself, for every `w:sdt`/`w:smartTag` shape this issue touches.

    This is the assertion that a comparison of extraction against
    `_accepted_text` CANNOT make: those two are this repo's own code, so
    changing both together keeps them agreeing while silently redefining
    what an offset means to the writer. That is exactly the regression the
    first cut of this issue shipped -- excluding a placeholder `w:sdt` from
    `_accepted_text_runs` made the paragraph resolve, and the
    `occurrence=0` computed from placeholder-free text then addressed the
    PLACEHOLDER's copy of a repeated word."""
    for label, build in _ALL_FIXTURES:
        docx_bytes = build()
        elements = list(_document_root(docx_bytes).iter(_w("p")))
        try:
            third_view = _docx_editor_paragraph_texts(docx_bytes)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{label}] could not read docx_editor's view: {type(exc).__name__}: {exc}")
            continue
        for index, p_el in enumerate(elements):
            writer_view = redline_block_apply._accepted_text(p_el)
            if writer_view != third_view[index]:
                failures.append(
                    f"[{label}] paragraph {index}: _accepted_text_runs must mirror "
                    f"docx_editor.build_text_map character for character.\n"
                    f"  _accepted_text: {writer_view!r}\n"
                    f"  docx_editor:    {third_view[index]!r}"
                )


def test_extraction_agrees_with_the_writers_accepted_view(failures: list) -> None:
    """The INVARIANT behind the extraction tests above, asserted directly:
    for every physical `<w:p>` extraction actually RESOLVED to a p_index --
    exactly the set `redline_block_apply._resolve_physical_paragraphs`
    looks up (`paragraphs[p_index]` keyed off `record["physical_p_indexes"]`,
    never a blind scan of the whole tree) -- the clean text extraction
    produced is exactly the text the WRITER sees
    (`redline_block_apply._accepted_text`). This is the shape of check
    issues #663 and #686 each pinned for their own container; #94 repeats
    it for a FILLED-IN `w:sdt` and for `w:smartTag`.

    A paragraph carrying an UNFILLED control is asserted the other way
    round, deliberately: extraction drops the display-only copy and the
    writer keeps it (because `docx_editor` keeps it -- see
    `test_all_three_views_agree_character_for_character`), so the two MUST
    differ there. That disagreement is the fail-closed half of the trade
    recorded in ARCHITECTURE.md: the paragraph does not resolve, so its
    edits are refused one at a time instead of written at an offset the
    writer reads differently. Asserting the difference, rather than
    exempting the shape, is what stops a future diff from "fixing" the
    divergence in the writer and re-shipping the wrong-span write.

    A `<w:p>` nested inside a PLACEHOLDER block-level `w:sdt` is scoped out
    of both halves: `_iter_body_paragraphs` never yields it, so it never
    receives a p_index and `_resolve_physical_paragraphs` structurally
    cannot reach it (see
    `test_block_level_placeholder_control_still_excluded` for the property
    that matters there -- its text never reaches output at all)."""
    for label, build in _ALL_FIXTURES:
        docx_bytes = build()
        normalized = stage.extract_and_normalize(docx_bytes)
        if normalized["status"] != "normalized":
            failures.append(f"[{label}] fixture must normalize, got {normalized['status']!r}")
            continue
        clean_by_index: dict[int, str] = {}
        for paragraph in normalized["paragraphs"]:
            text = paragraph["text"]
            for (start, end), p_index in zip(  # noqa: B905
                paragraph["physical_spans"], paragraph["physical_p_indexes"]
            ):
                clean_by_index[p_index] = text[start:end]
        headings = {
            record["heading_p_index"]
            for record in stage.extract_document_paragraphs(docx_bytes)
            if record.get("heading_p_index") is not None
        }
        placeholder_indexes = _paragraphs_containing_a_placeholder(docx_bytes)
        all_paragraphs = list(_document_root(docx_bytes).iter(_w("p")))
        for p_index, extracted in clean_by_index.items():
            if p_index in headings:
                continue
            writer_view = redline_block_apply._accepted_text(all_paragraphs[p_index]).strip()
            if p_index in placeholder_indexes:
                if extracted == writer_view:
                    failures.append(
                        f"[{label}] paragraph {p_index} carries an unfilled control: "
                        f"extraction must DROP its display-only copy while the writer "
                        f"keeps it (docx_editor does), so the two must differ. Both "
                        f"read {extracted!r} -- the writer-side placeholder exclusion "
                        f"is back, and with it the wrong-span write."
                    )
                elif _PLACEHOLDER_DISPLAY_COPY not in writer_view:
                    failures.append(
                        f"[{label}] paragraph {p_index}: the writer's view must keep "
                        f"the unfilled control's copy verbatim. Got {writer_view!r}"
                    )
                continue
            if extracted != writer_view:
                failures.append(
                    f"[{label}] paragraph {p_index}: extraction and the "
                    f"writer's accepted view must agree character for "
                    f"character.\n  extraction: {extracted!r}\n"
                    f"  writer:     {writer_view!r}"
                )


def test_table_row_and_cell_level_controls_reach_extraction(failures: list) -> None:
    """Word puts content controls at a table's ROW level (a
    repeating-section control: `<w:tbl><w:sdt><w:sdtContent><w:tr>`) and
    CELL level (`<w:tr><w:sdt><w:sdtContent><w:tc>`), not only around runs
    and whole paragraphs. `_iter_table_paragraphs` reads `w:tr`/`w:tc`
    children directly, so before this issue a control at either level
    dropped every paragraph inside it silently -- the same "a controlled
    clause never reaches the model" loss, one container up. A control at
    those levels that IS an unfilled placeholder stays excluded, same rule
    as everywhere else."""
    for level, expected in (("row", _ROW_CONTROLLED_TEXT), ("cell", _CELL_CONTROLLED_TEXT)):
        filled = stage.extract_and_normalize(_table_sdt_docx(level=level, placeholder=False))
        if filled.get("status") != "normalized":
            failures.append(f"[{level}-level] fixture must normalize, got: {filled.get('status')!r}")
            continue
        if expected not in repr(filled["paragraphs"]):
            failures.append(
                f"[{level}-level] a filled-in {level}-level content control must be a "
                f"transparent wrapper -- {expected!r} is missing from the review text: "
                f"{[p['text'] for p in filled['paragraphs']]!r}"
            )
        empty = stage.extract_and_normalize(_table_sdt_docx(level=level, placeholder=True))
        if empty.get("status") != "normalized":
            failures.append(f"[{level}-level placeholder] fixture must normalize, got: {empty.get('status')!r}")
            continue
        if expected in repr(empty["paragraphs"]):
            failures.append(
                f"[{level}-level placeholder] an UNFILLED {level}-level control is "
                f"display-only copy and must stay excluded, same as an inline or "
                f"block-level one."
            )


def test_filled_in_content_control_edit_compiles_end_to_end(failures: list) -> None:
    """Driven through the real `validate_block_patches` ->
    `apply_block_transcript` path (not a restatement of block text): an
    edit anchored against the extractor's block map for a paragraph that
    contains a filled-in content control must actually resolve against the
    live document and compile. This is the failure mode a silent
    extractor/writer divergence produces (`paragraph_not_resolved`) even
    when both sides "look" fine in isolation."""
    docx_bytes = _inline_sdt_party_name_docx()
    block_map = _block_map_of(docx_bytes)
    block_id, block = next(iter(block_map.items()))

    proven = block_transcript.validate_block_patches(
        [
            {
                "block_id": block_id,
                "segments": [
                    {"op": "keep", "text": block["text"]},
                    {"op": "insert", "text": " Additional text.", "issue_key": "I1"},
                ],
            }
        ],
        [],
        block_map,
    )
    if proven["status"] != "proven":
        failures.append(
            f"the block carrying the filled-in content control must "
            f"prove; got {proven['status']!r}: {proven.get('failures')!r}"
        )
        return

    result = redline_block_apply.apply_block_transcript(
        docx_bytes, proven, author="contract-toaster", timestamp_iso="2026-01-01T00:00:00Z"
    )
    if result["docx_bytes"] is None or len(result["applied"]) != 1 or result["failures"]:
        failures.append(
            f"an edit proven against a filled-in-content-control paragraph "
            f"must compile; got applied={len(result['applied'])} "
            f"failures={result['failures']!r}"
        )


def test_repeated_word_content_control_edit_lands_in_the_operative_text(failures: list) -> None:
    """The occurrence-counting hazard, driven end to end on the shape that
    exposes it: a FILLED-IN control holding "Acme Corp" in a sentence that
    also says "Acme Corp" outside the control. The edit targets the SECOND
    copy -- the operative one -- so it is only addressable if the offset
    the block map yields still means the same thing to `docx_editor`, which
    counts `occurrence=` over its own text map.

    A tracked change landing on the FIRST copy would rewrite the contents
    of the content control instead of the clause; the assertions below
    therefore check the control's own run is untouched as well as checking
    the resulting accepted text."""
    docx_bytes = _inline_sdt_repeated_word_docx()
    block_map = _block_map_of(docx_bytes)
    block_id, block = next(iter(block_map.items()))
    text = block["text"]
    second = text.index(_FILLED_PARTY_NAME, text.index(_FILLED_PARTY_NAME) + 1)

    proven = block_transcript.validate_block_patches(
        [
            {
                "block_id": block_id,
                "segments": [
                    {"op": "keep", "text": text[:second]},
                    {"op": "delete", "text": _FILLED_PARTY_NAME, "issue_key": "I1"},
                    {"op": "insert", "text": "the Supplier", "issue_key": "I1"},
                    {"op": "keep", "text": text[second + len(_FILLED_PARTY_NAME) :]},
                ],
            }
        ],
        [],
        block_map,
    )
    if proven["status"] != "proven":
        failures.append(f"the repeated-word block must prove; got {proven['status']!r}: {proven.get('failures')!r}")
        return

    result = redline_block_apply.apply_block_transcript(
        docx_bytes, proven, author="contract-toaster", timestamp_iso="2026-01-01T00:00:00Z"
    )
    if result["docx_bytes"] is None or len(result["applied"]) != 1 or result["failures"]:
        failures.append(
            f"the edit must compile: applied={len(result['applied'])} "
            f"failures={result['failures']!r} bytes={result['docx_bytes'] is not None}"
        )
        return

    out_root = _document_root(result["docx_bytes"])
    edited = list(out_root.iter(_w("p")))[1]
    expected = (
        f"This Agreement is between {_FILLED_PARTY_NAME} and Buyer; "
        f"the Supplier shall invoice Buyer monthly."
    )
    accepted = redline_block_apply._accepted_text(edited)
    if accepted != expected:
        failures.append(
            f"the tracked change must land on the OPERATIVE copy of "
            f"{_FILLED_PARTY_NAME!r}, not the one inside the content control.\n"
            f"  expected: {expected!r}\n  got:      {accepted!r}"
        )
    for sdt_el in out_root.iter(_w("sdt")):
        inside = "".join(t.text or "" for t in sdt_el.iter(_w("t")))
        if inside != _FILLED_PARTY_NAME:
            failures.append(f"the content control's own text must be untouched; it now reads {inside!r}")
        if any(sdt_el.iter(_w("ins"))) or any(sdt_el.iter(_w("del"))):
            failures.append("no tracked change may be written inside the content control")


def test_placeholder_control_edit_fails_closed_without_voiding_the_batch(failures: list) -> None:
    """The paragraph carrying an UNFILLED control, driven end to end on the
    shape that makes the three possible outcomes distinguishable: the
    placeholder's display copy repeats "Buyer", and the edit targets the
    operative "Buyer" the extractor DID see.

    Extraction drops the display copy; `_accepted_text_runs` keeps it,
    because `docx_editor` keeps it. So the paragraph does not resolve and
    this one edit is refused as `paragraph_not_resolved` -- while the
    batch's other, control-free edit still lands and the document still
    comes back. That is the fail-closed limitation ARCHITECTURE.md records,
    pinned here so a future diff cannot trade it for the alternative: teach
    the writer to skip the placeholder too and the paragraph resolves,
    `occurrence=0` computed over placeholder-free text addresses the
    display copy's "Buyer", the tracked change is written INSIDE the empty
    control, and the projection check then voids the whole batch --
    `applied=0`, no document at all."""
    docx_bytes = _inline_sdt_placeholder_docx()
    block_map = _block_map_of(docx_bytes)
    ids = list(block_map)
    if len(ids) != 2:
        failures.append(f"fixture must yield two blocks (controlled + clean), got {len(ids)}: {ids!r}")
        return
    controlled_id, clean_id = ids
    controlled_text = block_map[controlled_id]["text"]
    target = controlled_text.index("Buyer")

    proven = block_transcript.validate_block_patches(
        [
            {
                "block_id": controlled_id,
                "segments": [
                    {"op": "keep", "text": controlled_text[:target]},
                    {"op": "delete", "text": "Buyer", "issue_key": "I1"},
                    {"op": "insert", "text": "the Purchaser", "issue_key": "I1"},
                    {"op": "keep", "text": controlled_text[target + len("Buyer") :]},
                ],
            },
            {
                "block_id": clean_id,
                "segments": [
                    {"op": "keep", "text": block_map[clean_id]["text"]},
                    {"op": "insert", "text": " Late fees accrue monthly.", "issue_key": "I2"},
                ],
            },
        ],
        [],
        block_map,
    )
    if proven["status"] != "proven":
        failures.append(f"both blocks must prove; got {proven['status']!r}: {proven.get('failures')!r}")
        return

    result = redline_block_apply.apply_block_transcript(
        docx_bytes, proven, author="contract-toaster", timestamp_iso="2026-01-01T00:00:00Z"
    )
    if result["docx_bytes"] is None:
        failures.append(
            f"one unfilled content control must not void the whole redline; got "
            f"applied={len(result['applied'])} failures="
            f"{[f.get('reason') for f in result['failures']]!r}"
        )
        return
    if len(result["applied"]) != 1:
        failures.append(
            f"the control-free edit must still land: applied={len(result['applied'])} "
            f"failures={[f.get('reason') for f in result['failures']]!r}"
        )
    reasons = [f.get("reason") for f in result["failures"]]
    if reasons != [redline_block_apply.REASON_PARAGRAPH_NOT_RESOLVED]:
        failures.append(
            f"the edit on the unfilled-control paragraph must fail closed as "
            f"{redline_block_apply.REASON_PARAGRAPH_NOT_RESOLVED!r} -- per-edit, not by "
            f"voiding the batch. Got {reasons!r}"
        )

    out_root = _document_root(result["docx_bytes"])
    for sdt_el in out_root.iter(_w("sdt")):
        if any(sdt_el.iter(_w("ins"))) or any(sdt_el.iter(_w("del"))):
            failures.append("no tracked change may ever be written inside an UNFILLED content control")
        inside = "".join(t.text or "" for t in sdt_el.iter(_w("t")))
        if inside != _PLACEHOLDER_DISPLAY_COPY:
            failures.append(f"the unfilled control's display copy must be untouched; it now reads {inside!r}")
    controlled_p = list(out_root.iter(_w("p")))[1]
    if "the Purchaser" in redline_block_apply._accepted_text(controlled_p):
        failures.append("the refused edit must not have been written to the document at all")


TESTS = [
    test_filled_in_content_control_reaches_extraction,
    test_placeholder_content_control_still_excluded,
    test_block_level_content_control_reaches_extraction,
    test_block_level_placeholder_control_still_excluded,
    test_smart_tag_wrapped_text_reaches_extraction,
    test_table_row_and_cell_level_controls_reach_extraction,
    test_all_three_views_agree_character_for_character,
    test_extraction_agrees_with_the_writers_accepted_view,
    test_filled_in_content_control_edit_compiles_end_to_end,
    test_repeated_word_content_control_edit_lands_in_the_operative_text,
    test_placeholder_control_edit_fails_closed_without_voiding_the_batch,
]


def main() -> int:
    all_failures: list[str] = []
    for test in TESTS:
        failures: list[str] = []
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{test.__name__} raised {type(exc).__name__}: {exc}")
        if failures:
            print(f"FAIL: {test.__name__}")
            for failure in failures:
                print(f"  - {failure}")
            all_failures.extend(failures)
        else:
            print(f"PASS: {test.__name__}")

    print()
    if all_failures:
        print(f"FAIL: {len(all_failures)} failure(s) (issue #94).")
        return 1
    print("PASS: content controls and smart tags are transparent wrappers, except a genuine placeholder (issue #94).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
