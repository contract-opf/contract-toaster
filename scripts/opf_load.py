#!/usr/bin/env python3
"""
OPF loader/validator (v0.2 and v0.3) -- issue #283, extended for the OPF 0.3
launch.

Loads an Open Playbook Format (OPF) document, validates it against the VENDORED
schema named by its own `opf_version` (playbooks/opf/playbook.schema-0.2.json or
playbook.schema-0.3.json), verifies its `identity.content_hash`, runs the
fail-closed prompt-injection scan, and matches its `agreement_type` to a
registered playbook_id (scripts/playbook_registry.py) via `agreement_type.id`/
`aliases`.

## Two upload forms (OPF 0.3)

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

## Version selection (two independent versions)

`opf_version` selects the schema. An absent/unknown version is rejected
(OpfVersionError) rather than defaulted -- a document that declares a version we
do not vendor a schema for has not been validated by anything, and guessing
would be a fail-open.

`digest.digest_version` is a SEPARATE version governing the digest section's
shape and SELECTION SEMANTICS, and is resolved BEFORE schema validation -- same
dispatch-then-validate order as `opf_version`, and for the same reason: the
version tells you which shape to expect, so validating against the wrong one
yields a confusing error about a field rather than a legible one about a
version. (OPF 0.3 is frozen at digest_version 2; see playbooks/opf/README.md.)

Schema validation is not a substitute for this check, on two counts:

  - LEGIBILITY: a whole digest_version 1 document does fail the frozen v2 schema
    -- but at `/digest/clauses/N/preferred_variations/0: failed the 'oneOf'
    check`, which describes a symptom. "unsupported digest version" names the
    cause.
  - SEMANTICS: a version can change what the data MEANS without changing its
    shape, and no schema check can see that. digest_version 1 -> 2 is exactly
    this class in part: v1's `concessions`/`unacceptable` were 1:1 projections,
    v2's are deduped, precedent-weighted and capped -- yet both validate against
    the v2 schema, since `n`/`band` are optional there. A future version whose
    lists stay shape-compatible but change how entries are ranked or capped
    would be invisible to the schema and visible only here.

Unsupported digest_version is rejected, never coerced -- reading a digest whose
selection semantics we do not understand would silently mis-weight precedent,
which is worse than refusing to run.

## posture.rubric

Not consumed here, deliberately. As of engine #178 (see issue #283's
2026-07-14 engine-drift correction), the vendored schema no longer even
accepts `posture.rubric` -- `posture` has `additionalProperties: false` and
no `rubric` property, so a document carrying it now FAILS schema validation
like any other unrecognized property. This module needs no special-case for
it either way: `load_opf` already raises on any schema violation.

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

# opf_version -> vendored schema path. Adding a version means vendoring its
# schema (see playbooks/opf/README.md) and pinning it in test_opf_schema_sync.
OPF_SCHEMA_PATHS = {
    "0.2": OPF_SCHEMA_DIR / "playbook.schema-0.2.json",
    "0.3": OPF_SCHEMA_DIR / "playbook.schema-0.3.json",
}
SUPPORTED_OPF_VERSIONS = tuple(sorted(OPF_SCHEMA_PATHS))

# digest_version values this repo can read. OPF 0.3 is FROZEN at digest_version
# 2 (playbook-engine spec/CHANGELOG.md, engine 204057e), and v2 is the shape the
# digest prompt is built for: all four lists deduped/ranked/capped and carrying
# precedent-weighted `n` + frequency `band`, with preferred_variations projected
# to {if, to, observation_ref, n, band} (`rationale` stays in the full OPF).
# v1 is deliberately NOT accepted: its lists are 1:1 projections without n/band,
# so reading it as v2 would mis-weight precedent instead of failing.
SUPPORTED_DIGEST_VERSIONS = ("2",)

# Back-compat alias: the 0.2 path this module used to hard-code.
OPF_SCHEMA_PATH = OPF_SCHEMA_PATHS["0.2"]

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
    """Raised when `opf_version` is missing, non-string, or names a version
    this repo does not vendor a schema for. Never defaulted: an unvalidated
    document must not load."""


class OpfHashMismatchError(OpfValidationError):
    """Raised when `identity.content_hash` does not match the hash recomputed
    over the document's own canonical serialization -- i.e. the bytes changed
    after the engine compiled them.

    Also raised, on the upload path, when `identity` is absent entirely:
    skipping verification for a document that simply omits the block would let
    an attacker disable the integrity check by deleting it.
    """


class OpfDigestVersionError(OpfValidationError):
    """Raised when a document's `digest.digest_version` is one this repo cannot
    read (see SUPPORTED_DIGEST_VERSIONS).

    Separate from OpfVersionError because the two versions are independent: a
    perfectly valid OPF 0.3 document can carry a digest whose selection
    semantics we do not understand. Rejecting is the fail-closed choice --
    reading a v1 digest as if it were v2 would silently mis-weight precedent
    (v1's lists carry no `n`), which is worse than refusing to run.
    """


class OpfExtractError(OpfValidationError):
    """Raised when a `.opf.html` bundle carries no single, parseable embedded
    canonical OPF JSON block (wraps opf_html.OpfHtmlExtractError)."""


def _load_schema(version: str) -> dict:
    if version not in _SCHEMA_CACHE:
        with open(OPF_SCHEMA_PATHS[version], encoding="utf-8") as f:
            _SCHEMA_CACHE[version] = json.load(f)
    return _SCHEMA_CACHE[version]


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


def _describe(exc: "jsonschema.ValidationError") -> str:
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

    Never defaults -- see module docstring ("Version selection").
    """
    version = doc.get("opf_version")
    if not isinstance(version, str) or version not in OPF_SCHEMA_PATHS:
        raise OpfVersionError(
            "OPF validation failed at /opf_version: missing or unsupported version; "
            f"supported versions are {list(SUPPORTED_OPF_VERSIONS)}"
        )
    return version


def resolve_digest_version(doc: dict) -> Optional[str]:
    """Return the document's `digest.digest_version`, or None if it has no digest.

    Raises OpfDigestVersionError if a digest is present but declares a version
    this repo cannot read. None is a legitimate answer (0.2 documents have no
    digest section at all) -- the caller decides whether a digest is required.
    """
    digest = doc.get("digest")
    if not isinstance(digest, dict):
        return None
    version = digest.get("digest_version")
    if not isinstance(version, str) or version not in SUPPORTED_DIGEST_VERSIONS:
        raise OpfDigestVersionError(
            "OPF validation failed at /digest/digest_version: unsupported digest version; "
            f"this repo reads {list(SUPPORTED_DIGEST_VERSIONS)}. A digest whose selection "
            "semantics we do not understand is refused rather than read as if it were "
            "current -- its lists may not carry the precedent counts the review weights."
        )
    return version


def _validate_doc(doc: dict, *, require_identity: bool) -> dict:
    """Schema-validate, hash-verify, and injection-scan an in-memory OPF doc.

    Order is deliberate: versions first (both of them), then structure (schema),
    then artifact integrity (content_hash), then content safety (injection
    scan). Dispatching on a version before validating against a shape is what
    turns "failed the 'oneOf' check at /digest/clauses/3/preferred_variations/0"
    into "unsupported digest version" -- and it is the only check that can see a
    semantic version change that leaves the shape intact (module docstring).
    The scan runs even on a hash-verified document -- a faithfully-compiled
    playbook can still carry injected text mined out of the corpus.
    """
    version = resolve_opf_version(doc)
    resolve_digest_version(doc)  # independent version; see module docstring

    try:
        jsonschema.validate(instance=doc, schema=_load_schema(version))
    except jsonschema.ValidationError as exc:
        raise OpfValidationError(_describe(exc)) from None

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
    """Load and validate a bare OPF JSON document (v0.2 or v0.3).

    Validates against the schema named by the document's own `opf_version`,
    verifies `identity.content_hash` IF the document carries one, and runs the
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
    candidates = [agreement_type.get("id")] + list(agreement_type.get("aliases") or [])
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
) -> Optional[str]:
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
