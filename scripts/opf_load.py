#!/usr/bin/env python3
"""
OPF loader/validator -- issue #283, extended for the OPF 0.3 launch and cut
over to OPF 0.5 ONLY by issue #128.

Loads an Open Playbook Format (OPF) document, validates it against the VENDORED
schema (playbooks/opf/playbook.schema-0.5.json), verifies its
`identity.content_hash`, runs the
fail-closed prompt-injection scan, and matches its `agreement_type` to a
registered playbook_id (scripts/playbook_registry.py) via `agreement_type.id`/
`aliases`.

## Two upload forms

`load_opf_document` accepts BOTH shapes an engine-derived playbook travels in:

  - `playbook.opf.html` -- the PRIMARY form. The engine has consolidated on this
    single-file bundle as its ONE distribution artifact: a human-readable
    document that wraps the canonical OPF JSON and the digest in
    `<script type="application/json">` blocks (scripts/opf_html.py). The
    embedded canonical JSON is extracted, then validated exactly like a bare
    document -- the bare JSON is what the bundle *contains*, so the two paths
    converge immediately and share one validation core.
  - a bare `.opf.json` document -- secondary, still supported (and still what
    `bind_bundle` reads off disk).

For the upload path the `identity` block is REQUIRED and its `content_hash` must
match the hash recomputed over the document's own canonical serialization
(scripts/opf_canonicalize.py) -- otherwise the upload is rejected. Verifying
only *when identity happens to be present* would fail open: stripping `identity`
would silently disable the integrity check and let arbitrary playbook content
through. `load_opf` (the internal/bare loader used by bind) keeps the softer
verify-if-present contract for backward compatibility; `bind_bundle` requires
`identity` on its own.

## One format: OPF 0.5 / digest_version 4 (issue #128)

The owner's 2026-10-06 direction: this repo supports exactly ONE format,
`opf_version` "0.5" with `digest_version` "4" -- no backward compatibility and
no dispatch by version (there are zero installed copies to stay compatible
with). The 0.2/0.3 schemas, the digest-2 reader and every 0.3 path are gone.

Both versions are still CHECKED, and checked BEFORE schema validation, because
the check names the cause instead of a symptom:

  - `opf_version`: anything but "0.5" (absent, non-string, "0.3", "0.4") is
    refused with OpfVersionError rather than validated against a schema it was
    never written for -- which would fail at some field deep inside the
    document and describe a symptom, not the cause. Never defaulted: a
    document that declares a version we do not vendor a schema for has not
    been validated by anything, and guessing would be a fail-open.
  - `digest.digest_version`: a SEPARATE version governing the digest section's
    shape and SELECTION SEMANTICS. Anything but "4" is refused with
    OpfDigestVersionError. The 0.5 schema pins the const too, but schema
    validation is not a substitute: a version can change what the data MEANS
    without changing its shape (what the lists include, how entries are
    ranked or capped), and no schema check can see that. Reading a digest
    whose selection semantics we do not understand would silently mis-weight
    precedent, which is worse than refusing to run.

## posture.rubric, curation

Not consumed here, deliberately. The vendored schema does not accept
`posture.rubric` (engine #178) or the retired top-level `curation` section
(engine #233) -- both objects are `additionalProperties: false` -- so a
document carrying either FAILS schema validation like any other unrecognized
property. This module needs no special-case for them: `load_opf` already
raises on any schema violation.

## No document content in errors

`OpfValidationError` messages carry a JSON Pointer (RFC 6901) to the failing
location and, for a missing-required-property failure, the SCHEMA's own
property name(s) -- never a value pulled from the document being validated.
A raw `jsonschema.ValidationError.message` can embed the offending instance
value verbatim (e.g. "'Acme Corp' is not of type 'array'"), which could be
confidential contract text; this module never surfaces that string
(no-substance-in-logs discipline -- see ARCHITECTURE.md).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_canonicalize  # noqa: E402
import opf_html  # noqa: E402
import opf_injection_scan  # noqa: E402
import playbook_registry  # noqa: E402

# Same try/except + helpful-error convention as scripts/primary_review_pass.py:82-90.
try:
    import jsonschema
except ImportError as _exc:  # pragma: no cover - dev dependency, see requirements-dev.txt
    raise ImportError(
        "opf_load.py requires jsonschema (requirements-dev.txt). "
        "Activate the project venv and `pip install -r requirements-dev.txt`."
    ) from _exc

OPF_SCHEMA_DIR = REPO_ROOT / "playbooks" / "opf"

# The ONE format this repo reads (issue #128; see "One format" above). The
# schema is vendored byte-for-byte from playbook-engine and pinned in
# tests/test_opf_schema_sync.py (playbooks/opf/README.md has the procedure).
OPF_VERSION = "0.5"
OPF_SCHEMA_PATH = OPF_SCHEMA_DIR / "playbook.schema-0.5.json"

# The ONE digest_version this repo reads. Digest 4 is the verdict-free
# precedent projection `scripts/opf_prompt.py` renders: per clause our
# standard (as parts), the opened/kept counts, the positions by `vs_standard`
# label, the signed variants with their provenance counts, the refused asks
# and the openings not signed as proposed.
DIGEST_VERSION = "4"

_SCHEMA_CACHE: dict[str, dict] = {}


class OpfValidationError(ValueError):
    """Raised when an OPF document fails schema validation.

    The message carries a JSON Pointer to the failing location and never
    the document's own content -- see module docstring.
    """


class OpfInjectionError(OpfValidationError):
    """Raised when `opf_injection_scan.scan_untrusted_playbook_text` finds
    one or more hardcoded prompt-injection patterns in an OPF document's
    model-bound text fields (issue #346).

    Fail closed: an OPF document that trips the scan never loads. The
    message lists rule_ids + json_paths only -- NEVER the matched text
    (same leakage discipline as OpfValidationError / floor_judge.py /
    leakage_scan.py). This is a tripwire for casual/known injection
    patterns, NOT a security boundary against a determined adversary --
    see scripts/opf_injection_scan.py's module docstring.
    """


class OpfVersionError(OpfValidationError):
    """Raised when `opf_version` is missing, non-string, or anything but
    OPF_VERSION ("0.5"). Never defaulted: an unvalidated document must not
    load."""


class OpfHashMismatchError(OpfValidationError):
    """Raised when `identity.content_hash` does not match the hash recomputed
    over the document's own canonical serialization -- i.e. the bytes changed
    after the engine compiled them.

    Also raised, on the upload path, when `identity` is absent entirely:
    skipping verification for a document that simply omits the block would let
    an attacker disable the integrity check by deleting it.
    """


class OpfDigestVersionError(OpfValidationError):
    """Raised when a document's `digest.digest_version` is anything but
    DIGEST_VERSION ("4").

    Separate from OpfVersionError because the two versions are independent:
    the digest's shape and selection semantics are versioned on their own.
    Rejecting is the fail-closed choice -- reading a digest whose selection
    semantics we do not understand would silently mis-weight precedent, which
    is worse than refusing to run.
    """


class OpfExtractError(OpfValidationError):
    """Raised when a `.opf.html` bundle carries no single, parseable embedded
    canonical OPF JSON block (wraps opf_html.OpfHtmlExtractError)."""


class OpfDuplicateIdError(OpfValidationError):
    """Raised when an OPF document repeats an id within one of the four
    sibling sets the engine's validator makes normative for OPF 0.5:
    `evidence.clauses[].id`, `evidence.precedent[].id`,
    `floor.invariants[].id`, and `corpus.documents[].document_id`.

    Parity with the playbook-engine's fail-closed validator rules
    (playbook_engine/validator.py::_check_duplicate_ids for clauses,
    invariants and documents; `_check_precedent` for precedent ids, which
    the engine requires unique since OPF 0.4): sibling-id uniqueness is a
    blocking rule the JSON Schema cannot express, so schema validation
    alone cannot catch it -- a document the engine refuses to produce
    would otherwise load here. Undetected, the id-keyed lookups the runtime
    relies on (digest `clauses[].id` <-> evidence ids, digest
    `precedent_ids` <-> evidence precedent ids, floor invariant ids for
    floor_refs and the floor judge) would silently resolve to the first
    match instead of failing loudly.

    The message names the JSON Pointer to the offending sibling array,
    including the index of the duplicate occurrence -- value-free and
    sufficient on its own to locate the problem. The vendored schema does
    NOT constrain these four id fields (`precedentClause.id`,
    `precedent.id`, `floor.invariants[].id`, `corpus.documents[].document_id`
    are each bare `{"type": "string"}`, with no `pattern` or `maxLength` --
    unlike `agreement_type.id`, which the schema does pin to
    `^[a-z0-9-]+$`). That means an id here is arbitrary, unbounded,
    uploader-controlled text, and this check runs BEFORE the injection
    scan (see `_validate_doc`), so it must not assume the value is safe to
    echo. The id is only ever included in the message when it independently
    matches `^[a-z0-9_-]+$` and is within the length cap (see
    `_safe_id_repr`); otherwise it is omitted and the pointer/index is all
    that's shown, preserving the no-document-content-in-errors discipline
    (module docstring).
    """


def _load_schema() -> dict:
    if OPF_VERSION not in _SCHEMA_CACHE:
        with open(OPF_SCHEMA_PATH, encoding="utf-8") as f:
            _SCHEMA_CACHE[OPF_VERSION] = json.load(f)
    return _SCHEMA_CACHE[OPF_VERSION]


def _json_pointer(path_segments: Any) -> str:
    """RFC 6901 JSON Pointer for a jsonschema ValidationError.absolute_path.

    The empty deque (failure located at the document root, e.g. a missing
    top-level required property) maps to the RFC 6901 root pointer "".
    """
    segments = list(path_segments)
    if not segments:
        return ""
    escaped = [str(seg).replace("~", "~0").replace("/", "~1") for seg in segments]
    return "/" + "/".join(escaped)


def _describe(exc: "jsonschema.ValidationError") -> str:  # noqa: UP037
    pointer = _json_pointer(exc.absolute_path)
    location = pointer if pointer else "'' (document root)"
    if exc.validator == "required":
        instance = exc.instance if isinstance(exc.instance, dict) else {}
        required = exc.validator_value if isinstance(exc.validator_value, list) else []
        missing = [name for name in required if name not in instance]
        if missing:
            noun = "property" if len(missing) == 1 else "properties"
            return f"OPF validation failed at {location}: missing required {noun} {missing}"
        return f"OPF validation failed at {location}: missing a required property"
    return f"OPF validation failed at {location}: failed the '{exc.validator}' check"


def _describe_injection(findings: list[dict]) -> str:
    """Render injection-scan findings as rule_ids + json_paths only --
    never the matched text (see OpfInjectionError docstring)."""
    parts = [f"{f['rule_id']} at {f['json_path']}" for f in findings]
    return "OPF injection scan failed (" + str(len(parts)) + " finding(s)): " + "; ".join(parts)


def resolve_opf_version(doc: dict) -> str:
    """Return the document's `opf_version`, or raise OpfVersionError.

    Never defaults -- see module docstring ("One format").
    """
    version = doc.get("opf_version")
    if version != OPF_VERSION:
        raise OpfVersionError(
            "OPF validation failed at /opf_version: missing or unsupported version; "
            f"this repo reads only OPF {OPF_VERSION} (no other version is accepted). "
            f"Recompile the playbook with a playbook-engine that emits OPF {OPF_VERSION}."
        )
    return version


def resolve_digest_version(doc: dict) -> Optional[str]:  # noqa: UP045
    """Return the document's `digest.digest_version`, or None if it has no digest.

    Raises OpfDigestVersionError if a digest is present but declares anything
    but DIGEST_VERSION. None is a legitimate answer (the 0.5 schema marks
    `digest` optional) -- the caller decides whether a digest is required;
    `scripts/opf_prompt.py` refuses to compose a review prompt without one.
    """
    digest = doc.get("digest")
    if not isinstance(digest, dict):
        return None
    version = digest.get("digest_version")
    if version != DIGEST_VERSION:
        raise OpfDigestVersionError(
            "OPF validation failed at /digest/digest_version: unsupported digest version; "
            f"this repo reads only digest_version {DIGEST_VERSION}. A digest whose selection "
            "semantics we do not understand is refused rather than read as if it were "
            "current -- its lists may not carry the precedent counts the review weighs."
        )
    return version


# (JSON Pointer to the sibling array, dotted path to reach it from the
# document root, id field name) for each of the four sets the engine's
# validator makes unique for OPF 0.5 -- parity with its _check_duplicate_ids
# (clauses, invariants, documents) and _check_precedent (precedent ids).
_SIBLING_ID_SETS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("/evidence/clauses", ("evidence", "clauses"), "id"),
    ("/evidence/precedent", ("evidence", "precedent"), "id"),
    ("/floor/invariants", ("floor", "invariants"), "id"),
    ("/corpus/documents", ("corpus", "documents"), "document_id"),
)

# Only ids that already look like machine identifiers -- and are short --
# are safe to echo in an error message. The vendored schema does NOT pin
# precedentClause.id / precedent.id / floor.invariants[].id /
# corpus.documents[].document_id to this shape (see OpfDuplicateIdError
# docstring), so this is a defensive filter this module applies itself,
# not a guarantee inherited from the schema. `^[a-z0-9_-]+$` is close to
# the pattern the schema DOES use for agreement_type.id; the length cap is
# this module's own sane bound, not a schema value.
_SAFE_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")
_SAFE_ID_MAX_LEN = 100


def _safe_id_repr(item_id: str) -> str:
    """Return a quoted, echo-safe form of `item_id`, or "" if the value does
    not look like a machine identifier. Never echoes document content --
    this check runs before the injection scan (_validate_doc), so an id
    that fails the safety filter has never been injection-scanned."""
    if len(item_id) <= _SAFE_ID_MAX_LEN and _SAFE_ID_PATTERN.match(item_id):
        return f" {item_id!r}"
    return ""


def _check_duplicate_sibling_ids(doc: dict) -> None:
    """Raise OpfDuplicateIdError if any of the four sibling-id sets repeats
    an id. Must run AFTER schema validation, which is what
    guarantees each set here is shaped as an array of objects carrying the
    named id field as a string -- this function does not re-derive that,
    it only walks what schema validation already proved well-formed
    (defensive isinstance checks are still here since a caller could, in
    principle, invoke this on an unvalidated doc).

    The error message always names the offending array index in the JSON
    Pointer (e.g. `/corpus/documents/1`) -- value-free, and enough on its
    own to locate the duplicate. The id value itself is only appended when
    `_safe_id_repr` judges it safe to echo; these four id fields are
    uploader-controlled, schema-unconstrained strings (see
    OpfDuplicateIdError docstring), not guaranteed-safe machine tokens.
    """
    for pointer, path, id_field in _SIBLING_ID_SETS:
        node: Any = doc
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if not isinstance(node, list):
            continue
        seen: set[str] = set()
        for index, item in enumerate(node):
            if not isinstance(item, dict):
                continue
            item_id = item.get(id_field)
            if not isinstance(item_id, str):
                continue
            if item_id in seen:
                raise OpfDuplicateIdError(
                    f"OPF validation failed at {pointer}/{index}: duplicate {id_field}"
                    f"{_safe_id_repr(item_id)} "
                    "(OPF requires ids to be unique among siblings)"
                )
            seen.add(item_id)


def _validate_doc(doc: dict, *, require_identity: bool) -> dict:
    """Schema-validate, hash-verify, and injection-scan an in-memory OPF doc.

    Order is deliberate: versions first (both of them), then structure (schema),
    then sibling-id uniqueness (schema-shaped but not schema-checkable, see
    OpfDuplicateIdError), then artifact integrity (content_hash), then
    content safety (injection scan). Checking a version before validating
    against a shape is what turns "failed the 'required' check at
    /evidence" on an OPF 0.3 upload into "unsupported version" -- and it is
    the only check that can see a semantic version change that leaves the
    shape intact (module docstring).
    The duplicate-id check runs right after schema validation because it
    depends on the shape guarantees schema validation just proved (each
    sibling set is an array of objects carrying its id field as a string)
    and, like the schema check, it is about structural validity rather than
    the document's own content -- it belongs before the content_hash check,
    which is about whether the (structurally valid) bytes were tampered
    with after compilation. The scan runs even on a hash-verified document --
    a faithfully-compiled playbook can still carry injected text mined out
    of the corpus.
    """
    resolve_opf_version(doc)
    resolve_digest_version(doc)  # independent version; see module docstring

    try:
        jsonschema.validate(instance=doc, schema=_load_schema())
    except jsonschema.ValidationError as exc:
        raise OpfValidationError(_describe(exc)) from None

    _check_duplicate_sibling_ids(doc)

    identity = doc.get("identity")
    has_identity = isinstance(identity, dict) and isinstance(identity.get("content_hash"), str)
    if require_identity and not has_identity:
        raise OpfHashMismatchError(
            "OPF validation failed at /identity: an uploaded playbook must carry "
            "identity.content_hash so its integrity can be verified"
        )
    if has_identity and not opf_canonicalize.verify_content_hash(doc):
        raise OpfHashMismatchError(
            "OPF validation failed at /identity/content_hash: declared hash does not "
            "match the hash recomputed over the document's canonical content "
            "(the document changed after it was compiled)"
        )

    findings = opf_injection_scan.scan_untrusted_playbook_text(doc)
    if findings:
        raise OpfInjectionError(_describe_injection(findings))

    return doc


def is_html_bundle(path: Path) -> bool:
    """True if *path* looks like a single-file `.opf.html` bundle."""
    name = path.name.lower()
    return name.endswith(".opf.html") or path.suffix.lower() in (".html", ".htm")


def load_opf(path: Path) -> dict:
    """Load and validate a bare OPF 0.5 JSON document.

    Validates against the vendored 0.5 schema, verifies `identity.content_hash` IF the document carries one, and runs the
    fail-closed injection tripwire.

    For untrusted uploads use `load_opf_document`, which additionally REQUIRES
    `identity` (see module docstring).
    """
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return _validate_doc(doc, require_identity=False)


def load_opf_document(path: Path, *, require_identity: bool = True) -> dict:
    """Load and validate an OPF playbook from either upload form.

    THE upload entrypoint. Dispatches on the file: a `.opf.html` bundle (the
    primary distribution artifact) has its embedded canonical OPF JSON
    extracted first (scripts/opf_html.py); a bare `.opf.json` is read directly.
    Both then go through identical schema validation, content_hash
    verification, and the injection scan.

    `require_identity` defaults True: this is the upload path, and a document
    without `identity` cannot be integrity-checked at all.
    """
    path = Path(path)
    if is_html_bundle(path):
        html = path.read_text(encoding="utf-8")
        try:
            doc = opf_html.extract_opf_from_html(html)
        except opf_html.OpfHtmlExtractError as exc:
            raise OpfExtractError(f"OPF bundle extraction failed: {exc}") from None
    else:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    return _validate_doc(doc, require_identity=require_identity)


def agreement_type_keys(opf_doc: dict) -> list[str]:
    """[agreement_type.id] + agreement_type.aliases (if present), lowercased,
    order-preserved, de-duplicated."""
    agreement_type = opf_doc.get("agreement_type") or {}
    candidates = [agreement_type.get("id")] + list(agreement_type.get("aliases") or [])  # noqa: RUF005
    keys: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        lowered = str(candidate).lower()
        if lowered not in seen:
            seen.add(lowered)
            keys.append(lowered)
    return keys


def match_registry_playbook(
    opf_doc: dict,
    registry_path: Path = playbook_registry.REGISTRY_PATH,
) -> Optional[str]:  # noqa: UP045
    """First registry playbook_id (via playbook_registry.load_registry) that
    appears in agreement_type_keys(opf_doc); None if no match.

    Never a fuzzy match, never a default.
    """
    registry = playbook_registry.load_registry(registry_path)
    keys = set(agreement_type_keys(opf_doc))
    for playbook_id in registry.get("playbooks", {}):
        if playbook_id.lower() in keys:
            return playbook_id
    return None
