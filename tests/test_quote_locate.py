#!/usr/bin/env python3
"""
Slice test (TDD) for issue #375: "Quote-locate harness: find a verbatim
model quote in a .docx (whitespace-tolerant, unique)".

## Root problem this proves fixed

Before this slice, no code located a verbatim model-quoted `source_quote`
back inside the uploaded `.docx` at all -- the quote-based redline plan's
span-apply wrapper (issue #N-2) has nothing to anchor an edit to without a
reliable locator. This test drives the real `scripts/quote_locate.py`
module (which does not exist before this slice -- this file FAILS on
import until it does) over small, hand-built OOXML fixtures reproducing the
documented divergence cases between what the model quotes and the raw
OOXML: a clean single paragraph, a multi-`<w:p>` logical paragraph whose
siblings are joined by a newline (`extraction_normalization_stage.py`;
issue #564 -- see Case B below), a paragraph carrying a pending tracked
change resolved via accept-all (`normalize_input.py:116-196`), a
field-result paragraph, and a paragraph containing a `w:tab`.

## What this test asserts (mirrors the issue's Acceptance criteria)

  1. `locate_quote` returns `found` with the correct `para_index` for a
     clean single-paragraph quote.
  2. Returns `ambiguous` when the quote appears in 2+ paragraphs; `not_
     found` when absent.
  3. Whitespace-variant quotes (extra spaces / a newline vs. the doc, a
     space standing in for a `w:tab`) still locate in the clean case; in
     the multi-`<w:p>` case (Case B), a quote spanning the physical join
     locates but classifies as `spans_paragraph_break` (issue #564), not
     `found` -- `docx_editor` cannot write a tracked change across two
     physical paragraphs even though the text is genuinely present.
  4. A per-case locate-rate table is printed (this issue's harness
     requirement).

Uses nothing but `zipfile` + `xml.etree.ElementTree` to build fixtures --
the same dependency-free convention `tests/test_extraction_normalization_
stage_80.py` and `scripts/redline_docx_writer.py` use (several fixtures
below need raw `w:ins`/`w:del`/`w:tab`/`w:fldSimple` markup python-docx's
public API does not expose).

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "quote_locate"

sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage  # type: ignore  # noqa: E402
import quote_locate  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder (same convention as
# tests/test_extraction_normalization_stage_80.py).
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

_DOC_NAMESPACES = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _build_docx_bytes(body_paragraphs_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NAMESPACES}>"
        f"<w:body>{body_paragraphs_xml}<w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _heading_p(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _pending_change_p(original: str, resulting: str, author: str = "counterparty") -> str:
    """Lone single-author pending tracked change -- accept-all disposition
    (issue #199 / `normalize_input.py:116-196`) replaces the paragraph's
    operative text with `resulting`."""
    return (
        "<w:p>"
        f'<w:del w:id="1" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        f'<w:ins w:id="2" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:t>{resulting}</w:t></w:r></w:ins>"
        "</w:p>"
    )


def _field_result_p(prefix: str, field_result: str, suffix: str, instr: str) -> str:
    """A static (non-edited) field result -- resolves to its literal
    displayed text, interleaved with visible run text."""
    return (
        "<w:p>"
        f"<w:r><w:t>{prefix}</w:t></w:r>"
        f'<w:fldSimple w:instr="{instr}"><w:r><w:t>{field_result}</w:t></w:r></w:fldSimple>'
        f"<w:r><w:t>{suffix}</w:t></w:r>"
        "</w:p>"
    )


def _tab_p(prefix: str, suffix: str) -> str:
    """A single physical paragraph whose two visible runs are separated by
    a real `<w:tab/>` -- `extraction_normalization_stage._process_run`
    folds each `w:tab` into a literal `\\t` character (issue #375's Read-
    first list, `normalize_input.py`'s 'NO interior-whitespace collapse')."""
    return f"<w:p><w:r><w:t>{prefix}</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>{suffix}</w:t></w:r></w:p>"


# ---------------------------------------------------------------------------
# Per-case locate-rate tracking
# ---------------------------------------------------------------------------

CASE_STATS: dict[str, list[int]] = {}  # case -> [passed, total]


def _check(
    failures: list[str],
    case: str,
    description: str,
    docx_bytes: bytes,
    quote: str,
    expected_status: str,
    expected_para_index: int | None = None,
) -> None:
    passed, total = CASE_STATS.setdefault(case, [0, 0])
    total += 1

    result = quote_locate.locate_quote(docx_bytes, quote)
    ok = result.get("status") == expected_status
    if ok and expected_para_index is not None:
        ok = result.get("para_index") == expected_para_index
    if ok and expected_status == "found":
        span = result.get("span")
        ok = isinstance(span, list) and len(span) == 2 and span[0] < span[1]

    if ok:
        passed += 1
    else:
        failures.append(
            f"[{case}] {description}: expected status={expected_status!r}"
            + (f" para_index={expected_para_index}" if expected_para_index is not None else "")
            + f", got {result!r}"
        )
    CASE_STATS[case] = [passed, total]


# ---------------------------------------------------------------------------
# Case A: clean single paragraph
# ---------------------------------------------------------------------------

_LIABILITY_TEXT = "Each party's aggregate liability under this Agreement shall not exceed $150,000."


def _generate_clean_single_paragraph_fixture() -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / "clean-single-paragraph.SYNTHETIC.docx"
    if not path.exists():
        body = _heading_p("Limitation on Liability") + _body_p(_LIABILITY_TEXT)
        path.write_bytes(_build_docx_bytes(body))
    return path


def test_clean_single_paragraph(failures: list[str]) -> None:
    case = "clean_single_paragraph"
    docx_bytes = _generate_clean_single_paragraph_fixture().read_bytes()

    _check(
        failures, case, "exact substring locates uniquely",
        docx_bytes,
        "aggregate liability under this Agreement shall not exceed $150,000.",
        "found", expected_para_index=0,
    )
    _check(
        failures, case, "whitespace-variant quote (extra interior spaces) still locates",
        docx_bytes,
        "aggregate  liability   under this Agreement shall not exceed $150,000.",
        "found", expected_para_index=0,
    )
    _check(
        failures, case, "whitespace-variant quote (leading/trailing whitespace) still locates",
        docx_bytes,
        "  aggregate liability under this Agreement shall not exceed $150,000.  ",
        "found", expected_para_index=0,
    )
    _check(
        failures, case, "absent quote is not_found",
        docx_bytes,
        "aggregate liability under this Agreement shall not exceed $999,000.",
        "not_found",
    )


# ---------------------------------------------------------------------------
# Case B: multi-<w:p> logical paragraph, siblings joined by a newline
# (issue #564: was a single space before this issue -- the whitespace-
# collapse matcher below treats the two identically, so this changes no
# locate outcome, only the returned classification of a spanning match).
# ---------------------------------------------------------------------------

_CONFIDENTIALITY_P1 = "The Receiving Party shall protect Confidential Information using reasonable care."
_CONFIDENTIALITY_P2 = "Any breach shall be reported within five business days."


def _generate_multi_physical_paragraph_fixture() -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / "multi-physical-paragraph.SYNTHETIC.docx"
    if not path.exists():
        body = _heading_p("Confidentiality") + _body_p(_CONFIDENTIALITY_P1) + _body_p(_CONFIDENTIALITY_P2)
        path.write_bytes(_build_docx_bytes(body))
    return path


def test_multi_physical_paragraph_joined_by_newline(failures: list[str]) -> None:
    case = "multi_physical_paragraph_joined_by_newline"
    docx_bytes = _generate_multi_physical_paragraph_fixture().read_bytes()

    # A quote fully inside ONE physical sibling still locates as "found" --
    # the ordinary case, unaffected by issue #564's classification.
    _check(
        failures, case, "quote inside the first physical sibling still locates as found",
        docx_bytes,
        _CONFIDENTIALITY_P1,
        "found", expected_para_index=0,
    )

    # A quote spanning the physical-paragraph join is genuinely present (the
    # whitespace-collapse matcher treats the "\n" join as elastic
    # whitespace, same as any other run) but `docx_editor` -- which edits
    # PHYSICAL paragraphs -- has nowhere to write a tracked change spanning
    # two of them. Issue #529/#564: this is REASON_SPANS_PARAGRAPH_BREAK,
    # never "found" and never "not_found".
    _check(
        failures, case, "quote spanning the physical-paragraph join classifies as spans_paragraph_break",
        docx_bytes,
        "using reasonable care. Any breach shall be reported within five business days.",
        quote_locate.REASON_SPANS_PARAGRAPH_BREAK, expected_para_index=0,
    )
    _check(
        failures, case, "whitespace-variant quote (literal newline at the join) still classifies the same way",
        docx_bytes,
        "using reasonable care.\nAny breach shall be reported within five business days.",
        quote_locate.REASON_SPANS_PARAGRAPH_BREAK, expected_para_index=0,
    )


# ---------------------------------------------------------------------------
# Case C: paragraph carrying a pending tracked change (accept-all)
# ---------------------------------------------------------------------------


def _generate_tracked_change_accept_all_fixture() -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / "tracked-change-accept-all.SYNTHETIC.docx"
    if not path.exists():
        body = _heading_p("Limitation on Liability") + _pending_change_p(
            _LIABILITY_TEXT,
            "Each party's liability under this Agreement shall be uncapped.",
        )
        path.write_bytes(_build_docx_bytes(body))
    return path


def test_tracked_change_accept_all(failures: list[str]) -> None:
    case = "tracked_change_accept_all"
    docx_bytes = _generate_tracked_change_accept_all_fixture().read_bytes()

    _check(
        failures, case, "quote from the accepted (resulting) text locates",
        docx_bytes,
        "liability under this Agreement shall be uncapped.",
        "found", expected_para_index=0,
    )
    _check(
        failures, case, "quote from the pre-edit (original) text is not_found post accept-all",
        docx_bytes,
        "shall not exceed $150,000.",
        "not_found",
    )


# ---------------------------------------------------------------------------
# Case D: field-result paragraph
# ---------------------------------------------------------------------------


def _generate_field_result_fixture() -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / "field-result.SYNTHETIC.docx"
    if not path.exists():
        body = _heading_p("Governing Law") + _field_result_p(
            "This Agreement shall be governed by the laws of ",
            "Delaware",
            ".",
            " REF GoverningLawJurisdiction ",
        )
        path.write_bytes(_build_docx_bytes(body))
    return path


def test_field_result(failures: list[str]) -> None:
    case = "field_result"
    docx_bytes = _generate_field_result_fixture().read_bytes()

    _check(
        failures, case, "quote spanning prefix + resolved field result + suffix locates",
        docx_bytes,
        "governed by the laws of Delaware.",
        "found", expected_para_index=0,
    )
    _check(
        failures, case, "the raw field code is not in the shown text and is not_found",
        docx_bytes,
        "REF GoverningLawJurisdiction",
        "not_found",
    )


# ---------------------------------------------------------------------------
# Case E: tabs
# ---------------------------------------------------------------------------


def _generate_tabs_fixture() -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / "tabs.SYNTHETIC.docx"
    if not path.exists():
        body = _heading_p("Notice") + _tab_p("Party A:", "Party B")
        path.write_bytes(_build_docx_bytes(body))
    return path


def test_tabs(failures: list[str]) -> None:
    case = "tabs"
    docx_bytes = _generate_tabs_fixture().read_bytes()

    _check(
        failures, case, "quote with the literal tab character locates",
        docx_bytes,
        "Party A:\tParty B",
        "found", expected_para_index=0,
    )
    _check(
        failures, case, "whitespace-variant quote (space standing in for the tab) still locates",
        docx_bytes,
        "Party A: Party B",
        "found", expected_para_index=0,
    )


# ---------------------------------------------------------------------------
# Case F: ambiguous -- same quote text present in 2+ paragraphs
# ---------------------------------------------------------------------------


def _generate_ambiguous_repeated_clause_fixture() -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / "ambiguous-repeated-clause.SYNTHETIC.docx"
    if not path.exists():
        repeated = "This is a repeated boilerplate clause used twice in this document."
        body = (
            _heading_p("Section A")
            + _body_p(repeated)
            + _heading_p("Section B")
            + _body_p(repeated)
        )
        path.write_bytes(_build_docx_bytes(body))
    return path


def test_ambiguous_when_quote_in_two_or_more_paragraphs(failures: list[str]) -> None:
    case = "ambiguous_repeated_clause"
    docx_bytes = _generate_ambiguous_repeated_clause_fixture().read_bytes()

    _check(
        failures, case, "a quote present in 2 paragraphs is ambiguous, not a silent first-match guess",
        docx_bytes,
        "This is a repeated boilerplate clause used twice in this document.",
        "ambiguous",
    )
    _check(
        failures, case, "a quote absent from either paragraph is still not_found",
        docx_bytes,
        "This clause does not appear anywhere in this document.",
        "not_found",
    )


# ---------------------------------------------------------------------------
# Case G: typographic punctuation (measured against the real corpus)
#
# Word autocorrects a typed apostrophe to U+2019 and typed quotes to U+201C/
# U+201D, so real counterparty paper is full of them: 15 of the 16 normalizable
# documents in the real EIAA corpus carry curly punctuation (doc01 alone: 45x
# U+2019, 20x U+201C, 20x U+201D). Models reliably ASCII-fold it when copying a
# `source_quote` back out -- measured 2026-08-05 on a real review, where BOTH
# of the model's quotes located `not_found` and the whole redline died with
# `quote_patches_not_applied`:
#
#   Section 8  quote matched 393/522 chars, stopping at `Institution's`  vs `Institution’s`
#   Section 16 quote matched  78/539 chars, stopping at `"Confidential"` vs `“Confidential”`
#
# This is the same class of divergence as the whitespace case this module
# already tolerates -- a faithful copy that differs only in how a character is
# encoded, never a semantic edit -- so it belongs in the comparison-only form
# rather than in a fuzzy matcher. Word content and casing must still match
# exactly, and the returned span still points into the paragraph's REAL text
# (curly punctuation intact), which the last assertion below pins.
# ---------------------------------------------------------------------------

_CURLY_INDEMNITY_TEXT = (
    "The Institution’s indemnity covers the acts and omissions of the "
    "Institution’s employees, agents and volunteers while acting on the "
    "Institution’s behalf."
)
_CURLY_CONFIDENTIALITY_TEXT = (
    "The Institution shall not disclose any “Confidential Information” "
    "of the Company – including any protected health information "
    "(“PHI”) – to any third party."
)


def _generate_typographic_punctuation_fixture() -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / "typographic-punctuation.SYNTHETIC.docx"
    if not path.exists():
        body = (
            _heading_p("Indemnification")
            + _body_p(_CURLY_INDEMNITY_TEXT)
            + _heading_p("Confidentiality")
            + _body_p(_CURLY_CONFIDENTIALITY_TEXT)
        )
        path.write_bytes(_build_docx_bytes(body))
    return path


def test_typographic_punctuation(failures: list[str]) -> None:
    case = "typographic_punctuation"
    docx_bytes = _generate_typographic_punctuation_fixture().read_bytes()

    _check(
        failures, case, "an ASCII-folded apostrophe still locates the curly original",
        docx_bytes,
        _CURLY_INDEMNITY_TEXT.replace("’", "'"),
        "found",
        0,
    )
    _check(
        failures, case, "ASCII-folded double quotes and dashes still locate",
        docx_bytes,
        _CURLY_CONFIDENTIALITY_TEXT.replace("“", '"').replace("”", '"').replace("–", "-"),
        "found",
        1,
    )
    _check(
        failures, case, "a partial quote ending mid-apostrophe locates its own span",
        docx_bytes,
        "acts and omissions of the Institution's employees",
        "found",
        0,
    )
    _check(
        failures, case, "the curly original still locates unchanged",
        docx_bytes,
        _CURLY_INDEMNITY_TEXT,
        "found",
        0,
    )
    _check(
        failures, case, "folding punctuation does not make unrelated word content match",
        docx_bytes,
        "The Institution's indemnity covers nothing whatsoever.",
        "not_found",
    )

    # The span must point into the paragraph's REAL text: the redline quotes
    # the document, never the model's ASCII-folded transcription of it. This
    # is the whole reason the fold is comparison-only.
    result = quote_locate.locate_quote(
        docx_bytes, "acts and omissions of the Institution's employees"
    )
    passed, total = CASE_STATS.setdefault(case, [0, 0])
    total += 1
    located = ""
    if result.get("status") == "found":
        norm = extraction_normalization_stage.extract_and_normalize(docx_bytes)
        start, end = result["span"]
        located = norm["paragraphs"][result["para_index"]]["text"][start:end]
    if located == "acts and omissions of the Institution’s employees":
        passed += 1
    else:
        failures.append(
            f"[{case}] the returned span must carry the document's own U+2019, "
            f"not the model's ASCII fold; got {located!r}"
        )
    CASE_STATS[case] = [passed, total]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_clean_single_paragraph,
    test_multi_physical_paragraph_joined_by_newline,
    test_tracked_change_accept_all,
    test_field_result,
    test_tabs,
    test_ambiguous_when_quote_in_two_or_more_paragraphs,
    test_typographic_punctuation,
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
            for f in failures[before:]:
                print(f"FAIL: {f}")

    print()
    print("Per-case locate-rate table:")
    print(f"  {'case':<40} {'passed/total':<15} rate")
    for case, (passed, total) in CASE_STATS.items():
        rate = f"{(100.0 * passed / total):.0f}%" if total else "n/a"
        print(f"  {case:<40} {f'{passed}/{total}':<15} {rate}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: all quote-locate (issue #375) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
