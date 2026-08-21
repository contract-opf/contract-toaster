#!/usr/bin/env python3
"""
Gate for issue #529, fixed properly by issue #564: a quote that spans a
paragraph break says so.

## History

PR #552 fixed #529 with `_fits_in_one_physical_paragraph`, a SEPARATE regex
re-parse of `word/document.xml` run inside `redline_quote_apply.py` after
`quote_locate` had already located a quote against the NORMALIZED text.
That PR never merged to main (superseded by this ticket -- see #564's
landing comment on PR #552). Issue #564 supersedes it with the proper fix:
`extraction_normalization_stage.normalize_paragraphs` now carries
`physical_spans` (real per-`<w:p>` character ranges) on every normalized
paragraph, and `quote_locate.locate_quote_in_paragraphs` itself classifies a
located span as fitting one physical paragraph or crossing a join -- nothing
downstream needs to re-parse OOXML to find out.

## The two halves of the quote path disagree about what a paragraph is

`quote_locate` searches the NORMALIZED, LOGICAL paragraph list, where
`extraction_normalization_stage` joins multi-`<w:p>` logical-paragraph
siblings with `"\\n"` (issue #564; was a single space before this issue --
case B of `tests/test_quote_locate.py` pins the whitespace-tolerant
matching that makes this change invisible to matching outcomes either way).

`apply_quote_patches` then hands the located substring to `docx_editor` via
`doc.find_all(actual_text)`, which searches PHYSICAL paragraphs. A quote
that spans the join locates cleanly in step 1 and returns zero matches in
step 2 -- reporting that as `not_found` is actively wrong, because the quote
WAS found.

That matters because the prompt asks the model to quote a whole clause, so
this is not an edge case: measured on real corpus doc01, one of the two
proposed edits failed exactly this way, and the analysis report told the
attorney the quoted text was not in their document.

## What this file asserts

  1. A quote spanning a multi-`<w:p>` logical paragraph gets its OWN reason,
     `spans_paragraph_break`, never `not_found`.
  2. A quote genuinely absent from the document still gets `not_found` -- the
     new token must not swallow the real case.
  3. The classification lives in `quote_locate.locate_quote_in_paragraphs`
     itself, computed from `physical_spans` -- not a downstream regex
     re-parse -- and PR #552's regex helper is gone from
     `redline_quote_apply` for good.
  4. The distinction reaches the analysis report a human reads, because a
     reason token nobody surfaces is the same as no reason token.

Built with the same dependency-free OOXML fixtures as the other redline
tests.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage  # noqa: E402
import quote_locate  # noqa: E402
import redline_generate  # noqa: E402
import redline_quote_apply  # noqa: E402

# Two PHYSICAL paragraphs that `extraction_normalization_stage` joins into one
# LOGICAL paragraph -- the exact shape real corpus doc01's Section 8 has,
# where the model quoted across the join.
_FIRST = "Obligations of the Institution. The Institution will be responsible for its own acts."
_SECOND = "Survival. The provisions of this Section 8 shall survive termination."
_SPANNING_QUOTE = f"{_FIRST} {_SECOND}"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def _docx(paragraph_texts: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraph_texts)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}<w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


def _apply(quote: str, docx_bytes: bytes) -> dict:
    return redline_quote_apply.apply_quote_patches(
        docx_bytes,
        [{"source_quote": quote, "new_text": "Replacement text.", "rationale": "r"}],
        author="test",
        timestamp_iso="2026-01-01T00:00:00Z",
    )


def test_the_fixture_really_joins_two_physical_paragraphs(failures: list) -> None:
    """Guards the guard. If normalization stopped joining these -- a change
    to the multi-`<w:p>` rule -- the spanning test below would pass for the
    wrong reason, because the quote would simply locate inside one
    paragraph."""
    result = extraction_normalization_stage.extract_and_normalize(_docx([_FIRST, _SECOND]))
    texts = [p.get("text", "") for p in result.get("paragraphs", [])]
    joined = f"{_FIRST}\n{_SECOND}"
    if not any(joined in text for text in texts):
        failures.append(
            "the fixture no longer produces one logical paragraph spanning "
            f"both physical ones (joined by a single '\\n'), so this file "
            f"tests nothing: {texts!r}"
        )


def test_a_spanning_quote_is_not_reported_as_not_found(failures: list) -> None:
    result = _apply(_SPANNING_QUOTE, _docx([_FIRST, _SECOND]))
    flagged = result["flag_only"]
    if len(flagged) != 1:
        failures.append(f"expected exactly one flag-only patch, got {len(flagged)}")
        return
    reason = flagged[0].get("reason")
    if reason == "not_found":
        failures.append(
            "a quote that LOCATED is still reported as not_found -- the "
            "analysis report tells the attorney the text is not in their "
            "document"
        )
    elif reason != redline_quote_apply.REASON_SPANS_PARAGRAPH_BREAK:
        failures.append(f"expected spans_paragraph_break, got {reason!r}")


def test_a_genuinely_absent_quote_is_still_not_found(failures: list) -> None:
    """The new token must not swallow the real case: a quote the model
    hallucinated, or mis-transcribed beyond the locator's tolerance, is still
    `not_found`."""
    result = _apply("This sentence appears nowhere in the document.", _docx([_FIRST, _SECOND]))
    reason = result["flag_only"][0].get("reason")
    if reason != redline_quote_apply.REASON_NOT_FOUND:
        failures.append(f"expected not_found for an absent quote, got {reason!r}")


def test_the_locator_itself_distinguishes_the_two_cases(failures: list) -> None:
    """The classification lives in `quote_locate.locate_quote_in_paragraphs`
    (issue #564), computed from `physical_spans` -- real per-`<w:p>` data --
    never by re-parsing OOXML downstream. This is hard to provoke end to end
    and easy to assert here -- a classifier only exercised through the path
    where one branch never runs is half untested."""
    norm = extraction_normalization_stage.extract_and_normalize(_docx([_FIRST, _SECOND]))
    paragraphs = norm.get("paragraphs") or []
    if not paragraphs:
        failures.append(f"fixture did not normalize: {norm!r}")
        return

    within = quote_locate.locate_quote_in_paragraphs(paragraphs, _FIRST)
    if within.get("status") != "found" or within.get("physical_para_index") != 0:
        failures.append(
            f"text contained in the FIRST physical paragraph was not "
            f"recognised as such: {within!r}"
        )

    spanning = quote_locate.locate_quote_in_paragraphs(paragraphs, _SPANNING_QUOTE)
    if spanning.get("status") != quote_locate.REASON_SPANS_PARAGRAPH_BREAK:
        failures.append(
            f"text spanning two physical paragraphs was not classified "
            f"spans_paragraph_break: {spanning!r}"
        )
    if spanning.get("physical_para_index") is not None:
        failures.append(f"a spanning quote must not report a physical_para_index: {spanning!r}")


def test_the_regex_reparse_helper_is_gone(failures: list) -> None:
    """Acceptance criterion, verbatim: 'The regex _PARAGRAPH_RE/_RUN_TEXT_RE
    re-parse in redline_quote_apply is gone.' This supersedes PR #552's
    (never-merged) `_fits_in_one_physical_paragraph`, which read
    `word/document.xml` runs directly instead of using `physical_spans`."""
    for name in ("_fits_in_one_physical_paragraph", "_PARAGRAPH_RE", "_RUN_TEXT_RE"):
        if hasattr(redline_quote_apply, name):
            failures.append(
                f"redline_quote_apply.{name} must not exist -- issue #564 "
                f"supersedes the regex re-parse with physical_spans-based "
                f"classification computed by quote_locate"
            )


def test_a_quote_inside_one_paragraph_still_applies(failures: list) -> None:
    """The ordinary path is untouched."""
    result = _apply(_FIRST, _docx([_FIRST, _SECOND]))
    if len(result["applied"]) != 1 or result["docx_bytes"] is None:
        failures.append(
            "a quote contained in a single paragraph no longer applies: "
            f"applied={len(result['applied'])} flag_only={result['flag_only']!r}"
        )


def test_the_report_explains_the_difference_to_a_human(failures: list) -> None:
    """A reason token nobody surfaces is the same as no reason token. The
    attorney reading this report has to be able to tell 'your document does
    not contain this' from 'we could not edit across a paragraph break'."""
    report = redline_generate._build_quote_analysis_report(
        [
            {
                "source_quote": _SPANNING_QUOTE,
                "reason": redline_quote_apply.REASON_SPANS_PARAGRAPH_BREAK,
                "_source_issue": {"section_ref": "Section 8", "section_title": "Indemnification"},
            }
        ]
    )
    # Assert the PROSE, not the whole repr: the reason token itself contains
    # the word "paragraph", so a repr-wide search would pass while the only
    # human-readable sentence in the report still said the text was simply
    # not found. That is the exact failure being fixed.
    prose = report.get("fail_closed_path", "").lower()
    if "paragraph break" not in prose:
        failures.append(
            "the report's explanation never mentions a paragraph break, so a "
            f"reader still cannot tell this from an absent quote: {prose!r}"
        )
    entry_reasons = {e.get("reason") for e in report.get("changes_not_applied", [])}
    if redline_quote_apply.REASON_SPANS_PARAGRAPH_BREAK not in entry_reasons:
        failures.append(f"the per-change reason is missing from the report: {entry_reasons!r}")


TESTS = [
    test_the_fixture_really_joins_two_physical_paragraphs,
    test_a_spanning_quote_is_not_reported_as_not_found,
    test_a_genuinely_absent_quote_is_still_not_found,
    test_the_locator_itself_distinguishes_the_two_cases,
    test_the_regex_reparse_helper_is_gone,
    test_a_quote_inside_one_paragraph_still_applies,
    test_the_report_explains_the_difference_to_a_human,
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
    print("\nPASS: a quote spanning a paragraph break says so (issue #529/#564).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
