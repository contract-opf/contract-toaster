#!/usr/bin/env python3
"""
Backend HTTP surface for the per-playbook pen-rules / posture-override
authoring layer (issue #432).

## What this is

`scripts/bind_bundle.py` fail-closes at *build time* on a candidate
pen-rules / posture-override document: an unknown ``floor_ref``, a stale
``parent_section_digest``, a non-monotonic posture ``version``, or a
``floor_additions`` id that collides with a genesis Floor invariant. Until
this module, that validation lived only inside a standalone CLI script with
no HTTP surface and no caller from ``backend/src`` -- so an admin authoring
UI (a separate, dependent ticket) had nothing to call.

This module wraps `bind_bundle`'s own validators -- it **reuses**
``_validate_pen_rules_floor_refs`` and ``_validate_overrides``, it never
reimplements the comparisons -- and returns **one machine-readable error per
failure** (``code`` + ``field`` + ``message``) so a frontend can point at
the specific offending field rather than parsing one opaque string.

## Where the OPF document comes from

The validators judge a pen-rules / posture-override document against a
specific OPF document (its ``opf.floor.invariants`` ids and its
``opf.identity.section_digests.posture``). There is **no server-side OPF
store keyed by playbook_id** today: every entry in ``playbooks/registry.json``
is a v1 playbook (``playbook_path``, a plain playbook JSON with no
``identity``/``floor`` block), and ``pipeline_runner._load_playbook_bundle``
only ever reads ``entry.playbook_path``. So the OPF document is supplied in
the request body -- exactly as ``bind_bundle.py``'s CLI takes it as a
required ``--opf`` input. The ``{playbook_id}`` in the route path is
cross-checked against the submitted OPF's own ``agreement_type`` id/aliases
(the same fail-closed check ``bind_bundle`` runs first), surfaced as a
``playbook_id_mismatch`` error rather than silently validating pen-rules
against the wrong OPF.

## Zero runtime effect (preserve this truth)

This is validation only -- **no persistence**. And even a validated,
schema-correct pen-rules / posture bundle has **zero effect on any live
review**: ``pipeline_runner._load_playbook_bundle`` reads only
``entry.playbook_path`` (v1), never a v2 ``bundle_path``, so nothing in the
review pipeline consumes a v2 bundle yet (see ``bind_bundle.py``'s module
docstring: "an artifact-only slice: no runtime consumer reads v2 bundles
yet", and ARCHITECTURE.md's "Guidance-precedence model" -> item 4). The
persist/activate counterpart route -- which would call ``bind_bundle``'s
actual bundle-construction logic and write a v2 artifact into a server-side
store -- is a deliberate follow-up: it needs a v2-bundle storage/activation
model that does not exist server-side yet (the DynamoDB/S3 + registry
plumbing that ``playbook_versions.py`` provides for v1 versions), a larger
change this ticket intentionally does not include.

## Auth

Admin only. ``is_admin`` is a DynamoDB ``users``-row flag, never a JWT claim
(same convention as ``src/users.py::_is_admin``). A non-admin caller gets
HTTP 403. The route is read-only, so -- unlike every state-mutating admin
route -- it writes **no** audit entry (there is nothing to ledger).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

# scripts/ import seam -- the same idempotent sys.path insertion
# src.pipeline_runner uses to import scripts modules by bare name. Every
# module bind_bundle pulls in transitively is either stdlib, another scripts
# module (canonicalize / opf_load / opf_canonicalize / opf_html /
# opf_injection_scan / policy_load / playbook_registry), or jsonschema --
# and jsonschema is a pinned *backend* runtime dependency
# (backend/requirements.txt), so importing bind_bundle at app startup is safe
# in production, not only under requirements-dev.txt.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import bind_bundle  # noqa: E402
import opf_load  # noqa: E402


def _is_admin(caller_user_row: dict[str, Any]) -> bool:
    """`is_admin` is a DynamoDB `users`-row flag, never a JWT claim -- same
    convention as src/users.py::_is_admin / src/retention.py::_is_admin."""
    return bool(caller_user_row.get("is_admin", False))


def _error(code: str, field: str, message: str) -> dict[str, str]:
    """One machine-readable validation error. `code` is a stable slug a
    frontend can switch on; `field` names the offending input path; `message`
    is bind_bundle's own actionable explanation, surfaced verbatim."""
    return {"code": code, "field": field, "message": message}


def _require_dict(value: Any, field: str) -> None:
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field!r} must be a JSON object when present.",
        )


def validate_pen_rules_document(
    playbook_id: str,
    body: dict[str, Any],
    caller_user_row: dict[str, Any],
) -> dict[str, Any]:
    """Admin-only, read-only validation of a candidate pen-rules /
    posture-override document against a submitted OPF document.

    Body shape (matches `scripts/bind_bundle.py`'s CLI inputs one-for-one --
    no invented schema):

      - ``opf`` (required object): the OPF document to validate against.
      - ``pen_rules`` (optional object): ``{"default": {...},
        "per_topic": {...}}`` -- ``--pen-rules``.
      - ``posture_override`` (optional object): ``{version, system_prompt,
        parent_section_digest, ...}`` -- ``--posture-override``.
      - ``floor_additions`` (optional array): ``[{id, statement,
        rationale}, ...]`` -- ``--floor-additions``.
      - ``previous_bundle`` (optional object): a previously-bound bundle,
        used ONLY to enforce monotonic posture versioning -- ``--previous-bundle``.

    Returns ``{"playbook_id", "valid": bool, "errors": [ {code, field,
    message}, ... ]}``. ``valid`` is ``True`` iff ``errors`` is empty. Every
    failure is reported as its own structured error (all independent rules
    are checked; the pass does not stop at the first failure), so a frontend
    can point at each offending field.

    Raises HTTP 403 for a non-admin caller and HTTP 400 for a malformed
    request body (missing/non-object ``opf``, or a present-but-wrong-typed
    optional field). A document that is well-formed but fails a validation
    rule is **not** an HTTP error -- it is a 200 response carrying
    ``valid: false`` and the structured errors.
    """
    if not _is_admin(caller_user_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to validate pen-rules/posture overrides.",
        )

    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be a JSON object.",
        )

    opf_doc = body.get("opf")
    if not isinstance(opf_doc, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Request body must include an 'opf' object -- the OPF document to "
                "validate the pen-rules/posture-override against. There is no "
                "server-side OPF keyed by playbook_id; the authoring surface supplies "
                "it, exactly as bind_bundle.py's CLI takes --opf."
            ),
        )

    pen_rules = body.get("pen_rules")
    posture_override = body.get("posture_override")
    floor_additions = body.get("floor_additions")
    previous_bundle = body.get("previous_bundle")

    # Request-shape guards (types only -- not the domain rules below), so a
    # malformed payload is a clean 400 rather than an unhandled 500 inside a
    # bind_bundle validator.
    if pen_rules is not None:
        _require_dict(pen_rules, "pen_rules")
    if posture_override is not None:
        _require_dict(posture_override, "posture_override")
    if previous_bundle is not None:
        _require_dict(previous_bundle, "previous_bundle")
    if floor_additions is not None:
        if not isinstance(floor_additions, list):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="'floor_additions' must be a JSON array when present.",
            )
        if not all(isinstance(entry, dict) for entry in floor_additions):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Every 'floor_additions' entry must be a JSON object.",
            )

    errors: list[dict[str, str]] = []

    # (0) playbook_id must be one of THIS OPF's own agreement_type id/aliases
    # -- bind_bundle's first fail-closed check, reused so we never validate
    # pen-rules against an OPF that is not for this playbook.
    try:
        valid_keys = opf_load.agreement_type_keys(opf_doc)
    except Exception:  # noqa: BLE001 -- a malformed/absent agreement_type block
        valid_keys = []
    if playbook_id.lower() not in valid_keys:
        errors.append(
            _error(
                "playbook_id_mismatch",
                "playbook_id",
                f"playbook_id {playbook_id!r} is not one of this OPF document's own "
                f"agreement_type id/aliases {sorted(valid_keys)!r}.",
            )
        )

    # (1) unknown floor_ref -- bind_bundle._validate_pen_rules_floor_refs.
    if pen_rules is not None:
        try:
            bind_bundle._validate_pen_rules_floor_refs(pen_rules, opf_doc)
        except bind_bundle.BindBundleError as exc:
            errors.append(
                _error("unknown_floor_ref", "pen_rules.must_not_introduce[].floor_ref", str(exc))
            )

    # (2) stale parent_section_digest -- isolated by passing previous_bundle=None,
    # which turns OFF the version check inside _validate_overrides, so only the
    # posture digest rule can fire. No message parsing, no reimplementation --
    # the rule that fires is controlled purely by the input slice.
    posture_digest_ok = True
    if posture_override is not None:
        try:
            bind_bundle._validate_overrides({"posture": posture_override}, opf_doc, None)
        except bind_bundle.BindBundleError as exc:
            posture_digest_ok = False
            errors.append(
                _error(
                    "stale_parent_section_digest",
                    "posture_override.parent_section_digest",
                    str(exc),
                )
            )

    # (3) non-monotonic posture version -- only meaningful once the digest is
    # accepted (else the digest error above already identifies the problem) and
    # only when a previous_bundle is supplied to compare against. With the
    # digest already passing, the only rule _validate_overrides can now raise is
    # the monotonic-version one.
    if posture_override is not None and previous_bundle is not None and posture_digest_ok:
        try:
            bind_bundle._validate_overrides({"posture": posture_override}, opf_doc, previous_bundle)
        except bind_bundle.BindBundleError as exc:
            errors.append(
                _error("non_monotonic_version", "posture_override.version", str(exc))
            )

    # (4) colliding floor_additions id -- isolated with a floor_additions-only
    # overrides slice (no posture key, so the posture block is skipped).
    if floor_additions:
        try:
            bind_bundle._validate_overrides({"floor_additions": floor_additions}, opf_doc, None)
        except bind_bundle.BindBundleError as exc:
            errors.append(
                _error("colliding_floor_additions", "floor_additions[].id", str(exc))
            )

    return {"playbook_id": playbook_id, "valid": not errors, "errors": errors}
