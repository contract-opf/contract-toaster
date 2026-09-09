#!/usr/bin/env python3
"""
Parse, validate, and classify a playbook-version upload -- issue #478.

## Why this exists

Before this module, `backend/src/main.py::post_admin_playbook_version_upload`
hashed the uploaded bytes and recorded `{playbook_id, version, content_hash,
status:"draft", ...}` via `src.playbook_versions.record_playbook_version_upload`
-- and dropped the bytes. Nothing was parsed, validated, or persisted; an
admin got "draft recorded" for a file that might not even be JSON.

This module wires up the ingestion stack that already existed and was
already tested but unused: `scripts/opf_load.py::load_opf_document` (OPF 0.2/
0.3 schema dispatch, `identity.content_hash` verification, injection scan,
`.opf.html` bundle extraction via `scripts/opf_html.py`) and
`scripts/playbook_validation.py::validate_playbook_document` (the legacy v1
`playbooks/schema.json` path).

## Detection + dispatch (issue #478 "What to build", steps 1-2)

  - `.opf.html` / `.html` / `.htm`  -> always OPF, via
    `opf_load.load_opf_document` (which extracts the embedded canonical JSON
    via `scripts/opf_html.py` internally).
  - `.json` carrying a top-level `opf_version` key -> OPF, via the same
    loader (bare-JSON form).
  - `.json` with no `opf_version` key -> the legacy v1 playbook path, via
    `playbook_validation.validate_playbook_document`.
  - Anything else (unrecognized extension) is refused outright.

`load_opf_document(..., require_identity=True)` is the upload-path contract:
a document with no `identity.content_hash` is refused, exactly like any
other OPF validation failure -- this module never loosens that.

## Spec-normative minimums (issue #676)

`_load_opf_from_bytes` additionally refuses an OPF document that omits a
field the OPF spec's own prose calls mandatory but the JSON Schema's
`required` list does not enforce -- today exactly one, `perspective`. The
list lives in ONE named place, `OPF_UPLOAD_MINIMUMS` below, with the
requirement's source and the field's purpose stated on each entry (both
surfaced to the operator on rejection). Because the check sits in
`_load_opf_from_bytes` rather than in `validate_playbook_upload`, it fires
at upload AND at review time -- `pipeline_runner._load_opf_bundle_if_active`
re-validates the stored artifact through the same function on every review
-- so an artifact activated before the gate existed is caught too, with no
migration.

## Agreement-type match + stub-basis watermark (steps 4-5)

OPF uploads additionally must have their `agreement_type` (id or any alias)
match the target `playbook_id` (`opf_load.agreement_type_keys`), and a
`compiler.stub_basis_present` document is refused unless the caller passes
`accept_stub_basis=True` -- mirrors `scripts/review_knowledge.py`'s
`accept_stub_basis` gate for the same watermark at review-composition time.
Neither check applies to the legacy v1 path: v1 playbooks carry no
`agreement_type`/`compiler` block at all.

## No document content in errors

Every error this module raises carries a `PlaybookUploadRejected` message
that is safe to return to an HTTP caller verbatim: the OPF loader's own
messages are already JSON-Pointer/value-free (scripts/opf_load.py's own
"No document content in errors" discipline). The legacy v1 path is
different: `playbook_validation.validate_playbook_document`'s schema-failure
branch builds its message from raw `jsonschema.ValidationError.message`,
which DOES embed the offending instance value verbatim (e.g. a `pattern`
mismatch echoes the bad string; `additionalProperties` echoes the extra
key's own name) -- exactly the value-in-errors leak scripts/opf_load.py's
own docstring warns about. `_describe_v1_schema_error` below strips that
back down to a JSON-Pointer + validator-name message (mirroring
`opf_load._describe`) before it is ever wrapped in a `PlaybookUploadRejected`,
so what actually reaches the HTTP caller carries only schema-defined
identifiers (topic ids, section refs), a bare pointer/validator name, or --
for the our_standard covering-topic branch -- a COUNT of covering anchors
(`playbook_validation.describe_missing_standard_text`, sanitized in fix
round 2, finding 4: `section_anchors` entries carry no schema `pattern`,
unlike `id`, so the raw anchor strings themselves are never safe to
surface). Never the raw field values pulled from the uploaded document.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# scripts/ import seam -- the same idempotent sys.path insertion every other
# backend/src module that reaches into scripts/ uses (see
# backend/src/bundle_authoring.py, backend/src/reviews.py,
# backend/src/pipeline_runner.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import opf_canonicalize  # noqa: E402
import opf_load  # noqa: E402
import playbook_validation  # noqa: E402

# Same try/except + helpful-error convention as scripts/opf_load.py -- this
# module reaches into raw jsonschema.ValidationError attributes (via
# `playbook_validation.PlaybookValidationError.__cause__`) in
# `_describe_v1_schema_error` below, so it needs the same dependency
# `playbook_validation.validate_playbook_document` already requires at
# runtime (backend/requirements.txt / requirements-dev.txt).
try:
    import jsonschema
except ImportError as _exc:  # pragma: no cover - dev dependency
    raise ImportError(
        "playbook_upload.py requires jsonschema (requirements-dev.txt). "
        "Activate the project venv and `pip install -r requirements-dev.txt`."
    ) from _exc

ARTIFACT_KIND_V1 = "v1"


class PlaybookUploadRejected(Exception):
    """Raised when an uploaded playbook artifact fails detection, parsing,
    schema validation, agreement-type matching, or the stub-basis watermark
    check. The message is safe to surface to an HTTP caller verbatim -- see
    module docstring's "No document content in errors"."""


@dataclass
class ValidatedPlaybookUpload:
    """The result of a successful `validate_playbook_upload` call.

    `storage_text` is what gets persisted at the content-addressed storage
    key -- the parsed document re-serialized deterministically (issue #478
    step 3: "store the extracted canonical JSON text for HTML bundles"). For
    a bare `.opf.json` or legacy v1 upload the parsed document IS the
    uploaded content, so this is just its canonical re-serialization; for a
    `.opf.html` bundle it is the EXTRACTED canonical JSON, never the
    surrounding HTML envelope.
    """

    artifact_kind: str
    doc: dict[str, Any]
    is_opf: bool
    opf_content_hash: str | None
    accepted_stub_basis: bool
    storage_text: str
    source_was_html: bool


def _opf_artifact_kind(opf_version: Any) -> str:
    return f"opf-{opf_version}"


# ---------------------------------------------------------------------------
# Spec-normative minimums (issue #676)
# ---------------------------------------------------------------------------
#
# THE ONE named place for "an OPF document this deployment refuses even
# though the JSON Schema would accept it". Issue #676 is explicit about the
# failure mode to avoid: this list quietly growing into a second,
# undocumented schema. So the rules are:
#
#   - every entry states WHERE its requirement comes from (spec prose vs a
#     product decision) and WHAT the field is for, and both strings are
#     surfaced to the operator on rejection -- a gate nobody can act on is a
#     support ticket;
#   - nothing goes in here that the schema's own `required` list already
#     enforces (that would be duplication), and nothing goes in here without
#     a stated reason on the entry itself.
#
# Today the list is exactly one field. Every top-level property of
# `playbooks/opf/playbook.schema-0.3.json` AND `...-0.2.json` was scanned
# for normative language (must/required/shall) in its description that the
# schema's `required` list does NOT back, and `perspective` was the only
# match in either (issue #676). That scan is not a claim to trust: it is
# re-run against the schema files themselves by
# tests/test_playbook_minimums_676.py::TestSchemaPremise, so a schema bump
# that promotes `perspective` into `required` -- or that adds a second
# normative-but-optional field -- turns that test red rather than leaving a
# silent duplicate, or a silent gap, here.


@dataclass(frozen=True)
class OpfMinimum:
    """One spec-normative minimum enforced beyond the JSON Schema.

    `defect` returns a short, VALUE-FREE phrase describing what is wrong
    with the document (or None when the minimum is satisfied) -- it must
    never echo a value pulled out of the uploaded document, per the module
    docstring's "No document content in errors".
    """

    field: str
    requirement_source: str
    purpose: str
    defect: Callable[[dict[str, Any]], str | None]


def _perspective_defect(doc: dict[str, Any]) -> str | None:
    """Value-free description of what is missing from `/perspective`, or
    None when it names a principal.

    Both the absent-block and the blank-string cases are defects: the
    schema requires `party` and `counterparty_type` to be present strings
    WHEN the block exists, but `""` is a string, so a document can satisfy
    the schema and still name nobody. Since the entire point of the field
    is to say who "us" is, a blank value fails the requirement exactly as
    an absent block does.
    """
    perspective = doc.get("perspective")
    if perspective is None:
        return "the document carries no top-level 'perspective' block."
    if not isinstance(perspective, dict):
        return "the document's top-level 'perspective' is not an object."
    blank = [
        name
        for name in ("party", "counterparty_type")
        if not str(perspective.get(name) or "").strip()
    ]
    if blank:
        return (
            "the document's 'perspective' block leaves "
            + " and ".join(f"'{name}'" for name in blank)
            + " empty."
        )
    return None


OPF_UPLOAD_MINIMUMS: tuple[OpfMinimum, ...] = (
    OpfMinimum(
        field="perspective",
        # SOURCE: spec prose. `playbooks/opf/playbook.schema-0.3.json` (and
        # -0.2.json, identically) describes /perspective as "Whose
        # perspective this playbook is reviewed 'as' -- an open-standard OPF
        # instance must say who 'us' is", but omits it from the schema's own
        # `required` list, so nothing enforced it.
        requirement_source=(
            "the OPF schema's own /perspective description ('an open-standard "
            "OPF instance must say who \"us\" is') -- normative prose the "
            "schema's `required` list does not enforce"
        ),
        # WHY IT IS ENFORCED: without a principal the review has no side to
        # be on. Issue #675 measured the consequence on a live model run
        # against the perspective-less production playbook: a fluent
        # surgical redline drafted FOR the counterparty.
        purpose=(
            "'perspective.party' names our legal entity for this agreement type "
            "and 'perspective.counterparty_type' names what the other side "
            "typically is; without them a review has no principal and can draft "
            "a redline against the operator's own side"
        ),
        defect=_perspective_defect,
    ),
)


def _check_opf_minimums(doc: dict[str, Any]) -> None:
    """Enforce `OPF_UPLOAD_MINIMUMS` against a schema-valid OPF document.

    Raises `PlaybookUploadRejected` with an operator-facing message naming
    the field, what is wrong, what the field is for, and where the
    requirement comes from -- deliberately NOT a generic schema error
    (issue #676). Carries no document content, like every other message
    this module raises.
    """
    for minimum in OPF_UPLOAD_MINIMUMS:
        defect = minimum.defect(doc)
        if defect is None:
            continue
        raise PlaybookUploadRejected(
            f"OPF minimums check failed at /{minimum.field}: {defect} "
            f"Required because {minimum.requirement_source}. "
            f"What it is for: {minimum.purpose}. "
            f"Recompile the playbook with a '{minimum.field}' block and upload "
            "it again."
        )


def _load_opf_from_bytes(contents: bytes, *, suffix: str) -> dict[str, Any]:
    """Write *contents* to a throwaway temp file, run it through
    `opf_load.load_opf_document`, and enforce `OPF_UPLOAD_MINIMUMS` on the
    result. Reuses the tested, Path-based loader (schema dispatch,
    `identity.content_hash` verification, the injection scan, sibling-id
    uniqueness) rather than re-implementing any of that against an
    in-memory API this module was never meant to duplicate.

    Issue #676: the minimums check lives HERE, not in
    `validate_playbook_upload`, because this function is the one seam BOTH
    surfaces already share -- the two admin upload routes in
    `backend/src/main.py`, and `pipeline_runner._load_opf_bundle_if_active`,
    which re-validates the stored artifact through it on every review
    rather than trusting the activated row. One check therefore also covers
    an artifact activated BEFORE the gate existed, with no migration and no
    separate review-path gate.

    Raises `opf_load.OpfValidationError` for a schema/identity/injection
    failure (callers catch it and re-wrap) and `PlaybookUploadRejected` for
    a minimums failure -- the latter is already safe to surface verbatim,
    so callers pass it straight through as a 4xx.
    """
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=suffix, delete=False, dir=None
        ) as tmp:
            tmp.write(contents)
            tmp_path = Path(tmp.name)
        doc = opf_load.load_opf_document(tmp_path, require_identity=True)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    _check_opf_minimums(doc)
    return doc


def _check_agreement_type_match(doc: dict[str, Any], playbook_id: str) -> None:
    keys = opf_load.agreement_type_keys(doc)
    if playbook_id.lower() not in keys:
        raise PlaybookUploadRejected(
            "OPF validation failed at /agreement_type: this document's "
            f"agreement_type (id + aliases) does not include the target "
            f"playbook_id {playbook_id!r}."
        )


def _check_stub_basis(doc: dict[str, Any], accept_stub_basis: bool) -> bool:
    """Returns whether the stub-basis watermark was present AND accepted.
    Raises PlaybookUploadRejected if present and not accepted -- mirrors
    scripts/review_knowledge.py's resolve_knowledge stub-basis gate."""
    compiler = doc.get("compiler") or {}
    if compiler.get("stub_basis_present") is not True:
        return False
    if not accept_stub_basis:
        raise PlaybookUploadRejected(
            "opf.compiler.stub_basis_present is True: this playbook artifact was "
            "compiled from judgment stubs -- structurally valid but SEMANTICALLY "
            "BLANK. Pass accept_stub_basis=true to record the upload anyway, or "
            "upload a playbook compiled against its corpus."
        )
    return True


def _finish_opf(
    doc: dict[str, Any],
    *,
    playbook_id: str,
    accept_stub_basis: bool,
    source_was_html: bool,
) -> ValidatedPlaybookUpload:
    _check_agreement_type_match(doc, playbook_id)
    accepted_stub_basis = _check_stub_basis(doc, accept_stub_basis)

    identity = doc.get("identity") or {}
    opf_content_hash = identity.get("content_hash")
    if not isinstance(opf_content_hash, str):
        opf_content_hash = None

    return ValidatedPlaybookUpload(
        artifact_kind=_opf_artifact_kind(doc.get("opf_version")),
        doc=doc,
        is_opf=True,
        opf_content_hash=opf_content_hash,
        accepted_stub_basis=accepted_stub_basis,
        storage_text=opf_canonicalize.canonicalize(doc),
        source_was_html=source_was_html,
    )


def validate_playbook_upload(
    *,
    filename: str,
    contents: bytes,
    playbook_id: str,
    accept_stub_basis: bool = False,
) -> ValidatedPlaybookUpload:
    """Detect the artifact kind, validate it, and (for OPF) enforce the
    agreement-type match and stub-basis watermark -- issue #478 "What to
    build" steps 1-2, 4-5.

    Dispatch (by filename, per the issue):
      - `.opf.html` / `.html` / `.htm` -> OPF, via `opf_load.load_opf_document`.
      - `.json` with a top-level `opf_version` key -> OPF, same loader.
      - `.json` with no `opf_version` key -> legacy v1
        (`playbook_validation.validate_playbook_document`).
      - anything else -> refused.

    Raises `PlaybookUploadRejected` (safe to surface to the HTTP caller
    verbatim) on any failure. Never returns a partially-validated result.
    """
    lower_name = (filename or "").lower()
    is_html = (
        lower_name.endswith(".opf.html")
        or lower_name.endswith(".html")
        or lower_name.endswith(".htm")
    )
    is_json = lower_name.endswith(".json")

    if not is_html and not is_json:
        raise PlaybookUploadRejected(
            f"Unrecognized playbook upload filename {filename!r}: expected a "
            ".json (OPF or legacy v1) or .opf.html/.html (OPF bundle) file."
        )

    if is_html:
        try:
            doc = _load_opf_from_bytes(contents, suffix=".opf.html")
        except opf_load.OpfValidationError as exc:
            raise PlaybookUploadRejected(str(exc)) from exc
        except UnicodeDecodeError as exc:
            raise PlaybookUploadRejected(
                f"Upload is not valid UTF-8 text: {exc}"
            ) from exc
        return _finish_opf(
            doc,
            playbook_id=playbook_id,
            accept_stub_basis=accept_stub_basis,
            source_was_html=True,
        )

    # is_json
    try:
        parsed = json.loads(contents.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise PlaybookUploadRejected(f"Upload is not valid UTF-8 text: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PlaybookUploadRejected(f"Upload is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PlaybookUploadRejected("Upload is not a JSON object.")

    if "opf_version" in parsed:
        try:
            doc = _load_opf_from_bytes(contents, suffix=".json")
        except opf_load.OpfValidationError as exc:
            raise PlaybookUploadRejected(str(exc)) from exc
        return _finish_opf(
            doc,
            playbook_id=playbook_id,
            accept_stub_basis=accept_stub_basis,
            source_was_html=False,
        )

    # Legacy v1.
    try:
        playbook_validation.validate_playbook_document(parsed, playbook_id=playbook_id)
    except playbook_validation.PlaybookValidationError as exc:
        raise PlaybookUploadRejected(_describe_v1_schema_error(exc)) from exc

    return ValidatedPlaybookUpload(
        artifact_kind=ARTIFACT_KIND_V1,
        doc=parsed,
        is_opf=False,
        opf_content_hash=None,
        accepted_stub_basis=False,
        storage_text=json.dumps(parsed, sort_keys=True, separators=(",", ":")),
        source_was_html=False,
    )


def _describe_v1_schema_error(exc: playbook_validation.PlaybookValidationError) -> str:
    """Value-free description of a legacy-v1 `PlaybookValidationError`,
    safe to surface to an HTTP caller (see module docstring's "No document
    content in errors").

    `playbook_validation.validate_playbook_document` raises this exception
    from two different branches:

      - Schema-validation failure: `raise PlaybookValidationError(...) from
        exc`, where `exc` is the raw `jsonschema.ValidationError` -- and
        `exc.message` (baked into that branch's own message text) embeds
        the offending instance value verbatim. We rebuild a JSON-Pointer +
        validator-name message from `exc.__cause__` instead (mirroring
        `opf_load._describe`'s discipline), never touching `exc.message` or
        `exc.instance`. The one instance-derived detail `opf_load._describe`
        itself allows -- the list of missing property names for a
        `required` failure -- comes from the schema's own `required` array,
        not from the document, so it is safe the same way there.
      - The our_standard covering-topic check: raised directly, no `from`
        clause, so `__cause__` is None. Its message
        (`describe_missing_standard_text`) carries the topic's `id`
        (schema `pattern`-constrained kebab-case, safe) and `section_ref`
        (a schema-defined display label), plus a COUNT of the topic's
        covering `section_anchors` -- never the anchor strings themselves.
        `section_anchors` entries carry no schema `pattern` (unlike `id`),
        so `describe_missing_standard_text` was sanitized (fix round 2,
        finding 4) to report only `len(real_anchors)`, matching the
        discipline the schema branch above already followed for
        `required`'s missing-property names.
    """
    cause = exc.__cause__
    if isinstance(cause, jsonschema.ValidationError):
        pointer = "/".join(str(p) for p in cause.absolute_path)
        location = pointer if pointer else "<root>"
        if cause.validator == "required":
            required = cause.validator_value if isinstance(cause.validator_value, list) else []
            instance = cause.instance if isinstance(cause.instance, dict) else {}
            missing = [name for name in required if name not in instance]
            if missing:
                noun = "property" if len(missing) == 1 else "properties"
                return (
                    f"playbook document failed schema validation at {location}: "
                    f"missing required {noun} {missing}"
                )
            return (
                f"playbook document failed schema validation at {location}: "
                "missing a required property"
            )
        return (
            f"playbook document failed schema validation at {location}: "
            f"failed the {cause.validator!r} check"
        )
    return str(exc)


def storage_key_for(playbook_id: str, storage_hash_hex: str) -> str:
    """The content-addressed S3 key a validated upload's `storage_text` is
    written to -- issue #478 step 3: `playbooks/{playbook_id}/{content_hash}.json`."""
    return f"playbooks/{playbook_id}/{storage_hash_hex}.json"


def _original_artifact_suffix(filename: str) -> str:
    """The filename suffix `original_artifact_key` preserves for the raw
    uploaded bytes -- one of the extensions `validate_playbook_upload`
    itself dispatches on, so this never has to guess at a kind
    `validate_playbook_upload` didn't already recognize."""
    lower = (filename or "").lower()
    if lower.endswith(".opf.html"):
        return ".opf.html"
    if lower.endswith(".html") or lower.endswith(".htm"):
        return ".html"
    if lower.endswith(".opf.json"):
        return ".opf.json"
    if lower.endswith(".json"):
        return ".json"
    return ""


def original_artifact_key(playbook_id: str, content_hash_hex: str, *, filename: str) -> str:
    """The (non-content-addressed-by-canonical-text) key the ORIGINAL
    uploaded bytes are persisted at, for EVERY artifact kind -- issue #478
    step 3: "...plus the original artifact". Keyed by the raw upload's own
    hash (already computed by the caller over the exact bytes it is about
    to write) so it never collides with the canonical-text key above, and so
    the row's own `content_hash` (also the hash of the raw uploaded bytes)
    addresses a real, retrievable object: `GET` this key and rehash to
    reproduce `content_hash` verbatim, for an `.opf.html` bundle, a bare
    `.opf.json`, or a legacy v1 `.json` alike.
    """
    suffix = _original_artifact_suffix(filename)
    return f"playbooks/{playbook_id}/{content_hash_hex}.original{suffix}"
