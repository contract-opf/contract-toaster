#!/usr/bin/env python3
"""
Slice test (TDD) for issue #80: "Extraction and normalization stage:
allowlisted OOXML parts, documented revision rule, fail-closed".

## Root problem this proves fixed

Before this slice, no code parsed a real `.docx`'s OOXML at all --
`scripts/normalize_input.py`'s per-paragraph decision layer existed (issue
#199) but had nothing feeding it real revision data, and
`backend/src/corpus.py` / `scripts/diff_standard_form.py` both documented
their `[{"heading": ..., "text": ...}, ...]` draft-input contract as
"issue #80's job" -- a stub seam, not real code. This test drives the real
`scripts/extraction_normalization_stage.py` module end-to-end over
hand-built OOXML fixtures (built with nothing but `zipfile` +
`xml.etree.ElementTree`, matching this repo's dependency-free
`scripts/redline_docx_writer.py` convention) and FAILS on a tree where that
module does not exist or does not implement the documented rule.

## What this test asserts (mirrors the issue's Required verification)

  1. A clean standard-form-shaped `.docx` -> an expected normalized
     PARAGRAPH LIST with headings (`[{"heading": ..., "text": ...}, ...]`),
     NOT a single lossy flattened `"heading: text"` string (that lossier
     shape is what `normalize_input.normalize()`'s own `clean_body` field
     produces -- this stage must NOT reduce to that for its own output).
     This slice CREATES that fixture as a committed SYNTHETIC placeholder
     standard-form `.docx` under `tests/fixtures/`.
  2. Each disallowed OOXML part (document properties, headers, footers,
     textbox/shape text, image alt text) carrying planted payload text ->
     the payload text is absent from the extracted output.
  3. Tracked-changes / comments / hidden-text fixtures normalize exactly
     per the ARCHITECTURE.md / issue #65 / issue #199 documented rule:
     accept path (single-author pending change -> accept-all) and reject
     paths (multi-author conflict, pending change inside a field code,
     malformed/corrupt revision record -> fail closed).
  4. An un-normalizable fixture fails closed to the issue #38 internal
     analysis report artifact with `status=MANUAL_REVIEW_REQUIRED`,
     `reason=unnormalizable_input`.
  5. The pipeline-stage entry point's input/output event carries S3
     pointers only -- no document substance -- per the issue #19
     POINTER-ONLY PAYLOAD RULE (infra/lambda/mock_review/handler.py).

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
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "extraction_normalization_80"

sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage as stage  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder for these fixtures (same
# zipfile-only convention as scripts/redline_docx_writer.py -- no
# python-docx here: several fixtures below need raw w:ins/w:del/w:vanish/
# w:fldSimple/w:commentReference/w:sdt/w:drawing markup python-docx's
# public API does not expose).
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
    additional named ZIP entries (e.g. `docProps/core.xml`) -- used to prove
    the extractor never opens them. Relationships/content-types for the
    extra parts are deliberately NOT wired up (the extractor doesn't consult
    them either); only their raw presence + planted payload text matters."""
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


def _hidden_p(text: str) -> str:
    return f'<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>{text}</w:t></w:r></w:p>'


def _pending_change_p(original: str, resulting: str, author: str = "counterparty") -> str:
    """A single-author, single-cluster pending tracked-change edit (the
    flagship counterparty-markup scenario) -- the entire paragraph's
    pre-edit text is deleted and its post-edit text inserted."""
    return (
        "<w:p>"
        f'<w:del w:id="1" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        f'<w:ins w:id="2" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:t>{resulting}</w:t></w:r></w:ins>"
        "</w:p>"
    )


def _two_author_conflict_p(original: str, resulting_a: str, resulting_b: str) -> str:
    """STILL-FAIL-CLOSED: two different authors' pending edits on the same
    paragraph, back-to-back with no intervening plain text."""
    return (
        "<w:p>"
        '<w:del w:id="1" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        '<w:ins w:id="2" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:t>{resulting_a}</w:t></w:r></w:ins>"
        '<w:del w:id="3" w:author="counterparty_second_reviewer" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        '<w:ins w:id="4" w:author="counterparty_second_reviewer" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:t>{resulting_b}</w:t></w:r></w:ins>"
        "</w:p>"
    )


def _malformed_deletion_p(original: str) -> str:
    """A tracked-change deletion with nothing inserted to replace it, plus
    an open reviewer comment on the same clause -- structurally
    irreconcilable (empty resulting_text): must fail closed."""
    return (
        "<w:p>"
        '<w:del w:id="1" w:author="unknown" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        '<w:r><w:commentReference w:id="0"/></w:r>'
        "</w:p>"
    )


def _field_code_conflict_p(original: str, resulting: str, author: str = "counterparty") -> str:
    """A pending tracked change living INSIDE a field's cached-result
    region (w:fldSimple) -- ambiguous independent of the accept-all rule."""
    return (
        "<w:p>"
        '<w:fldSimple w:instr=" REF GoverningLawJurisdiction ">'
        f'<w:del w:id="1" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        f'<w:ins w:id="2" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:t>{resulting}</w:t></w:r></w:ins>"
        "</w:fldSimple>"
        "</w:p>"
    )


def _field_result_p(prefix: str, field_result: str, suffix: str, instr: str) -> str:
    """A static (non-edited) field result, interleaved with visible text --
    must RESOLVE to its literal displayed text, not fail closed."""
    return (
        "<w:p>"
        f"<w:r><w:t>{prefix}</w:t></w:r>"
        f'<w:fldSimple w:instr="{instr}"><w:r><w:t>{field_result}</w:t></w:r></w:fldSimple>'
        f"<w:r><w:t>{suffix}</w:t></w:r>"
        "</w:p>"
    )


def _textbox_payload_p(payload: str) -> str:
    """Textbox/shape text (wps:txbx/w:txbxContent) nested inside a run's
    w:drawing -- must never reach extraction. Also carries an alt-text
    attribute payload on the drawing's docPr element."""
    return (
        "<w:p><w:r>"
        f'<w:drawing><wp:anchor><wp:docPr id="1" name="TextBox 1" descr="{payload}_ALTTEXT"/>'
        "<a:graphic><a:graphicData>"
        f"<wps:txbx><w:txbxContent><w:p><w:r><w:t>{payload}_TEXTBOX</w:t></w:r></w:p></w:txbxContent></wps:txbx>"
        "</a:graphicData></a:graphic></wp:anchor></w:drawing>"
        "</w:r></w:p>"
    )


def _sdt_payload_p(payload: str) -> str:
    """Content-control placeholder (w:sdt/w:sdtContent) -- excluded."""
    return f"<w:p><w:sdt><w:sdtContent><w:p><w:r><w:t>{payload}</w:t></w:r></w:p></w:sdtContent></w:sdt></w:p>"


def _table_p(rows: list[list[str]]) -> str:
    trs = []
    for row in rows:
        tcs = "".join(f"<w:tc>{_body_p(cell)}</w:tc>" for cell in row)
        trs.append(f"<w:tr>{tcs}</w:tr>")
    return f"<w:tbl>{''.join(trs)}</w:tbl>"


# ---------------------------------------------------------------------------
# G1: clean standard-form-shaped .docx -> structured paragraph list
# ---------------------------------------------------------------------------


def _generate_clean_standard_form_fixture() -> Path:
    """Creates the committed SYNTHETIC placeholder fixture (issue #80's
    Required-verification-mandated deliverable) if it does not already
    exist. NOT the real EIAA standard-form text -- short, generic,
    de-branded placeholder prose only, matching the
    scripts/generate_synthetic_standard_form.py (issue #200) convention."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / "clean-standard-form.SYNTHETIC.docx"
    if path.exists():
        return path

    body = "".join(
        [
            _heading_p("Limitation on Liability", level=1),
            _body_p(
                "Each party's aggregate liability under this Agreement "
                "shall not exceed $150,000."
            ),
            _heading_p("Governing Law", level=1),
            _body_p("This Agreement shall be governed by the laws of Delaware."),
            _heading_p("Notices", level=1),
            _table_p(
                [
                    ["Party", "Address"],
                    ["You", "your notice address on file"],
                ]
            ),
        ]
    )
    path.write_bytes(_build_docx_bytes(body))
    return path


def test_clean_standard_form_yields_structured_paragraph_list(failures: list[str]) -> None:
    fixture_path = _generate_clean_standard_form_fixture()
    docx_bytes = fixture_path.read_bytes()

    result = stage.extract_and_normalize(docx_bytes)

    if result.get("status") != "normalized":
        failures.append(f"[G1] Clean standard-form fixture did not normalize: {result}")
        return

    paragraphs = result.get("paragraphs")
    if not isinstance(paragraphs, list) or len(paragraphs) < 3:
        failures.append(
            f"[G1] Expected an expected normalized PARAGRAPH LIST (>=3 entries, "
            f"one per heading), got: {paragraphs!r}"
        )
        return

    if any(not isinstance(p, dict) or "heading" not in p or "text" not in p for p in paragraphs):
        failures.append(
            f"[G1] Every paragraph must be a {{'heading', 'text'}} dict -- "
            f"NOT a single lossy flattened 'heading: text' string. Got: {paragraphs!r}"
        )

    headings = [p.get("heading") for p in paragraphs if isinstance(p, dict)]
    if "Limitation on Liability" not in headings or "Governing Law" not in headings:
        failures.append(f"[G1b] Expected headings not found. Got headings: {headings!r}")

    liability = next((p for p in paragraphs if p.get("heading") == "Limitation on Liability"), None)
    if liability is None or "$150,000" not in liability.get("text", ""):
        failures.append(f"[G1c] 'Limitation on Liability' paragraph text wrong: {liability!r}")

    notices = next((p for p in paragraphs if p.get("heading") == "Notices"), None)
    if notices is None or "your notice address on file" not in notices.get("text", ""):
        failures.append(
            f"[G1d] Table cell text ('Notices' section) must be extracted alongside "
            f"body text (tables are 'Allowed' per the OOXML part allowlist). Got: {notices!r}"
        )

    # "NOT a lossy flattened heading: text string" -- explicitly assert the
    # output is not normalize_input.normalize()'s clean_body shape.
    if isinstance(result.get("paragraphs"), str):
        failures.append("[G1e] paragraphs must not be a single joined string.")


# ---------------------------------------------------------------------------
# G2: disallowed OOXML parts carrying planted payload text -> absent
# ---------------------------------------------------------------------------


def test_disallowed_parts_payload_never_reaches_output(failures: list[str]) -> None:
    body = "".join(
        [
            _heading_p("Preamble", level=1),
            _body_p("This is ordinary, allowed clause text."),
            _textbox_payload_p("PAYLOAD_MARKER"),
            _sdt_payload_p("PAYLOAD_MARKER_SDT"),
        ]
    )
    extra_parts = {
        "docProps/core.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>PAYLOAD_MARKER_CORE_PROPS</dc:title>"
            "<dc:creator>PAYLOAD_MARKER_CREATOR</dc:creator>"
            "</cp:coreProperties>"
        ),
        "docProps/app.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            "<Company>PAYLOAD_MARKER_APP_PROPS</Company></Properties>"
        ),
        "docProps/custom.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties">'
            "PAYLOAD_MARKER_CUSTOM_PROPS</Properties>"
        ),
        "word/header1.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:p><w:r><w:t>PAYLOAD_MARKER_HEADER</w:t></w:r></w:p></w:hdr>"
        ),
        "word/footer1.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:p><w:r><w:t>PAYLOAD_MARKER_FOOTER</w:t></w:r></w:p></w:ftr>"
        ),
    }
    docx_bytes = _build_docx_bytes(body, extra_parts=extra_parts)

    result = stage.extract_and_normalize(docx_bytes)

    if result.get("status") != "normalized":
        failures.append(f"[G2] Fixture with disallowed-part payloads unexpectedly failed to normalize: {result}")
        return

    serialized = repr(result)
    for marker in (
        "PAYLOAD_MARKER_CORE_PROPS",
        "PAYLOAD_MARKER_CREATOR",
        "PAYLOAD_MARKER_APP_PROPS",
        "PAYLOAD_MARKER_CUSTOM_PROPS",
        "PAYLOAD_MARKER_HEADER",
        "PAYLOAD_MARKER_FOOTER",
        "PAYLOAD_MARKER_TEXTBOX",
        "PAYLOAD_MARKER_ALTTEXT",
        "PAYLOAD_MARKER_SDT",
    ):
        if marker in serialized:
            failures.append(
                f"[G2] Disallowed-part payload marker {marker!r} leaked into extraction "
                f"output -- the OOXML part allowlist was violated."
            )

    preamble = next((p for p in result["paragraphs"] if p.get("heading") == "Preamble"), None)
    if preamble is None or "ordinary, allowed clause text" not in preamble.get("text", ""):
        failures.append(f"[G2b] Allowed body text was lost alongside the disallowed-part exclusion: {preamble!r}")


# ---------------------------------------------------------------------------
# G3: tracked-changes / comments / hidden-text -- accept + reject paths
# ---------------------------------------------------------------------------


def test_single_author_pending_change_accepts_all(failures: list[str]) -> None:
    """MUST-NORMALIZE (issue #199 rule, exercised via real OOXML extraction):
    a lone pending tracked change from one author is the proposal under
    review -- accept-all, disposition recorded, never fail closed."""
    body = _heading_p("Limitation on Liability") + _pending_change_p(
        "Each party's aggregate liability under this Agreement shall not exceed $150,000.",
        "Each party's liability under this Agreement shall be uncapped.",
    )
    result = stage.extract_and_normalize(_build_docx_bytes(body))

    if result.get("status") != "normalized":
        failures.append(f"[G3a] Single-author pending change must accept-all, not fail closed. Got: {result}")
        return
    para = result["paragraphs"][0]
    if "uncapped" not in para["text"]:
        failures.append(f"[G3a2] Accepted resulting text not folded into output: {para!r}")
    if "$150,000" in para["text"]:
        failures.append(f"[G3a3] Pre-edit text must not remain once accept-all applies: {para!r}")
    if "normalization_notes" not in result or not result["normalization_notes"]:
        failures.append(
            f"[G3a4] Disposition must be recorded in normalization_notes, never silent. Got: {result}"
        )


def test_multi_author_conflict_fails_closed(failures: list[str]) -> None:
    body = _heading_p("Limitation on Liability") + _two_author_conflict_p(
        "Each party's aggregate liability under this Agreement shall not exceed $150,000.",
        "Each party's liability under this Agreement shall be uncapped.",
        "Each party's aggregate liability under this Agreement shall not exceed $250,000.",
    )
    result = stage.extract_and_normalize(_build_docx_bytes(body))
    if result.get("status") != "unnormalizable_input":
        failures.append(f"[G3b] Two-author conflicting pending changes must fail closed. Got: {result}")
        return
    report = result["analysis_report"]
    if report.get("reason") != "unnormalizable_input" or report.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[G3b2] Fail-closed report has wrong reason/status: {report!r}")
    if "decision" in report:
        failures.append(
            f"[G3b3] The fail-closed report must never carry a 'decision' field "
            f"(system status, never a legal decision). Got: {report!r}"
        )


def test_pending_change_inside_field_code_fails_closed(failures: list[str]) -> None:
    body = _heading_p("Governing Law") + _field_code_conflict_p("Delaware", "New York")
    result = stage.extract_and_normalize(_build_docx_bytes(body))
    if result.get("status") != "unnormalizable_input":
        failures.append(f"[G3c] Pending change inside a field code must fail closed. Got: {result}")


def test_malformed_deletion_with_comment_fails_closed(failures: list[str]) -> None:
    body = _heading_p("Limitation on Liability") + _malformed_deletion_p(
        "Each party's aggregate liability under this Agreement shall not exceed $150,000."
    )
    result = stage.extract_and_normalize(_build_docx_bytes(body))
    if result.get("status") != "unnormalizable_input":
        failures.append(
            f"[G3d] A deletion with nothing inserted to replace it (malformed -- no "
            f"resulting_text) must fail closed even though it co-occurs with an open "
            f"comment. Got: {result}"
        )


def test_hidden_text_and_field_result_normalize_cleanly(failures: list[str]) -> None:
    """Hidden text is stripped; a STATIC field result resolves to its
    literal displayed text -- neither is fatal."""
    body = _heading_p("Governing Law") + _hidden_p(
        "HIDDEN: internal drafting note, do not disclose"
    ) + _field_result_p(
        "This Agreement shall be governed by the laws of ",
        "Delaware",
        ".",
        " REF GoverningLawJurisdiction ",
    )
    result = stage.extract_and_normalize(_build_docx_bytes(body))
    if result.get("status") != "normalized":
        failures.append(f"[G3e] Hidden text + static field result must normalize cleanly. Got: {result}")
        return
    para = result["paragraphs"][0]
    if "do not disclose" in para["text"] or "internal drafting note" in para["text"]:
        failures.append(f"[G3e2] Hidden text must never reach the clean body. Got: {para!r}")
    if "Delaware" not in para["text"]:
        failures.append(f"[G3e3] Field result must resolve to its literal displayed text. Got: {para!r}")


def test_sibling_body_paragraph_survives_accept_all_on_other_sibling(failures: list[str]) -> None:
    """Regression (issue #80 fix round 1 / #200): a heading with MULTIPLE
    body `<w:p>` siblings where only ONE sibling carries a lone pending
    tracked change. Accept-all must replace only that sibling's own text --
    the other, untouched sibling's clause text must survive in the
    operative output, never silently dropped. Before the fix, all siblings
    under a heading were merged into one logical paragraph before
    normalization, so accept-all overwrote the WHOLE merged text and
    dropped the plain sibling."""
    body = (
        _heading_p("Section 10")
        + _body_p("Subclause A: this text must survive.")
        + _pending_change_p(
            "Subclause B: liability capped at $150,000.",
            "Subclause B: liability uncapped.",
        )
    )
    result = stage.extract_and_normalize(_build_docx_bytes(body))

    if result.get("status") != "normalized":
        failures.append(
            f"[G3g] Heading with one clean sibling + one lone-pending-change "
            f"sibling must normalize (accept-all), not fail closed. Got: {result}"
        )
        return

    section = next((p for p in result["paragraphs"] if p.get("heading") == "Section 10"), None)
    if section is None:
        failures.append(f"[G3g2] Expected 'Section 10' paragraph missing. Got: {result['paragraphs']!r}")
        return

    text = section.get("text", "")
    if "Subclause A: this text must survive." not in text:
        failures.append(
            f"[G3g3] The untouched sibling clause ('Subclause A') must survive accept-all "
            f"on its sibling paragraph -- it must NOT be silently dropped. Got: {section!r}"
        )
    if "Subclause B: liability uncapped." not in text:
        failures.append(f"[G3g4] Accepted resulting text for 'Subclause B' not folded into output: {section!r}")
    if "$150,000" in text:
        failures.append(f"[G3g5] Pre-edit text for the accepted sibling must not remain: {section!r}")
    if "normalization_notes" not in result or not result["normalization_notes"]:
        failures.append(f"[G3g6] Sibling accept-all disposition must be recorded, never silent. Got: {result}")


def test_comment_never_gates_accept_all(failures: list[str]) -> None:
    """A comment co-located with an otherwise-clean single-author pending
    change must not, by itself, change the accept-all outcome."""
    body = (
        "<w:p>"
        '<w:del w:id="1" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:delText>Old term.</w:delText></w:r></w:del>"
        '<w:ins w:id="2" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:t>New term.</w:t></w:r></w:ins>"
        '<w:r><w:commentReference w:id="0"/></w:r>'
        "</w:p>"
    )
    result = stage.extract_and_normalize(_build_docx_bytes(_heading_p("Term") + body))
    if result.get("status") != "normalized":
        failures.append(f"[G3f] A comment must never gate normalization by itself. Got: {result}")


# ---------------------------------------------------------------------------
# G4: fail-closed path emits the issue #38 analysis-report artifact
# ---------------------------------------------------------------------------


def test_unnormalizable_document_emits_analysis_report_shape(failures: list[str]) -> None:
    body = _heading_p("Limitation on Liability") + _malformed_deletion_p(
        "Each party's aggregate liability under this Agreement shall not exceed $150,000."
    )
    result = stage.extract_and_normalize(_build_docx_bytes(body))
    if result.get("status") != "unnormalizable_input":
        failures.append(f"[G4] Expected fail-closed status. Got: {result}")
        return
    report = result["analysis_report"]
    required_keys = {
        "report_type",
        "reason",
        "fail_closed_path",
        "changes_not_applied",
        "normalization_notes",
        "status",
    }
    missing = required_keys - set(report.keys())
    if missing:
        failures.append(f"[G4b] analysis_report missing required keys {missing}: {report!r}")
    if report.get("report_type") != "analysis_report":
        failures.append(f"[G4c] report_type must be 'analysis_report'. Got: {report!r}")
    if report.get("changes_not_applied") != []:
        failures.append(
            f"[G4d] changes_not_applied must be [] for the un-normalizable-input path "
            f"(no model has run yet). Got: {report!r}"
        )
    if not report.get("normalization_notes"):
        failures.append(f"[G4e] normalization_notes must describe what could not be resolved: {report!r}")


def test_one_unnormalizable_paragraph_fails_whole_document(failures: list[str]) -> None:
    """One un-normalizable paragraph fails the WHOLE document closed, even
    when every other paragraph is clean -- a partially normalized document
    is not a safe input to diff or review."""
    body = (
        _heading_p("Governing Law")
        + _body_p("This Agreement shall be governed by the laws of Delaware.")
        + _heading_p("Limitation on Liability")
        + _malformed_deletion_p(
            "Each party's aggregate liability under this Agreement shall not exceed $150,000."
        )
    )
    result = stage.extract_and_normalize(_build_docx_bytes(body))
    if result.get("status") != "unnormalizable_input":
        failures.append(
            f"[G4f] A document with one un-normalizable paragraph among clean ones "
            f"must fail closed as a WHOLE, not partially normalize. Got: {result}"
        )


# ---------------------------------------------------------------------------
# G5: pointer-only pipeline-stage entry point
# ---------------------------------------------------------------------------


def test_run_stage_pointer_only_payload_success_path(failures: list[str]) -> None:
    clean_body = _heading_p("Governing Law") + _body_p(
        "This Agreement shall be governed by the laws of Delaware."
    )
    docx_bytes = _build_docx_bytes(clean_body)

    fake_s3: dict[str, Any] = {"uploads/u1/r1/in.docx": docx_bytes}
    stored: dict[str, dict[str, Any]] = {}

    def fetch_docx_bytes(key: str) -> bytes:
        return fake_s3[key]

    def store_json(key: str, obj: dict[str, Any]) -> None:
        stored[key] = obj

    event = {"review_id": "r1", "owner_sub": "u1", "upload_s3_key": "uploads/u1/r1/in.docx"}
    output = stage.run_stage(event, fetch_docx_bytes=fetch_docx_bytes, store_json=store_json)

    if output.get("status") != "EXTRACTED":
        failures.append(f"[G5a] Expected status=EXTRACTED. Got: {output}")
    if output.get("review_id") != "r1":
        failures.append(f"[G5b] review_id must round-trip. Got: {output}")
    normalized_key = output.get("normalized_s3_key")
    if not normalized_key or normalized_key not in stored:
        failures.append(f"[G5c] normalized_s3_key must point at something actually stored. Got: {output}")

    allowed_output_keys = {"review_id", "status", "normalized_s3_key"}
    if set(output.keys()) - allowed_output_keys:
        failures.append(
            f"[G5d] Pointer-only output carries unexpected keys "
            f"{set(output.keys()) - allowed_output_keys}: {output!r}"
        )
    for value in output.values():
        if isinstance(value, str) and ("Delaware" in value or "Agreement" in value):
            failures.append(f"[G5e] Document substance leaked into the pointer-only output payload: {output!r}")

    # The stored artifact itself may carry document text (it's the actual
    # S3 object) -- but the STATE PAYLOAD (the `output` dict returned to
    # Step Functions / the caller) must not, which is what G5d/G5e assert.
    if "Delaware" not in str(stored[normalized_key]):
        failures.append(f"[G5f] Stored normalized artifact should carry the actual extracted content: {stored}")


def test_run_stage_pointer_only_payload_fail_closed_path(failures: list[str]) -> None:
    body = _heading_p("Limitation on Liability") + _malformed_deletion_p(
        "Each party's aggregate liability under this Agreement shall not exceed $150,000."
    )
    docx_bytes = _build_docx_bytes(body)

    fake_s3 = {"uploads/u2/r2/in.docx": docx_bytes}
    stored: dict[str, dict[str, Any]] = {}

    event = {"review_id": "r2", "owner_sub": "u2", "upload_s3_key": "uploads/u2/r2/in.docx"}
    output = stage.run_stage(
        event,
        fetch_docx_bytes=lambda key: fake_s3[key],
        store_json=lambda key, obj: stored.__setitem__(key, obj),
    )

    if output.get("status") != "MANUAL_REVIEW_REQUIRED" or output.get("reason") != "unnormalizable_input":
        failures.append(f"[G5g] Expected fail-closed pointer-only output. Got: {output}")
    report_key = output.get("analysis_report_s3_key")
    if not report_key or report_key not in stored:
        failures.append(f"[G5h] analysis_report_s3_key must point at the stored report. Got: {output}")

    allowed_output_keys = {"review_id", "status", "reason", "analysis_report_s3_key"}
    if set(output.keys()) - allowed_output_keys:
        failures.append(
            f"[G5i] Pointer-only fail-closed output carries unexpected keys: {output!r}"
        )
    for value in output.values():
        if isinstance(value, str) and "$150,000" in value:
            failures.append(f"[G5j] Document substance leaked into fail-closed pointer-only output: {output!r}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_clean_standard_form_yields_structured_paragraph_list,
    test_disallowed_parts_payload_never_reaches_output,
    test_single_author_pending_change_accepts_all,
    test_multi_author_conflict_fails_closed,
    test_pending_change_inside_field_code_fails_closed,
    test_malformed_deletion_with_comment_fails_closed,
    test_hidden_text_and_field_result_normalize_cleanly,
    test_sibling_body_paragraph_survives_accept_all_on_other_sibling,
    test_comment_never_gates_accept_all,
    test_unnormalizable_document_emits_analysis_report_shape,
    test_one_unnormalizable_paragraph_fails_whole_document,
    test_run_stage_pointer_only_payload_success_path,
    test_run_stage_pointer_only_payload_fail_closed_path,
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
    print("PASS: all extraction/normalization stage (issue #80) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
