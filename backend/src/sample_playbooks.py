"""
Bundled sample playbook activation -- issue #402 (empty-shell first-run
flow).

## Why this exists, and why it is NOT src/playbook_versions.py

`src/playbook_versions.py`'s `activate_release_bundle` (issue #242) is the
governed production activation path: it requires a `playbook_versions`
upload row and enforces Gate 7 (uploaded `content_hash` ==
`legal_approval.content_hash`) before writing `playbooks.
active_release_bundle_hash`. That ceremony exists because activating a
bundle there means "put this content in front of real counterparties" --
someone with legal authority signed off on these exact bytes.

The bundled sample this module activates is a different thing entirely: a
synthetic, brand-free playbook that ships IN the repo/image (issue #402),
whose whole purpose is letting a fresh install -- one with an EMPTY
playbook store, per issue #401's no-active-bundle empty-shell state --
become runnable end to end with one click, with nothing to upload and
nothing to approve. Routing that click through the Gate-7 ceremony would
mean fabricating a `legal_approval` record for content nobody reviewed as
a real position, which is worse than not having the ceremony at all. This
module is the deliberately separate, lower-ceremony mechanism issue #402's
Scope describes -- explicitly NOT the full playbook admin UI (issue #78).

## What it actually does

1. Resolves `playbook_id` via `playbook_registry` and refuses
   (`SampleNotAvailableError`) unless its `registry.json` entry carries
   `"bundled_sample": true` -- issue #289's type-blindness convention: no
   playbook_id literal gates this, a registry field does, so this
   low-ceremony path can only ever activate a playbook the repo itself has
   marked as a shippable sample, never an arbitrary registered playbook_id
   (`eiaa`, `sample-agreement`) by accident.
2. Runs the SAME runtime validation every other resolution of an active
   bundle relies on (`playbook_validation.load_and_validate_playbook`,
   issue #266) -- fail-closed (`SampleInvalidError`) rather than activating
   content that would not itself resolve as valid.
3. Reuses `scripts/seed_active_bundle.py`'s real content_hash computation
   and `playbooks` table write -- the exact same hash every other consumer
   of this playbook_id would compute, never a fabricated one.
4. Appends one audit row (actor, action, target, content_hash) to the
   shared `audit` table -- same posture as every other admin mutation in
   this codebase (ARCHITECTURE.md -> "Audit posture").
5. (Issue #412) Writes a REAL `playbook_versions` row for `(playbook_id,
   _SAMPLE_VERSION)` -- via the SAME `src.playbook_versions` functions the
   governed upload/activate/rollback path uses (`record_playbook_version_
   upload` / `activate_playbook_version` / `update_playbook_version_notes`),
   never a hand-rolled write -- and, ONLY the first time the row is created
   and when the registry entry carries a `seed_notes` string, sets it as
   that version's admin-editable `notes`. This is what makes the bundled
   sample genuinely first-class rather than a second-class entry: with a
   real version row it can be rolled back, superseded by an uploaded
   version, and have its note edited by an admin through the exact same
   `src.playbook_versions` endpoints any other playbook_id uses -- none of
   that requires touching this module, and this module never overwrites an
   admin's edit on a later re-activation. Calling `activate_bundled_sample`
   again (e.g. a re-click after a page reload) is idempotent: the upload
   row is created once (a second attempt's `PlaybookVersionConflictError`
   is swallowed, after reconciling a possibly-stale `content_hash` -- see
   `_reconcile_content_hash`) and the row is then re-activated, matching
   this function's existing "repeatable" contract for `seed_active_bundle`
   -- but `notes` is deliberately NOT re-seeded on that re-activation, so a
   second click can never revert an admin's edited note.

Admin-gated: `activate_bundled_sample` takes the caller's row and 403s a
non-admin caller itself (same convention as `src/model_settings.py` /
`src/retention.py` -- the admin check lives in the `src/*.py` function, not
the route handler).

Endpoint this module backs (wired in `src/main.py`):
  POST /api/admin/playbooks/{playbook_id}/activate-sample

Environment variables consumed:
  PLAYBOOKS_TABLE          playbooks table name (PK: playbook_id) -- same
                           table `backend/src/reviews.py`'s active-bundle
                           resolver reads.
  PLAYBOOK_VERSIONS_TABLE  playbook_versions table name (PK: playbook_id,
                           SK: version) -- issue #412; the same table
                           `backend/src/playbook_versions.py` reads/writes
                           for every other playbook_id's upload/activate/
                           rollback/notes lifecycle.
  AUDIT_TABLE              audit table name (append-only) -- same table/
                           shape as `backend/src/users.py` /
                           `backend/src/retention.py` /
                           `backend/src/model_settings.py`. This module and
                           `backend/src/playbook_versions.py` both append to
                           it, so a single `activate_bundled_sample` call now
                           writes more than one row (this module's own
                           `bundled_sample_activate` entry, plus whatever
                           `playbook_versions.activate_playbook_version` /
                           `update_playbook_version_notes` append).
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

# Cross-directory import (same convention backend/src/reviews.py and
# backend/src/pipeline_runner.py already use) to reach
# scripts/playbook_registry.py, scripts/playbook_validation.py, and
# scripts/seed_active_bundle.py. Idempotent: harmless if some other module
# already inserted it.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import playbook_registry  # noqa: E402
import playbook_validation  # noqa: E402
import seed_active_bundle  # noqa: E402

try:  # production runs `src.main`; tests put backend/src on sys.path
    # (same dual-context import shape as backend/src/pipeline_runner.py)
    from src import playbook_versions
except ImportError:  # pragma: no cover
    import playbook_versions  # type: ignore[no-redef]

# Issue #412: the version identifier stamped on the ONE `playbook_versions`
# row this module writes. A fixed literal (rather than something derived
# from the registry) is correct here: this is the low-ceremony FIRST-RUN
# activation of the bundled sample as it ships in the image today -- once an
# admin uploads a genuinely new version of it, that upload goes through the
# normal governed path (`src.playbook_versions.record_playbook_version_
# upload`), which owns picking the next version identifier from then on.
_SAMPLE_VERSION = "1.0.0"


class SampleNotAvailableError(Exception):
    """Raised when `playbook_id` has no bundled sample to activate --
    either unregistered, or registered without registry.json's
    `"bundled_sample": true` marker. Fail closed: this low-ceremony path
    never activates a playbook the repo has not explicitly marked
    shippable-as-a-sample."""


class SampleInvalidError(Exception):
    """Raised when the bundled sample's on-disk content fails the same
    runtime validation every other active-bundle resolution relies on
    (scripts/playbook_validation.py). Should never happen for a committed,
    tested sample -- this is defense in depth, not the expected path."""


def _is_admin(caller_user_row: dict[str, Any]) -> bool:
    """`is_admin` is a DynamoDB `users`-row flag, never a JWT claim -- same
    convention as src/users.py / src/retention.py / src/model_settings.py."""
    return bool(caller_user_row.get("is_admin", False))


def _write_audit_entry(
    dynamodb_resource: Any,
    actor: str,
    action: str,
    target: str,
    detail: dict[str, Any],
) -> None:
    """Append an immutable audit row -- same table/shape as every other
    admin mutation in this codebase (ARCHITECTURE.md -> "Audit posture";
    src/model_settings.py's own `_write_audit_entry` is the closest
    sibling)."""
    table = dynamodb_resource.Table(os.environ["AUDIT_TABLE"])
    now = time.time()
    event_id = uuid.uuid4().hex
    partition = time.strftime("%Y-%m", time.gmtime(now))
    timestamp = f"{int(now)}#{event_id}"

    item: dict[str, Any] = {
        "partition": partition,
        "timestamp": timestamp,
        "event_id": event_id,
        "actor": actor,
        "action": action,
        "target": target,
        "target_type": "bundled_sample_playbook",
        "outcome": "success",
    }
    item.update(detail)
    table.put_item(Item=item)


def activate_bundled_sample(
    playbook_id: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """Activate `playbook_id`'s bundled sample -- issue #402's one-click
    first-run affordance. See module docstring for the full contract.

    Raises HTTPException(403) for a non-admin caller, `SampleNotAvailableError`
    when `playbook_id` has no bundled sample (unregistered, or registered
    without `"bundled_sample": true`), `SampleInvalidError` when the on-disk
    sample fails runtime validation.

    Returns `{"playbook_id", "content_hash", "status": "active"}`.
    """
    if not _is_admin(caller_user_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to activate a bundled sample playbook.",
        )

    try:
        entry = playbook_registry.resolve_playbook(playbook_id)
    except playbook_registry.PlaybookNotRegisteredError as exc:
        raise SampleNotAvailableError(
            f"playbook_id {playbook_id!r} is not registered."
        ) from exc

    if not entry.bundled_sample:
        raise SampleNotAvailableError(
            f"playbook_id {playbook_id!r} has no bundled sample to activate."
        )

    try:
        playbook_validation.load_and_validate_playbook(playbook_id)
    except playbook_validation.PlaybookValidationError as exc:
        raise SampleInvalidError(
            f"bundled sample for playbook_id {playbook_id!r} failed runtime "
            f"validation: {exc}"
        ) from exc

    content_hash = seed_active_bundle.seed_active_bundle(playbook_id, dynamodb_resource)

    actor_identity = caller_user_row.get("cognito_sub", "")

    _clear_removed_tombstone(playbook_id, dynamodb_resource)

    _activate_version_row(entry, content_hash, actor_identity, dynamodb_resource)

    _write_audit_entry(
        dynamodb_resource=dynamodb_resource,
        actor=actor_identity,
        action="bundled_sample_activate",
        target=f"{playbook_id}#{content_hash}",
        detail={"playbook_id": playbook_id, "content_hash": content_hash},
    )

    return {"playbook_id": playbook_id, "content_hash": content_hash, "status": "active"}


def _clear_removed_tombstone(playbook_id: str, dynamodb_resource: Any) -> None:
    """Clear the `removed` tombstone `playbook_versions.remove_playbook`
    (issue #412) writes, so re-activating a bundled sample an admin had
    removed brings it back into the catalog rather than silently seeding a
    bundle for a playbook the catalog still filters out. This is what makes
    "remove the shipped sample" reversible rather than a one-way door.
    """
    table = dynamodb_resource.Table(os.environ["PLAYBOOKS_TABLE"])
    table.update_item(
        Key={"playbook_id": playbook_id},
        UpdateExpression="SET removed = :false",
        ExpressionAttributeValues={":false": False},
    )


def _activate_version_row(
    entry: "playbook_registry.PlaybookEntry",
    content_hash: str,
    actor_identity: str,
    dynamodb_resource: Any,
) -> None:
    """Give the bundled sample a REAL `playbook_versions` row (issue #412) --
    via the SAME functions the governed upload/activate path uses, never a
    hand-rolled write. This is what makes the sample genuinely first-class:
    once this row exists, an admin can upload a new version, roll back, or
    edit its note through the normal `src.playbook_versions` admin surface,
    with zero special-casing for a bundled sample.

    Idempotent across repeated activations (e.g. a page-reload re-click,
    already a documented contract of `activate_bundled_sample` above): the
    upload row is created once -- a second attempt's
    `PlaybookVersionConflictError` (the append-only trail rejecting a
    re-upload of the same `(playbook_id, version)`) is expected and
    swallowed -- and the row is re-activated either way. The seed note is
    written ONLY the first time (the branch where the upload row is newly
    created): once a row exists, `notes` is admin-editable exactly like any
    other playbook's note (issue #411's `update_playbook_version_notes`),
    and re-seeding it on every re-click would silently revert an admin's
    edit -- that is NOT what "idempotent" means for this field. Instead,
    the conflict branch reconciles `content_hash` against the freshly
    computed one, so a new image shipping revised sample content under the
    same fixed `_SAMPLE_VERSION` literal never leaves the version row
    carrying a stale hash while `playbooks.active_release_bundle_hash`
    moves on.
    """
    try:
        playbook_versions.record_playbook_version_upload(
            entry.playbook_id,
            _SAMPLE_VERSION,
            actor_identity,
            dynamodb_resource,
            content_hash=content_hash,
        )
        if entry.seed_notes:
            playbook_versions.update_playbook_version_notes(
                entry.playbook_id, _SAMPLE_VERSION, entry.seed_notes, actor_identity, dynamodb_resource
            )
    except playbook_versions.PlaybookVersionConflictError:
        _reconcile_content_hash(entry.playbook_id, content_hash, actor_identity, dynamodb_resource)

    playbook_versions.activate_playbook_version(
        entry.playbook_id, _SAMPLE_VERSION, actor_identity, dynamodb_resource
    )


def _reconcile_content_hash(
    playbook_id: str,
    fresh_content_hash: str,
    actor_identity: str,
    dynamodb_resource: Any,
) -> None:
    """Issue #412 fix: when `record_playbook_version_upload` rejects a
    re-activation's upload attempt (the `(playbook_id, _SAMPLE_VERSION)`
    row already exists), that existing row's `content_hash` may now be
    stale relative to the freshly computed one -- e.g. a new image ships
    revised sample content under the same fixed `_SAMPLE_VERSION` literal
    (see `_SAMPLE_VERSION`'s own docstring). `content_hash` is write-once
    by design for every OTHER caller of `src.playbook_versions`, so this
    reconciles the divergence directly rather than through a public
    mutator: if the hashes already match, do nothing; otherwise update the
    row's `content_hash` and append an audit record documenting the
    change, so `activate_playbook_version`'s own `release_bundle_activate`
    audit entry (which reads `content_hash` off this row) never reports a
    stale hash.
    """
    table = dynamodb_resource.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])
    existing = table.get_item(Key={"playbook_id": playbook_id, "version": _SAMPLE_VERSION}).get("Item")
    existing_hash = existing.get("content_hash") if existing else None
    if existing_hash == fresh_content_hash:
        return

    table.update_item(
        Key={"playbook_id": playbook_id, "version": _SAMPLE_VERSION},
        UpdateExpression="SET content_hash = :h",
        ExpressionAttributeValues={":h": fresh_content_hash},
    )

    _write_audit_entry(
        dynamodb_resource=dynamodb_resource,
        actor=actor_identity,
        action="bundled_sample_content_hash_reconciled",
        target=f"{playbook_id}#{_SAMPLE_VERSION}",
        detail={
            "playbook_id": playbook_id,
            "version": _SAMPLE_VERSION,
            "previous_content_hash": existing_hash,
            "content_hash": fresh_content_hash,
        },
    )
