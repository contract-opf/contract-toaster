#!/usr/bin/env python3
"""
Executable test for issue #117: a forged/corrupted ZIP central directory
crashes the upload route with a bare 500 instead of a HostileFileError.

`_check_zip_bomb_limits` (backend/src/upload_validation.py) only ever trusts
the central directory's DECLARED `file_size` for an entry -- by design, it
never inflates an entry to verify that declaration (that's the whole point
of computing the zip-bomb caps from metadata alone). A `.docx` whose
`word/document.xml` central-directory entry understates its real size
passes that ratio/size check, then fails later when something actually
reads the entry: Python's zipfile stops decompressing once it has produced
as many bytes as the (forged, too-small) declared size says to expect, so
the CRC it computes over that truncated output no longer matches the
entry's real CRC-32, and `zipfile.read()` raises `BadZipFile` ("Bad CRC-32
for file ..."). That exception was unhandled by run_upload_gauntlet's
try/except (which only caught HostileFileError), so it propagated as a bare
Starlette 500 with no HostileFileError, no reason code, and no
`upload_rejected` audit row -- violating the gauntlet's own contract that
every rejection is a HostileFileError with a reason code and an audit row.

The fix normalizes the WHOLE family of exceptions a failed `zf.read()` can
raise, so this file drives each arm with a real archive rather than a mock:

  * `zipfile.BadZipFile` -- the forged/understated `file_size` above;
  * `RuntimeError`       -- an entry flagged encrypted in the central
                            directory, which `zipfile` refuses to extract
                            without a password;
  * `zlib.error`         -- a corrupted raw deflate stream (`zlib.error`
                            inherits straight from `Exception`, so it is
                            neither of the two above).

It also pins the OTHER half of the contract: the normalization is scoped to
the part-read call itself, so an unrelated failure inside the gauntlet (an
AV-scanner invoke blowing up with `RuntimeError`, say) still surfaces as
itself and does NOT get laundered into a false `upload_rejected` row for a
file that is not hostile.

Follows the same third-party-stubbing convention as
tests/test_upload_hostile_file_gauntlet.py (no live AWS, no external AV
binary) so the suite runs in CI without extra installs.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import io
import struct
import sys
import types
import unittest
import zipfile
import zlib
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

# ---------------------------------------------------------------------------
# Fixture: a valid minimal .docx whose word/document.xml central-directory
# file_size is then forged to 2 x its real compress_size.
# ---------------------------------------------------------------------------

CONTENT_TYPES_WORDPROCESSINGML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.document.main+xml"/>'
    "</Types>"
)

# A long, highly-repetitive run of text so the entry actually compresses
# well (real compress_size << real file_size) -- this is what makes
# 2 x compress_size an UNDERSTATEMENT of the true decompressed size below,
# which is what triggers zipfile's truncate-then-CRC-mismatch path rather
# than a harmless no-op (an OVERSTATED declared size does not truncate the
# real decompression and therefore never trips this bug).
DOCUMENT_XML_LARGE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>"
    + ("Lorem ipsum dolor sit amet. " * 400)
    + "</w:t></w:r></w:p></w:body>"
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", RELS_XML_BENIGN)
        zf.writestr("word/document.xml", DOCUMENT_XML_LARGE)
    return buf.getvalue()


def _find_central_directory_header(data: bytearray, target_name: bytes) -> int:
    """Return the offset of the central-directory file header for
    `target_name`.

    Walks the raw central-directory file headers (signature `PK\\x01\\x02`)
    by hand rather than via zipfile, because zipfile has no supported way to
    write back a single tampered metadata field. The fixed part of that
    header is 46 bytes; the variable-length name/extra/comment fields whose
    lengths live at offsets 28/30/32 follow it.
    """
    idx = 0
    while True:
        idx = data.find(b"PK\x01\x02", idx)
        if idx == -1:
            raise ValueError(f"central directory entry for {target_name!r} not found")
        n_len = struct.unpack_from("<H", data, idx + 28)[0]
        m_len = struct.unpack_from("<H", data, idx + 30)[0]
        k_len = struct.unpack_from("<H", data, idx + 32)[0]
        if bytes(data[idx + 46 : idx + 46 + n_len]) == target_name:
            return idx
        idx += 46 + n_len + m_len + k_len


def _forge_central_directory_file_size(payload: bytes, target_name: bytes, new_size: int) -> bytes:
    """Rewrite the declared (central-directory) `file_size` field of the
    central-directory file header for `target_name` to `new_size`, leaving
    every other byte -- including the entry's actual compressed data and
    its CRC-32 -- untouched. This is exactly the "forged central directory"
    class this test targets: nothing about the real bytes changes, only
    what the central directory CLAIMS about them.
    """
    data = bytearray(payload)
    idx = _find_central_directory_header(data, target_name)
    # file_size is the 4-byte little-endian field at offset 24.
    struct.pack_into("<I", data, idx + 24, new_size)
    return bytes(data)


def _flag_entry_encrypted(payload: bytes, target_name: bytes) -> bytes:
    """Set bit 0 ("encrypted") of the general-purpose bit flag on
    `target_name`'s central-directory header.

    `zipfile` reads the flag from the central directory and refuses the
    entry outright when no password is supplied -- `RuntimeError("File %r
    is encrypted, password required for extraction")`, the second arm of
    the read-failure family. Real archives in this shape exist (any
    password-protected .docx); `zipfile` simply cannot WRITE one, so the
    flag is set by hand.
    """
    data = bytearray(payload)
    idx = _find_central_directory_header(data, target_name)
    # General-purpose bit flag is the 2-byte little-endian field at offset 8.
    flags = struct.unpack_from("<H", data, idx + 8)[0]
    struct.pack_into("<H", data, idx + 8, flags | 0x1)
    return bytes(data)


def _corrupt_deflate_stream(payload: bytes, target_name: bytes) -> bytes:
    """Corrupt the first byte of `target_name`'s raw deflate stream, leaving
    the declared sizes and the CRC-32 untouched.

    0x07 sets BFINAL=1 with BTYPE=0b11, a block type reserved by RFC 1951,
    so zlib rejects the stream on its very first block instead of producing
    wrong-but-decodable bytes. That makes this deterministic for any entry
    contents, and it raises `zlib.error` -- NOT `BadZipFile` -- which is the
    arm the two-type handler originally missed. Equivalent to ordinary
    in-transit corruption of a single byte.
    """
    data = bytearray(payload)
    idx = _find_central_directory_header(data, target_name)
    # Offset 42: relative offset of the entry's LOCAL file header. The local
    # header's fixed part is 30 bytes, then its own name/extra fields
    # (lengths at local offsets 26/28), then the compressed data.
    local = struct.unpack_from("<I", data, idx + 42)[0]
    n_len = struct.unpack_from("<H", data, local + 26)[0]
    x_len = struct.unpack_from("<H", data, local + 28)[0]
    data_start = local + 30 + n_len + x_len
    data[data_start] = 0x07
    return bytes(data)


def _build_docx_with_forged_document_xml_size() -> bytes:
    """A valid minimal docx whose word/document.xml central-directory
    file_size is patched to 2 x its real compress_size -- understating the
    entry's true decompressed size, which is what reproduces the
    CRC-mismatch crash (see module docstring)."""
    payload = _build_valid_docx()
    zf = zipfile.ZipFile(io.BytesIO(payload))
    info = zf.getinfo("word/document.xml")
    forged_size = info.compress_size * 2
    assert forged_size < info.file_size, (
        "fixture sanity: the forged size must understate the real "
        "decompressed size, or this test does not reproduce the bug"
    )
    return _forge_central_directory_file_size(payload, b"word/document.xml", forged_size)


class _FakeAuditSink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __call__(self, **kwargs) -> None:
        self.rows.append(kwargs)


class _FakeAvClient:
    def __init__(self, verdict: str = "CLEAN") -> None:
        self.verdict = verdict

    def scan(self, file_bytes: bytes) -> str:
        return self.verdict


class _ExplodingAvClient:
    """An AV client whose scanner invoke fails -- an INFRASTRUCTURE fault,
    not a hostile file. Mirrors the real client's `.scan(bytes) -> str`
    protocol; it just never gets to return a verdict."""

    MESSAGE = "scanner lambda invoke failed: connection reset by peer"

    def scan(self, file_bytes: bytes) -> str:
        raise RuntimeError(self.MESSAGE)


DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# The single user-facing string every arm of the read-failure family maps
# to. Deliberately cause-NEUTRAL: the same clause catches a forged declared
# size, an encrypted entry and a corrupt deflate stream, so it must not
# claim a specific cause (an earlier draft said "its declared size does not
# match its actual contents", which is simply false for the other two).
EXPECTED_DETAIL = "This archive entry could not be read, so the upload was rejected."


class TestForgedCentralDirectory(unittest.TestCase):
    def test_forged_central_directory_size_rejected_with_audit_row(self) -> None:
        payload = _build_docx_with_forged_document_xml_size()
        av = _FakeAvClient()
        audit = _FakeAuditSink()

        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="forged.docx",
                declared_content_type=(
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ),
                av_client=av,
                audit_write=audit,
                review_id="rev-117",
            )

        self.assertEqual(ctx.exception.reason_code, "zip_entry_corrupt")
        self.assertEqual(ctx.exception.detail, EXPECTED_DETAIL)
        self.assertEqual(len(audit.rows), 1)
        self.assertEqual(audit.rows[0]["action"], "upload_rejected")
        self.assertEqual(audit.rows[0]["reason_code"], "zip_entry_corrupt")
        self.assertEqual(audit.rows[0]["review_id"], "rev-117")
        self.assertEqual(audit.rows[0]["filename"], "forged.docx")

        # The gauntlet's own contract (module docstring, #63 AC): a failed
        # validation raises HostileFileError and is mapped to a client
        # HTTPException, never a bare 500.
        http_exc = uv.to_http_exception(ctx.exception)
        self.assertEqual(http_exc.status_code, 400)
        # Escaped/fixed copy only: the client never sees the underlying
        # zipfile message (which names the offending part).
        self.assertEqual(http_exc.detail, EXPECTED_DETAIL)
        self.assertNotIn("CRC", http_exc.detail)
        self.assertNotIn("word/document.xml", http_exc.detail)

    def test_encrypted_entry_rejected_with_audit_row(self) -> None:
        """The RuntimeError arm: an entry flagged encrypted in the central
        directory. zipfile refuses to extract it without a password and
        raises RuntimeError -- which must land on the same reason code and
        audit row as the BadZipFile arm, not escape as a bare 500."""
        payload = _flag_entry_encrypted(_build_valid_docx(), b"word/document.xml")

        # Pin the fixture to the exception type this arm exists for, so the
        # test cannot quietly start exercising a different arm.
        with self.assertRaises(RuntimeError):
            zipfile.ZipFile(io.BytesIO(payload)).read("word/document.xml")

        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="encrypted.docx",
                declared_content_type=DOCX_CONTENT_TYPE,
                av_client=_FakeAvClient(),
                audit_write=audit,
                review_id="rev-117c",
            )

        self.assertEqual(ctx.exception.reason_code, "zip_entry_corrupt")
        self.assertEqual(ctx.exception.detail, EXPECTED_DETAIL)
        self.assertEqual(len(audit.rows), 1)
        self.assertEqual(audit.rows[0]["action"], "upload_rejected")
        self.assertEqual(audit.rows[0]["reason_code"], "zip_entry_corrupt")
        self.assertEqual(audit.rows[0]["review_id"], "rev-117c")
        self.assertEqual(audit.rows[0]["filename"], "encrypted.docx")
        self.assertEqual(uv.to_http_exception(ctx.exception).status_code, 400)

    def test_corrupt_deflate_stream_rejected_with_audit_row(self) -> None:
        """The zlib.error arm: a single corrupted byte in the raw deflate
        stream, declared sizes and CRC left correct. zlib.error inherits
        straight from Exception -- it is neither BadZipFile nor RuntimeError
        -- so a handler naming only those two lets it escape as the very
        bare-500-with-no-audit-row defect #117 exists to close."""
        payload = _corrupt_deflate_stream(_build_valid_docx(), b"word/document.xml")

        with self.assertRaises(zlib.error):
            zipfile.ZipFile(io.BytesIO(payload)).read("word/document.xml")
        # Guard the premise of this test rather than assuming it: zlib.error
        # really is outside the other two arms.
        self.assertNotIsInstance(zlib.error(), (zipfile.BadZipFile, RuntimeError))

        audit = _FakeAuditSink()
        with self.assertRaises(uv.HostileFileError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="corrupt-stream.docx",
                declared_content_type=DOCX_CONTENT_TYPE,
                av_client=_FakeAvClient(),
                audit_write=audit,
                review_id="rev-117d",
            )

        self.assertEqual(ctx.exception.reason_code, "zip_entry_corrupt")
        self.assertEqual(ctx.exception.detail, EXPECTED_DETAIL)
        self.assertEqual(len(audit.rows), 1)
        self.assertEqual(audit.rows[0]["action"], "upload_rejected")
        self.assertEqual(audit.rows[0]["reason_code"], "zip_entry_corrupt")
        self.assertEqual(audit.rows[0]["review_id"], "rev-117d")
        self.assertEqual(audit.rows[0]["filename"], "corrupt-stream.docx")
        self.assertEqual(uv.to_http_exception(ctx.exception).status_code, 400)

    def test_av_scanner_runtime_error_is_not_laundered_into_a_rejection(self) -> None:
        """The normalization must be scoped to the part-read call, not hung
        on the gauntlet's whole body.

        A RuntimeError out of the AV client is an infrastructure fault on a
        perfectly VALID, uncorrupted .docx. If the handler sat on the
        function body it would relabel that as reason_code
        "zip_entry_corrupt" and write an `upload_rejected` row -- a false
        hostile-file record in the very audit trail #117 exists to protect,
        for a file that was never even scanned. It must surface as itself,
        with no audit row at all."""
        payload = _build_valid_docx()
        audit = _FakeAuditSink()

        with self.assertRaises(RuntimeError) as ctx:
            uv.run_upload_gauntlet(
                payload,
                filename="valid.docx",
                declared_content_type=DOCX_CONTENT_TYPE,
                av_client=_ExplodingAvClient(),
                audit_write=audit,
                review_id="rev-117e",
            )

        self.assertNotIsInstance(ctx.exception, uv.HostileFileError)
        self.assertEqual(str(ctx.exception), _ExplodingAvClient.MESSAGE)
        self.assertEqual(audit.rows, [])

    def test_forged_size_alone_does_not_reproduce_without_understating(self) -> None:
        """Sanity check on the fixture itself: an OVERSTATED declared size
        does not truncate the real decompression and so must NOT trip this
        bug -- confirming the failure mode really is "declared size too
        small", not "any size field being non-default"."""
        payload = _build_valid_docx()
        zf = zipfile.ZipFile(io.BytesIO(payload))
        info = zf.getinfo("word/document.xml")
        overstated = _forge_central_directory_file_size(
            payload, b"word/document.xml", info.file_size + 10_000
        )
        av = _FakeAvClient()
        audit = _FakeAuditSink()

        # Must not raise at all: the real decompression completes and its
        # CRC still matches, regardless of what the central directory
        # over-claims about the size.
        result = uv.run_upload_gauntlet(
            overstated,
            filename="overstated.docx",
            declared_content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            av_client=av,
            audit_write=audit,
            review_id="rev-117b",
        )
        self.assertIsInstance(result, bytes)
        self.assertEqual(audit.rows, [])


def _run_suite() -> bool:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (TestForgedCentralDirectory,):
        suite.addTests(loader.loadTestsFromTestCase(test_case))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    ok = _run_suite()
    sys.exit(0 if ok else 1)
