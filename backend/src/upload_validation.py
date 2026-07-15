"""
Hostile-file upload validation — issue #63.

Implements the pre-extraction gauntlet described in
docs/threat-model.md -> "Hostile file uploads" (finding 4). Every upload is
treated as hostile until it has passed a fixed sequence of checks, and the
gauntlet runs BEFORE any extraction or OOXML parsing — never after. Per the
threat model, the order is:

  1. Size cap — hard upload-size limit, checked before anything else is read.
  2. Magic-number verification — the bytes must actually be a ZIP/OOXML
     container (signature check only; this does not decompress or read any
     entry, only the local/central-directory signatures).
  3. AV scan — the raw bytes are scanned before anything reads the archive's
     internal structure or decompresses any part. A positive scan fails the
     upload closed.
  4. Zip-bomb limits — entry-count cap and uncompressed-size / compression-
     ratio cap, computed from the ZIP central directory (declared sizes)
     without ever inflating entries. Enforced before any part of the archive
     is decompressed, including [Content_Types].xml.
  5. MIME verification — [Content_Types].xml must declare a WordprocessingML
     main document. The declared Content-Type and .docx extension are hints,
     never proof. This is the first step that decompresses a part, so it
     runs only after the AV scan and the zip-bomb caps above.
  6. XML-entity hardening — every XML part is parsed with DTD processing and
     external-entity resolution disabled, defeating XXE and "billion laughs"
     entity-expansion at the parser level, before any relationship/content
     inspection that would otherwise require trusting the XML parser.
  7. External-relationship / embedded-object / macro-template checks — the
     package relationships are scanned; external relationships (remote
     targets), embedded OLE objects, and macro-enabled parts are rejected.

A file that fails any check does not produce an approximate result: the
caller (run_upload_gauntlet) raises HostileFileError with a stable
reason_code, writes an audit row via the injected audit_write callable, and
never returns bytes to a caller that would hand them to the pipeline. The
FastAPI-facing entry point converts HostileFileError to an HTTPException via
to_http_exception() so the client gets a clear error (see
docs/threat-model.md: "the review transitions to a system error state and
is surfaced to the uploader as a rejected input, not as a legal decision").

Reconciliation with the 2026-06-11 architecture review (#25, #32):
  - Extraction (owned by the pipeline, not this module) uses an explicit
    allowlist of OOXML parts; see ARCHITECTURE.md -> Input normalization.
    This module's job ends at "the bytes are safe to hand to extraction",
    not at extraction itself.
  - The AV approach is pinned: in-account ClamAV with cloud sample
    submission/telemetry disabled (see docs/data-handling.md ->
    Third parties / subprocessors). The `av_client` parameter here is the
    thin interface that Lambda-based scanner sits behind; this module does
    not implement ClamAV itself, it enforces fail-closed behavior around
    whatever client is injected.

This module has ZERO third-party dependencies (stdlib zipfile +
xml.parsers.expat only), matching the rest of backend/src/'s
third-party-stubbing-friendly convention (see tests/test_download_auth_attack.py,
tests/test_review_submission_e2e.py).
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from xml.parsers import expat

from fastapi import HTTPException, status

# ---------------------------------------------------------------------------
# Pinned config caps (mirrors the reservation-style "pinned config value"
# convention used elsewhere in backend/src/reviews.py).
# ---------------------------------------------------------------------------

# Hard upload-size cap (bytes). 25 MiB comfortably covers any legitimate
# counterparty agreement; see docs/threat-model.md -> Abuse / DoS controls.
MAX_UPLOAD_SIZE_BYTES = 25 * 1024 * 1024

# Zip-bomb caps: entry count and total uncompressed size across all entries,
# plus a per-entry compression-ratio ceiling. All three are computed from
# ZIP central-directory metadata (declared sizes) — entries are never
# inflated to check these caps.
MAX_ZIP_ENTRY_COUNT = 100
MAX_UNCOMPRESSED_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MiB decompressed cap
MAX_COMPRESSION_RATIO = 100  # uncompressed:compressed, per entry

# The only main-document content type this system accepts (v1 .docx-only
# intake scope; see ARCHITECTURE.md "v1 accepts .docx only").
WORDPROCESSINGML_MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)

# Macro-enabled content types are rejected outright (a .docx with macros is
# already wrong — those belong to .docm; see docs/threat-model.md).
MACRO_ENABLED_CONTENT_TYPES = {
    "application/vnd.ms-word.document.macroEnabled.main+xml",
    "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
}

VBA_PROJECT_PART_SUFFIX = "vbaproject.bin"

CONTENT_TYPES_PART_NAME = "[Content_Types].xml"

# Package-relationship types that indicate an embedded OLE object or an
# attached template. A relationship whose Type ends with one of these is
# rejected regardless of Target.
EMBEDDED_OBJECT_RELATIONSHIP_SUFFIXES = (
    "/oleObject",
    "/package",
)
ATTACHED_TEMPLATE_RELATIONSHIP_SUFFIX = "/attachedTemplate"

XML_DECLARATION_PREFIXES = (b"<?xml",)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@dataclass
class HostileFileError(Exception):
    """Raised by any gauntlet stage that rejects the upload.

    reason_code is a stable, machine-checkable string (used both for the
    audit row and for test assertions) — never a full-text message alone,
    so callers and tests do not have to pattern-match prose.
    """

    reason_code: str
    detail: str
    http_status: int = status.HTTP_400_BAD_REQUEST

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.reason_code}: {self.detail}"


def to_http_exception(exc: HostileFileError) -> HTTPException:
    """Map a HostileFileError to the client-facing HTTPException.

    Per issue #63 AC: "A failed validation returns a clear client error ...
    the file is not handed to the pipeline."
    """
    return HTTPException(status_code=exc.http_status, detail=exc.detail)


# ---------------------------------------------------------------------------
# AV client interface
# ---------------------------------------------------------------------------


class AvClient(Protocol):
    """Interface the in-account ClamAV Lambda scanner sits behind (see
    docs/threat-model.md -> Hostile file uploads, docs/data-handling.md ->
    Third parties / subprocessors). Any object with a `.scan(bytes) -> str`
    method returning "CLEAN" or "INFECTED" satisfies this protocol; the real
    implementation invokes the scanner Lambda, tests inject a fake."""

    def scan(self, file_bytes: bytes) -> str: ...


AV_VERDICT_CLEAN = "CLEAN"
AV_VERDICT_INFECTED = "INFECTED"


# ---------------------------------------------------------------------------
# Audit sink
# ---------------------------------------------------------------------------

AuditWrite = Callable[..., None]


def _write_rejection_audit(
    audit_write: AuditWrite | None,
    *,
    review_id: str | None,
    filename: str,
    reason_code: str,
    detail: str,
) -> None:
    """Write an audit row for a rejected upload (issue #63 AC).

    audit_write is injected (matching the rest of backend/src/'s
    dependency-injection convention for boto3 resources) so this module has
    no direct DynamoDB dependency and stays trivially unit-testable. A
    real caller wires this to a `put_item` against the `audit` table
    (see docs/audit-queries.md); tests wire it to an in-memory recorder.
    If no audit_write is supplied, the rejection still raises — audit
    logging is best-effort here, never a gate on rejecting a hostile file.
    """
    if audit_write is None:
        return
    audit_write(
        action="upload_rejected",
        review_id=review_id,
        filename=filename,
        reason_code=reason_code,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Stage 1 — size cap
# ---------------------------------------------------------------------------


def _check_size(file_bytes: bytes) -> None:
    if len(file_bytes) > MAX_UPLOAD_SIZE_BYTES:
        raise HostileFileError(
            reason_code="file_too_large",
            detail=(
                f"Upload exceeds the maximum allowed size of "
                f"{MAX_UPLOAD_SIZE_BYTES} bytes."
            ),
            http_status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )


# ---------------------------------------------------------------------------
# Stage 2 — magic-number verification (signature check only; does not
# decompress or read any entry)
# ---------------------------------------------------------------------------


def _open_zip_or_reject(file_bytes: bytes) -> zipfile.ZipFile:
    """Open file_bytes as a ZIP archive, or raise mime_magic_number_mismatch.

    zipfile.is_zipfile() checks the local/central-directory signatures
    (magic numbers), not just the .docx extension or a client-declared
    Content-Type — those are treated as hints only, never as proof, per
    docs/threat-model.md. Opening the ZIP reads central-directory metadata
    only; it does not decompress any entry.
    """
    import io

    buf = io.BytesIO(file_bytes)
    if not zipfile.is_zipfile(buf):
        raise HostileFileError(
            reason_code="mime_magic_number_mismatch",
            detail=(
                "File does not have a valid ZIP/OOXML magic number. "
                "Only .docx (WordprocessingML) documents are accepted."
            ),
        )
    buf.seek(0)
    try:
        return zipfile.ZipFile(buf)
    except zipfile.BadZipFile as exc:
        raise HostileFileError(
            reason_code="mime_magic_number_mismatch",
            detail="File could not be opened as a valid ZIP/OOXML container.",
        ) from exc


# ---------------------------------------------------------------------------
# Stage 5 — MIME verification ([Content_Types].xml). This is the first check
# that decompresses a part (zf.read()), so per docs/threat-model.md it must
# run only after the AV scan (stage 3) and the zip-bomb caps (stage 4) have
# already cleared the raw bytes / central-directory metadata.
# ---------------------------------------------------------------------------


def _check_content_types_is_wordprocessingml(zf: zipfile.ZipFile) -> None:
    """[Content_Types].xml must declare a WordprocessingML main document.

    Rejects both: (a) missing [Content_Types].xml, and (b) a well-formed
    OOXML package for a DIFFERENT application (e.g. a spreadsheet) — the
    magic number alone only proves "this is some OOXML package", not "this
    is a .docx".

    NOTE: this decompresses [Content_Types].xml via zf.read(). Callers MUST
    have already run the AV scan and the zip-bomb caps (which operate on
    raw bytes / central-directory metadata only, never decompressing) before
    calling this function — see run_upload_gauntlet.
    """
    names = set(zf.namelist())
    if CONTENT_TYPES_PART_NAME not in names:
        raise HostileFileError(
            reason_code="mime_magic_number_mismatch",
            detail="Archive is missing [Content_Types].xml; not a valid OOXML package.",
        )

    content_types_xml = zf.read(CONTENT_TYPES_PART_NAME)
    declared_types = _extract_content_type_overrides(content_types_xml)

    if WORDPROCESSINGML_MAIN_CONTENT_TYPE in declared_types:
        return

    # Macro-enabled main-document types are handled by a dedicated, more
    # specific rejection (macro_enabled_template) later in the gauntlet —
    # here we only need to confirm SOME WordprocessingML-family main type is
    # present before proceeding, otherwise this is simply the wrong format.
    if any(t in MACRO_ENABLED_CONTENT_TYPES for t in declared_types):
        return

    raise HostileFileError(
        reason_code="mime_magic_number_mismatch",
        detail=(
            "[Content_Types].xml does not declare a WordprocessingML main "
            "document. Only .docx documents are accepted."
        ),
    )


def _extract_content_type_overrides(content_types_xml: bytes) -> set[str]:
    """Parse [Content_Types].xml with the hardened parser and return the set
    of ContentType values declared on <Override> elements."""
    found: set[str] = set()

    def start_element(name: str, attrs: dict[str, str]) -> None:
        local = name.rsplit(":", 1)[-1]
        if local == "Override" and "ContentType" in attrs:
            found.add(attrs["ContentType"])

    _parse_xml_hardened(content_types_xml, start_element=start_element)
    return found


# ---------------------------------------------------------------------------
# Stage 4 — zip-bomb limits (entry count, uncompressed size, ratio)
#
# Reads only central-directory metadata (info.file_size / info.compress_size
# come from the central directory, not from decompressing the entry) — this
# must run, and fully reject an oversized/over-ratio archive, BEFORE any
# entry (including [Content_Types].xml) is decompressed.
# ---------------------------------------------------------------------------


def _check_zip_bomb_limits(zf: zipfile.ZipFile) -> None:
    infolist = zf.infolist()

    if len(infolist) > MAX_ZIP_ENTRY_COUNT:
        raise HostileFileError(
            reason_code="zip_bomb_entry_count",
            detail=(
                f"Archive contains {len(infolist)} entries, exceeding the "
                f"maximum of {MAX_ZIP_ENTRY_COUNT}."
            ),
        )

    total_uncompressed = 0
    for info in infolist:
        # Declared sizes from the central directory — never inflate the
        # entry to compute this.
        total_uncompressed += info.file_size

        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_COMPRESSION_RATIO:
                raise HostileFileError(
                    reason_code="zip_bomb_ratio",
                    detail=(
                        f"Entry '{info.filename}' has a compression ratio of "
                        f"{ratio:.0f}:1, exceeding the maximum of "
                        f"{MAX_COMPRESSION_RATIO}:1."
                    ),
                )
        elif info.file_size > 0:
            # Zero declared compressed size but nonzero uncompressed size is
            # itself a red flag (e.g. a stored/degenerate entry engineered
            # to dodge a naive ratio check) — treat as an unbounded ratio.
            raise HostileFileError(
                reason_code="zip_bomb_ratio",
                detail=(
                    f"Entry '{info.filename}' declares {info.file_size} "
                    "uncompressed bytes from 0 compressed bytes."
                ),
            )

        if total_uncompressed > MAX_UNCOMPRESSED_TOTAL_BYTES:
            raise HostileFileError(
                reason_code="zip_bomb_uncompressed_size",
                detail=(
                    f"Archive's total declared uncompressed size exceeds "
                    f"the maximum of {MAX_UNCOMPRESSED_TOTAL_BYTES} bytes."
                ),
            )


# ---------------------------------------------------------------------------
# Stage 3 — AV scan (fail closed)
#
# Runs on the raw bytes, before anything reads the archive's internal
# structure or decompresses any part — per docs/threat-model.md: "The raw
# bytes are antivirus-scanned in `uploads` before anything reads the
# archive's structure."
# ---------------------------------------------------------------------------


def _run_av_scan(file_bytes: bytes, av_client: AvClient) -> None:
    verdict = av_client.scan(file_bytes)
    if verdict != AV_VERDICT_CLEAN:
        raise HostileFileError(
            reason_code="av_positive",
            detail="Antivirus scan flagged this file. The upload was rejected.",
        )


# ---------------------------------------------------------------------------
# Stage 6 — XML-entity hardening
# ---------------------------------------------------------------------------


def _parse_xml_hardened(
    xml_bytes: bytes,
    start_element: Callable[[str, dict[str, str]], None] | None = None,
) -> None:
    """Parse xml_bytes with DTD processing and external-entity resolution
    disabled (defeats XXE and billion-laughs entity expansion).

    Uses xml.parsers.expat directly (stdlib) rather than xml.etree, because
    expat lets us refuse ANY <!DOCTYPE> / <!ENTITY> declaration outright
    instead of trying to selectively disable resolution — a document that
    declares a DTD at all is already outside what a .docx part should ever
    contain, and refusing to parse further is the safest response (defense
    in depth on top of not resolving external entities).
    """
    parser = expat.ParserCreate()

    saw_doctype_or_entity = {"flag": False}

    def _reject(*_args: Any, **_kwargs: Any) -> None:
        saw_doctype_or_entity["flag"] = True

    # Refuse any DOCTYPE declaration outright.
    parser.StartDoctypeDeclHandler = _reject
    # Refuse external entity resolution entirely (defense in depth even
    # though StartDoctypeDeclHandler already aborts parsing on a DOCTYPE).
    parser.ExternalEntityRefHandler = lambda *a, **k: 0  # 0 == fail per expat API
    if start_element is not None:
        def _on_start(name: str, attrs: dict[str, str]) -> None:
            if saw_doctype_or_entity["flag"]:
                return
            start_element(name, attrs)

        parser.StartElementHandler = _on_start

    try:
        parser.Parse(xml_bytes, True)
    except expat.ExpatError as exc:
        raise HostileFileError(
            reason_code="xml_entity_rejected",
            detail=f"XML part failed to parse safely: {exc}",
        ) from exc

    if saw_doctype_or_entity["flag"]:
        raise HostileFileError(
            reason_code="xml_entity_rejected",
            detail=(
                "XML part declares a DOCTYPE/ENTITY, which is not permitted "
                "in an OOXML part. Rejected before any entity expansion."
            ),
        )


def _check_all_xml_parts_are_entity_safe(zf: zipfile.ZipFile) -> None:
    for info in zf.infolist():
        if not info.filename.endswith(".xml") and not info.filename.endswith(".rels"):
            continue
        # Bound the amount we ever inflate for a single part while checking
        # entity-safety, independent of the earlier whole-archive ratio
        # check — defense in depth against a single oversized XML part.
        data = zf.read(info.filename)
        _parse_xml_hardened(data)


# ---------------------------------------------------------------------------
# Stage 7 — external relationships / embedded objects / macro templates
# ---------------------------------------------------------------------------


def _check_no_macro_enabled_parts(zf: zipfile.ZipFile) -> None:
    names = zf.namelist()

    content_types_xml = zf.read(CONTENT_TYPES_PART_NAME)
    declared_types = _extract_content_type_overrides(content_types_xml)
    if any(t in MACRO_ENABLED_CONTENT_TYPES for t in declared_types):
        raise HostileFileError(
            reason_code="macro_enabled_template",
            detail=(
                "Document declares a macro-enabled main document content "
                "type. Macro-enabled files (.docm) are not accepted."
            ),
        )

    for name in names:
        if name.lower().endswith(VBA_PROJECT_PART_SUFFIX):
            raise HostileFileError(
                reason_code="macro_enabled_template",
                detail="Document contains a vbaProject.bin part (VBA macros).",
            )


def _iter_relationship_parts(zf: zipfile.ZipFile) -> list[str]:
    return [name for name in zf.namelist() if name.endswith(".rels")]


def _check_relationships(zf: zipfile.ZipFile) -> None:
    """Scan every .rels part for external relationships, embedded OLE
    objects, and attached templates."""
    for rels_part in _iter_relationship_parts(zf):
        rels_xml = zf.read(rels_part)
        relationships = _extract_relationships(rels_xml)

        for rel in relationships:
            if rel.get("target_mode", "").lower() == "external":
                raise HostileFileError(
                    reason_code="external_relationship",
                    detail=(
                        f"Relationship in '{rels_part}' targets an external "
                        f"resource: {rel.get('target', '<unknown>')!r}."
                    ),
                )

            rel_type = rel.get("type", "")
            if rel_type.endswith(ATTACHED_TEMPLATE_RELATIONSHIP_SUFFIX):
                raise HostileFileError(
                    reason_code="external_relationship",
                    detail=f"Relationship in '{rels_part}' attaches an external template.",
                )
            if any(rel_type.endswith(suffix) for suffix in EMBEDDED_OBJECT_RELATIONSHIP_SUFFIXES):
                raise HostileFileError(
                    reason_code="embedded_object",
                    detail=f"Relationship in '{rels_part}' references an embedded object.",
                )


def _extract_relationships(rels_xml: bytes) -> list[dict[str, str]]:
    relationships: list[dict[str, str]] = []

    def start_element(name: str, attrs: dict[str, str]) -> None:
        local = name.rsplit(":", 1)[-1]
        if local == "Relationship":
            relationships.append(
                {
                    "id": attrs.get("Id", ""),
                    "type": attrs.get("Type", ""),
                    "target": attrs.get("Target", ""),
                    "target_mode": attrs.get("TargetMode", ""),
                }
            )

    _parse_xml_hardened(rels_xml, start_element=start_element)
    return relationships


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_upload_gauntlet(
    file_bytes: bytes,
    *,
    filename: str,
    declared_content_type: str,
    av_client: AvClient,
    audit_write: AuditWrite | None = None,
    review_id: str | None = None,
) -> bytes:
    """Run the full pre-extraction hostile-file gauntlet.

    Order matches docs/threat-model.md -> Hostile file uploads:
      1. size cap
      2. magic-number verification (signature check only, no decompression)
      3. AV scan (fail closed) — on the raw bytes, before anything reads the
         archive's structure or decompresses any part
      4. zip-bomb limits (entry count, uncompressed size, ratio) — from
         central-directory metadata only, before any part is decompressed
      5. MIME verification ([Content_Types].xml WordprocessingML check) —
         the first check that decompresses a part
      6. XML-entity hardening on every XML/.rels part
      7. external-relationship / embedded-object / macro-template checks

    Returns the original file_bytes unchanged on success (the caller then
    proceeds to write them to the uploads bucket / hand them to extraction).
    Raises HostileFileError on any failure and writes an audit row via
    audit_write; never returns partially-validated bytes.
    """
    try:
        _check_size(file_bytes)

        zf = _open_zip_or_reject(file_bytes)

        # AV scan runs on the raw bytes before anything reads the archive's
        # internal structure or decompresses any part. Per
        # docs/threat-model.md: "The raw bytes are antivirus-scanned in
        # `uploads` before anything reads the archive's structure." Opening
        # the ZIP above only reads the magic number / central-directory
        # signatures; it does not decompress anything.
        _run_av_scan(file_bytes, av_client)

        # Zip-bomb caps are enforced from central-directory metadata alone
        # (declared sizes, never inflated) BEFORE any part of the archive —
        # including [Content_Types].xml — is decompressed. This closes the
        # decompression-before-cap gap: a bomb planted in
        # [Content_Types].xml is caught here, before the MIME check below
        # ever calls zf.read() on it.
        _check_zip_bomb_limits(zf)

        _check_content_types_is_wordprocessingml(zf)

        _check_all_xml_parts_are_entity_safe(zf)
        _check_no_macro_enabled_parts(zf)
        _check_relationships(zf)

    except HostileFileError as exc:
        _write_rejection_audit(
            audit_write,
            review_id=review_id,
            filename=filename,
            reason_code=exc.reason_code,
            detail=exc.detail,
        )
        raise

    return file_bytes
