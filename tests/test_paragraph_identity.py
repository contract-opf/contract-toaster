#!/usr/bin/env python3
"""
Slice test for issue #564: "make the locator and editor agree about what a
paragraph is -- newline joins + physical spans".

## Root problem this proves fixed

`scripts/quote_locate.py` searches the NORMALIZED, LOGICAL paragraph list,
where `scripts/extraction_normalization_stage.py::normalize_paragraphs`
joins sibling PHYSICAL `<w:p>`s into one `text`.
`scripts/redline_quote_apply.py::apply_quote_patches` then hands the located
substring to `docx_editor`, which edits PHYSICAL paragraphs one at a time.
Before this issue, the join character (`" "`) carried no information about
where one physical paragraph ended and the next began, so a quote that
located cleanly ACROSS the join returned zero matches in `docx_editor` and
was reported as `not_found` -- telling the attorney their document does not
contain text it demonstrably does (issue #529).

This test drives the real `normalize_paragraphs` / `locate_quote_in_
paragraphs` / `apply_quote_patches` end to end over a fixture with THREE
sibling physical `<w:p>`s forming one logical paragraph, and asserts issue
#564's Acceptance criteria, verbatim:

  1. A logical paragraph spanning 3 physical `<w:p>`s yields `text` with two
     `"\\n"` joins and three `physical_spans` entries whose concatenation
     (with the joins) reconstructs `text` exactly.
  2. A quote inside one physical paragraph locates with the correct physical
     index and applies.
  3. A quote spanning a join locates (whitespace-elastic) but classifies as
     `REASON_SPANS_PARAGRAPH_BREAK` and joins the flag-only path -- never
     `not_found`.

Uses nothing but `zipfile` + `xml.etree.ElementTree` to build fixtures, the
same dependency-free convention as `tests/test_quote_locate.py` and
`tests/test_extraction_normalization_stage_80.py`.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "paragraph_identity"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage as stage  # type: ignore  # noqa: E402
import quote_locate  # type: ignore  # noqa: E402
import redline_quote_apply  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder (same convention as
# tests/test_quote_locate.py).
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


# ---------------------------------------------------------------------------
# Fixture: ONE heading, THREE sibling physical <w:p>s -> one logical
# paragraph with three physical_spans entries.
# ---------------------------------------------------------------------------

_P1 = "Obligations of the Institution."
_P2 = "The Institution will be responsible for its own acts and omissions."
_P3 = "Survival. This Section shall survive termination of the Agreement."


def _generate_three_physical_fixture() -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / "three-physical-siblings.SYNTHETIC.docx"
    if not path.exists():
        body = _heading_p("Section 8") + _body_p(_P1) + _body_p(_P2) + _body_p(_P3)
        path.write_bytes(_build_docx_bytes(body))
    return path


def _normalized_paragraphs() -> list[dict]:
    docx_bytes = _generate_three_physical_fixture().read_bytes()
    result = stage.extract_and_normalize(docx_bytes)
    assert result["status"] == "normalized", result
    return result["paragraphs"]


# ---------------------------------------------------------------------------
# AC1: physical_spans reconstruct text exactly.
# ---------------------------------------------------------------------------


def test_physical_spans_reconstruct_text_exactly(failures: list[str]) -> None:
    paragraphs = _normalized_paragraphs()
    if len(paragraphs) != 1:
        failures.append(
            f"fixture premise broken: expected ONE logical paragraph (three "
            f"physical siblings under one heading), got {len(paragraphs)}: {paragraphs!r}"
        )
        return

    para = paragraphs[0]
    text = para.get("text", "")
    spans = para.get("physical_spans")

    if text.count("\n") != 2:
        failures.append(
            f"a logical paragraph spanning 3 physical <w:p>s must yield text "
            f"with exactly two '\\n' joins; got {text.count(chr(10))} in {text!r}"
        )
    if not spans or len(spans) != 3:
        failures.append(f"expected exactly three physical_spans entries, got {spans!r}")
        return

    pieces = [text[start:end] for start, end in spans]
    if pieces != [_P1, _P2, _P3]:
        failures.append(
            f"physical_spans do not map back to the three physical paragraphs, "
            f"in order: {pieces!r} != {[_P1, _P2, _P3]!r}"
        )

    reconstructed = "\n".join(pieces)
    if reconstructed != text:
        failures.append(
            f"concatenating physical_spans (joined by '\\n') does not "
            f"reconstruct text exactly: {reconstructed!r} != {text!r}"
        )


# ---------------------------------------------------------------------------
# AC2: a quote inside one physical paragraph locates with the correct
# physical index and applies.
# ---------------------------------------------------------------------------


def test_quote_inside_one_physical_paragraph_locates_with_correct_index(failures: list[str]) -> None:
    paragraphs = _normalized_paragraphs()

    loc = quote_locate.locate_quote_in_paragraphs(paragraphs, _P2)
    if loc["status"] != "found":
        failures.append(f"expected status=found for a quote inside one physical paragraph; got {loc!r}")
        return
    if loc.get("physical_para_index") != 1:
        failures.append(
            f"expected physical_para_index=1 (the second of three physical "
            f"siblings); got {loc!r}"
        )


def test_quote_inside_one_physical_paragraph_applies(failures: list[str]) -> None:
    docx_bytes = _generate_three_physical_fixture().read_bytes()
    result = redline_quote_apply.apply_quote_patches(
        docx_bytes,
        [{"source_quote": _P2, "new_text": "Replacement text.", "rationale": "test"}],
        author="test",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    if len(result["applied"]) != 1 or result["docx_bytes"] is None:
        failures.append(
            f"a quote fully inside one physical paragraph must apply: "
            f"applied={result['applied']} flag_only={result['flag_only']!r}"
        )


# ---------------------------------------------------------------------------
# AC3: a quote spanning a join locates (whitespace-elastic) but classifies
# as spans_paragraph_break -- never not_found.
# ---------------------------------------------------------------------------


def test_quote_spanning_a_join_locates_but_is_never_not_found(failures: list[str]) -> None:
    paragraphs = _normalized_paragraphs()

    # The document's own text joins P1/P2 with a literal "\n"; a model
    # copying it back out commonly collapses that to a plain space -- both
    # must locate identically (the whitespace-collapse matcher treats "\n"
    # as elastic whitespace, same as any other run).
    exact_join = f"{_P1}\n{_P2}"
    space_variant = f"{_P1} {_P2}"

    for quote in (exact_join, space_variant):
        loc = quote_locate.locate_quote_in_paragraphs(paragraphs, quote)
        if loc["status"] != quote_locate.REASON_SPANS_PARAGRAPH_BREAK:
            failures.append(
                f"expected status={quote_locate.REASON_SPANS_PARAGRAPH_BREAK!r} "
                f"for a quote spanning the physical join; got {loc!r} (quote={quote!r})"
            )
        if loc.get("physical_para_index") is not None:
            failures.append(f"a spanning quote must not report a physical_para_index: {loc!r}")
        if loc["status"] == "not_found":
            failures.append(
                "a quote that LOCATED (across a physical-paragraph join) must "
                "never be reported as not_found -- the document does contain it"
            )


def test_quote_spanning_a_join_never_applies_and_never_reports_not_found(failures: list[str]) -> None:
    docx_bytes = _generate_three_physical_fixture().read_bytes()
    spanning_quote = f"{_P1} {_P2}"

    result = redline_quote_apply.apply_quote_patches(
        docx_bytes,
        [{"source_quote": spanning_quote, "new_text": "Replacement text.", "rationale": "test"}],
        author="test",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    if result["applied"] or result["docx_bytes"] is not None:
        failures.append(
            "a quote spanning a physical-paragraph join must never apply -- "
            "there is no single physical paragraph docx_editor can write the "
            f"tracked change into; got applied={result['applied']!r}"
        )
    if len(result["flag_only"]) != 1:
        failures.append(f"expected exactly one flag-only patch, got {result['flag_only']!r}")
        return

    reason = result["flag_only"][0].get("reason")
    if reason == redline_quote_apply.REASON_NOT_FOUND:
        failures.append(
            "a quote that LOCATED is reported as not_found -- the analysis "
            "report would tell the attorney the text is not in their "
            "document when it demonstrably is"
        )
    elif reason != redline_quote_apply.REASON_SPANS_PARAGRAPH_BREAK:
        failures.append(f"expected reason=spans_paragraph_break, got {reason!r}")


def test_a_genuinely_absent_quote_is_still_not_found(failures: list[str]) -> None:
    """The new classification must not swallow the real case: a quote the
    model hallucinated, or mis-transcribed beyond the locator's whitespace
    tolerance, is still `not_found`."""
    paragraphs = _normalized_paragraphs()
    loc = quote_locate.locate_quote_in_paragraphs(
        paragraphs, "This sentence appears nowhere in this document at all."
    )
    if loc["status"] != "not_found":
        failures.append(f"expected not_found for a genuinely absent quote; got {loc!r}")


TESTS = [
    test_physical_spans_reconstruct_text_exactly,
    test_quote_inside_one_physical_paragraph_locates_with_correct_index,
    test_quote_inside_one_physical_paragraph_applies,
    test_quote_spanning_a_join_locates_but_is_never_not_found,
    test_quote_spanning_a_join_never_applies_and_never_reports_not_found,
    test_a_genuinely_absent_quote_is_still_not_found,
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
    print("\nPASS: the locator and editor agree about what a paragraph is (issue #564).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
