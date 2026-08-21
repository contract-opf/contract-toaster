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
  7. Attached-template sanitization — every .rels part is scanned for a
     relationship whose Type ends in /attachedTemplate (the Word template a
     document was DRAFTED FROM; carried by essentially every real-world
     .docx produced from a firm/organization template) and, together with
     the word/settings.xml <w:attachedTemplate r:id="..."/> element that
     references it by Id, it is STRIPPED from the archive rather than
     rejecting the whole upload — see docs/threat-model.md -> "Hostile file
     uploads". Unlike an external image, subdocument, or OLE link, an
     attached-template reference contributes nothing to document CONTENT,
     so removing it cannot silently change what the document says. The
     sanitization is recorded via audit_write. This step is a no-op
     (returns the original bytes, unchanged) for the overwhelmingly common
     case of a document with no attachedTemplate relationship at all.
  8. External-relationship / embedded-object / macro-template checks — the
     package relationships are scanned; external relationships (remote
     targets) and embedded OLE objects are rejected, and macro-enabled
     parts are rejected. (Attached templates were already sanitized in step
     7 above, so this step no longer sees them in the normal case; the
     rejection branch for that relationship type remains here only as a
     defense-in-depth fail-safe.)

A file that fails any check does not produce an approximate result: the
caller (run_upload_gauntlet) raises HostileFileError with a stable
reason_code, writes an audit row via the injected audit_write callable, and
never returns bytes to a caller that would hand them to the pipeline. The
FastAPI-facing entry point converts HostileFileError to an HTTPException via
to_http_exception() so the client gets a clear error (see
docs/threat-model.md: "the review transitions to a system error state and
is surfaced to the uploader as a rejected input, not as a legal decision").
The one exception to "rejects or passes through unchanged" is step 7 above:
a sanitized upload proceeds, but with different bytes than what was
uploaded — see run_upload_gauntlet's docstring for the exact contract.

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

import io
import re
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
# The one relationship type permitted to target an external resource. A
# hyperlink is inert until a human clicks it and never causes a network fetch
# when the document is parsed or opened, so — unlike an external image,
# subdocument, template, or OLE link — it does not violate the "no fetch at
# parse time" guarantee. See docs/threat-model.md -> Hostile file uploads.
HYPERLINK_RELATIONSHIP_SUFFIX = "/hyperlink"

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
            rel_type = rel.get("type", "")

            if rel.get("target_mode", "").lower() == "external":
                # Hyperlinks are the sole permitted external target: inert until
                # a human clicks them, they never fetch at parse/open time.
                # Every other external target (image, subdocument, template,
                # OLE link) is rejected.
                if not rel_type.endswith(HYPERLINK_RELATIONSHIP_SUFFIX):
                    raise HostileFileError(
                        reason_code="external_relationship",
                        detail=(
                            f"Relationship in '{rels_part}' targets an external "
                            f"resource: {rel.get('target', '<unknown>')!r}."
                        ),
                    )
            # Defense-in-depth only: run_upload_gauntlet calls
            # _sanitize_attached_template_relationships BEFORE this
            # function, which strips every attachedTemplate relationship
            # from the archive this function actually sees. In normal
            # operation this branch is therefore unreachable — it exists
            # so that if the sanitizer is ever bypassed or misses one,
            # the upload still fails closed instead of silently admitting
            # an unsanitized external template reference.
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
# Stage 7 (input side only, see module docstring) — attached-template
# sanitization.
#
# An attached-template relationship names the local Word template a
# document was drafted from (e.g. a firm's standard letterhead .dotx); it
# is carried by essentially every real-world .docx produced from an
# organization template and contributes nothing to document CONTENT. So,
# unlike every other relationship class this module rejects, it is safe to
# strip rather than refuse the whole upload over. Two places must agree:
# the .rels Relationship element AND the referencing
# word/settings.xml <w:attachedTemplate r:id="..."/> element (matched by
# Id) — removing only one leaves either an orphan relationship or a
# dangling r:id that can make Word show a repair prompt on open.
#
# NOTE: this is the INPUT-side gauntlet only. docs/threat-model.md's output
# OOXML scan (generated redlines) intentionally does NOT share this
# sanitizer — a generated .docx containing an attached-template reference
# would be a genuine defect in our own output pipeline, not a drafter's
# template, and must keep routing to ERROR_MANUAL_REVIEW_REQUIRED.
# ---------------------------------------------------------------------------

_RELS_MARKER = "/_rels/"

# Matches one whole <Relationship .../> element. Per the OPC spec a
# Relationship element is always empty (self-closing) — it never has child
# content — so this pattern alone is sufficient to isolate one element for
# surgical removal without touching any sibling element's bytes.
_RELATIONSHIP_ELEMENT_RE = re.compile(rb"<Relationship\b[^>]*?/>")
_RELATIONSHIP_ID_ATTR_RE = re.compile(rb'\bId\s*=\s*"([^"]*)"')
_RELATIONSHIP_TYPE_ATTR_RE = re.compile(rb'\bType\s*=\s*"([^"]*)"')

# Matches one whole <[prefix:]attachedTemplate .../> element, any namespace
# prefix (or none). CT_RelId carries no child content, so a producer writes
# this element either self-closed (`<w:attachedTemplate r:id="rId1"/>`, what
# Word itself emits) or in paired start/end-tag form
# (`<w:attachedTemplate r:id="rId1"></w:attachedTemplate>`, what an XML
# serializer that does not special-case empty elements emits). BOTH spellings
# are the same element and both must be matched: stripping the .rels side
# while leaving one of these behind is precisely the dangling r:id this
# module promises never to leave, and Word raises an "unreadable content"
# repair prompt on a document carrying one.
#
# The paired branch permits only whitespace between the tags -- CT_RelId has
# no child content, so anything else is not this element and must NOT be
# silently deleted (the residual-reference check in
# _sanitize_attached_template_relationships fails such a package closed
# instead).
_ATTACHED_TEMPLATE_ELEMENT_RE = re.compile(
    rb"<(?:[A-Za-z0-9_.]+:)?attachedTemplate\b[^>]*?"
    rb"(?:/>|>\s*</(?:[A-Za-z0-9_.]+:)?attachedTemplate\s*>)"
)
# The relationship-id attribute on that element is conventionally "r:id"
# (bound to the OOXML relationships namespace), but the prefix bound to
# that namespace is a per-document choice, not a fixed string — match any
# prefix (or none) on an "id" local name rather than hard-coding "r:id".
_GENERIC_ID_ATTR_RE = re.compile(rb'(?:^|\s)(?:[A-Za-z0-9_.]+:)?id\s*=\s*"([^"]*)"')


def _owner_part_for_rels_part(rels_part: str) -> str:
    """Map a .rels part name to the part it describes, per the OPC
    convention that "<dir>/_rels/<name>.rels" describes "<dir>/<name>" (and
    a root-level "_rels/<name>.rels" describes "<name>"). Returns "" if
    rels_part doesn't have that shape — callers treat "" as "no owner part
    to look in" rather than guessing."""
    idx = rels_part.rfind(_RELS_MARKER)
    if idx == -1:
        if not rels_part.startswith("_rels/"):
            return ""
        prefix, tail = "", rels_part[len("_rels/") :]
    else:
        prefix, tail = rels_part[:idx], rels_part[idx + len(_RELS_MARKER) :]
    if not tail.endswith(".rels"):
        return ""
    tail = tail[: -len(".rels")]
    return f"{prefix}/{tail}" if prefix else tail


def _find_attached_template_relationship_ids(rels_xml: bytes) -> set[str]:
    """Return the Id of every Relationship element in rels_xml whose Type
    ends in ATTACHED_TEMPLATE_RELATIONSHIP_SUFFIX — regardless of
    TargetMode, mirroring the unconditional match _check_relationships uses
    to reject on today."""
    ids: set[str] = set()
    for match in _RELATIONSHIP_ELEMENT_RE.finditer(rels_xml):
        element = match.group(0)
        type_match = _RELATIONSHIP_TYPE_ATTR_RE.search(element)
        if type_match is None:
            continue
        if not type_match.group(1).decode("utf-8").endswith(ATTACHED_TEMPLATE_RELATIONSHIP_SUFFIX):
            continue
        id_match = _RELATIONSHIP_ID_ATTR_RE.search(element)
        if id_match is not None:
            ids.add(id_match.group(1).decode("utf-8"))
    return ids


def _strip_relationship_elements_by_id(rels_xml: bytes, ids_to_strip: set[str]) -> bytes:
    """Remove each <Relationship .../> element whose Id is in ids_to_strip.
    Byte-level and surgical: every other Relationship element in the same
    part — including a coexisting external hyperlink — is left exactly as
    it was, and no other part is touched at all."""

    def _repl(match: re.Match[bytes]) -> bytes:
        element = match.group(0)
        id_match = _RELATIONSHIP_ID_ATTR_RE.search(element)
        if id_match is None:
            return element
        return b"" if id_match.group(1).decode("utf-8") in ids_to_strip else element

    return _RELATIONSHIP_ELEMENT_RE.sub(_repl, rels_xml)


def _strip_attached_template_elements_by_id(
    owner_xml: bytes, ids_to_strip: set[str]
) -> tuple[bytes, set[str]]:
    """Remove each <w:attachedTemplate .../> element whose id attribute is
    in ids_to_strip, leaving no dangling r:id behind. Returns the (possibly
    unchanged) bytes plus the subset of ids_to_strip that were actually
    found and removed — the caller uses this to tell "no matching element"
    apart from "matched and removed" for the audit record."""
    removed: set[str] = set()

    def _repl(match: re.Match[bytes]) -> bytes:
        element = match.group(0)
        id_match = _GENERIC_ID_ATTR_RE.search(element)
        if id_match is None:
            return element
        rid = id_match.group(1).decode("utf-8")
        if rid not in ids_to_strip:
            return element
        removed.add(rid)
        return b""

    new_xml = _ATTACHED_TEMPLATE_ELEMENT_RE.sub(_repl, owner_xml)
    return new_xml, removed


def _rewrite_zip_with_replacements(file_bytes: bytes, replacements: dict[str, bytes]) -> bytes:
    """Re-zip file_bytes with the given part replacements, preserving every
    OTHER entry's bytes, compression type, and archive order exactly — no
    part is dropped, [Content_Types].xml is untouched, and entries are not
    reordered. Never called on the no-replacement path; see
    _sanitize_attached_template_relationships, which returns the original
    bytes object unchanged when there is nothing to strip."""
    src = zipfile.ZipFile(io.BytesIO(file_bytes))
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w") as dst:
        for info in src.infolist():
            data = replacements.get(info.filename, None)
            if data is None:
                data = src.read(info.filename)
            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new_info.compress_type = info.compress_type
            new_info.external_attr = info.external_attr
            new_info.internal_attr = info.internal_attr
            new_info.create_system = info.create_system
            dst.writestr(new_info, data)
    return out_buf.getvalue()


def _sanitize_attached_template_relationships(
    file_bytes: bytes, zf: zipfile.ZipFile
) -> tuple[bytes, list[dict[str, Any]]]:
    """Strip every attachedTemplate relationship — and its referencing
    w:attachedTemplate element, matched by Id — from the package, in place
    of rejecting the whole upload for it (docs/threat-model.md -> Hostile
    file uploads). Every other relationship class is untouched here;
    _check_relationships (run by the caller immediately after this) still
    rejects those.

    Returns (file_bytes, []) — the SAME bytes object, not a re-zipped copy
    — when no .rels part declares an attachedTemplate relationship (the
    overwhelmingly common case). Otherwise returns (sanitized_bytes,
    records), one record per stripped relationship:
      {"rels_part", "owner_part", "relationship_id", "element_removed"}
    Records carry only part names, relationship ids, and a bool — never
    document text or the raw external Target path — so they are safe to
    hand to the audit trail unfiltered.

    Defense in depth: each replacement part is re-parsed with the same
    hardened parser used elsewhere in this module before being accepted,
    so a sanitizer bug that somehow produced malformed XML fails closed
    (HostileFileError) instead of shipping a broken .docx.
    """
    replacements: dict[str, bytes] = {}
    records: list[dict[str, Any]] = []
    part_names = set(zf.namelist())

    for rels_part in _iter_relationship_parts(zf):
        rels_xml = zf.read(rels_part)
        attached_ids = _find_attached_template_relationship_ids(rels_xml)
        if not attached_ids:
            continue

        replacements[rels_part] = _strip_relationship_elements_by_id(rels_xml, attached_ids)

        owner_part = _owner_part_for_rels_part(rels_part)
        owner_xml = None
        if owner_part and owner_part in part_names:
            owner_xml = replacements.get(owner_part, zf.read(owner_part))

        removed_from_owner: set[str] = set()
        if owner_xml is not None:
            new_owner_xml, removed_from_owner = _strip_attached_template_elements_by_id(
                owner_xml, attached_ids
            )
            if removed_from_owner:
                replacements[owner_part] = new_owner_xml
            # Fail closed rather than ship a dangling reference. Reaching
            # here means this .rels part DID declare an attachedTemplate
            # relationship (so it is being stripped), yet the owner part
            # still spells "attachedTemplate" somewhere after the removal
            # pass -- an element form _ATTACHED_TEMPLATE_ELEMENT_RE does not
            # recognize, or a reference to an id that never had a
            # relationship. Either way the stored/extracted document would
            # point at a relationship that no longer exists, and Word would
            # show the attorney a repair prompt on a file THIS pipeline
            # produced. A rejection is honest and visible; a silently
            # corrupted document is neither.
            if b"attachedTemplate" in new_owner_xml:
                raise HostileFileError(
                    reason_code="attached_template_sanitize_failed",
                    detail=(
                        f"Stripped an attachedTemplate relationship from "
                        f"'{rels_part}', but '{owner_part}' still references "
                        f"one -- refusing to store a document with a dangling "
                        f"relationship id."
                    ),
                )

        for rid in sorted(attached_ids):
            records.append(
                {
                    "rels_part": rels_part,
                    "owner_part": owner_part,
                    "relationship_id": rid,
                    "element_removed": rid in removed_from_owner,
                }
            )

    if not replacements:
        return file_bytes, []

    for part_name, new_bytes in replacements.items():
        try:
            _parse_xml_hardened(new_bytes)
        except HostileFileError as exc:
            raise HostileFileError(
                reason_code="attached_template_sanitize_failed",
                detail=(
                    f"Sanitizing the attached-template reference in "
                    f"'{part_name}' produced invalid XML: {exc.detail}"
                ),
            ) from exc

    sanitized_bytes = _rewrite_zip_with_replacements(file_bytes, replacements)
    return sanitized_bytes, records


def _write_sanitization_audit(
    audit_write: AuditWrite | None,
    *,
    review_id: str | None,
    filename: str,
    records: list[dict[str, Any]],
) -> None:
    """Write an audit row recording that an upload was SANITIZED — not
    rejected — before storage/extraction. The input-side counterpart to
    _write_rejection_audit: a sanitization is not a failure (the upload
    proceeds), but the stored artifact is not byte-identical to what the
    user uploaded, and an operator reading the audit trail must be able to
    tell that. detail is substance-free: part names, relationship ids, and
    whether a referencing element was found — never document text or the
    raw external template path.
    """
    if audit_write is None or not records:
        return
    parts_detail = "; ".join(
        f"{record['rels_part']} (rId={record['relationship_id']}, "
        f"owner={record['owner_part'] or '<none>'}, "
        f"element_removed={record['element_removed']})"
        for record in records
    )
    audit_write(
        action="upload_sanitized",
        review_id=review_id,
        filename=filename,
        reason_code="attached_template_stripped",
        detail=(
            f"Stripped {len(records)} attachedTemplate relationship(s) "
            f"before storage/extraction: {parts_detail}"
        ),
    )


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
      7. attached-template sanitization — runs only after every check above
         has established that the archive is safe to touch at all (not a
         bomb, not XXE, not macro-enabled). Strips any attachedTemplate
         relationship + its referencing w:attachedTemplate element in
         place of rejecting the upload for it.
      8. external-relationship / embedded-object / macro-template checks —
         run against the (possibly sanitized) archive from step 7, so an
         attachedTemplate relationship stripped in step 7 is no longer
         present to reject here.

    Returns file_bytes on success:
      - the ORIGINAL bytes object, unchanged, in the overwhelmingly common
        case where the archive contains no attachedTemplate relationship;
      - otherwise, SANITIZED bytes with every attachedTemplate relationship
        (and its referencing w:attachedTemplate element) stripped, and the
        sanitization recorded via audit_write (action="upload_sanitized").
        The caller MUST treat this returned value — not the bytes it
        passed in — as the artifact to store and to extract from; storing
        one and extracting the other is a provenance bug (the stored
        object would no longer match what was actually parsed).
    Raises HostileFileError on any OTHER failure and writes a rejection
    audit row via audit_write; never returns partially-validated bytes.
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

        # Sanitize BEFORE the relationship check below, so that a stripped
        # attachedTemplate relationship is no longer present to trip it.
        # Only reopens the archive (from the sanitized bytes) when a
        # sanitization record was actually produced — the no-op path never
        # pays for a second zip open.
        file_bytes, sanitization_records = _sanitize_attached_template_relationships(
            file_bytes, zf
        )
        if sanitization_records:
            zf = _open_zip_or_reject(file_bytes)

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

    _write_sanitization_audit(
        audit_write,
        review_id=review_id,
        filename=filename,
        records=sanitization_records,
    )
    return file_bytes
