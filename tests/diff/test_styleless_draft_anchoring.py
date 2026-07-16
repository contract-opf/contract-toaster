#!/usr/bin/env python3
"""
RED test -- issue #277: clause-boundary detection must not require Word
Heading styles.

## Root problem this proves fixed

`scripts/extraction_normalization_stage.py::extract_document_paragraphs()`
is the REAL first-party draft-ingestion path (`scripts/review_spine.py`
Stage 1, `review_spine.py:315`): its output (`draft_paragraphs`) is what
actually feeds `scripts/diff_standard_form.py::diff_draft_against_standard()`
(`review_spine.py:327`). Before this issue's fix, that function grouped
paragraphs into logical sections using ONLY a Word `Heading*` style gate
(`_paragraph_style_is_heading()`). Real counterparty drafts routinely lose
named heading styles (retypes, format-stripping, other editors, PDF
round-trips) and mark section breaks with bold single-line paragraphs or
manually-typed numbering instead -- with the style-only gate, every
paragraph in such a draft collapses into ONE `"<untitled>"` logical
paragraph, which then anchors to nothing but the `sec-_new` fallback tier
of `diff_draft_against_standard()` instead of the real standard-form
sections it actually corresponds to.

NOTE: this issue's ORIGINAL body named two other locations
(`scripts/diff_standard_form.py:431`/`scripts/build_anchor_map.py:242`) as
the defect site; the issue's "Grind notes" correction (2026-07-14) confirms
those two are canonical-STANDARD-FORM-only loaders (never invoked on a
counterparty draft) and the real draft-ingestion defect is in
`extraction_normalization_stage.py` as described above -- this test targets
the corrected location. The two canonical loaders are untouched by this
fix (see `scripts/clause_boundaries.py`'s module docstring).

## What this test asserts

A synthetic derivative draft `.docx` with ALL heading styles stripped
(bold single-line paragraphs for one section heading, a manually-typed
numeric lead-in for another) must still:

  1. Produce draft paragraphs with real headings -- never `"<untitled>"`.
  2. Anchor to the SAME standard-form sections, in the SAME hunk order,
     with the SAME hunk kinds, as an equivalent draft that DOES carry real
     Heading styles (issue #277's AC: "Style-stripped derivative fixture
     anchors to the same sections as the styled fixture (same diff hunks
     module ordering)").
  3. Never fall through to the `sec-_new` fallback tier for those two
     sections.

Fails on the pre-fix tree because `extract_document_paragraphs()` detects
zero headings in the styleless fixture (no `Heading*` style anywhere), so
both sections collapse into one `"<untitled>"` logical paragraph that
matches no standard-form heading.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import diff_standard_form  # type: ignore  # noqa: E402
import extraction_normalization_stage as stage  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder (same zipfile-only convention
# as tests/test_extraction_normalization_stage_80.py / scripts/redline_docx_
# writer.py -- no python-docx here).
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

_DOC_NAMESPACES = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
)


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


def _heading_style_p(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _bold_heading_p(text: str) -> str:
    """A style-stripped heading rendered as a bold single-line paragraph --
    no `w:pStyle` at all (implicitly Normal), matching a real counterparty
    retype/format-strip."""
    return f"<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{text}</w:t></w:r></w:p>"


def _numbered_heading_p(lead_in: str, text: str) -> str:
    """A style-stripped heading rendered with a manually-typed numeric
    lead-in -- no `w:pStyle`, not bold -- the OTHER common style-stripped
    retype pattern."""
    return f"<w:p><w:r><w:t>{lead_in} {text}</w:t></w:r></w:p>"


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _draft_paragraphs_from_body_xml(body_xml: str, label: str) -> list:
    result = stage.extract_and_normalize(_build_docx_bytes(body_xml))
    if result.get("status") != "normalized":
        raise AssertionError(f"[{label}] fixture failed to normalize: {result}")
    return result["paragraphs"]


def main() -> int:
    failures: list[str] = []

    standard = diff_standard_form.load_standard_form_paragraphs(playbook_id="eiaa")
    by_anchor = {p["anchor"]: p for p in standard}
    liability = by_anchor["sec-8"]  # heading: "Limitation on Liability"
    nondiscrim = by_anchor["sec-4"]  # heading: "Non-Discrimination"

    styled_body = "".join(
        [
            _heading_style_p(liability["heading"]),
            _body_p(liability["text"]),
            _heading_style_p(nondiscrim["heading"]),
            _body_p(nondiscrim["text"]),
        ]
    )
    styleless_body = "".join(
        [
            _bold_heading_p(liability["heading"]),
            _body_p(liability["text"]),
            _numbered_heading_p("4.", nondiscrim["heading"]),
            _body_p(nondiscrim["text"]),
        ]
    )

    styled_draft = _draft_paragraphs_from_body_xml(styled_body, "styled")
    styleless_draft = _draft_paragraphs_from_body_xml(styleless_body, "styleless")

    untitled = [p for p in styleless_draft if p.get("heading") == "<untitled>"]
    if untitled:
        failures.append(
            f"[1] Style-stripped draft must not collapse any section into "
            f"'<untitled>'. Got: {styleless_draft!r}"
        )

    styleless_headings = [p.get("heading") for p in styleless_draft]
    if liability["heading"] not in styleless_headings:
        failures.append(
            f"[2] Bold-single-line heading {liability['heading']!r} not recovered "
            f"from the style-stripped draft. Got headings: {styleless_headings!r}"
        )
    if nondiscrim["heading"] not in styleless_headings:
        failures.append(
            f"[3] Numbered-lead-in heading {nondiscrim['heading']!r} (typed as "
            f"'4. {nondiscrim['heading']}') not recovered (leading marker must be "
            f"stripped so it normalize-matches the standard heading). Got headings: "
            f"{styleless_headings!r}"
        )

    hunks_styled = diff_standard_form.diff_draft_against_standard(standard, styled_draft)
    hunks_styleless = diff_standard_form.diff_draft_against_standard(standard, styleless_draft)

    if hunks_styled != hunks_styleless:
        failures.append(
            "[4] Style-stripped derivative fixture must anchor to the same "
            "sections, in the same order, with the same hunk kinds, as the "
            "styled fixture (issue #277 AC). Diverged:\n"
            f"  styled:    {hunks_styled!r}\n"
            f"  styleless: {hunks_styleless!r}"
        )

    for anchor, human_label in (("sec-8", "Limitation on Liability"), ("sec-4", "Non-Discrimination")):
        matches = [h for h in hunks_styleless if h["anchor"] == anchor]
        if len(matches) != 1 or matches[0]["kind"] != "unchanged":
            failures.append(
                f"[5] Styleless draft's {human_label!r} section (anchor {anchor!r}) "
                f"must anchor with kind 'unchanged' (body text identical to the "
                f"standard form). Got: {matches!r}"
            )

    bad_sec_new = [h for h in hunks_styleless if h["anchor"] == "sec-_new"]
    if bad_sec_new:
        failures.append(
            f"[6] Styleless draft must not fall through to the 'sec-_new' "
            f"fallback tier for sections that actually match standard-form "
            f"headings. Got: {bad_sec_new!r}"
        )

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print()
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1

    print("PASS: styleless-draft anchoring (issue #277) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
