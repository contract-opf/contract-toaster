#!/usr/bin/env python3
"""
OPF v0.2 loader/validator -- issue #283 (slice 1 of 5 of the #278 OPF-bind chain).

Pure loader slice: load an Open Playbook Format (OPF) v0.2 document, validate
it against the vendored schema (playbooks/opf/playbook.schema-0.2.json), and
match its `agreement_type` to a registered playbook_id
(scripts/playbook_registry.py) via `agreement_type.id`/`aliases`. No prompt
composition (slice 2), no Floor judging (slice 3), no bind CLI (slice 4), no
change to `resolve_playbook` or any runtime wiring -- those are later slices.

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

OPF_SCHEMA_PATH = REPO_ROOT / "playbooks" / "opf" / "playbook.schema-0.2.json"

_SCHEMA_CACHE: Optional[dict] = None


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


def _load_schema(path: Path = OPF_SCHEMA_PATH) -> dict:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        with open(path, encoding="utf-8") as f:
            _SCHEMA_CACHE = json.load(f)
    return _SCHEMA_CACHE


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


def load_opf(path: Path) -> dict:
    """Load and schema-validate an OPF v0.2 document.

    Raises OpfValidationError (JSON Pointer to the failure, no document
    content -- see module docstring) if the document is not schema-valid.

    After schema validation, runs the hardcoded prompt-injection tripwire
    (opf_injection_scan.scan_untrusted_playbook_text, issue #346) over
    every model-bound text field. A positive finding raises
    OpfInjectionError (a subclass of OpfValidationError) listing rule_ids
    + json_paths -- fail closed, the document never loads.
    """
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    schema = _load_schema()
    try:
        jsonschema.validate(instance=doc, schema=schema)
    except jsonschema.ValidationError as exc:
        raise OpfValidationError(_describe(exc)) from None

    findings = opf_injection_scan.scan_untrusted_playbook_text(doc)
    if findings:
        raise OpfInjectionError(_describe_injection(findings))

    return doc


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
