#!/usr/bin/env python3
"""
Slice test (TDD) for issue #80: "Extraction and normalization stage:
allowlisted OOXML parts, documented revision rule, fail-closed".

## Root problem this proves fixed

Before this slice, no code parsed a real `.docx`'s OOXML at all --
`scripts/normalize_input.py`'s per-paragraph decision layer existed (issue
#199) but had nothing feeding it real revision data, and
`backend/src/corpus.py` and the retired standard-form diff both documented
their `[{"heading": ..., "text": ...}, ...]` draft-input contract as
"issue #80's job" -- a stub seam, not real code. This test drives the real
`scripts/extraction_normalization_stage.py` module end-to-end over
hand-built OOXML fixtures (built with nothing but `zipfile` +
`xml.etree.ElementTree`, matching this repo's dependency-free
`scripts/docx_parts.py` convention) and FAILS on a tree where that
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
     per the ARCHITECTURE.md / issue #65 / issue #199 / issue #563 / issue
     #530 documented rule: accept paths (single-author pending change,
     multi-author/multi-cluster pending changes -- issue #563 -- and a
     pending change inside a field code -- issue #530 -- all accept-all
     with a disclosure note) and the one reject path (a malformed/corrupt
     revision record with no resulting_text -> fail closed).
  4. An un-normalizable fixture fails closed to the issue #38 internal
     analysis report artifact with `status=MANUAL_REVIEW_REQUIRED`,
     `reason=unnormalizable_input`.
  5. The pipeline-stage entry point's input/output event carries S3
     pointers only -- no document substance -- per the issue #19
     POINTER-ONLY PAYLOAD RULE (infra/lambda/mock_review/handler.py).
  6. (Issue #663) `<w:hyperlink>` is a TRANSPARENT, text-bearing paragraph
     child: its runs reach extraction in document order, a pending
     `<w:ins>`/`<w:del>` inside one splits into the original/resulting
     streams like any other, and the containers that stay deliberately
     excluded (textbox body, content-control placeholder) are still
     excluded even when they contain a hyperlink. Proved against this
     file's own OOXML AND against a committed document produced by a real
     word-processor toolchain, because #663 was exactly a shape no
     hand-built fixture had ever exercised.

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
# zipfile-only convention as scripts/docx_parts.py -- no
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
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
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


def _whole_paragraph_deletion_p(original: str) -> str:
    """A tracked-change deletion with nothing inserted to replace it, plus
    an open reviewer comment on the same clause. Every run sits inside the
    `<w:del>`, so `_build_paragraph_record` computes `resulting_text == ""`.

    Until issue #93 this was read as "structurally irreconcilable" and
    failed the whole upload closed. It is not irreconcilable: the operative
    text after accepting the change is determinately the empty string. It
    now normalizes as a whole-paragraph DELETION (see
    `tests/test_whole_paragraph_deletion_93.py`)."""
    return (
        "<w:p>"
        '<w:del w:id="1" w:author="unknown" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        '<w:r><w:commentReference w:id="0"/></w:r>'
        "</w:p>"
    )


# U+200B ZERO WIDTH SPACE -- the control-character screen's own fail-closed
# shape (issue #632), and, since issue #93 reclassified the whole-paragraph
# deletion above, the fail-closed shape these fixtures reach through REAL
# OOXML extraction. `scripts/normalize_input.py`'s malformed-record branch
# is unreachable from a real `.docx` (the extractor always sets the
# `resulting_text` key), so a fail-closed document fixture must use a
# refusal the extractor can actually produce.
_ZERO_WIDTH_SPACE = "​"


def _control_character_p(text: str) -> str:
    """A paragraph carrying an invisible zero-width space -- fails closed
    under the control-character screen (issue #632)."""
    return f"<w:p><w:r><w:t>{text}{_ZERO_WIDTH_SPACE}payable monthly.</w:t></w:r></w:p>"


def _field_code_conflict_p(original: str, resulting: str, author: str = "counterparty") -> str:
    """A pending tracked change living INSIDE a field's cached-result
    region (w:fldSimple) -- accepts-all the same as an ordinary pending
    change (issue #530): the cluster's own resulting_text is already the
    field's resolved display text."""
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


_HYPERLINK_RUNS = (
    '<w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
    '<w:t xml:space="preserve">{text}</w:t></w:r>'
)


def _hyperlink(inner: str, *, anchor: str | None = None, rel_id: str = "rId9") -> str:
    """One `<w:hyperlink>` wrapper (issue #663). `anchor` gives the INTERNAL
    cross-reference form (`w:anchor`, what Word writes for "see Section 7.2"
    and defined-term links); the default gives the EXTERNAL form (`r:id`
    into `word/_rels/document.xml.rels`). The extractor reads neither
    attribute -- it never opens the rels part -- so both must behave
    identically; both are exercised.

    The run shape (`Hyperlink` character style in the run's own `<w:rPr>`,
    `xml:space="preserve"`) is copied from a real word-processor-generated
    document, not invented here -- see
    `tests/fixtures/extraction_normalization_80/hyperlink-cross-reference.PROVENANCE.md`,
    the committed bytes this file also asserts against directly.
    """
    attr = f' w:anchor="{anchor}"' if anchor is not None else f' r:id="{rel_id}"'
    return f"<w:hyperlink{attr}>{inner}</w:hyperlink>"


def _linked_cross_reference_p(prefix: str, link_text: str, suffix: str, *, anchor: str | None = None) -> str:
    """A clause whose middle words live inside a `<w:hyperlink>` -- the
    silent-content-loss shape of issue #663."""
    return (
        "<w:p>"
        f'<w:r><w:t xml:space="preserve">{prefix}</w:t></w:r>'
        + _hyperlink(_HYPERLINK_RUNS.format(text=link_text), anchor=anchor)
        + f'<w:r><w:t xml:space="preserve">{suffix}</w:t></w:r>'
        "</w:p>"
    )


def _unlinked_cross_reference_p(prefix: str, link_text: str, suffix: str) -> str:
    """The SAME three runs with the `<w:hyperlink>` wrapper removed -- the
    control for the transparency invariant (the wrapper must contribute
    nothing of its own to either text stream)."""
    return (
        "<w:p>"
        f'<w:r><w:t xml:space="preserve">{prefix}</w:t></w:r>'
        + _HYPERLINK_RUNS.format(text=link_text)
        + f'<w:r><w:t xml:space="preserve">{suffix}</w:t></w:r>'
        "</w:p>"
    )


def _tracked_change_inside_hyperlink_p(
    prefix: str, original: str, resulting: str, suffix: str, author: str = "counterparty"
) -> str:
    """A pending tracked change living INSIDE a `<w:hyperlink>` -- the
    counterparty renumbered the section a cross-reference points at. The
    hyperlink is a transparent wrapper, so the existing `<w:ins>`/`<w:del>`
    handlers must split it into the original/resulting streams exactly as
    they would outside one."""
    return (
        "<w:p>"
        f'<w:r><w:t xml:space="preserve">{prefix}</w:t></w:r>'
        + _hyperlink(
            f'<w:del w:id="1" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
            f'<w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
            f'<w:delText xml:space="preserve">{original}</w:delText></w:r></w:del>'
            f'<w:ins w:id="2" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
            f'<w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
            f'<w:t xml:space="preserve">{resulting}</w:t></w:r></w:ins>',
            anchor="_Ref663",
        )
        + f'<w:r><w:t xml:space="preserve">{suffix}</w:t></w:r>'
        "</w:p>"
    )


def _textbox_hyperlink_payload_p(payload: str) -> str:
    """A `<w:hyperlink>` nested inside a TEXTBOX body. Descending into
    hyperlinks must not smuggle in the containers that stay excluded: the
    walk still never enters `<w:drawing>`, so nothing here can reach the
    output."""
    return (
        "<w:p><w:r>"
        f'<w:drawing><wp:anchor><wp:docPr id="2" name="TextBox 2"/>'  # noqa: F541
        "<a:graphic><a:graphicData><wps:txbx><w:txbxContent><w:p>"
        + _hyperlink(_HYPERLINK_RUNS.format(text=f"{payload}_TEXTBOX_LINK"), anchor="x")
        + "</w:p></w:txbxContent></wps:txbx>"
        "</a:graphicData></a:graphic></wp:anchor></w:drawing>"
        "</w:r></w:p>"
    )


def _sdt_hyperlink_payload_p(payload: str) -> str:
    """A `<w:hyperlink>` inside a content control GENUINELY showing its
    display-only placeholder text (`w:sdtPr/w:showingPlcHdr`, issue #94) --
    still excluded, same reason as `_sdt_payload_p` below. (A content
    control the counterparty or a template actually filled in is NOT this
    shape -- see `tests/test_content_control_extraction_94.py`, which pins
    that a filled-in `w:sdt`/`w:sdtContent` is a transparent wrapper, same
    as `w:hyperlink` itself.)"""
    return (
        "<w:p><w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent><w:p>"
        + _hyperlink(_HYPERLINK_RUNS.format(text=f"{payload}_SDT_LINK"), anchor="y")
        + "</w:p></w:sdtContent></w:sdt></w:p>"
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
    """A content control GENUINELY showing its display-only placeholder
    text (`w:sdtPr/w:showingPlcHdr`) -- excluded (issue #94 narrowed this
    from "every `w:sdt`" to exactly this shape; a FILLED-IN content control
    -- no `showingPlcHdr` -- is a transparent wrapper instead, see
    `tests/test_content_control_extraction_94.py`)."""
    return (
        f"<w:p><w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr>"
        f"<w:sdtContent><w:p><w:r><w:t>{payload}</w:t></w:r></w:p></w:sdtContent></w:sdt></w:p>"
    )


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
    de-branded placeholder prose only, matching the synthetic-placeholder
    convention issue #200 established. (The generator that convention was
    named after was deleted by issue #631 with the rest of the
    standard-form subsystem; the convention still governs every committed
    fixture.)"""
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


def test_multi_author_conflict_accepts_all(failures: list[str]) -> None:
    """MUST-NORMALIZE (issue #563, exercised via real OOXML extraction):
    two different authors' pending edits on the same paragraph, back-to-back
    with no intervening plain text, used to fail the whole document closed
    (issue #199). Issue #563 redefines this as ACCEPT-ALL with a disclosure
    note, same as the single-author case -- every cluster's resulting_text
    is the SAME whole-paragraph accept-all text."""
    resulting_a = "Each party's liability under this Agreement shall be uncapped."
    resulting_b = "Each party's aggregate liability under this Agreement shall not exceed $250,000."
    body = _heading_p("Limitation on Liability") + _two_author_conflict_p(
        "Each party's aggregate liability under this Agreement shall not exceed $150,000.",
        resulting_a,
        resulting_b,
    )
    result = stage.extract_and_normalize(_build_docx_bytes(body))
    if result.get("status") != "normalized":
        failures.append(f"[G3b] Two-author pending changes must accept-all, not fail closed. Got: {result}")
        return
    para = result["paragraphs"][0]
    if resulting_a not in para["text"] or resulting_b not in para["text"]:
        failures.append(f"[G3b2] Both authors' resulting text must be folded into the operative draft: {para!r}")
    if "normalization_notes" not in result or not result["normalization_notes"]:
        failures.append(
            f"[G3b3] Multi-author accept-all disposition must be recorded in "
            f"normalization_notes, never silent. Got: {result}"
        )


def test_pending_change_inside_field_code_accepts_all(failures: list[str]) -> None:
    """MUST-NORMALIZE (issue #530, exercised via real OOXML extraction): a
    pending tracked change living inside a field's cached-result region
    used to fail the whole document closed (issue #199's original rule).
    Owner decision 2026-08-09 redefines this as ACCEPT-ALL with a
    disclosure note naming what the field resolved to -- the field's own
    resulting_text is already the resolved display text, so there is no
    separate "which field result wins" decision left to make."""
    body = _heading_p("Governing Law") + _field_code_conflict_p("Delaware", "New York")
    result = stage.extract_and_normalize(_build_docx_bytes(body))
    if result.get("status") != "normalized":
        failures.append(
            f"[G3c] Pending change inside a field code must accept-all, not fail "
            f"closed. Got: {result}"
        )
        return
    para = result["paragraphs"][0]
    if "New York" not in para["text"]:
        failures.append(f"[G3c2] Field's resolved resulting text not folded into output: {para!r}")
    if "Delaware" in para["text"]:
        failures.append(f"[G3c3] Pre-edit field text must not remain once accept-all applies: {para!r}")
    notes = result.get("normalization_notes") or ""
    if not notes:
        failures.append(
            f"[G3c4] Disposition must be recorded in normalization_notes, never silent. Got: {result}"
        )
    if "New York" not in notes or "field" not in notes.lower():
        failures.append(
            f"[G3c5] normalization_notes must name what the field resolved to. Got: {notes!r}"
        )


def test_whole_paragraph_deletion_with_comment_normalizes(failures: list[str]) -> None:
    """Issue #93 reverses this case's expected outcome. A tracked deletion
    covering an ENTIRE paragraph, with nothing inserted to replace it, used
    to fail the whole upload closed as a malformed revision record; it is an
    ordinary redline move whose operative text is determinately empty, and
    an open comment on the same clause still does not gate it (issue #199)."""
    struck = "Each party's aggregate liability under this Agreement shall not exceed $150,000."
    body = (
        _heading_p("Limitation on Liability")
        + _whole_paragraph_deletion_p(struck)
        + _body_p("Fees are payable monthly.")
    )
    result = stage.extract_and_normalize(_build_docx_bytes(body))
    if result.get("status") != "normalized":
        failures.append(
            f"[G3d] A tracked deletion covering a whole paragraph must normalize as a "
            f"deletion, not fail closed as a malformed record (issue #93). Got: {result}"
        )
        return
    texts = " ".join(p["text"] for p in result["paragraphs"])
    if struck in texts:
        failures.append(
            f"[G3d2] The struck paragraph must not survive into the operative draft. "
            f"Got: {texts!r}"
        )
    if "Fees are payable monthly." not in texts:
        failures.append(
            f"[G3d3] A sibling paragraph under the same heading must survive the "
            f"deletion of its neighbour. Got: {texts!r}"
        )
    notes = result.get("normalization_notes") or ""
    if "accepted-all into the operative draft." not in notes or "struck paragraph is omitted" not in notes:
        failures.append(
            f"[G3d4] The deletion must be disclosed in normalization_notes, in the "
            f"established accept-all sentence shape. Got: {notes!r}"
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
    # The refusal fixture is the control-character screen (issue #632), not
    # the whole-paragraph deletion this file used before issue #93 -- that
    # shape now normalizes, and the malformed-record branch it used to reach
    # is unreachable from real OOXML. See `_control_character_p`.
    body = _heading_p("Limitation on Liability") + _control_character_p(
        "Each party's aggregate liability under this Agreement shall not exceed $150,000. Fees are "
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
        + _control_character_p(
            "Each party's aggregate liability under this Agreement shall not exceed $150,000. Fees are "
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
    # Control-character screen, not the pre-#93 whole-paragraph deletion --
    # see `test_unnormalizable_document_emits_analysis_report_shape`.
    body = _heading_p("Limitation on Liability") + _control_character_p(
        "Each party's aggregate liability under this Agreement shall not exceed $150,000. Fees are "
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
# G6: `<w:hyperlink>` is a transparent, text-bearing wrapper (issue #663)
# ---------------------------------------------------------------------------

_HYPERLINK_PREFIX = "The Supplier shall indemnify the Customer against any Loss arising under "
_HYPERLINK_TEXT = "Section 7.2"
_HYPERLINK_SUFFIX = ", subject to the cap set out in this Agreement."


def test_hyperlink_wrapped_text_reaches_extraction(failures: list[str]) -> None:
    """AC1 (issue #663): text inside `<w:hyperlink>` used to be dropped
    WITHOUT recursion, so a clause that cross-references another section
    reached the model with words missing out of its middle -- silent
    content loss, reported nowhere.

    Asserted as an INVARIANT rather than a spelling: wrapping runs in a
    hyperlink must change NOTHING about the extracted record, because the
    wrapper carries no text of its own. Both wrapper forms (internal
    `w:anchor` cross-reference, external `r:id` link) are checked against
    the same unwrapped control.
    """
    expected = _HYPERLINK_PREFIX + _HYPERLINK_TEXT + _HYPERLINK_SUFFIX

    control = stage.extract_document_paragraphs(
        _build_docx_bytes(
            _heading_p("Indemnity")
            + _unlinked_cross_reference_p(_HYPERLINK_PREFIX, _HYPERLINK_TEXT, _HYPERLINK_SUFFIX)
        )
    )
    if control[0]["physical_paragraphs"][0]["text"] != expected:
        failures.append(
            f"[G6a] Control (no hyperlink) did not extract the expected clause text: "
            f"{control[0]['physical_paragraphs'][0]['text']!r}"
        )
        return

    for label, anchor in (("internal w:anchor", "_Ref90210"), ("external r:id", None)):
        linked = stage.extract_document_paragraphs(
            _build_docx_bytes(
                _heading_p("Indemnity")
                + _linked_cross_reference_p(
                    _HYPERLINK_PREFIX, _HYPERLINK_TEXT, _HYPERLINK_SUFFIX, anchor=anchor
                )
            )
        )
        got = linked[0]["physical_paragraphs"][0]["text"]
        if _HYPERLINK_TEXT not in got:
            failures.append(
                f"[G6b] {label}: text inside <w:hyperlink> never reached extraction -- "
                f"the clause the model reviews is missing words that are in the "
                f"document. Got {got!r}"
            )
        if linked != control:
            failures.append(
                f"[G6c] {label}: a <w:hyperlink> wrapper must be TRANSPARENT -- the same "
                f"runs must extract identically wrapped and unwrapped. "
                f"linked={linked!r} control={control!r}"
            )

    normalized = stage.extract_and_normalize(
        _build_docx_bytes(
            _heading_p("Indemnity")
            + _linked_cross_reference_p(
                _HYPERLINK_PREFIX, _HYPERLINK_TEXT, _HYPERLINK_SUFFIX, anchor="_Ref90210"
            )
        )
    )
    if normalized.get("status") != "normalized":
        failures.append(f"[G6d] Hyperlink-bearing document must normalize cleanly. Got: {normalized}")
    elif normalized["paragraphs"][0]["text"] != expected:
        failures.append(
            f"[G6e] Hyperlink text lost between extraction and the normalized body: "
            f"{normalized['paragraphs'][0]['text']!r}"
        )


def test_tracked_change_inside_hyperlink_splits_original_and_resulting(failures: list[str]) -> None:
    """AC2 (issue #663): a counterparty renumbering the section a
    cross-reference points at leaves `<w:ins>`/`<w:del>` INSIDE the
    `<w:hyperlink>`. Descending must hand those to the existing revision
    handlers with mode/author carried through, so the original/resulting
    split is exactly what it would be outside a hyperlink -- not a cluster
    with an empty stream (which would fail the whole document closed)."""
    raw = stage.extract_document_paragraphs(
        _build_docx_bytes(
            _heading_p("Indemnity")
            + _tracked_change_inside_hyperlink_p(
                _HYPERLINK_PREFIX, "Section 7.2", "Section 8.4", _HYPERLINK_SUFFIX
            )
        )
    )
    record = raw[0]["physical_paragraphs"][0]
    tracked = [rev for rev in record["revisions"] if rev.get("type") == "tracked_change"]
    if len(tracked) != 1:
        failures.append(
            f"[G6f] A pending change inside a hyperlink must surface as exactly one "
            f"tracked-change cluster. Got: {record['revisions']!r}"
        )
        return
    revision = tracked[0]
    if revision.get("author") != "counterparty":
        failures.append(f"[G6g] Author must survive the hyperlink descent. Got: {revision!r}")
    expected_original = _HYPERLINK_PREFIX + "Section 7.2" + _HYPERLINK_SUFFIX
    expected_resulting = _HYPERLINK_PREFIX + "Section 8.4" + _HYPERLINK_SUFFIX
    if revision.get("original_text") != expected_original:
        failures.append(
            f"[G6h] original_text must carry the deleted hyperlink text in document "
            f"order. Expected {expected_original!r}, got {revision.get('original_text')!r}"
        )
    if revision.get("resulting_text") != expected_resulting:
        failures.append(
            f"[G6i] resulting_text must carry the inserted hyperlink text in document "
            f"order. Expected {expected_resulting!r}, got {revision.get('resulting_text')!r}"
        )

    normalized = stage.extract_and_normalize(
        _build_docx_bytes(
            _heading_p("Indemnity")
            + _tracked_change_inside_hyperlink_p(
                _HYPERLINK_PREFIX, "Section 7.2", "Section 8.4", _HYPERLINK_SUFFIX
            )
        )
    )
    if normalized.get("status") != "normalized":
        failures.append(
            f"[G6j] A pending change inside a hyperlink must accept-all, not fail "
            f"closed. Got: {normalized}"
        )
        return
    body = normalized["paragraphs"][0]["text"]
    if body != expected_resulting:
        failures.append(f"[G6k] Accept-all body wrong for a hyperlinked revision: {body!r}")
    if not (normalized.get("normalization_notes") or ""):
        failures.append("[G6l] The accept-all disposition must still be recorded, never silent.")


def test_hyperlink_inside_an_excluded_container_stays_excluded(failures: list[str]) -> None:
    """AC4 (issue #663 Scope: "this adds ONE tag"): descending into
    `<w:hyperlink>` must not become a general "recurse into every child"
    walk. A hyperlink nested in a textbox body is still unreachable, because
    the walk never enters `<w:drawing>` in the first place. A hyperlink
    nested in a content control that is GENUINELY showing its display-only
    placeholder text (issue #94's `w:sdtPr/w:showingPlcHdr` carve-out) is
    unreachable for the same reason `<w:sdt>` itself is: the walk skips a
    true placeholder's `w:sdtContent` without recursing into it at all.
    (A hyperlink nested in a FILLED-IN content control is a different shape
    entirely, and IS reachable -- see
    `tests/test_content_control_extraction_94.py`, which is where that
    positive case is pinned, matching this file's convention of pinning
    only the negative/excluded shape.)"""
    body = (
        _heading_p("Indemnity")
        + _linked_cross_reference_p(
            _HYPERLINK_PREFIX, _HYPERLINK_TEXT, _HYPERLINK_SUFFIX, anchor="_Ref90210"
        )
        + _textbox_hyperlink_payload_p("PAYLOAD_MARKER")
        + _sdt_hyperlink_payload_p("PAYLOAD_MARKER")
    )
    result = stage.extract_and_normalize(_build_docx_bytes(body))
    if result.get("status") != "normalized":
        failures.append(f"[G6m] Fixture must normalize. Got: {result}")
        return
    serialized = repr(result)
    for marker in ("PAYLOAD_MARKER_TEXTBOX_LINK", "PAYLOAD_MARKER_SDT_LINK"):
        if marker in serialized:
            failures.append(
                f"[G6n] {marker!r} leaked: recursing into <w:hyperlink> must not open the "
                f"walk up to the containers that stay deliberately excluded."
            )
    if _HYPERLINK_TEXT not in serialized:
        failures.append("[G6o] The allowed hyperlink text was lost alongside the exclusion.")


def test_real_word_processor_document_extracts_its_hyperlink_text(failures: list[str]) -> None:
    """The same property against a COMMITTED document produced by a real
    word-processor toolchain rather than by this file's own XML strings --
    see `hyperlink-cross-reference.PROVENANCE.md`. #663 was precisely a
    shape no hand-built fixture had ever exercised, so the regression is
    pinned against markup nobody here wrote: full package, `Hyperlink`
    character styles, curly punctuation, both wrapper forms."""
    fixture = FIXTURES_DIR / "hyperlink-cross-reference.SYNTHETIC.docx"
    if not fixture.exists():
        failures.append(f"[G6p] Missing committed fixture: {fixture}")
        return
    docx_bytes = fixture.read_bytes()

    # Guard the guard: if the fixture is ever regenerated without both
    # hyperlink forms, every assertion below would still pass while proving
    # nothing.
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        document_xml = zf.read("word/document.xml").decode("utf-8")
    if "<w:hyperlink w:anchor=" not in document_xml or "<w:hyperlink r:id=" not in document_xml:
        failures.append(
            "[G6q] The committed fixture no longer carries BOTH an internal (w:anchor) "
            "and an external (r:id) hyperlink -- it can no longer prove #663."
        )
        return

    result = stage.extract_and_normalize(docx_bytes)
    if result.get("status") != "normalized":
        failures.append(f"[G6r] Real word-processor document failed to normalize: {result}")
        return
    indemnity = next((p for p in result["paragraphs"] if p.get("heading") == "Indemnity"), None)
    if indemnity is None:
        failures.append(f"[G6s] Expected an 'Indemnity' block. Got: {result['paragraphs']!r}")
        return
    expected = (
        "The Supplier shall indemnify the Customer against any Loss arising under "
        "Section 7.2 and the Data Protection Addendum, subject to the cap set out "
        "in this Agreement."
    )
    if indemnity["text"] != expected:
        failures.append(
            f"[G6t] Real-document clause text is not what the document says. "
            f"Expected {expected!r}, got {indemnity['text']!r}"
        )


def test_boundary_paragraph_detection_evaluates_operative_text(failures: list[str]) -> None:
    """A paragraph with a pending tracked change must evaluate clause-boundary
    detection against its OPERATIVE (resulting) text, not its pre-edit text.

    If someone typed over a short all-caps text (e.g. 'EEE') to insert a full
    body paragraph, evaluating the pre-edit text would falsely detect 'EEE' as
    a heading boundary in Stage 1, while Stage 5 (evaluating the materialized
    document) would not -- breaking block-map consistency across stages."""
    docx = _build_docx_bytes(
        _heading_p("Section 1. General")
        + _body_p("The parties agree to the following terms.")
        + _pending_change_p(
            "EEE",
            "This is an ordinary long body paragraph that should never be detected as a heading boundary.",
        )
        + _body_p("Final sentence of the section.")
    )
    res = stage.extract_and_normalize(docx)
    if res["status"] != "normalized":
        failures.append(f"[G7a] Expected normalized document, got {res['status']!r}")
        return
    paragraphs = res["paragraphs"]
    if len(paragraphs) != 1:
        failures.append(
            f"[G7b] Expected exactly 1 logical paragraph under 'Section 1. General', "
            f"got {len(paragraphs)}: {[p.get('heading') for p in paragraphs]!r}"
        )
        return
    if paragraphs[0]["heading"] != "Section 1. General":
        failures.append(
            f"[G7c] Expected heading 'Section 1. General', got {paragraphs[0]['heading']!r}"
        )
    # Also verify that Stage 5 materialization yields the identical block map
    mat_bytes = stage.materialize_accept_all(docx)
    res_mat = stage.extract_and_normalize(mat_bytes)
    bm_raw = stage.build_block_map(paragraphs)
    bm_mat = stage.build_block_map(res_mat["paragraphs"])
    if bm_raw != bm_mat:
        failures.append(
            f"[G7d] Block map from raw docx must match block map from materialized docx. "
            f"bm_raw={bm_raw!r} vs bm_mat={bm_mat!r}"
        )


def test_run_children_extract_in_document_order(failures: list[str]) -> None:
    """Issue #97: `w:tab`, `w:br`/`w:cr`, `w:noBreakHyphen`, `w:softHyphen`,
    and `w:sym` inside one run must be walked IN DOCUMENT ORDER alongside
    the run's `w:t` text -- not have every `w:tab` moved to the run's end
    (fusing the words on either side of it) with every other element
    silently dropped (fusing words across a `w:br`, dropping a defined
    term's non-breaking hyphen). `w:cr` is pinned on its own, not left to
    ride on the `w:br` branch it shares: a walk that matched only `w:br`
    would stay green on every other case here. This body paragraph is
    deliberately long and does not start with a numbered/lettered marker, so
    it stays ordinary body text under 'Fees' on the length cap alone."""
    body_p = (
        "<w:p><w:r>"
        '<w:t xml:space="preserve">Payments under this Agreement</w:t>'
        "<w:tab/>"
        '<w:t xml:space="preserve">shall be made by wire transfer</w:t>'
        "<w:br/>"
        '<w:t xml:space="preserve">to the account for any Non</w:t>'
        "<w:noBreakHyphen/>"
        '<w:t xml:space="preserve">Disclosure</w:t>'
        "<w:softHyphen/>"
        '<w:t xml:space="preserve"> fees and</w:t>'
        '<w:sym w:font="Symbol" w:char="F0E0"/>'
        '<w:t xml:space="preserve"> related</w:t>'
        "<w:cr/>"
        '<w:t xml:space="preserve">charges.</w:t>'
        "</w:r></w:p>"
    )
    result = stage.extract_and_normalize(_build_docx_bytes(_heading_p("Fees") + body_p))
    if result.get("status") != "normalized":
        failures.append(f"[G8a] Expected normalized document, got {result}")
        return
    text = result["paragraphs"][0]["text"]
    expected = (
        "Payments under this Agreement\tshall be made by wire transfer\n"
        "to the account for any Non\u2011Disclosure fees and\uf0e0 related\ncharges."
    )
    if text != expected:
        failures.append(f"[G8b] Expected {expected!r}, got {text!r}")
    if "transfer\nto" not in text:
        failures.append(f"[G8c] w:br must become a newline in place, not vanish. Got: {text!r}")
    if "related\ncharges." not in text:
        failures.append(f"[G8d] w:cr must become a newline in place, not vanish. Got: {text!r}")


def test_lettered_lead_in_tab_is_no_longer_relocated(failures: list[str]) -> None:
    """The exact everyday case issue #97 names: a manually-lettered lead-in
    like '(a)<tab>...' must keep its tab immediately after '(a)', not have
    it moved to the run's end (which pre-fix produced '(a)Buyer
    paysSeller.' -- the tab gone AND the words fused). This is the issue's
    own Required-verification fixture.

    It also pins the segmentation half of the fix. A SHORT paragraph
    starting '(a)\\t' matches `clause_boundaries.LETTERED_LEAD_IN_RE`
    (`^\\([a-z]\\)\\s+`), so handing the boundary tier the corrected text
    would have promoted this body paragraph to a clause boundary and moved
    its text out of `text` into `heading_source_text`, leaving the block
    with no reviewable text at all. The tier is handed the record's
    `boundary_text` instead -- the same accept-all text read as before #97,
    '(a)Buyer paysSeller.', which matches no lead-in -- so the paragraph
    stays exactly where it was: ordinary body text under 'Fees'.
    """
    body_p = (
        "<w:p><w:r>"
        '<w:t xml:space="preserve">(a)</w:t>'
        "<w:tab/>"
        '<w:t xml:space="preserve">Buyer pays</w:t>'
        "<w:br/>"
        '<w:t xml:space="preserve">Seller.</w:t>'
        "</w:r></w:p>"
    )
    result = stage.extract_and_normalize(_build_docx_bytes(_heading_p("Fees") + body_p))
    if result.get("status") != "normalized":
        failures.append(f"[G9a] Expected normalized document, got {result}")
        return
    paragraphs = result["paragraphs"]
    if len(paragraphs) != 1:
        failures.append(
            f"[G9b] The lettered lead-in carries its own w:br, so it stays BODY text "
            f"under 'Fees' -- expected 1 logical paragraph, got {len(paragraphs)}: "
            f"{[p.get('heading') for p in paragraphs]!r}"
        )
        return
    text = paragraphs[0]["text"]
    if "paysSeller" in text or not text.startswith("(a)\t"):
        failures.append(
            f"[G9c] Expected the tab preserved right after '(a)' and the words not "
            f"fused across w:br, got: {text!r}"
        )
    if text != "(a)\tBuyer pays\nSeller.":
        failures.append(f"[G9d] Expected '(a)\\tBuyer pays\\nSeller.', got {text!r}")


def test_short_tab_separated_sub_clauses_stay_body_text(failures: list[str]) -> None:
    """The everyday case, and the one thing #97 must NOT change: a clause
    whose body is three ordinary manually-lettered sub-clauses, each SHORT
    enough to clear `clause_boundaries.MAX_FALLBACK_HEADING_CHARS` and each
    one single line, still segments exactly as it did before the fix -- ONE
    block under 'Payment Terms' whose `text` carries all three sentences.

    This is the regression `boundary_text` exists for.
    `LETTERED_LEAD_IN_RE` is `^\\([a-z]\\)\\s+`: before #97 the extracted
    text was '(a)Buyer shall pay...' -- the `w:tab` relocated to the run's
    end and stripped -- so it did not match and the paragraph was body
    text. Decide boundaries on the corrected text instead and all three
    paragraphs become clause BOUNDARIES: four blocks, every one of them
    with an EMPTY `text`, each sentence lifted into a `heading`. The
    document would then have no reviewable text and no addressable span
    anywhere -- strictly worse than the fused words #97 set out to fix,
    because the model reads nothing and the writer has nothing to redline.
    The length cap cannot separate the two (each sentence here is well
    under 80 characters).
    """
    sentences = (
        ("a", "Buyer shall pay all fees within 30 days."),
        ("b", "Seller shall issue invoices monthly."),
        ("c", "Late amounts accrue interest at 1% per month."),
    )
    body = "".join(
        "<w:p><w:r>"
        f'<w:t xml:space="preserve">({letter})</w:t>'
        "<w:tab/>"
        f'<w:t xml:space="preserve">{sentence}</w:t>'
        "</w:r></w:p>"
        for letter, sentence in sentences
    )
    result = stage.extract_and_normalize(
        _build_docx_bytes(_heading_p("Payment Terms") + body)
    )
    if result.get("status") != "normalized":
        failures.append(f"[G9e] Expected normalized document, got {result}")
        return
    paragraphs = result["paragraphs"]
    if len(paragraphs) != 1:
        failures.append(
            f"[G9f] Three short '(x)\\t...' sub-clauses are BODY text under one "
            f"heading: expected 1 logical paragraph, got {len(paragraphs)} -- "
            f"headings {[p.get('heading') for p in paragraphs]!r}, texts "
            f"{[p.get('text') for p in paragraphs]!r}"
        )
        return
    block = paragraphs[0]
    if block.get("heading") != "Payment Terms":
        failures.append(f"[G9g] Expected the 'Payment Terms' heading, got {block!r}")
    text = block["text"]
    if not text.strip():
        failures.append("[G9h] The block's own text is empty -- nothing left to review")
        return
    for letter, sentence in sentences:
        if sentence not in text:
            failures.append(f"[G9i] Sub-clause ({letter})'s sentence is missing from {text!r}")
        if f"({letter})\t" not in text:
            failures.append(f"[G9j] Sub-clause ({letter})'s lead-in tab is missing from {text!r}")


def test_marker_then_tab_then_title_in_the_next_run_is_still_a_boundary(
    failures: list[str],
) -> None:
    """Issue #97 un-parking (1), and the review of its first fix round:
    Word's routine "marker + tab in one run, title in the NEXT run" shape --
    `<w:r><w:t>1.</w:t><w:tab/></w:r><w:r><w:t>Title</w:t></w:r>` -- is a
    clause boundary on HEAD (the tab sat at the end of its own run, right
    after the marker, so the lead-in pattern matched) and must stay one,
    WHATEVER the title ends with. Two earlier cuts of #97 broke it: one
    folded the tab out of the lead-in check, and one ruled a tab-led title
    ending in '.' or ':' a body sentence. Each collapsed a style-stripped
    draft of two such headings into ONE '<untitled>' block with both
    headings folded into the body -- 'Definitions.' and 'Definitions:' are
    how a great many real drafts title a section.

    Pinned at both levels, because each can regress alone:

      - STRING level. `clause_boundaries.is_boundary_paragraph` is unchanged
        by #97 and a string carries no record of which run its tab sat in,
        so a tab after a marker is the whitespace it always was.
      - DOCUMENT level. The decision is made on the record's
        `boundary_text`, which for this shape is the very string HEAD
        decided on. The previous two tests hold the shape it must stay
        separable from: `(a)<tab>Buyer shall pay ...` in ONE run is body
        text, because its `boundary_text` has no tab after the marker.

    NOT the churn corpus: it writes its headings as literal "N. Title" with
    a SPACE, so the document-spine smoke cannot see a tab-led heading."""
    import clause_boundaries  # type: ignore  # noqa: PLC0415

    for text in (
        "1.\tTerm and Termination",
        "1.\tDefinitions.",
        "1.\tDefinitions:",
        "2.1\tScope of Services.",
        "(a)\tDefinitions",
    ):
        if clause_boundaries.is_boundary_paragraph(text) is not True:
            failures.append(f"[G14a] is_boundary_paragraph({text!r}) must be True, as on HEAD")

    def two_run(marker: str, title: str) -> str:
        return (
            "<w:p>"
            f'<w:r><w:t xml:space="preserve">{marker}</w:t><w:tab/></w:r>'
            f'<w:r><w:t xml:space="preserve">{title}</w:t></w:r>'
            "</w:p>"
        )

    def body(sentence: str) -> str:
        return f'<w:p><w:r><w:t xml:space="preserve">{sentence}</w:t></w:r></w:p>'

    first_body = "This Agreement runs for one year and renews annually thereafter by consent."
    second_body = "Each party shall hold the other's information in confidence indefinitely."
    # No Heading style anywhere: this is the fallback tier's own territory.
    for (m1, t1), (m2, t2) in (
        (("1.", "Term and Termination"), ("2.", "Confidentiality")),
        (("1.", "Definitions."), ("2.", "Confidentiality.")),
        (("1.", "Definitions:"), ("2.", "Confidentiality:")),
        (("2.1", "Scope of Services."), ("2.2", "Fees.")),
    ):
        doc = two_run(m1, t1) + body(first_body) + two_run(m2, t2) + body(second_body)
        result = stage.extract_and_normalize(_build_docx_bytes(doc))
        if result.get("status") != "normalized":
            failures.append(f"[G14b] {t1!r}: expected normalized document, got {result}")
            continue
        headings = [p.get("heading") for p in result["paragraphs"]]
        texts = [p.get("text") for p in result["paragraphs"]]
        if headings != [t1, t2] or texts != [first_body, second_body]:
            failures.append(
                f"[G14c] Both tab-led headings ({t1!r}, {t2!r}) must open a clause, as on "
                f"HEAD; got headings {headings!r} with texts {texts!r}"
            )


def test_multi_line_all_caps_and_bold_headings_are_still_boundaries(
    failures: list[str],
) -> None:
    """The other half of "#97 must not change segmentation": a
    style-stripped heading that Word split across two lines with a `w:br`
    is still a heading. Before #97 the break was dropped and the lines
    arrived fused ('GOVERNING LAWAND JURISDICTION'), which is ALL-CAPS and
    short, so `clause_boundaries.py`'s fallback tier opened a clause there.
    Its `boundary_text` is still that fused line, so it still does -- and
    the bold case is the one that would otherwise be LOST outright, because
    the bold signal requires a single line and the corrected text now has
    two. Either way the heading the clause carries is the corrected text,
    line break included.

    Bold and ALL-CAPS are exactly the pair `strip_heading_styles` exists to
    exercise (docs/document-spine-smoke.md), so both are pinned here."""
    cases = (
        (
            "all-caps",
            "<w:p><w:r>"
            '<w:t xml:space="preserve">GOVERNING LAW</w:t>'
            "<w:br/>"
            '<w:t xml:space="preserve">AND JURISDICTION</w:t>'
            "</w:r></w:p>",
            "GOVERNING LAW\nAND JURISDICTION",
        ),
        (
            "whole-paragraph bold",
            "<w:p><w:r><w:rPr><w:b/></w:rPr>"
            '<w:t xml:space="preserve">Governing Law</w:t>'
            "<w:br/>"
            '<w:t xml:space="preserve">and Jurisdiction</w:t>'
            "</w:r></w:p>",
            "Governing Law\nand Jurisdiction",
        ),
    )
    body_sentence = "This Agreement is governed by the laws of the State."
    for label, heading_p, expected_heading in cases:
        result = stage.extract_and_normalize(
            _build_docx_bytes(
                heading_p + f'<w:p><w:r><w:t xml:space="preserve">{body_sentence}</w:t></w:r></w:p>'
            )
        )
        if result.get("status") != "normalized":
            failures.append(f"[G13a] {label}: expected normalized document, got {result}")
            continue
        paragraphs = result["paragraphs"]
        if len(paragraphs) != 1:
            failures.append(
                f"[G13b] {label}: expected the heading to open exactly one clause, got "
                f"{[p.get('heading') for p in paragraphs]!r}"
            )
            continue
        block = paragraphs[0]
        if block.get("heading_source_text") != expected_heading:
            failures.append(
                f"[G13c] {label}: expected the w:br-split heading "
                f"{expected_heading!r}, got {block.get('heading_source_text')!r}"
            )
        if block["text"] != body_sentence:
            failures.append(
                f"[G13d] {label}: the heading must not fold into the body -- expected "
                f"{body_sentence!r}, got {block['text']!r}"
            )


def test_line_break_split_numbered_heading_is_still_a_boundary(
    failures: list[str],
) -> None:
    """A style-stripped NUMBERED heading Word wrapped with a soft line break
    -- `1. Term<w:br/>and Termination`, or the same with `<w:cr/>` -- was a
    boundary on HEAD: the break was dropped, the line arrived as
    '1. Termand Termination', and the numbered lead-in matched. An earlier
    cut of #97 ruled any multi-line paragraph out of the two lead-in
    signals and collapsed such a draft into ONE '<untitled>' block. Its
    `boundary_text` is still the line HEAD decided on, so it opens its
    clause exactly as before, and the heading it carries is the corrected
    text with the break in place.

    Text alone cannot tell this heading from a `w:br`-separated notices
    block that happens to start with a street number ('1 Main Street /
    Suite 200 / ...'), which HEAD promoted for the same reason. #97 changes
    neither -- it corrects the text, not the segmentation -- so that block
    is promoted exactly as it was before; making it body text is a
    segmentation change of its own, not this issue's."""
    first_body = "This Agreement runs for one year and renews annually thereafter by consent."
    second_body = "Each party shall hold the other's information in confidence indefinitely."
    for separator in ("<w:br/>", "<w:cr/>"):
        doc = (
            "<w:p><w:r>"
            f'<w:t xml:space="preserve">1. Term</w:t>{separator}'
            '<w:t xml:space="preserve">and Termination</w:t>'
            "</w:r></w:p>"
            f'<w:p><w:r><w:t xml:space="preserve">{first_body}</w:t></w:r></w:p>'
            "<w:p><w:r>"
            f'<w:t xml:space="preserve">2. Confidential</w:t>{separator}'
            '<w:t xml:space="preserve">Information</w:t>'
            "</w:r></w:p>"
            f'<w:p><w:r><w:t xml:space="preserve">{second_body}</w:t></w:r></w:p>'
        )
        result = stage.extract_and_normalize(_build_docx_bytes(doc))
        if result.get("status") != "normalized":
            failures.append(f"[G11a] {separator}: expected normalized document, got {result}")
            continue
        headings = [p.get("heading") for p in result["paragraphs"]]
        texts = [p.get("text") for p in result["paragraphs"]]
        if headings != ["Term\nand Termination", "Confidential\nInformation"]:
            failures.append(
                f"[G11b] {separator}: both line-break-split numbered headings must open a "
                f"clause, as on HEAD; got headings {headings!r} with texts {texts!r}"
            )
        elif texts != [first_body, second_body]:
            failures.append(f"[G11c] {separator}: the headings must not fold into the body: {texts!r}")


def test_boundary_text_reads_only_the_accept_all_view(failures: list[str]) -> None:
    """`boundary_text` is built beside `resulting_text`, from the same
    `add_plain`/`add_ins` calls, so it is the ACCEPT-ALL view by
    construction: a deleted run is not in it, an inserted one is. That is
    load-bearing, not tidy -- Stage 1 extracts the raw upload and Stage 5
    the `materialize_accept_all` bytes, where the deletion is gone, and a
    boundary decided on text only one of them can see moves every later
    block id (see `extract_document_paragraphs`).

    (1) The record itself, for the issue's own run and for a tracked pair.
    (2) A DELETED `1.<w:tab/>` ahead of a short body sentence must not make
        that sentence a heading -- the accept-all paragraph has no marker.
    (3) An INSERTED title after a plain `1.<w:tab/>` run is a heading, as on
        HEAD."""
    import xml.etree.ElementTree as ET  # noqa: PLC0415

    def record(paragraph_xml: str) -> dict[str, Any]:
        root = ET.fromstring(  # noqa: S314 - fixture XML this file builds itself
            "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
            f"<w:body>{paragraph_xml}</w:body></w:document>"
        )
        return stage._build_paragraph_record(root[0][0])

    deleted_marker = (
        '<w:del w:id="1" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        '<w:r><w:delText xml:space="preserve">1.</w:delText><w:tab/></w:r></w:del>'
    )
    inserted_title = (
        '<w:ins w:id="2" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        '<w:r><w:t xml:space="preserve">Definitions.</w:t></w:r></w:ins>'
    )
    for paragraph_xml, expected_text, expected_boundary in (
        (
            "<w:p><w:r>"
            '<w:t xml:space="preserve">(a)</w:t><w:tab/>'
            '<w:t xml:space="preserve">Buyer pays</w:t><w:br/>'
            '<w:t xml:space="preserve">Seller.</w:t>'
            "</w:r></w:p>",
            "(a)\tBuyer pays\nSeller.",
            "(a)Buyer paysSeller.",
        ),
        (
            f"<w:p>{deleted_marker}"
            '<w:r><w:t xml:space="preserve">Buyer shall pay.</w:t></w:r></w:p>',
            "Buyer shall pay.",
            "Buyer shall pay.",
        ),
        (
            '<w:p><w:r><w:t xml:space="preserve">1.</w:t><w:tab/></w:r>'
            f"{inserted_title}</w:p>",
            "1.\tDefinitions.",
            "1.\tDefinitions.",
        ),
    ):
        got = record(paragraph_xml)
        if (got["resulting_text"], got["boundary_text"]) != (expected_text, expected_boundary):
            failures.append(
                f"[G15a] expected resulting/boundary text "
                f"{(expected_text, expected_boundary)!r}, got "
                f"{(got['resulting_text'], got['boundary_text'])!r}"
            )

    body_sentence = "Buyer shall pay."
    result = stage.extract_and_normalize(
        _build_docx_bytes(
            _heading_p("Fees")
            + f"<w:p>{deleted_marker}"
            + f'<w:r><w:t xml:space="preserve">{body_sentence}</w:t></w:r></w:p>'
        )
    )
    if result.get("status") != "normalized":
        failures.append(f"[G15b] Expected normalized document, got {result.get('status')!r}")
    elif [p.get("heading") for p in result["paragraphs"]] != ["Fees"]:
        failures.append(
            f"[G15c] a struck '1.<tab>' must not make the accepted sentence a heading; got "
            f"{[(p.get('heading'), p.get('text')) for p in result['paragraphs']]!r}"
        )

    result = stage.extract_and_normalize(
        _build_docx_bytes(
            '<w:p><w:r><w:t xml:space="preserve">1.</w:t><w:tab/></w:r>'
            f"{inserted_title}</w:p>"
            '<w:p><w:r><w:t xml:space="preserve">Terms used here have these meanings.</w:t></w:r></w:p>'
        )
    )
    if result.get("status") != "normalized":
        failures.append(f"[G15d] Expected normalized document, got {result.get('status')!r}")
    elif [p.get("heading") for p in result["paragraphs"]] != ["Definitions."]:
        failures.append(
            f"[G15e] an inserted title after '1.<tab>' must open its clause, as on HEAD; got "
            f"{[(p.get('heading'), p.get('text')) for p in result['paragraphs']]!r}"
        )


def test_hostile_or_malformed_sym_char_degrades_to_a_space(failures: list[str]) -> None:
    """`<w:sym w:char="...">` is the one channel through which an arbitrary
    codepoint can reach the extracted text -- `w:t` content is parsed XML,
    so the parser has already rejected control characters and lone
    surrogates there. `sym_char` must therefore never raise and never emit
    a character the rest of the pipeline cannot carry: a lone surrogate
    would make the UTF-8 encode of any downstream payload raise (a bare
    500), and a `w:char="000A"` would inject a newline no `w:br` put there,
    which is load-bearing for the paragraph-boundary rule. Each of these
    degrades to ONE space -- the run's neighbours must still not fuse."""
    cases = [
        ("<w:sym/>", "no w:char attribute at all"),
        ('<w:sym w:font="Symbol" w:char=""/>', "empty w:char"),
        ('<w:sym w:font="Symbol" w:char="ZZZZ"/>', "non-hex w:char"),
        ('<w:sym w:font="Symbol" w:char="-1"/>', "negative w:char"),
        ('<w:sym w:font="Symbol" w:char="110000"/>', "out-of-range w:char"),
        ('<w:sym w:font="Symbol" w:char="D800"/>', "lone surrogate"),
        ('<w:sym w:font="Symbol" w:char="000A"/>', "control character (LF)"),
        ('<w:sym w:font="Symbol" w:char="0000"/>', "control character (NUL)"),
    ]
    for sym_xml, label in cases:
        body_p = (
            "<w:p><w:r>"
            '<w:t xml:space="preserve">Alpha</w:t>'
            f"{sym_xml}"
            '<w:t xml:space="preserve">Beta</w:t>'
            "</w:r></w:p>"
        )
        try:
            raw = stage.extract_document_paragraphs(
                _build_docx_bytes(_heading_p("Fees") + body_p)
            )
        except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
            failures.append(f"[G12a] {label}: extraction raised {exc!r}")
            continue
        text = raw[0]["physical_paragraphs"][0]["text"]
        if text != "Alpha Beta":
            failures.append(f"[G12b] {label}: expected 'Alpha Beta', got {text!r}")
            continue
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            failures.append(f"[G12c] {label}: extracted text is not UTF-8 encodable: {exc!r}")

    # The happy variant of the same branch: a REAL Symbol-font codepoint is
    # kept verbatim, so the guard above cannot be widened into "every
    # w:sym becomes a space" and stay green.
    body_p = (
        "<w:p><w:r>"
        '<w:t xml:space="preserve">Alpha</w:t>'
        '<w:sym w:font="Symbol" w:char="F0E0"/>'
        '<w:t xml:space="preserve">Beta</w:t>'
        "</w:r></w:p>"
    )
    raw = stage.extract_document_paragraphs(_build_docx_bytes(_heading_p("Fees") + body_p))
    text = raw[0]["physical_paragraphs"][0]["text"]
    if text != "Alpha\uf0e0Beta":
        failures.append(f"[G12d] Expected the F0E0 symbol kept verbatim, got {text!r}")

    # The other half of "kept verbatim": a `w:sym` is a NEW way to put an
    # arbitrary codepoint into the extracted text, so the document-wide
    # zero-width/bidi screen (ARCHITECTURE.md -> "Input normalization") must
    # see it exactly as it sees one typed into a `w:t`. These two are NOT
    # degraded to a space -- silently repairing a planted RLO/ZWSP is the one
    # thing that screen exists to refuse -- the whole document fails closed.
    for label, hex_char in (("RLO", "202E"), ("ZWSP", "200B")):
        body_p = (
            "<w:p><w:r>"
            '<w:t xml:space="preserve">Alpha</w:t>'
            f'<w:sym w:font="Symbol" w:char="{hex_char}"/>'
            '<w:t xml:space="preserve">Beta</w:t>'
            "</w:r></w:p>"
        )
        result = stage.extract_and_normalize(_build_docx_bytes(_heading_p("Fees") + body_p))
        if result.get("status") != "unnormalizable_input":
            failures.append(
                f"[G12e] a {label} smuggled in through w:sym must fail the document "
                f"closed, got status={result.get('status')!r}"
            )


def test_mixed_run_children_inside_a_pending_tracked_change(failures: list[str]) -> None:
    """`w:tab`/`w:br`/`w:noBreakHyphen` must be walked in document order in
    BOTH streams of a pending tracked change -- the deleted (`mode="del"`,
    `w:delText`) run and the inserted (`mode="ins"`, `w:t`) run -- not just
    the plain-run path, since `_process_run`'s child walk is shared across
    all three modes."""
    body_p = (
        "<w:p>"
        '<w:del w:id="1" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        "<w:r>"
        '<w:delText xml:space="preserve">Old term</w:delText>'
        "<w:tab/>"
        '<w:delText xml:space="preserve">unbounded.</w:delText>'
        "</w:r></w:del>"
        '<w:ins w:id="2" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        "<w:r>"
        '<w:t xml:space="preserve">New Non</w:t>'
        "<w:noBreakHyphen/>"
        '<w:t xml:space="preserve">Disclosure term</w:t>'
        "<w:br/>"
        '<w:t xml:space="preserve">capped at $1.</w:t>'
        "</w:r></w:ins>"
        "</w:p>"
    )
    raw = stage.extract_document_paragraphs(_build_docx_bytes(_heading_p("Term") + body_p))
    record = raw[0]["physical_paragraphs"][0]
    tracked = [rev for rev in record["revisions"] if rev.get("type") == "tracked_change"]
    if len(tracked) != 1:
        failures.append(f"[G10a] Expected exactly one tracked-change cluster. Got: {record['revisions']!r}")
        return
    revision = tracked[0]
    expected_original = "Old term\tunbounded."
    expected_resulting = "New Non\u2011Disclosure term\ncapped at $1."
    if revision.get("original_text") != expected_original:
        failures.append(
            f"[G10b] Deleted-stream w:tab must land in place. Expected "
            f"{expected_original!r}, got {revision.get('original_text')!r}"
        )
    if revision.get("resulting_text") != expected_resulting:
        failures.append(
            f"[G10c] Inserted-stream w:noBreakHyphen/w:br must survive. Expected "
            f"{expected_resulting!r}, got {revision.get('resulting_text')!r}"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_clean_standard_form_yields_structured_paragraph_list,
    test_disallowed_parts_payload_never_reaches_output,
    test_single_author_pending_change_accepts_all,
    test_multi_author_conflict_accepts_all,
    test_pending_change_inside_field_code_accepts_all,
    test_whole_paragraph_deletion_with_comment_normalizes,
    test_hidden_text_and_field_result_normalize_cleanly,
    test_sibling_body_paragraph_survives_accept_all_on_other_sibling,
    test_comment_never_gates_accept_all,
    test_unnormalizable_document_emits_analysis_report_shape,
    test_one_unnormalizable_paragraph_fails_whole_document,
    test_run_stage_pointer_only_payload_success_path,
    test_run_stage_pointer_only_payload_fail_closed_path,
    test_hyperlink_wrapped_text_reaches_extraction,
    test_tracked_change_inside_hyperlink_splits_original_and_resulting,
    test_hyperlink_inside_an_excluded_container_stays_excluded,
    test_real_word_processor_document_extracts_its_hyperlink_text,
    test_boundary_paragraph_detection_evaluates_operative_text,
    test_run_children_extract_in_document_order,
    test_lettered_lead_in_tab_is_no_longer_relocated,
    test_short_tab_separated_sub_clauses_stay_body_text,
    test_marker_then_tab_then_title_in_the_next_run_is_still_a_boundary,
    test_multi_line_all_caps_and_bold_headings_are_still_boundaries,
    test_line_break_split_numbered_heading_is_still_a_boundary,
    test_boundary_text_reads_only_the_accept_all_view,
    test_mixed_run_children_inside_a_pending_tracked_change,
    test_hostile_or_malformed_sym_char_degrades_to_a_space,
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
