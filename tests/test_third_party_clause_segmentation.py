#!/usr/bin/env python3
"""
Slice test (TDD) for issue #248: "Third-party paper: segment an arbitrary
uploaded .docx into self-anchored clause records".

## Root problem this proves fixed

Once the router (#247) sends an upload down the `THIRD_PARTY_POSITIONS`
route, the document is the counterparty's OWN template -- it has no
relationship to your form's headings or anchor map, so none of the
first-party machinery (`diff_standard_form.py` heading-anchor matching, the
section-anchor map, `sec-_new`) can segment it. Before this slice, nothing
turned a normalized third-party `.docx` into an ordered list of clause
records anchored to the uploaded document itself.

This test drives `scripts/third_party_clause_segmentation.py` (which does
not exist on the pre-fix tree) end-to-end over a hand-built synthetic
counterparty-own-form `.docx` fixture (built with nothing but `zipfile` +
`xml.etree.ElementTree`, the same dependency-free convention as
`tests/test_extraction_normalization_stage_80.py` /
`scripts/redline_docx_writer.py`) and FAILS on a tree where that module
does not exist.

## What this test asserts (mirrors the issue's Required verification)

  1. The document segments into the expected ordered clause list, each
     clause carrying `clause_id`, `heading`, `text`, and `order`.
  2. `clause_id`s are content-addressed and stable across two runs
     (deterministic).
  3. Numbered/lettered/styled headings are used as boundaries -- a
     multi-paragraph clause stays one record and two distinct clauses are
     not merged.
  4. Planted payload in disallowed OOXML parts (properties/headers/footers/
     textboxes) is absent from every clause record.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "third_party_clause_segmentation_248"

sys.path.insert(0, str(SCRIPTS_DIR))

import third_party_clause_segmentation as segmentation  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder (same zipfile-only convention
# as tests/test_extraction_normalization_stage_80.py) -- no python-docx here:
# planting raw payload in disallowed parts needs markup python-docx's public
# API does not expose.
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
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
)


def _build_docx_bytes(body_paragraphs_xml: str, extra_parts: dict[str, str] | None = None) -> bytes:
    """Assembles a minimal but valid `.docx` ZIP. `extra_parts` plants
    additional named ZIP entries (e.g. `word/header1.xml`) -- used to prove
    the segmenter (via the extraction stage it delegates to) never opens
    them."""
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
        for name, content in (extra_parts or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


def _heading_p(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _bold_heading_p(text: str) -> str:
    """A whole-paragraph-bold single-line heading with NO Word Heading
    style -- the style-stripped fallback signal `clause_boundaries.py`
    (issue #277) detects."""
    return f'<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{text}</w:t></w:r></w:p>'


def _numbered_lead_in_p(text: str) -> str:
    """A manually-typed numeric lead-in heading with no Word Heading
    style -- the other style-stripped fallback signal `clause_boundaries.py`
    detects."""
    return _body_p(text)


def _textbox_payload_p(payload: str) -> str:
    """Textbox/shape text nested inside a run's w:drawing -- must never
    reach a clause record. Also carries an alt-text attribute payload."""
    return (
        "<w:p><w:r>"
        f'<w:drawing><wp:anchor><wp:docPr id="1" name="TextBox 1" descr="{payload}_ALTTEXT"/>'
        "<a:graphic><a:graphicData>"
        f"<wps:txbx><w:txbxContent><w:p><w:r><w:t>{payload}_TEXTBOX</w:t></w:r></w:p></w:txbxContent></wps:txbx>"
        "</a:graphicData></a:graphic></wp:anchor></w:drawing>"
        "</w:r></w:p>"
    )


_HEADER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:p><w:r><w:t>PAYLOAD_MARKER_HEADER</w:t></w:r></w:p></w:hdr>"
)
_FOOTER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:p><w:r><w:t>PAYLOAD_MARKER_FOOTER</w:t></w:r></w:p></w:ftr>"
)
_CORE_PROPS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<dc:title>PAYLOAD_MARKER_CORE_PROPS</dc:title>"
    "<dc:creator>PAYLOAD_MARKER_CREATOR</dc:creator>"
    "</cp:coreProperties>"
)


# ---------------------------------------------------------------------------
# Shared synthetic counterparty-own-form fixture
# ---------------------------------------------------------------------------


def _generate_counterparty_fixture() -> Path:
    """Creates the committed SYNTHETIC counterparty-own-form `.docx`
    fixture (this issue's Required-verification-mandated deliverable) if it
    does not already exist. NOT real legal-form content -- short, generic,
    de-branded placeholder prose only. Deliberately mixes a real Word
    Heading style with two style-STRIPPED fallback signals
    (numbered lead-in, whole-paragraph-bold) so the fixture exercises both
    tiers of `scripts/clause_boundaries.py`'s detector, plus a
    multi-paragraph clause body (two siblings under one heading) to prove
    they stay one record, plus planted payload in disallowed OOXML parts."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / "counterparty-own-form.SYNTHETIC.docx"
    if path.exists():
        return path

    body = "".join(
        [
            _heading_p("Confidentiality", level=1),
            _body_p("Each party shall keep the other's confidential information secret."),
            _body_p("This obligation survives termination of this Agreement for five years."),
            _numbered_lead_in_p("2. Indemnification"),
            _body_p("Counterparty shall indemnify you against third-party claims arising from breach."),
            _bold_heading_p("Assignment"),
            _body_p("Neither party may assign this Agreement without the other's written consent."),
            _textbox_payload_p("PAYLOAD_MARKER"),
        ]
    )
    extra_parts = {
        "docProps/core.xml": _CORE_PROPS_XML,
        "word/header1.xml": _HEADER_XML,
        "word/footer1.xml": _FOOTER_XML,
    }
    path.write_bytes(_build_docx_bytes(body, extra_parts=extra_parts))
    return path


# ---------------------------------------------------------------------------
# G1: ordered clause list with clause_id / heading / text / order
# ---------------------------------------------------------------------------


def test_segments_into_ordered_clause_records(failures: list[str]) -> None:
    docx_bytes = _generate_counterparty_fixture().read_bytes()

    result = segmentation.segment_document(docx_bytes, source_document_id="counterparty-doc-1")

    if result.get("status") != "segmented":
        failures.append(f"[G1] Expected status='segmented', got: {result}")
        return

    clauses = result.get("clauses")
    if not isinstance(clauses, list) or len(clauses) != 3:
        failures.append(f"[G1] Expected exactly 3 clause records, got: {clauses!r}")
        return

    for i, clause in enumerate(clauses):
        for key in ("clause_id", "heading", "text", "order"):
            if key not in clause:
                failures.append(f"[G1] clause[{i}] missing required key {key!r}: {clause!r}")

    orders = [c.get("order") for c in clauses]
    if orders != [0, 1, 2]:
        failures.append(f"[G1] Expected document order [0, 1, 2], got: {orders!r}")

    headings = [c.get("heading") for c in clauses]
    if headings != ["Confidentiality", "Indemnification", "Assignment"]:
        failures.append(
            f"[G1] Expected headings ['Confidentiality', 'Indemnification', 'Assignment'] "
            f"in document order, got: {headings!r}"
        )


# ---------------------------------------------------------------------------
# G2: content-addressed, deterministic clause_ids
# ---------------------------------------------------------------------------


def test_clause_ids_are_content_addressed_and_stable(failures: list[str]) -> None:
    docx_bytes = _generate_counterparty_fixture().read_bytes()

    result_a = segmentation.segment_document(docx_bytes, source_document_id="counterparty-doc-1")
    result_b = segmentation.segment_document(docx_bytes, source_document_id="counterparty-doc-1")

    if result_a != result_b:
        failures.append(
            f"[G2] Two runs over the identical input must yield byte-identical clause "
            f"records. Run A: {result_a!r} != Run B: {result_b!r}"
        )
        return

    clause_ids = [c["clause_id"] for c in result_a.get("clauses", [])]
    if len(clause_ids) != len(set(clause_ids)):
        failures.append(f"[G2] clause_ids must be unique within a document: {clause_ids!r}")

    if any(not cid.startswith("clause_") for cid in clause_ids):
        failures.append(f"[G2] clause_ids must follow the 'clause_<hex>' convention: {clause_ids!r}")

    # A different source_document_id must change the id (content-addressed
    # on more than just the raw clause text, so two different uploads whose
    # clauses happen to share identical text don't collide).
    result_c = segmentation.segment_document(docx_bytes, source_document_id="counterparty-doc-2")
    if [c["clause_id"] for c in result_c.get("clauses", [])] == clause_ids:
        failures.append(
            "[G2] clause_ids must depend on source_document_id (content-addressed"
            " on document identity + clause content, not clause content alone)."
        )


# ---------------------------------------------------------------------------
# G3: numbered/lettered/styled headings recognised as boundaries;
#     multi-paragraph clause stays one record, distinct clauses not merged
# ---------------------------------------------------------------------------


def test_heading_signals_are_boundaries_and_siblings_stay_one_clause(failures: list[str]) -> None:
    docx_bytes = _generate_counterparty_fixture().read_bytes()
    result = segmentation.segment_document(docx_bytes, source_document_id="counterparty-doc-1")
    clauses = result.get("clauses", [])

    confidentiality = next((c for c in clauses if c.get("heading") == "Confidentiality"), None)
    if confidentiality is None:
        failures.append(f"[G3] 'Confidentiality' clause (Heading-style boundary) not found: {clauses!r}")
    else:
        text = confidentiality.get("text", "")
        if "keep the other's confidential information secret" not in text:
            failures.append(f"[G3] 'Confidentiality' clause missing its first sibling paragraph: {text!r}")
        if "survives termination" not in text:
            failures.append(
                f"[G3] 'Confidentiality' clause missing its second sibling paragraph -- two physical "
                f"paragraphs under one heading must stay ONE clause record, not split: {text!r}"
            )

    indemnification = next((c for c in clauses if c.get("heading") == "Indemnification"), None)
    if indemnification is None:
        failures.append(
            f"[G3] 'Indemnification' clause (style-stripped numbered-lead-in boundary) not found: {clauses!r}"
        )
    elif "shall indemnify" not in indemnification.get("text", ""):
        failures.append(f"[G3] 'Indemnification' clause text wrong: {indemnification!r}")

    assignment = next((c for c in clauses if c.get("heading") == "Assignment"), None)
    if assignment is None:
        failures.append(f"[G3] 'Assignment' clause (style-stripped bold-heading boundary) not found: {clauses!r}")
    elif "without the other's written consent" not in assignment.get("text", ""):
        failures.append(f"[G3] 'Assignment' clause text wrong: {assignment!r}")

    # Two distinct clauses must never merge into one record.
    if confidentiality is not None and indemnification is not None:
        if confidentiality.get("clause_id") == indemnification.get("clause_id"):
            failures.append("[G3] Distinct clauses must not share a clause_id.")


# ---------------------------------------------------------------------------
# G4: disallowed OOXML parts payload never reaches a clause record
# ---------------------------------------------------------------------------


def test_disallowed_parts_payload_absent_from_clause_records(failures: list[str]) -> None:
    docx_bytes = _generate_counterparty_fixture().read_bytes()
    result = segmentation.segment_document(docx_bytes, source_document_id="counterparty-doc-1")

    serialized = repr(result)
    for marker in (
        "PAYLOAD_MARKER_CORE_PROPS",
        "PAYLOAD_MARKER_CREATOR",
        "PAYLOAD_MARKER_HEADER",
        "PAYLOAD_MARKER_FOOTER",
        "PAYLOAD_MARKER_ALTTEXT",
        "PAYLOAD_MARKER_TEXTBOX",
    ):
        if marker in serialized:
            failures.append(f"[G4] Disallowed-part payload {marker!r} leaked into clause records: {result!r}")


TESTS = [
    test_segments_into_ordered_clause_records,
    test_clause_ids_are_content_addressed_and_stable,
    test_heading_signals_are_boundaries_and_siblings_stay_one_clause,
    test_disallowed_parts_payload_absent_from_clause_records,
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
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: all third-party clause segmentation (issue #248) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
