#!/usr/bin/env python3
"""
Slice test for issue #114: "a Table of Contents (complex TOC field) becomes
one empty clause block per entry, each with its own block_id".

## The defect this pins fixed

`scripts/extraction_normalization_stage.py` modelled only `<w:fldSimple>`.
The OTHER way OOXML writes a field -- the COMPLEX field: a run carrying
`<w:fldChar w:fldCharType="begin"/>`, one or more `<w:instrText>` runs
holding the code, `separate`, the cached RESULT, `end` -- was invisible to
it, and a Table of Contents is exactly that: a `TOC` field whose cached
result Word writes as one ordinary body paragraph per heading, styled
`TOC1`, carrying the heading text, a tab, and a nested `PAGEREF` field for
the page number.

Read as body, every one of those lines is a clause boundary by
`clause_boundaries`' own rules (a numbered lead-in). So each opened its own
EMPTY logical paragraph, named like the real clause and holding a `block_id`
AHEAD of it -- the issue's Evidence, measured on committed HEAD:

    'Contents' => ''  |  'Definitions\t2' => ''  |  'Term\t3' => ''
    |  'Definitions' => 'Real body.'

Three consequences, all live: the model was shown N empty "clauses" named
like the real ones, the block map's clause count was wrong for any long-form
agreement (most of them carry a TOC), and `delete_block` on one of those ids
would have written `[Intentionally omitted.]` into the table of contents.

## What the fix is, and what it deliberately is NOT

`_partition_field_result_paragraphs` carries complex-field state ACROSS
`<w:p>` boundaries and drops a paragraph iff it carries visible text and ALL
of that text lies inside a `TOC`/`INDEX` field's result region, with one
disclosure line in `normalization_notes`. Every OTHER field code (`REF`,
`PAGEREF`, `PAGE`, `DATE`, ...) keeps its cached result INLINE, exactly as
`_process_fld_simple` resolves a simple field's -- that is the pre-existing
behavior and the one that cannot drop clause text, and
`test_a_complex_ref_field_keeps_its_result_inline` holds it there.

Four containment rules are load-bearing rather than decoration, and each
has its own test below:

  - A region only counts once its frame has been closed by an `end`
    (`test_a_toc_whose_end_is_missing_drops_nothing`, and in isolation
    `test_an_unclosed_toc_at_the_document_end_drops_nothing`). A missing
    `end` -- a truncated file, or an `end` the counterparty struck -- leaves
    the field's extent unknown, so nothing proves the lines after its
    `separate` are generated result. The region guards below cannot stand
    in for this rule: an unclosed TOC with nothing after it but its own
    lines is entry-shaped throughout, and shape alone would drop it.
  - A closed region is dropped only when every paragraph in it looks like
    a generated listing line, none is a real heading, and none carries a
    bookmark one of its own entries links to. In the accept-all view a
    LATER field whose `begin` is struck or hidden can close a TOC whose own
    `end` is struck, and the region then spans the agreement
    (`test_a_foreign_end_cannot_close_a_toc_over_the_agreement`, its `REF`
    and style-stripped twins, and one test per guard). Those regions are
    kept whole, so the worst case is the pre-#114 behavior.
  - Visible text OUTSIDE the region keeps the paragraph WHOLE
    (`test_body_text_after_the_field_end_in_the_same_paragraph_is_kept`,
    whose K3-K6 fixture puts the last entry, the TOC's own `end` and a body
    sentence in ONE `<w:p>`), so this can never drop a sentence of the
    agreement.
  - Field state is read over the ACCEPT-ALL view only: struck (`<w:del>`)
    content, `<w:delInstrText>` and `<w:delText>` are ignored
    (`test_a_struck_toc_end_gives_both_reads_the_same_block_map`, its
    struck-`begin` twin, and `test_a_stage_1_block_id_deletes_the_same_
    clause_in_stage_5`). Stage 1 extracts the raw upload and Stage 5 the
    `materialize_accept_all`'d bytes, which no longer carry a struck field
    character, and `delete_block` finds its clause by `block_id` alone --
    so a walk that read struck field characters made the two reads
    disagree, and a proven delete landed on the wrong clause.

## Fixture provenance (what production actually writes)

Every shape below is raw OOXML built with `zipfile` + `ElementTree` only
(this repo's dependency-free `.docx` convention, same as
`tests/test_field_code_note_99.py` and `tests/test_reserved_namespace_
prefix_560.py`) and driven through the REAL producer,
`extraction_normalization_stage.extract_and_normalize` -- no test here
hand-builds a paragraph record or a revision dict and then asserts over it.

The markup follows Word's: `begin`/`instrText`/`separate` on the FIRST
entry's paragraph, each entry a `w:hyperlink` to the heading's bookmark
wrapping the heading text, a `w:tab`, and a nested `PAGEREF` complex field.
Word's TOC content control commonly closes with the `end` ALONE in its own
trailing paragraph -- the issue's Required-verification shape, pinned by
`test_the_tickets_own_shape_end_alone_in_a_trailing_paragraph` -- while
`_toc` below puts the `end` on the last entry's paragraph, a variant that
proves the same state machine. `tools/churn_docx.py::
table_of_contents` generates the last-entry variant programmatically onto a
negotiation-shaped base contract, and `tests/test_document_shapes.py::
test_table_of_contents_survives` drives it through the whole model-free
spine (extract -> normalize -> prove -> compile) -- this file is the
unit-level half.

Branch coverage seeds more than the happy variant: the field code split
across several `<w:instrText>` runs as well as one; the tab as its own run
as well as a sibling inside the text run; `TOC` as well as `INDEX`; a field
that closes as well as one that never does; a field with a cached result as
well as one that was never updated (`begin`/`instr`/`end`, no `separate`);
a stray `end` with no `begin` at all; the `end`, or the `begin` + field code
+ `separate`, struck inside a tracked `<w:del>`; and the TOC inside the
block-level `<w:sdt>` (docPartGallery "Table of Contents") Word 2007+ wraps
it in.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import block_transcript as bt  # noqa: E402
import extraction_normalization_stage as ens  # noqa: E402
import preflight_pass  # noqa: E402
import redline_block_apply as rba  # noqa: E402

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_DECL = f'xmlns:w="{WORD_NS}"'

# The accept-all tail `frontend/src/toaster/receipt.ts::
# acceptedChangesSummary` counts to report "N pending edits accepted before
# review". The #114 disclosure must never carry it: nothing was accepted.
_ACCEPT_ALL_TAIL = "accepted-all into the operative draft."


# ---------------------------------------------------------------------------
# Raw-OOXML fixture builders (no python-docx)
# ---------------------------------------------------------------------------


def _docx(body: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {_NS_DECL}><w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


def _heading(text: str) -> str:
    return (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def _para(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _fld_char(char_type: str) -> str:
    return f'<w:r><w:fldChar w:fldCharType="{char_type}"/></w:r>'


def _instr(text: str) -> str:
    return f'<w:r><w:instrText xml:space="preserve">{text}</w:instrText></w:r>'


def _toc_entry(heading: str, page: int, anchor: str, *, tab_in_text_run: bool = False) -> str:
    """One TOC result line, as Word writes it: a hyperlink to the heading's
    bookmark, a tab, and a nested `PAGEREF` complex field for the page
    number.

    `tab_in_text_run` flips the ONE run-splitting variant that matters to
    this module's walker: `<w:tab/>` as a sibling INSIDE the heading's own
    text run rather than a run of its own. `_process_run` appends a run's
    tabs after its `<w:t>` text, so the two shapes produce the same
    paragraph text -- and `_iter_field_events` has to see both."""
    if tab_in_text_run:
        label = f'<w:r><w:t xml:space="preserve">{heading}</w:t><w:tab/></w:r>'
    else:
        label = f'<w:r><w:t xml:space="preserve">{heading}</w:t></w:r><w:r><w:tab/></w:r>'
    return (
        f'<w:hyperlink w:anchor="{anchor}">{label}'
        + _fld_char("begin")
        + _instr(f" PAGEREF {anchor} \\h ")
        + _fld_char("separate")
        + f"<w:r><w:t>{page}</w:t></w:r>"
        + _fld_char("end")
        + "</w:hyperlink>"
    )


def _struck(markup: str) -> str:
    """`markup` inside a pending tracked DELETION, as Word writes it when the
    counterparty strikes it with track changes on."""
    return (
        '<w:del w:id="11" w:author="Counterparty" w:date="2026-01-01T00:00:00Z">'
        f"{markup}</w:del>"
    )


def _del_instr(text: str) -> str:
    """Field code inside a tracked deletion. ECMA-376 writes struck field
    code as `<w:delInstrText>`, never `<w:instrText>`, exactly as struck run
    text is `<w:delText>` rather than `<w:t>`."""
    return f'<w:r><w:delInstrText xml:space="preserve">{text}</w:delInstrText></w:r>'


def _toc(
    entries: list[tuple[str, int]],
    *,
    close: bool = True,
    split_instr: bool = False,
    strike_opening: bool = False,
    strike_end: bool = False,
) -> str:
    """A full `TOC` complex field. `begin`/`instrText`/`separate` open inside
    the FIRST entry's paragraph and `end` closes on the LAST entry's -- one
    valid layout (Word more commonly closes with `end` alone in a trailing
    paragraph, pinned separately), and one that proves the state machine
    carries across `<w:p>` boundaries.

    `strike_opening` puts `begin` + the field code + `separate` inside a
    tracked `<w:del>` (the code then written as `<w:delInstrText>`), and
    `strike_end` does the same to the `end` field character -- the two
    shapes where `materialize_accept_all` removes a field character the raw
    upload still carries."""
    if split_instr:
        instr_runs = _instr(" TO") + _instr('C \\o "1-3" ') + _instr("\\h \\z \\u ")
    else:
        instr_runs = _instr(' TOC \\o "1-3" \\h \\z \\u ')

    paragraphs: list[str] = []
    for position, (heading, page) in enumerate(entries):
        opening = ""
        if position == 0:
            if strike_opening:
                opening = _struck(
                    _fld_char("begin") + _del_instr(' TOC \\o "1-3" \\h \\z \\u ') + _fld_char("separate")
                )
            else:
                opening = _fld_char("begin") + instr_runs + _fld_char("separate")
        closing = ""
        if close and position == len(entries) - 1:
            closing = _struck(_fld_char("end")) if strike_end else _fld_char("end")
        paragraphs.append(
            '<w:p><w:pPr><w:pStyle w:val="TOC1"/></w:pPr>'
            + opening
            + _toc_entry(heading, page, f"_Toc{position + 1}", tab_in_text_run=(position % 2 == 1))
            + closing
            + "</w:p>"
        )
    return "".join(paragraphs)


# The clause body every "did the real document survive" assertion below
# anchors on. Fabricated; no real paper, no counterparty name.
_DEFINITIONS_BODY = "Confidential Information means any non-public information disclosed."
_TERM_BODY = "This Agreement continues for one year from the Effective Date."

_REAL_CLAUSES = (
    _heading("1. Definitions")
    + _para(_DEFINITIONS_BODY)
    + _heading("2. Term")
    + _para(_TERM_BODY)
)

#: What the block map must look like for `_REAL_CLAUSES`, with or without a
#: table of contents in front of it.
_EXPECTED_BLOCKS = [
    ("p0001", "Definitions", _DEFINITIONS_BODY),
    ("p0002", "Term", _TERM_BODY),
]


_NOTICES_BODY = "Notices must be given in writing to the address on the cover page."

#: Three clauses rather than two for the raw-vs-materialized tests, so a
#: shifted id lands on a DIFFERENT real clause (the issue's harm) rather
#: than off the end of the map.
_THREE_CLAUSES = _REAL_CLAUSES + _heading("3. Notices") + _para(_NOTICES_BODY)


def _blocks(result: dict) -> list[tuple[str, str, str]]:
    return [(p["block_id"], p["heading"], p["text"]) for p in result.get("paragraphs", [])]


def _both_reads(docx_bytes: bytes) -> tuple[list, list, bool]:
    """`(stage_1_blocks, stage_5_blocks, materialization_rewrote_the_part)`:
    the block map `review_spine` builds from the RAW upload, the one
    `redline_generate` builds from the `materialize_accept_all`'d bytes, and
    whether the fixture actually carried something to accept (a fixture
    that does not would prove nothing about the two reads)."""
    materialized_bytes = ens.materialize_accept_all(docx_bytes)
    return (
        _blocks(ens.extract_and_normalize(docx_bytes)),
        _blocks(ens.extract_and_normalize(materialized_bytes)),
        materialized_bytes != docx_bytes,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_a_toc_result_reaches_neither_the_block_map_nor_the_model(failures: list) -> None:
    """THE defect (issue #114's Evidence): every TOC entry became its own
    empty clause block, named like the real clause and holding a `block_id`
    ahead of it."""
    body = _toc([("1. Definitions", 2), ("2. Term", 3)]) + _REAL_CLAUSES
    result = ens.extract_and_normalize(_docx(body))
    if result.get("status") != "normalized":
        failures.append(f"[A1] the document must normalize. Got: {result!r}")
        return
    if _blocks(result) != _EXPECTED_BLOCKS:
        failures.append(f"[A2] a TOC must not reach the block map. Got: {_blocks(result)!r}")


def test_a_toc_does_not_shift_a_single_real_clauses_block_id(failures: list) -> None:
    """The ids the model is handed, and that `delete_block` addresses, must
    be identical to the same document with no contents list in front of
    it."""
    without = ens.extract_and_normalize(_docx(_REAL_CLAUSES))
    with_toc = ens.extract_and_normalize(
        _docx(_toc([("1. Definitions", 2), ("2. Term", 3)]) + _REAL_CLAUSES)
    )
    if _blocks(without) != _blocks(with_toc):
        failures.append(
            f"[B1] a TOC changed the block map: {_blocks(with_toc)!r} != {_blocks(without)!r}"
        )


def test_the_omission_is_disclosed_once_and_counts_the_paragraphs(failures: list) -> None:
    """Never silent, and ONE line for the whole document rather than one per
    entry -- a long-form agreement's contents list is routinely dozens of
    lines."""
    body = _toc([("1. Definitions", 2), ("2. Term", 3), ("3. Notices", 4)]) + _REAL_CLAUSES
    result = ens.extract_and_normalize(_docx(body))
    notes = result.get("normalization_notes") or ""
    if "3 paragraph(s) of TOC field result" not in notes:
        failures.append(f"[C1] the omission must be disclosed and counted. Got: {notes!r}")
    if notes.count("Generated field result omitted") != 1:
        failures.append(f"[C2] exactly one disclosure line is expected. Got: {notes!r}")


def test_the_disclosure_is_never_counted_as_an_accepted_edit(failures: list) -> None:
    """`frontend/src/toaster/receipt.ts::acceptedChangesSummary` counts the
    accept-all tail to report "N pending edits accepted before review", and
    returns null for the WHOLE line when a tail it cannot parse is present.
    Nothing was accepted here, so the sentence must carry no tail at all --
    neither inflating the count nor suppressing the receipt line."""
    body = _toc([("1. Definitions", 2)]) + _REAL_CLAUSES
    notes = ens.extract_and_normalize(_docx(body)).get("normalization_notes") or ""
    if _ACCEPT_ALL_TAIL in notes:
        failures.append(f"[D1] the disclosure must not carry the accept-all tail. Got: {notes!r}")


def test_the_disclosure_quotes_no_dropped_line_and_no_field_code(failures: list) -> None:
    """The sentence names the field-code WORD (from the fixed
    `OMITTED_FIELD_RESULT_CODES` set) and a count -- never the raw
    `<w:instrText>`, which is counterparty-controlled markup, and never a
    dropped line's own text."""
    body = _toc([("1. Definitions", 2)]) + _REAL_CLAUSES
    notes = ens.extract_and_normalize(_docx(body)).get("normalization_notes") or ""
    for forbidden in ('\\o "1-3"', "\\h \\z \\u", "1. Definitions", "PAGEREF"):
        if forbidden in notes:
            failures.append(f"[E1] the disclosure must not quote {forbidden!r}. Got: {notes!r}")


def test_a_complex_ref_field_keeps_its_result_inline(failures: list) -> None:
    """Every field code OTHER than `TOC`/`INDEX` keeps its cached result
    inline, exactly as `_process_fld_simple` resolves a simple field's. This
    is the branch that must NOT change: a cross-reference resolving to
    "Section 4" is part of the sentence a reader sees."""
    body = _heading("1. Definitions") + (
        "<w:p>"
        '<w:r><w:t xml:space="preserve">See </w:t></w:r>'
        + _fld_char("begin")
        + _instr(" REF _Ref1 \\h ")
        + _fld_char("separate")
        + "<w:r><w:t>Section 4</w:t></w:r>"
        + _fld_char("end")
        + '<w:r><w:t xml:space="preserve"> for detail.</w:t></w:r>'
        "</w:p>"
    )
    result = ens.extract_and_normalize(_docx(body))
    texts = [p["text"] for p in result.get("paragraphs", [])]
    if texts != ["See Section 4 for detail."]:
        failures.append(f"[F1] a REF field's result must stay inline. Got: {texts!r}")
    if result.get("normalization_notes"):
        failures.append(
            f"[F2] a non-omitted field must produce no omission note. "
            f"Got: {result['normalization_notes']!r}"
        )


def test_an_index_field_result_is_omitted_too(failures: list) -> None:
    """`INDEX` is the second member of `OMITTED_FIELD_RESULT_CODES`: its
    result is the same kind of generated listing of the document, one
    body-shaped paragraph per entry."""
    index_field = (
        "<w:p>"
        + _fld_char("begin")
        + _instr(' INDEX \\c "2" \\z "1033" ')
        + _fld_char("separate")
        + "<w:r><w:t>Confidentiality, 4</w:t></w:r>"
        "</w:p>"
        "<w:p><w:r><w:t>Term, 6</w:t></w:r>" + _fld_char("end") + "</w:p>"
    )
    result = ens.extract_and_normalize(_docx(_REAL_CLAUSES + index_field))
    if _blocks(result) != _EXPECTED_BLOCKS:
        failures.append(f"[G1] an INDEX result must not reach the block map. Got: {_blocks(result)!r}")
    notes = result.get("normalization_notes") or ""
    if "2 paragraph(s) of INDEX field result" not in notes:
        failures.append(f"[G2] the INDEX omission must be disclosed and counted. Got: {notes!r}")


def test_a_toc_whose_end_is_missing_drops_nothing(failures: list) -> None:
    """The containment rule. A field whose `end` never arrives (a truncated
    file, or an `end` the counterparty struck) must drop NOTHING -- the
    pre-#114 behavior, bad but bounded. Here the real clauses AFTER the
    unclosed field are headings and body sentences, so the region guards
    would keep the region even if it were committed; this test holds the
    outcome, and `test_an_unclosed_toc_at_the_document_end_drops_nothing`
    isolates the closed-frame rule itself."""
    body = _toc([("1. Definitions", 2)], close=False) + _REAL_CLAUSES
    result = ens.extract_and_normalize(_docx(body))
    headings = [p["heading"] for p in result.get("paragraphs", [])]
    if "Definitions" not in headings or "Term" not in headings:
        failures.append(
            f"[H1] an unterminated TOC field must not swallow the document. Got: {headings!r}"
        )
    if result.get("normalization_notes"):
        failures.append(
            f"[H2] nothing was dropped, so nothing may be disclosed. "
            f"Got: {result['normalization_notes']!r}"
        )


def test_an_unclosed_toc_at_the_document_end_drops_nothing(failures: list) -> None:
    """The closed-frame rule, isolated: an unclosed TOC with nothing after
    it. Every buffered line is TOC1-styled, hyperlinked and carries a
    `PAGEREF`, none is a real heading and none carries a bookmark, so every
    region guard would pass it -- only the missing `end` keeps it. Without
    an `end` nothing proves where the field stops, so its lines are kept as
    they were before #114 and nothing is disclosed as dropped."""
    body = _REAL_CLAUSES + _toc([("1. Definitions", 2), ("2. Term", 3)], close=False)
    result = ens.extract_and_normalize(_docx(body))
    blocks = _blocks(result)
    if blocks[: len(_EXPECTED_BLOCKS)] != _EXPECTED_BLOCKS:
        failures.append(f"[H3] the real clauses must be untouched: {blocks!r}")
    kept = "\n".join(f"{heading}\n{text}" for _, heading, text in blocks)
    for line in ("Definitions\t2", "Term\t3"):
        if line not in kept:
            failures.append(f"[H4] the unclosed TOC line {line!r} was dropped: {blocks!r}")
    if result.get("normalization_notes"):
        failures.append(
            f"[H5] nothing was dropped, so nothing may be disclosed. "
            f"Got: {result['normalization_notes']!r}"
        )


def test_a_toc_that_was_never_updated_has_no_result_to_omit(failures: list) -> None:
    """`begin` / `instrText` / `end` with no `separate` at all -- a field
    Word has never computed. There is no cached result, so there is nothing
    to drop and nothing to disclose."""
    never_updated = "<w:p>" + _fld_char("begin") + _instr(' TOC \\o "1-3" ') + _fld_char("end") + "</w:p>"
    result = ens.extract_and_normalize(_docx(never_updated + _REAL_CLAUSES))
    if _blocks(result) != _EXPECTED_BLOCKS:
        failures.append(f"[I1] an uncomputed TOC changes nothing. Got: {_blocks(result)!r}")
    if result.get("normalization_notes"):
        failures.append(f"[I2] nothing dropped, nothing disclosed. Got: {result['normalization_notes']!r}")


def test_a_stray_end_field_character_changes_nothing(failures: list) -> None:
    """Malformed field markup in a counterparty's file: an `end` with no
    `begin`. This is an extraction walk, not a validator -- it must not
    raise, and the conservative reading is "not inside an omitted field"."""
    result = ens.extract_and_normalize(_docx("<w:p>" + _fld_char("end") + "</w:p>" + _REAL_CLAUSES))
    if _blocks(result) != _EXPECTED_BLOCKS:
        failures.append(f"[J1] a stray end must change nothing. Got: {_blocks(result)!r}")


def test_body_text_after_the_field_end_in_the_same_paragraph_is_kept(failures: list) -> None:
    """The other containment rule: a paragraph is dropped only when ALL of
    its visible text is field result. Real clause text sharing a `<w:p>`
    with the field's `end` keeps the whole paragraph, so this can never drop
    a sentence of the agreement.

    K1/K2: the `end` OPENS the paragraph that carries the preamble, so no
    text in it is in-region. K3-K6 is the shape the rule actually decides:
    ONE `<w:p>` carries the last TOC entry (in-region), the TOC's own `end`,
    then a body sentence (outside). Every region guard passes that region
    (TOC1-styled, hyperlinked entries, no heading, no bookmark), so only the
    outside text keeps the paragraph -- and with it the sentence -- in both
    the raw and the materialized read. A pending `<w:ins>` in the Notices
    body makes materialization actually rewrite the part."""
    preamble = "This Agreement is made between the parties named below."
    body = (
        "<w:p>"
        + _fld_char("begin")
        + _instr(' TOC \\o "1-3" ')
        + _fld_char("separate")
        + '<w:r><w:t>1. Definitions</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>2</w:t></w:r>'
        "</w:p>"
        "<w:p>" + _fld_char("end") + f'<w:r><w:t xml:space="preserve">{preamble}</w:t></w:r></w:p>'
    ) + _REAL_CLAUSES
    result = ens.extract_and_normalize(_docx(body))
    texts = [p["text"] for p in result.get("paragraphs", [])]
    if preamble not in texts:
        failures.append(f"[K1] body text sharing a paragraph with the field end was dropped: {texts!r}")
    if any("\t" in p["heading"] for p in result.get("paragraphs", [])):
        failures.append(f"[K2] the TOC entry itself must still be dropped. Got: {_blocks(result)!r}")

    toc = _toc([("1. Definitions", 2), ("2. Term", 3)])
    shared = toc[: -len("</w:p>")] + f'<w:r><w:t xml:space="preserve">{preamble}</w:t></w:r></w:p>'
    pending_notices = (
        '<w:p><w:r><w:t xml:space="preserve">Notices go </w:t></w:r>'
        '<w:ins w:id="7" w:author="Counterparty" w:date="2026-01-01T00:00:00Z">'
        '<w:r><w:t xml:space="preserve">in writing </w:t></w:r></w:ins>'
        '<w:r><w:t xml:space="preserve">to the cover-page address.</w:t></w:r></w:p>'
    )
    docx_bytes = _docx(shared + _REAL_CLAUSES + _heading("3. Notices") + pending_notices)
    stage_1, stage_5, rewritten = _both_reads(docx_bytes)
    if not rewritten:
        failures.append("[K3] the fixture must carry a revision materialization actually rewrites")
    for label, blocks in (("raw", stage_1), ("materialized", stage_5)):
        if not any(preamble in heading or preamble in text for _, heading, text in blocks):
            failures.append(f"[K4] {label} read dropped the sentence after the TOC's own end: {blocks!r}")
    notes = ens.extract_and_normalize(docx_bytes).get("normalization_notes") or ""
    if "1 paragraph(s) of TOC field result" not in notes:
        failures.append(f"[K5] only the entry with no outside text may be dropped. Got: {notes!r}")
    if stage_1 != stage_5:
        failures.append(f"[K6] the two reads disagree: {stage_1!r} != {stage_5!r}")


def test_the_field_code_may_be_split_across_instr_text_runs(failures: list) -> None:
    """Word routinely splits a field code over several `<w:instrText>` runs
    (spell-check state, formatting, revision boundaries), so " TO" + 'C \\o
    "1-3" ' is the same `TOC` field as one run carrying the whole code."""
    body = _toc([("1. Definitions", 2), ("2. Term", 3)], split_instr=True) + _REAL_CLAUSES
    result = ens.extract_and_normalize(_docx(body))
    if _blocks(result) != _EXPECTED_BLOCKS:
        failures.append(f"[L1] a split field code must still be read as TOC. Got: {_blocks(result)!r}")


def test_the_raw_and_the_materialized_read_agree_on_the_block_map(failures: list) -> None:
    """Stage 1 (`review_spine`, raw uploaded bytes) and Stage 5
    (`redline_generate`, `materialize_accept_all`'d bytes) must agree about
    which `block_id` names which paragraph -- the one-canonical-document
    invariant (issue #563) the empty-TOC-block bug also sat on. Seeded with
    a real pending `<w:ins>` so materialization actually rewrites the part
    rather than handing back an unchanged tree."""
    pending = (
        '<w:p><w:r><w:t xml:space="preserve">This Agreement continues for </w:t></w:r>'
        '<w:ins w:id="7" w:author="Counterparty" w:date="2026-01-01T00:00:00Z">'
        '<w:r><w:t xml:space="preserve">two years</w:t></w:r></w:ins>'
        '<w:r><w:t xml:space="preserve"> from the Effective Date.</w:t></w:r></w:p>'
    )
    body = (
        _toc([("1. Definitions", 2), ("2. Term", 3)])
        + _heading("1. Definitions")
        + _para(_DEFINITIONS_BODY)
        + _heading("2. Term")
        + pending
    )
    docx_bytes = _docx(body)
    raw = ens.extract_and_normalize(docx_bytes)
    materialized_bytes = ens.materialize_accept_all(docx_bytes)
    materialized = ens.extract_and_normalize(materialized_bytes)
    if materialized_bytes == docx_bytes:
        failures.append("[M1] the fixture must carry a revision materialization actually rewrites")
    if _blocks(raw) != _blocks(materialized):
        failures.append(
            f"[M2] the raw and materialized reads disagree: {_blocks(raw)!r} != {_blocks(materialized)!r}"
        )


def test_a_struck_toc_end_gives_both_reads_the_same_block_map(failures: list) -> None:
    """The counterparty struck the TOC's `end` field character with track
    changes on. `materialize_accept_all` deletes the `<w:del>`, so a field
    walk that read struck content closed the TOC on the RAW side only: Stage
    1 dropped the TOC lines while Stage 5 kept them as clauses, and every
    real clause's id moved by two between the read the model is shown and
    the read `delete_block` resolves against. Reading the accept-all view
    only, both sides see the same unclosed field."""
    docx_bytes = _docx(_toc([("1. Definitions", 2), ("2. Term", 3)], strike_end=True) + _THREE_CLAUSES)
    stage_1, stage_5, rewritten = _both_reads(docx_bytes)
    if not rewritten:
        failures.append("[O1] the fixture must carry a deletion materialization actually removes")
    if stage_1 != stage_5:
        failures.append(f"[O2] a struck TOC end split the two reads: {stage_1!r} != {stage_5!r}")
    if [text for _, _, text in stage_1 if text] != [_DEFINITIONS_BODY, _TERM_BODY, _NOTICES_BODY]:
        failures.append(f"[O3] every real clause must survive the unclosed field: {stage_1!r}")


def test_a_struck_toc_begin_and_field_code_give_both_reads_the_same_block_map(failures: list) -> None:
    """The other half of the same split: `begin`, the field code (written
    as `<w:delInstrText>`, as ECMA-376 requires inside a deletion) and
    `separate` all struck. The raw upload carries a whole TOC field; the
    materialized bytes carry only its result lines and a stray `end`. Both
    reads must agree on which of those lines are clauses."""
    docx_bytes = _docx(_toc([("1. Definitions", 2), ("2. Term", 3)], strike_opening=True) + _THREE_CLAUSES)
    stage_1, stage_5, rewritten = _both_reads(docx_bytes)
    if not rewritten:
        failures.append("[P1] the fixture must carry a deletion materialization actually removes")
    if stage_1 != stage_5:
        failures.append(f"[P2] a struck TOC begin split the two reads: {stage_1!r} != {stage_5!r}")
    if [text for _, _, text in stage_1 if text] != [_DEFINITIONS_BODY, _TERM_BODY, _NOTICES_BODY]:
        failures.append(f"[P3] every real clause must survive: {stage_1!r}")


def test_a_stage_1_block_id_deletes_the_same_clause_in_stage_5(failures: list) -> None:
    """The harm itself, end to end. The model reads Stage 1's ids and names
    `delete_block` on Notices; Stage 5 proves and compiles that id against
    the MATERIALIZED document. With the two reads split by a struck TOC
    `end`, the id proved and applied with no failure -- and struck the
    Definitions body instead, writing `[Intentionally omitted.]` under the
    wrong heading. The id must name the same clause on both sides, and only
    Notices may change."""
    docx_bytes = _docx(_toc([("1. Definitions", 2), ("2. Term", 3)], strike_end=True) + _THREE_CLAUSES)
    stage_1_map = ens.build_block_map(ens.extract_and_normalize(docx_bytes).get("paragraphs", []))
    target = next((bid for bid, blk in stage_1_map.items() if blk["text"] == _NOTICES_BODY), None)
    if target is None:
        failures.append(f"[Q1] Notices is not addressable in Stage 1: {stage_1_map!r}")
        return

    materialized_bytes = ens.materialize_accept_all(docx_bytes)
    stage_5_map = ens.build_block_map(ens.extract_and_normalize(materialized_bytes).get("paragraphs", []))
    if stage_5_map.get(target, {}).get("text") != _NOTICES_BODY:
        # Not a `return`: on a regression the compile below shows the
        # consequence too -- the WRONG clause struck, with no failure.
        failures.append(
            f"[Q2] Stage-1 id {target!r} names a different clause in Stage 5: {stage_5_map.get(target)!r}"
        )

    proven = bt.validate_block_patches(
        [], [{"op": "delete_block", "block_id": target, "issue_key": "N1"}], stage_5_map
    )
    if proven["status"] != "proven":
        failures.append(f"[Q3] the delete did not prove: {[f.get('reason') for f in proven['failures']]}")
        return
    applied = rba.apply_block_transcript(
        materialized_bytes,
        proven,
        author="TocFieldResultTest",
        timestamp_iso="2026-09-19T00:00:00Z",
    )
    if applied["docx_bytes"] is None or applied["failures"]:
        failures.append(f"[Q4] the delete did not compile: failures={applied['failures']}")
        return

    accepted = ens.extract_and_normalize(ens.materialize_accept_all(applied["docx_bytes"]))
    pairs = [(p["heading"], p["text"]) for p in accepted.get("paragraphs", [])]
    texts = [text for _, text in pairs]
    if _NOTICES_BODY in texts or ("Notices", bt.OMITTED_CLAUSE_PLACEHOLDER) not in pairs:
        failures.append(f"[Q5] Notices was not the clause deleted: {pairs!r}")
    if _DEFINITIONS_BODY not in texts or _TERM_BODY not in texts:
        failures.append(f"[Q6] a clause the edit did not name was changed: {pairs!r}")


def test_a_toc_inside_its_block_level_content_control_is_omitted_too(failures: list) -> None:
    """Word 2007+ writes a table of contents inside a block-level `<w:sdt>`
    whose `w:docPartObj/w:docPartGallery` is "Table of Contents".
    `_iter_body_paragraphs` descends a filled-in block-level control
    transparently (issue #94), so the field walk sees the same paragraphs
    and the result is omitted exactly as it is unwrapped."""
    wrapped = (
        "<w:sdt><w:sdtPr><w:docPartObj>"
        '<w:docPartGallery w:val="Table of Contents"/><w:docPartUnique/>'
        "</w:docPartObj></w:sdtPr><w:sdtContent>"
        + _toc([("1. Definitions", 2), ("2. Term", 3)])
        + "</w:sdtContent></w:sdt>"
    )
    result = ens.extract_and_normalize(_docx(wrapped + _REAL_CLAUSES))
    if _blocks(result) != _EXPECTED_BLOCKS:
        failures.append(f"[R1] a TOC inside its content control must not reach the block map. Got: {_blocks(result)!r}")
    if "2 paragraph(s) of TOC field result" not in (result.get("normalization_notes") or ""):
        failures.append(f"[R2] the omission must be disclosed. Got: {result.get('normalization_notes')!r}")


def test_an_edit_still_compiles_against_a_document_carrying_a_toc(failures: list) -> None:
    """Identity, not just text: the surviving blocks' `physical_p_indexes`
    must still address the right `<w:p>` once earlier paragraphs have been
    dropped from the extraction but NOT from the document. `p_index` is
    preorder over the whole part, which is why it is computed before the
    filter -- if that ordering slipped, the anchor+hash check would fail the
    edit closed here."""
    body = _toc([("1. Definitions", 2), ("2. Term", 3)]) + _REAL_CLAUSES
    docx_bytes = _docx(body)
    result = ens.extract_and_normalize(docx_bytes)
    block_map = ens.build_block_map(result.get("paragraphs", []))
    target = next((bid for bid, blk in block_map.items() if _DEFINITIONS_BODY in blk["text"]), None)
    if target is None:
        failures.append("[N1] the Definitions clause is not addressable")
        return
    proven = bt.validate_block_patches(
        [
            {
                "block_id": target,
                "segments": [
                    {"op": "keep", "text": block_map[target]["text"]},
                    {"op": "insert", "text": " Disclosure must be in writing.", "issue_key": "I1"},
                ],
            }
        ],
        [],
        block_map,
    )
    if proven["status"] != "proven":
        failures.append(f"[N2] the clause did not prove: {[f.get('reason') for f in proven['failures']]}")
        return
    applied = rba.apply_block_transcript(
        docx_bytes,
        proven,
        author="TocFieldResultTest",
        timestamp_iso="2026-09-19T00:00:00Z",
    )
    if applied["docx_bytes"] is None or len(applied["applied"]) != 1 or applied["failures"]:
        failures.append(
            f"[N3] the edit did not compile against a TOC document: "
            f"applied={len(applied['applied'])} failures={applied['failures']}"
        )


def _hidden(markup_runs: str) -> str:
    """Every `<w:r>` in `markup_runs` given `<w:rPr><w:vanish/></w:rPr>` --
    hidden text, which `_process_run` strips and `_iter_field_events` skips."""
    return markup_runs.replace("<w:r>", "<w:r><w:rPr><w:vanish/></w:rPr>")


_MISC_BODY = "Misc body."

#: The clause bodies every "foreign end" test below requires to survive.
_FOUR_BODIES = [_DEFINITIONS_BODY, _TERM_BODY, _NOTICES_BODY, _MISC_BODY]


def _assert_every_clause_survives(tag: str, docx_bytes: bytes, failures: list, *, extra: tuple = ()) -> None:
    """Both reads (raw upload and materialized bytes) keep every real clause
    body, and agree with each other block for block."""
    stage_1, stage_5, _ = _both_reads(docx_bytes)
    for label, blocks in (("raw", stage_1), ("materialized", stage_5)):
        texts = [text for _, _, text in blocks]
        joined = "\n".join(texts)
        for body in (*_FOUR_BODIES, *extra):
            if body not in joined:
                failures.append(f"[{tag}] {label} read lost {body!r}: {blocks!r}")
        notes = ens.extract_and_normalize(docx_bytes).get("normalization_notes") or ""
        if "Generated field result omitted" in notes and label == "raw":
            failures.append(f"[{tag}] a region that swallowed clauses was still disclosed as dropped: {notes!r}")
    if stage_1 != stage_5:
        failures.append(f"[{tag}] the two reads disagree: {stage_1!r} != {stage_5!r}")


def test_a_foreign_end_cannot_close_a_toc_over_the_agreement(failures: list) -> None:
    """The round-2 reviewer's exact reproduction. A TOC whose own `end` is
    struck, the agreement, then a SECOND TOC whose `begin`/code/`separate`
    are struck: in the accept-all view the second field's `end` is the next
    `end` the stack sees, and it pops the FIRST TOC's frame. Every clause in
    between used to be dropped as "TOC field result" -- the block map came
    out as Miscellaneous alone. Fail toward keeping: every clause survives
    in both reads."""
    body = (
        _toc([("1. Definitions", 2), ("2. Term", 3)], strike_end=True)
        + _THREE_CLAUSES
        + _toc([("Figure 1", 5)], strike_opening=True)
        + _heading("4. Miscellaneous")
        + _para(_MISC_BODY)
    )
    _assert_every_clause_survives("S1", _docx(body), failures)


def _ref_with_opening(opening_wrapper) -> str:
    """A `REF` cross-reference whose cached result "Section 2" is its own
    paragraph, with `begin` + code + `separate` passed through
    `opening_wrapper` (struck, or hidden) so the accept-all view sees only
    its result and its `end`."""
    opening = _fld_char("begin") + _instr(" REF _Ref2 \\h ") + _fld_char("separate")
    return "<w:p>" + opening_wrapper(opening) + "<w:r><w:t>Section 2</w:t></w:r>" + _fld_char("end") + "</w:p>"


def _struck_opening(opening: str) -> str:
    return _struck(opening.replace("<w:instrText", "<w:delInstrText").replace("</w:instrText>", "</w:delInstrText>"))


def test_a_ref_with_a_struck_or_hidden_begin_cannot_close_a_toc(failures: list) -> None:
    """The reviewer's REF variant, both ways: the later field is a `REF`
    whose `begin`/code/`separate` are struck (T1) or sit in a hidden
    `w:vanish` run (T2) -- which `_iter_field_events` skips, exactly as
    `_process_run` strips hidden text. Its `end` pops the struck-end TOC's
    frame either way. Every clause, AND the REF's own result "Section 2",
    must survive; a field code other than TOC/INDEX keeps its result."""
    for tag, wrapper in (("T1", _struck_opening), ("T2", _hidden)):
        body = (
            _toc([("1. Definitions", 2), ("2. Term", 3)], strike_end=True)
            + _THREE_CLAUSES
            + _ref_with_opening(wrapper)
            + _heading("4. Miscellaneous")
            + _para(_MISC_BODY)
        )
        _assert_every_clause_survives(tag, _docx(body), failures, extra=("Section 2",))


def test_a_foreign_end_cannot_close_a_toc_over_unstyled_clauses(failures: list) -> None:
    """The same foreign `end`, over a style-stripped agreement: plain
    numbered paragraphs, no Heading style, no outline level -- so the
    real-heading guard cannot fire and only the entry-shape guard
    (`_paragraph_is_entry_shaped`: a body sentence is not a TOC line) stands
    between the region and the agreement."""
    plain = "".join(
        _para(text)
        for text in (
            "1. Definitions", _DEFINITIONS_BODY, "2. Term", _TERM_BODY,
            "3. Notices", _NOTICES_BODY, "4. Miscellaneous", _MISC_BODY,
        )
    )
    body = (
        _toc([("1. Definitions", 2), ("2. Term", 3)], strike_end=True)
        + plain[: plain.index("<w:p><w:r><w:t xml:space=\"preserve\">4. Miscellaneous")]
        + _toc([("Figure 1", 5)], strike_opening=True)
        + plain[plain.index("<w:p><w:r><w:t xml:space=\"preserve\">4. Miscellaneous"):]
    )
    _assert_every_clause_survives("U1", _docx(body), failures)


def _linked_heading(text: str, *, style: str, bookmark: str | None = None) -> str:
    """A clause heading whose text is itself an internal hyperlink -- a
    shape Word writes for a heading carrying a cross-reference link -- so it
    is entry-SHAPED (it carries an anchor) and only the real-heading or the
    bookmark guard can tell it from a TOC line."""
    mark = f'<w:bookmarkStart w:id="90" w:name="{bookmark}"/>' if bookmark else ""
    end_mark = '<w:bookmarkEnd w:id="90"/>' if bookmark else ""
    return (
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{mark}'
        f'<w:hyperlink w:anchor="_RefLink"><w:r><w:t>{text}</w:t></w:r></w:hyperlink>{end_mark}</w:p>'
    )


def test_the_real_heading_and_bookmark_guards_each_keep_a_region(failures: list) -> None:
    """Each remaining guard, isolated. The region between a struck-end TOC
    and a struck-opening one holds only entry-shaped paragraphs -- TOC lines
    plus ONE hyperlinked clause heading -- so the entry-shape guard passes it.
    V1: that heading is Heading-styled (a TOC is generated FROM headings,
    never made of them). V2: it is a custom style with no outline level on
    the paragraph, but carries the `_Toc1` bookmark the first TOC entry
    links to -- the region reached the very heading it lists. Either way the
    region is kept whole, so the heading still opens its clause."""
    cases = (
        ("V1", _linked_heading("1. Definitions", style="Heading1")),
        ("V2", _linked_heading("1. Definitions", style="ArticleTitle", bookmark="_Toc1")),
    )
    for tag, heading in cases:
        body = (
            _toc([("1. Definitions", 2)], strike_end=True)
            + heading
            + _toc([("Figure 1", 5)], strike_opening=True)
            + _para(_DEFINITIONS_BODY)
        )
        result = ens.extract_and_normalize(_docx(body))
        headings = [p["heading"] for p in result.get("paragraphs", [])]
        if "Definitions" not in headings:
            failures.append(f"[{tag}] the real heading was dropped as TOC field result: {_blocks(result)!r}")
        if "Generated field result omitted" in (result.get("normalization_notes") or ""):
            failures.append(f"[{tag}] the region must be kept, not disclosed as dropped: {result['normalization_notes']!r}")


def _web_hidden(run_inner: str) -> str:
    return f"<w:r><w:rPr><w:webHidden/></w:rPr>{run_inner}</w:r>"


def test_the_tickets_own_shape_end_alone_in_a_trailing_paragraph(failures: list) -> None:
    """Issue #114's Required-verification shape, committed: the `end` field
    character alone in its OWN trailing paragraph -- the way Word's TOC
    content control commonly closes -- inside the docPartGallery `w:sdt`,
    with each entry's `PAGEREF` page number in `w:webHidden` runs (hidden
    in WEB layout only, so NOT hidden text: `_run_is_hidden` reads
    `w:vanish`, and the page number stays visible field result)."""
    def entry(heading: str, page: int, anchor: str, opening: str = "") -> str:
        return (
            '<w:p><w:pPr><w:pStyle w:val="TOC1"/></w:pPr>' + opening
            + f'<w:hyperlink w:anchor="{anchor}"><w:r><w:t>{heading}</w:t></w:r>'
            + _web_hidden("<w:tab/>")
            + _web_hidden('<w:fldChar w:fldCharType="begin"/>')
            + _web_hidden(f'<w:instrText xml:space="preserve"> PAGEREF {anchor} \\h </w:instrText>')
            + _web_hidden('<w:fldChar w:fldCharType="separate"/>')
            + _web_hidden(f"<w:t>{page}</w:t>")
            + _web_hidden('<w:fldChar w:fldCharType="end"/>')
            + "</w:hyperlink></w:p>"
        )

    opening = _fld_char("begin") + _instr(' TOC \\o "1-3" \\h \\z \\u ') + _fld_char("separate")
    toc = (
        "<w:sdt><w:sdtPr><w:docPartObj>"
        '<w:docPartGallery w:val="Table of Contents"/><w:docPartUnique/>'
        "</w:docPartObj></w:sdtPr><w:sdtContent>"
        '<w:p><w:pPr><w:pStyle w:val="TOCHeading"/></w:pPr><w:r><w:t>Contents</w:t></w:r></w:p>'
        + entry("1. Definitions", 2, "_Toc1", opening)
        + entry("2. Term", 3, "_Toc2")
        + "<w:p>" + _fld_char("end") + "</w:p>"
        + "</w:sdtContent></w:sdt>"
    )
    result = ens.extract_and_normalize(_docx(toc + _REAL_CLAUSES))
    blocks = _blocks(result)
    # The "Contents" caption sits OUTSIDE the field (Word writes it as its
    # own TOCHeading paragraph), so it is ordinary preamble text and keeps
    # its block; only the field's result lines are dropped.
    expected = [("p0001", "<untitled>", "Contents")] + [
        (f"p{int(bid[1:]) + 1:04d}", heading, text) for bid, heading, text in _EXPECTED_BLOCKS
    ]
    if blocks != expected:
        failures.append(f"[W1] the ticket's own TOC shape reached the block map: {blocks!r}")
    if any("\t" in heading for _, heading, _ in blocks):
        failures.append(f"[W2] a TOC entry became a clause: {blocks!r}")
    if "2 paragraph(s) of TOC field result" not in (result.get("normalization_notes") or ""):
        failures.append(f"[W3] the omission must be disclosed and counted: {result.get('normalization_notes')!r}")


def test_hidden_text_after_the_field_end_is_not_body_text(failures: list) -> None:
    """Exercises the hidden-run skip in `_iter_field_events`. The TOC's last
    entry paragraph carries, after the field's `end`, a run of HIDDEN text
    (`w:vanish`). `_process_run` strips it, so it is not text a reader sees
    and must not count as "visible text outside the region" -- the entry is
    still dropped. Without the skip the hidden run would keep the whole
    entry paragraph, and "Definitions<tab>2" would come back as a clause."""
    toc = _toc([("1. Definitions", 2), ("2. Term", 3)])
    hidden_tail = _hidden('<w:r><w:t xml:space="preserve"> drafting note</w:t></w:r>')
    toc = toc[: -len("</w:p>")] + hidden_tail + "</w:p>"
    result = ens.extract_and_normalize(_docx(toc + _REAL_CLAUSES))
    if _blocks(result) != _EXPECTED_BLOCKS:
        failures.append(f"[X1] hidden text after the field end kept a TOC entry: {_blocks(result)!r}")
    if "2 paragraph(s) of TOC field result" not in (result.get("normalization_notes") or ""):
        failures.append(f"[X2] both entries must be dropped and disclosed: {result.get('normalization_notes')!r}")


def test_preflight_does_not_count_the_disclosure_group_as_a_paragraph(failures: list) -> None:
    """The notes-only group carrying the disclosure survives into the
    MATERIALIZED read (a dropped TOC exists there too), which is what
    `preflight_pass.compute_document_stats` counts. It is not a clause: the
    card's `paragraph_count` must equal the same document without a TOC."""
    without = preflight_pass.compute_document_stats(_docx(_REAL_CLAUSES))["paragraph_count"]
    with_toc = preflight_pass.compute_document_stats(
        _docx(_toc([("1. Definitions", 2), ("2. Term", 3)]) + _REAL_CLAUSES)
    )["paragraph_count"]
    if with_toc != without:
        failures.append(f"[Y1] preflight paragraph_count {with_toc} != {without} without the TOC")


TESTS = [
    test_a_toc_result_reaches_neither_the_block_map_nor_the_model,
    test_a_toc_does_not_shift_a_single_real_clauses_block_id,
    test_the_omission_is_disclosed_once_and_counts_the_paragraphs,
    test_the_disclosure_is_never_counted_as_an_accepted_edit,
    test_the_disclosure_quotes_no_dropped_line_and_no_field_code,
    test_a_complex_ref_field_keeps_its_result_inline,
    test_an_index_field_result_is_omitted_too,
    test_a_toc_whose_end_is_missing_drops_nothing,
    test_an_unclosed_toc_at_the_document_end_drops_nothing,
    test_a_toc_that_was_never_updated_has_no_result_to_omit,
    test_a_stray_end_field_character_changes_nothing,
    test_body_text_after_the_field_end_in_the_same_paragraph_is_kept,
    test_the_field_code_may_be_split_across_instr_text_runs,
    test_the_raw_and_the_materialized_read_agree_on_the_block_map,
    test_a_struck_toc_end_gives_both_reads_the_same_block_map,
    test_a_struck_toc_begin_and_field_code_give_both_reads_the_same_block_map,
    test_a_stage_1_block_id_deletes_the_same_clause_in_stage_5,
    test_a_toc_inside_its_block_level_content_control_is_omitted_too,
    test_an_edit_still_compiles_against_a_document_carrying_a_toc,
    test_a_foreign_end_cannot_close_a_toc_over_the_agreement,
    test_a_ref_with_a_struck_or_hidden_begin_cannot_close_a_toc,
    test_a_foreign_end_cannot_close_a_toc_over_unstyled_clauses,
    test_the_real_heading_and_bookmark_guards_each_keep_a_region,
    test_the_tickets_own_shape_end_alone_in_a_trailing_paragraph,
    test_hidden_text_after_the_field_end_is_not_body_text,
    test_preflight_does_not_count_the_disclosure_group_as_a_paragraph,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for failure in failures[before:]:
                print(f"FAIL: {failure}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found (issue #114).")
        return 1
    print("PASS: a TOC/INDEX field's generated result never becomes a clause block (issue #114).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
