#!/usr/bin/env python3
"""
Gate for issue #632: extracted text is screened for zero-width and
bidirectional control characters before any model call.

## Why this file exists

An uploaded `.docx` is counterparty-controlled text, and Unicode gives a
counterparty two ways to make the text the MODEL reads differ from the text
a HUMAN reviewer sees: zero-width characters (invisible when rendered, fully
present in the model's input) and bidirectional overrides (rendered order
differs from logical order). Either is a spoofing / prompt-injection surface
sitting in the one place this pipeline has no other control over -- the
model's own input.

`scripts/normalize_input.py` screens for both at the single choke point all
extracted text passes through (`_normalize_paragraph`, called once per
physical paragraph by `scripts/extraction_normalization_stage.py` and by
`normalize()`), and FAILS CLOSED on a hit -- `MANUAL_REVIEW_REQUIRED` /
`unnormalizable_input` -- rather than stripping or repairing. See that
module's docstring, "Control-character screen", for the reasoning.

## What this file asserts

  1. Every screened character class fails a document closed, and the
     resulting note classifies as the symbolic reason code
     `suspicious_control_characters`.
  2. The explicitly ALLOWED characters -- tab, newline, U+00A0 non-breaking
     space (common in real contracts), and a LEADING U+FEFF byte-order mark
     -- are never flagged, and a clean document is untouched.
  3. The screen runs on the OPERATIVE text an accept-all disposition
     produces, not just on the raw paragraph text -- including the field-code
     path, whose disposition note quotes the accepted text back.
  4. `tools/document_spine_smoke.py` reports the new code for a real `.docx`
     driven through `scan_document()`.
  5. PRIVACY: no document text -- not the offending run, not its neighbours,
     not the paragraph heading -- appears in any note, report, or smoke-tool
     result. Asserted with a fabricated sentinel party name unique to this
     file, mirroring `tests/test_document_shapes.py`'s sentinel check.

Every fixture here is synthetic: fabricated party names, fabricated clause
text, documents built in-process from literal XML.
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
import normalize_input  # noqa: E402

EXPECTED_REASON = "suspicious_control_characters"

# One representative per screened class, plus both ends of every range, so a
# range typo (off-by-one at either boundary) is caught rather than papered
# over by a single mid-range probe.
SCREENED_CODEPOINTS: list[tuple[int, str]] = [
    (0x200B, "zero-width space"),
    (0x200C, "zero-width non-joiner"),
    (0x200D, "zero-width joiner"),
    (0x200E, "left-to-right mark"),
    (0x200F, "right-to-left mark"),
    (0x202A, "left-to-right embedding"),
    (0x202B, "right-to-left embedding"),
    (0x202C, "pop directional formatting"),
    (0x202D, "left-to-right override"),
    (0x202E, "right-to-left override"),
    (0x2060, "word joiner"),
    (0x2061, "function application"),
    (0x2062, "invisible times"),
    (0x2063, "invisible separator"),
    (0x2064, "invisible plus"),
    (0x2066, "left-to-right isolate"),
    (0x2067, "right-to-left isolate"),
    (0x2068, "first strong isolate"),
    (0x2069, "pop directional isolate"),
]

# Explicitly allowed, per the module docstring's accept list.
ALLOWED_CHARACTERS: list[tuple[str, str]] = [
    ("\t", "tab"),
    ("\n", "newline"),
    (" ", "non-breaking space"),
    ("–", "en dash"),
    ("“", "left curly double quote"),
]

CLEAN_CLAUSE = "Each party shall bear its own costs of the arbitration."


def _document(text: str, *, heading: str = "Governing Law") -> dict[str, Any]:
    """A minimal one-paragraph document in the shape `normalize()` accepts."""
    return {"paragraphs": [{"heading": heading, "text": text, "revisions": []}]}


def _classify(normalize_result: dict) -> str:
    """Run a `normalize()` refusal through the exact artifact + classifier
    path the smoke tool uses, so this file tests the shipped chain rather
    than a hand-built note."""
    report = normalize_input.build_unnormalizable_report(normalize_result)
    return dss.classify_unnormalizable_reason(report)


# ---------------------------------------------------------------------------
# 1. Every screened class fails closed with the right reason token
# ---------------------------------------------------------------------------


def test_every_screened_class_fails_closed(failures: list) -> None:
    for codepoint, name in SCREENED_CODEPOINTS:
        char = chr(codepoint)
        text = f"The Supplier shall in{char}demnify the Customer for all claims."
        result = normalize_input.normalize(_document(text))

        if result.get("normalizable") is not False:
            failures.append(
                f"U+{codepoint:04X} ({name}) did not fail the document closed: "
                f"normalizable={result.get('normalizable')!r}"
            )
            continue

        report = normalize_input.build_unnormalizable_report(result)
        if report.get("reason") != "unnormalizable_input":
            failures.append(
                f"U+{codepoint:04X} ({name}): expected reason=unnormalizable_input, "
                f"got {report.get('reason')!r}"
            )
        if report.get("status") != "MANUAL_REVIEW_REQUIRED":
            failures.append(
                f"U+{codepoint:04X} ({name}): expected "
                f"status=MANUAL_REVIEW_REQUIRED, got {report.get('status')!r}"
            )

        reason = dss.classify_unnormalizable_reason(report)
        if reason != EXPECTED_REASON:
            failures.append(
                f"U+{codepoint:04X} ({name}): expected reason code "
                f"{EXPECTED_REASON!r}, got {reason!r}"
            )


def test_non_leading_bom_fails_closed_but_a_leading_one_does_not(failures: list) -> None:
    """U+FEFF is the one screened codepoint with a legitimate position: a
    LEADING byte-order mark. Anywhere else it is a zero-width no-break
    space and is screened like any other invisible character."""
    embedded = normalize_input.normalize(
        _document("The term of this Agreement is thirty-six (36﻿) months.")
    )
    if embedded.get("normalizable") is not False:
        failures.append("a non-leading U+FEFF did not fail the document closed")
    else:
        reason = _classify(embedded)
        if reason != EXPECTED_REASON:
            failures.append(
                f"non-leading U+FEFF: expected {EXPECTED_REASON!r}, got {reason!r}"
            )

    leading = normalize_input.normalize(_document("﻿" + CLEAN_CLAUSE))
    if not leading.get("normalizable"):
        failures.append(
            "a LEADING U+FEFF byte-order mark was screened, but it is an "
            f"ordinary BOM: {leading.get('normalization_notes')!r}"
        )

    # The helper is the unit under all of the above -- assert its offsets
    # directly so the BOM rule is pinned at the function boundary too.
    if normalize_input.find_suspicious_control_characters("﻿abc") != []:
        failures.append("find_suspicious_control_characters() flagged a leading BOM")
    if normalize_input.find_suspicious_control_characters("ab﻿c") != [2]:
        failures.append(
            "find_suspicious_control_characters() did not report a non-leading "
            "BOM at its offset"
        )


# ---------------------------------------------------------------------------
# 2. Clean, NBSP-bearing and whitespace-bearing documents are unaffected
# ---------------------------------------------------------------------------


def test_a_clean_document_is_unaffected(failures: list) -> None:
    result = normalize_input.normalize(_document(CLEAN_CLAUSE))
    if not result.get("normalizable"):
        failures.append(
            f"a clean document failed closed: {result.get('normalization_notes')!r}"
        )
    elif result.get("clean_body") != f"Governing Law: {CLEAN_CLAUSE}":
        failures.append(
            f"a clean document's body was altered: {result.get('clean_body')!r}"
        )


def test_allowed_characters_are_never_flagged(failures: list) -> None:
    for char, name in ALLOWED_CHARACTERS:
        text = f"Fees are due within thirty{char}(30) days of invoice."
        if normalize_input.find_suspicious_control_characters(text):
            failures.append(f"{name} ({char!r}) was flagged by the screen")
        result = normalize_input.normalize(_document(text))
        if not result.get("normalizable"):
            failures.append(
                f"a document containing {name} failed closed: "
                f"{result.get('normalization_notes')!r}"
            )
        elif char not in (result.get("clean_body") or ""):
            failures.append(
                f"{name} was silently removed from the clean body -- the screen "
                f"must never repair text"
            )


# ---------------------------------------------------------------------------
# 3. The screen runs on the OPERATIVE text, not just the raw paragraph text
# ---------------------------------------------------------------------------


def test_accepted_pending_revision_text_is_screened(failures: list) -> None:
    """A pending tracked change's `resulting_text` BECOMES the operative
    text; a hostile character there must fail closed exactly like one in the
    plain paragraph text."""
    document = {
        "paragraphs": [
            {
                "heading": "Limitation of Liability",
                "text": "Liability is capped at the fees paid.",
                "revisions": [
                    {
                        "type": "tracked_change",
                        "status": "unresolved",
                        "author": "Fabricated Counterparty Inc.",
                        "resulting_text": "Liability is un‮limited.",
                    }
                ],
            }
        ]
    }
    result = normalize_input.normalize(document)
    if result.get("normalizable") is not False:
        failures.append(
            "a pending tracked change whose accepted text carries a bidi "
            "override was accepted into the operative draft"
        )
        return
    reason = _classify(result)
    if reason != EXPECTED_REASON:
        failures.append(f"accepted pending text: expected {EXPECTED_REASON!r}, got {reason!r}")


def test_field_code_disposition_note_never_echoes_screened_text(failures: list) -> None:
    """The field-code accept-all note quotes the resolved text back verbatim
    (`normalize_input._normalize_paragraph`). The screen therefore has to run
    BEFORE that note is built, or the invisible characters ride out to the
    attorney inside the disposition note."""
    hostile_field_text = "Exhibit​A-2"
    document = {
        "paragraphs": [
            {
                "heading": "Cross-Reference",
                "text": "See Exhibit A.",
                "revisions": [
                    {
                        "type": "tracked_change",
                        "status": "unresolved",
                        "author": "Fabricated Counterparty Inc.",
                        "inside_field_code": True,
                        "resulting_text": hostile_field_text,
                    }
                ],
            }
        ]
    }
    result = normalize_input.normalize(document)
    notes = result.get("normalization_notes", "") or ""
    if result.get("normalizable") is not False:
        failures.append("a field-code accept-all with a zero-width space was not screened")
    if hostile_field_text in notes:
        failures.append("the disposition note echoed the screened field text back verbatim")
    if _classify(result) != EXPECTED_REASON:
        failures.append(
            f"field-code path: expected {EXPECTED_REASON!r}, got {_classify(result)!r}"
        )


# ---------------------------------------------------------------------------
# 4. The smoke tool reports the new code for a real .docx
# ---------------------------------------------------------------------------


def _build_docx(paragraph_texts: list[str]) -> bytes:
    """Build a minimal, valid `.docx` in memory from literal XML -- same
    approach as `tests/test_document_shapes.py`'s sentinel document, so no
    binary blob has to be committed."""
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


def test_the_smoke_tool_reports_the_new_reason_code(failures: list) -> None:
    hostile_docx = _build_docx(
        [
            "Governing Law",
            "This Agreement is governed by the laws of the State of Franklin.",
            "The Supplier shall in​demnify the Customer for all claims.",
        ]
    )
    result = dss.scan_document(hostile_docx)
    if result.get("outcome") != "refused":
        failures.append(
            f"scan_document() did not refuse a document carrying a zero-width "
            f"space: {result.get('outcome')!r}"
        )
    elif result.get("reason") != EXPECTED_REASON:
        failures.append(
            f"scan_document() reported reason {result.get('reason')!r}, expected "
            f"{EXPECTED_REASON!r}"
        )

    clean_docx = _build_docx(
        [
            "Governing Law",
            "This Agreement is governed by the laws of the State of Franklin.",
            "Each party shall bear its own costs of the arbitration.",
        ]
    )
    clean_result = dss.scan_document(clean_docx)
    if clean_result.get("outcome") != "normalized":
        failures.append(
            f"scan_document() refused a clean, NBSP-bearing document: {clean_result!r}"
        )


# ---------------------------------------------------------------------------
# 5. Privacy: no document text anywhere in the refusal path
# ---------------------------------------------------------------------------


def test_no_document_text_is_echoed_anywhere(failures: list) -> None:
    """The fabricated sentinel appears in BOTH the heading and the clause
    text of a screened document, and must appear in NOTHING the refusal path
    produces: not the normalization notes, not the analysis report, not the
    smoke tool's per-document result."""
    sentinel = "Quillfeather Marrowbone Institute"  # fabricated; unique to this file

    result = normalize_input.normalize(
        _document(
            f"{sentinel} shall in​demnify the Customer for all claims.",
            heading=f"{sentinel} Indemnity",
        )
    )
    if result.get("normalizable") is not False:
        failures.append("the sentinel document was not screened at all")
    notes = result.get("normalization_notes", "") or ""
    if sentinel in notes:
        failures.append(f"normalization_notes echoed the sentinel: {notes!r}")
    if "in​demnify" in notes or "​" in notes:
        failures.append("normalization_notes echoed the offending text or character")

    report = normalize_input.build_unnormalizable_report(result)
    if sentinel in repr(report):
        failures.append(f"the analysis_report echoed the sentinel: {report!r}")
    reason = dss.classify_unnormalizable_reason(report)
    if sentinel in reason:
        failures.append(f"classify_unnormalizable_reason() echoed the sentinel: {reason!r}")

    scan = dss.scan_document(
        _build_docx(
            [
                f"{sentinel} Indemnity",
                f"{sentinel} shall in​demnify the Customer for all claims.",
            ]
        )
    )
    blob = repr(scan)
    if sentinel in blob:
        failures.append(f"scan_document() echoed the sentinel: {blob!r}")
    if "​" in blob:
        failures.append(f"scan_document() echoed the offending character: {blob!r}")


TESTS = [
    test_every_screened_class_fails_closed,
    test_non_leading_bom_fails_closed_but_a_leading_one_does_not,
    test_a_clean_document_is_unaffected,
    test_allowed_characters_are_never_flagged,
    test_accepted_pending_revision_text_is_screened,
    test_field_code_disposition_note_never_echoes_screened_text,
    test_the_smoke_tool_reports_the_new_reason_code,
    test_no_document_text_is_echoed_anywhere,
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
    print("\nPASS: the control-character screen fails closed and leaks nothing (issue #632).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
