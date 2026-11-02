"""Shared, dependency-free OOXML builders for the W7 document-handling tests
(QA remediation 2026-10-31, area 4). Not a test file itself (no `test_`
prefix), so neither runner collects it.

Every string any caller passes in is fabricated. The builders write a
minimal but real WordprocessingML package -- content types, package and
document relationships, a styles part (with the two hidden styles the
style-inherited `w:vanish` tests use) and `word/document.xml` -- so each
test reads exactly the parts a real upload carries.
"""

from __future__ import annotations

import io
import zipfile

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f'xmlns:w="{W_NS}"'

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)
PACKAGE_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)
DOCUMENT_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/></Relationships>'
)
#: `Ghost` (a character style) and `GhostP` (a paragraph style) both carry
#: `w:vanish` -- text formatted with either is hidden in Word without any
#: `w:vanish` on the run itself.
STYLES = (
    f'<?xml version="1.0" encoding="UTF-8"?><w:styles {W}>'
    '<w:style w:type="character" w:styleId="Ghost"><w:name w:val="Ghost"/>'
    "<w:rPr><w:vanish/></w:rPr></w:style>"
    '<w:style w:type="character" w:styleId="GhostChild"><w:name w:val="GhostChild"/>'
    '<w:basedOn w:val="Ghost"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="GhostP"><w:name w:val="GhostP"/>'
    "<w:rPr><w:vanish/></w:rPr></w:style>"
    "</w:styles>"
)


def docx(body_xml: str, *, styles: str | None = STYLES, root_attrs: str = W) -> bytes:
    """A package whose `<w:body>` is `body_xml` followed by an empty
    `<w:sectPr/>`. `root_attrs` replaces the root's namespace declarations,
    for the tests that bind WordprocessingML to another prefix."""
    prefix = "w"
    if root_attrs != W:
        prefix = root_attrs.split("=", 1)[0].split(":", 1)[1]
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<{prefix}:document {root_attrs}><{prefix}:body>{body_xml}<{prefix}:sectPr/>"
        f"</{prefix}:body></{prefix}:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", PACKAGE_RELS)
        if styles is not None:
            zf.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
            zf.writestr("word/styles.xml", styles)
        zf.writestr("word/document.xml", doc)
    return buf.getvalue()


def r(text: str, rpr: str = "") -> str:
    props = f"<w:rPr>{rpr}</w:rPr>" if rpr else ""
    return f'<w:r>{props}<w:t xml:space="preserve">{text}</w:t></w:r>'


def p(inner: str, ppr: str = "") -> str:
    props = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    return f"<w:p>{props}{inner}</w:p>"


def heading(text: str) -> str:
    return p(r(text), '<w:pStyle w:val="Heading1"/>')


def replace_in_block(stage, bt, rba, docx_bytes: bytes, target: str, replacement: str) -> dict:
    """Prove and compile ONE edit -- `target` -> `replacement` -- against the
    block whose text carries `target`, through the real spine
    (`build_block_map` -> `validate_block_patches` -> `apply_block_transcript`),
    and re-extract the accept-all view of the result.

    Returns `{"proven", "result", "accepted_text", "expected_text"}`."""
    norm = stage.extract_and_normalize(docx_bytes)
    assert norm["status"] == "normalized", norm
    block_map = stage.build_block_map(norm["paragraphs"])
    block_id = next(bid for bid, block in block_map.items() if target in block["text"])
    text = block_map[block_id]["text"]
    index = text.index(target)
    segments = []
    if text[:index]:
        segments.append({"op": "keep", "text": text[:index]})
    segments.append({"op": "delete", "text": target, "issue_key": "k1"})
    if replacement:
        segments.append({"op": "insert", "text": replacement, "issue_key": "k1"})
    if text[index + len(target) :]:
        segments.append({"op": "keep", "text": text[index + len(target) :]})
    proven = bt.validate_block_patches([{"block_id": block_id, "segments": segments}], [], block_map)
    out = {"proven": proven, "result": None, "accepted_text": None}
    out["expected_text"] = text[:index] + replacement + text[index + len(target) :]
    if proven["status"] != "proven":
        return out
    result = rba.apply_block_transcript(
        docx_bytes, proven, author="Synthetic Reviewer", timestamp_iso="2026-01-01T00:00:00Z"
    )
    out["result"] = result
    if result["docx_bytes"] is not None:
        accepted = stage.extract_and_normalize(stage.materialize_accept_all(result["docx_bytes"]))
        if accepted["status"] == "normalized":
            out["accepted_text"] = stage.build_block_map(accepted["paragraphs"])[block_id]["text"]
    return out
