#!/usr/bin/env python3
"""
Gate for issue #100: a control-character refusal must carry a structured
sub-reason, not just the shared `reason="unnormalizable_input"` token every
other fail-closed normalization branch also uses.

## The bug this closes

`normalize_paragraphs` (scripts/extraction_normalization_stage.py) folds
EVERY un-normalizable disposition into the single token
`reason="unnormalizable_input"` (scripts/normalize_input.py's
`build_unnormalizable_report`). `frontend/src/ReviewSubmission.tsx` maps
that one token to copy written for a malformed TRACKED CHANGE ("a tracked
change the tool could not safely read ... see the paragraph named below").
The control-character screen (issue #632) is also `unnormalizable_input`,
but there is no tracked change and no paragraph is named -- ordinary
invisible Unicode (ZWNJ, LRM/RLM, ZWSP from a browser paste) trips it just
as often as anything hostile does, and the reader is told to go find and
resolve a tracked change that does not exist.

## What this file asserts

  1. `normalize_input.build_unnormalizable_report` carries
     `reason_detail == "suspicious_control_characters"` when the
     `normalize_result` it is handed came from the control-character
     screen, and carries NO `reason_detail` key at all for an ordinary
     malformed-tracked-change refusal -- `reason` itself is unchanged
     either way.
  2. `normalize_input.normalize()` (the whole-document convenience
     function) propagates that same `reason_detail` onto its own
     fail-closed result.
  3. PRODUCTION PATH: `scripts/extraction_normalization_stage
     .extract_and_normalize`, the function `scripts/review_spine.py`
     actually calls, carries `reason_detail` through to the
     `analysis_report` it returns for a real `.docx` built from raw OOXML
     -- not just through the convenience wrapper tests reach for. Through
     BOTH arms of `normalize_paragraphs`: the physical-paragraph one, and
     the clause-heading one (issue #98's boundary `<w:p>` check), which
     needs its own fixture because a heading group with no recognised
     number never enters it.
  4. `tools/document_spine_smoke.py`'s existing string-prefix classifier
     (`classify_unnormalizable_reason`) still agrees with the new
     structured field -- the two must not name different things for the
     same refusal.

Every fixture here is synthetic: fabricated clause text, a `.docx` built
in-process from literal XML (same approach as
`tests/test_control_character_screen.py`, which this file is a companion
to -- that file gates the screen itself; this one gates the sub-reason
issue #100 adds on top of it).

Run standalone: `python3 tests/test_reason_detail_100.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
TOOLS_DIR = REPO_ROOT / "tools"

for _dir in (SCRIPTS_DIR, TOOLS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import document_spine_smoke as dss  # noqa: E402
import extraction_normalization_stage  # noqa: E402
import normalize_input  # noqa: E402

EXPECTED_REASON_DETAIL = "suspicious_control_characters"

# Written as an escape, not pasted in literally, ONLY for the clause-heading
# fixture below: an invisible character inside a short heading string is the
# one place a copy/paste or a well-meaning reformat can silently drop it and
# turn that test into a tautology.
ZERO_WIDTH_SPACE = "\u200b"


def _document(text: str, *, heading: str = "Governing Law") -> dict[str, Any]:
    return {"paragraphs": [{"heading": heading, "text": text, "revisions": []}]}


def _build_docx(paragraph_texts: list[str]) -> bytes:
    """Build a minimal, valid `.docx` in memory from literal XML -- same
    approach as `tests/test_control_character_screen.py::_build_docx`, so
    fixture #3 below drives the REAL production extraction path
    (`extraction_normalization_stage.extract_and_normalize`), not a
    hand-built `raw_paragraphs` shape nothing else produces."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraph_texts)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. build_unnormalizable_report: present for the screen, absent otherwise
# ---------------------------------------------------------------------------


def test_report_carries_reason_detail_for_the_control_character_screen(
    failures: list,
) -> None:
    result = normalize_input.normalize(
        _document("The Supplier shall in​demnify the Customer for all claims.")
    )
    report = normalize_input.build_unnormalizable_report(result)

    if report.get("reason") != "unnormalizable_input":
        failures.append(
            f"reason changed: expected 'unnormalizable_input', got {report.get('reason')!r}"
        )
    if report.get("reason_detail") != EXPECTED_REASON_DETAIL:
        failures.append(
            f"expected reason_detail={EXPECTED_REASON_DETAIL!r}, "
            f"got {report.get('reason_detail')!r}"
        )


def test_report_carries_no_reason_detail_for_an_ordinary_tracked_change_refusal(
    failures: list,
) -> None:
    """The malformed-tracked-change refusal this token used to be confused
    with must be UNCHANGED: no reason_detail key at all, same `reason` as
    always."""
    document = {
        "paragraphs": [
            {
                "heading": "Indemnification",
                "text": "placeholder",
                "revisions": [{"type": "tracked_change", "status": "unresolved"}],
            }
        ]
    }
    result = normalize_input.normalize(document)
    report = normalize_input.build_unnormalizable_report(result)

    if report.get("reason") != "unnormalizable_input":
        failures.append(
            f"reason changed: expected 'unnormalizable_input', got {report.get('reason')!r}"
        )
    if "reason_detail" in report:
        failures.append(
            f"an ordinary malformed-revision refusal must carry NO reason_detail "
            f"key, got {report.get('reason_detail')!r}"
        )
    if "tracked change" not in (result.get("normalization_notes") or ""):
        failures.append(
            "fixture stopped producing a tracked-change note -- test no longer "
            "exercises the branch it claims to"
        )


# ---------------------------------------------------------------------------
# 2. normalize() propagates it onto its own fail-closed result
# ---------------------------------------------------------------------------


def test_normalize_propagates_reason_detail(failures: list) -> None:
    result = normalize_input.normalize(_document("clean‏marker text"))
    if result.get("normalizable") is not False:
        failures.append("fixture unexpectedly normalized")
    if result.get("reason_detail") != EXPECTED_REASON_DETAIL:
        failures.append(
            f"normalize() result: expected reason_detail={EXPECTED_REASON_DETAIL!r}, "
            f"got {result.get('reason_detail')!r}"
        )


# ---------------------------------------------------------------------------
# 3. PRODUCTION PATH: extraction_normalization_stage.extract_and_normalize
# ---------------------------------------------------------------------------


def test_extract_and_normalize_carries_reason_detail_through(failures: list) -> None:
    hostile_docx = _build_docx(
        [
            "Governing Law",
            "This Agreement is governed by the laws of the State of Franklin.",
            "The Supplier shall in​demnify the Customer for all claims.",
        ]
    )
    result = extraction_normalization_stage.extract_and_normalize(hostile_docx)

    if result.get("status") != "unnormalizable_input":
        failures.append(
            f"expected status='unnormalizable_input', got {result.get('status')!r}"
        )
        return

    report = result.get("analysis_report") or {}
    if report.get("reason_detail") != EXPECTED_REASON_DETAIL:
        failures.append(
            f"extract_and_normalize(): expected reason_detail="
            f"{EXPECTED_REASON_DETAIL!r} on analysis_report, got "
            f"{report.get('reason_detail')!r}"
        )


def test_extract_and_normalize_carries_reason_detail_from_a_clause_heading(
    failures: list,
) -> None:
    """The HEADING arm of `normalize_paragraphs`, on its own.

    `normalize_paragraphs` runs `_normalize_paragraph` twice per clause --
    once for the boundary `<w:p>` itself (issue #98) and once per physical
    paragraph -- so `reason_detail` has to be captured in BOTH arms. The
    fixture above only ever reaches the physical arm: `_build_docx` of
    plain sentences gives `extract_document_paragraphs` nothing it
    recognises as a numbered heading, so the whole document lands in one
    `<untitled>` group with `heading_p_index=None` and the heading arm is
    skipped entirely.

    This fixture isolates the heading arm instead. `1. Governing<ZWSP> Law`
    IS recognised as a numbered heading (`heading_p_index=0`), and it is
    followed immediately by the NEXT heading rather than by body text, so
    its group carries ZERO physical paragraphs -- the heading arm is then
    the only `_normalize_paragraph` call that can fail, and therefore the
    only thing that can set the document's `reason_detail`. Drop the
    heading-arm capture and this assertion goes to `None` while every other
    test in this file still passes: a heading-only control-character
    refusal would silently revert to #530's tracked-change copy, which is
    the exact #100 defect.

    The two structural guards below are deliberate: if a future change to
    heading recognition or clause grouping gives this group a physical
    paragraph (or no heading `<w:p>`), the test would quietly start passing
    through the other arm and stop gating anything. Fail loudly instead."""
    hostile_docx = _build_docx(
        [
            f"1. Governing{ZERO_WIDTH_SPACE} Law",
            "2. Indemnification",
            "The Supplier shall indemnify the Customer for all claims.",
        ]
    )

    groups = extraction_normalization_stage.extract_document_paragraphs(hostile_docx)
    first = groups[0] if groups else {}
    if first.get("heading_p_index") is None:
        failures.append(
            "fixture no longer produces a recognised clause heading "
            f"(heading_p_index={first.get('heading_p_index')!r}, "
            f"heading={first.get('heading')!r}) -- the heading arm is not "
            "being exercised, so this test gates nothing"
        )
        return
    if first.get("physical_paragraphs"):
        failures.append(
            "fixture's heading group gained "
            f"{len(first['physical_paragraphs'])} physical paragraph(s) -- the "
            "physical arm could now set reason_detail on its own, so this test "
            "no longer isolates the heading arm"
        )
        return

    result = extraction_normalization_stage.extract_and_normalize(hostile_docx)

    if result.get("status") != "unnormalizable_input":
        failures.append(
            f"expected status='unnormalizable_input', got {result.get('status')!r}"
        )
        return

    report = result.get("analysis_report") or {}
    if report.get("reason") != "unnormalizable_input":
        failures.append(
            f"reason changed: expected 'unnormalizable_input', got {report.get('reason')!r}"
        )
    if report.get("reason_detail") != EXPECTED_REASON_DETAIL:
        failures.append(
            f"heading-scope refusal: expected reason_detail="
            f"{EXPECTED_REASON_DETAIL!r} on analysis_report, got "
            f"{report.get('reason_detail')!r}"
        )

    notes = report.get("normalization_notes") or ""
    heading_scope = f"{normalize_input.CONTROL_CHARACTER_NOTE_PREFIX} heading:"
    text_scope = f"{normalize_input.CONTROL_CHARACTER_NOTE_PREFIX} text:"
    if heading_scope not in notes:
        failures.append(
            f"expected a heading-scope control-character note ({heading_scope!r}) "
            f"in normalization_notes, got {notes!r}"
        )
    if text_scope in notes:
        failures.append(
            "a body-text-scope note appeared too -- the refusal is no longer "
            f"heading-only, so the heading arm is not what is being gated: {notes!r}"
        )


def test_extract_and_normalize_carries_no_reason_detail_for_a_clean_refusal(
    failures: list,
) -> None:
    """A document that fails closed for a reason OTHER than the
    control-character screen (a genuinely malformed tracked-change record,
    via a paragraph with pending revisions -- not reachable through raw
    OOXML text alone) must not be mislabeled. Driven through
    `normalize_paragraphs` directly with a hand-built `raw_paragraphs`
    record, the exact shape `extract_document_paragraphs` produces for a
    physical paragraph (`heading`/`physical_paragraphs[].text`/`.revisions`
    -- see that function's own docstring)."""
    raw_paragraphs = [
        {
            "heading": "Indemnification",
            "heading_p_index": None,
            "heading_source_text": "",
            "heading_revisions": [],
            "physical_paragraphs": [
                {
                    "text": "placeholder",
                    "revisions": [{"type": "tracked_change", "status": "unresolved"}],
                    "p_index": 0,
                }
            ],
        }
    ]
    result = extraction_normalization_stage.normalize_paragraphs(raw_paragraphs)
    if result.get("status") != "unnormalizable_input":
        failures.append(
            f"expected status='unnormalizable_input', got {result.get('status')!r}"
        )
        return
    report = result.get("analysis_report") or {}
    if "reason_detail" in report:
        failures.append(
            f"a malformed-tracked-change refusal must carry no reason_detail, "
            f"got {report.get('reason_detail')!r}"
        )


# ---------------------------------------------------------------------------
# 4. The existing string-prefix classifier still agrees with the new field
# ---------------------------------------------------------------------------


def test_smoke_tool_classifier_agrees_with_the_structured_field(failures: list) -> None:
    result = normalize_input.normalize(_document("in‌visible joiner text"))
    report = normalize_input.build_unnormalizable_report(result)
    classified = dss.classify_unnormalizable_reason(report)
    if classified != report.get("reason_detail"):
        failures.append(
            f"classify_unnormalizable_reason() returned {classified!r}, which "
            f"disagrees with the structured reason_detail {report.get('reason_detail')!r}"
        )


TESTS = [
    test_report_carries_reason_detail_for_the_control_character_screen,
    test_report_carries_no_reason_detail_for_an_ordinary_tracked_change_refusal,
    test_normalize_propagates_reason_detail,
    test_extract_and_normalize_carries_reason_detail_through,
    test_extract_and_normalize_carries_reason_detail_from_a_clause_heading,
    test_extract_and_normalize_carries_no_reason_detail_for_a_clean_refusal,
    test_smoke_tool_classifier_agrees_with_the_structured_field,
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
    print(
        "\nPASS: a control-character refusal carries a structured reason_detail "
        "(issue #100)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
