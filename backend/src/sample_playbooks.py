"""
Deploy/startup seed for the playbook the image ships with -- issue #433
(the shipped sample stops being a special case).

## What changed, and why

Issues #402/#412 made the bundled "Synthetic NDA Sample" a bespoke path: a
`"bundled_sample": true` registry marker, a `has_bundled_sample` catalog
field, and a dedicated `POST /api/admin/playbooks/{id}/activate-sample`
route behind a one-off "Activate the bundled sample" button. Issue #433
reverses that design (a deliberate product-direction change, not a bug in
#412's work): the shipped playbook is installed by a DEPLOY-TIME SEED that
runs the same functions an admin-uploaded version runs, and from then on it
is an ordinary playbook with an ordinary ACTIVE/INACTIVE status. There is no
sample-only branch left anywhere -- no marker, no catalog field, no route,
no button.

## What the seed actually does

`seed_shipped_playbook` is called once, idempotently, from the deploy
bootstrap (`deploy/dts/bootstrap.py`) -- never from an HTTP route, and
never by an end user. For a fresh deployment (empty tables) it:

1. Resolves `playbook_id` through `playbook_registry` and refuses
   (`SampleNotAvailableError`) an unregistered id, or one marked
   `"test_only": true` -- the fixtures-only entries (`synthetic-generic`)
   that exist for the test suite are never installed into a real
   deployment. Fail closed: no playbook_id literal gates this, a registry
   field does (issue #289's type-blindness convention).
2. Runs the SAME runtime validation every other resolution of an active
   bundle relies on (`playbook_validation.load_and_validate_playbook`,
   issue #266) -- fail-closed (`SampleInvalidError`) rather than installing
   content that would not itself resolve as valid.
3. Computes the REAL content hash via `scripts/seed_active_bundle.py`'s
   `compute_seed_hash` -- the exact same canonicalization every other
   consumer of this playbook_id computes, never a fabricated one.
4. Writes a real `playbook_versions` row and activates it through
   `src.playbook_versions.record_playbook_version_upload` /
   `update_playbook_version_notes` / `activate_playbook_version` -- the
   SAME functions an admin-uploaded version goes through, never a
   hand-rolled write. So the seeded playbook can be superseded by an
   uploaded version, rolled back to, renamed, removed, and have its note
   edited through the ordinary admin surface, with zero special-casing.
5. Points the resolver at it (`playbooks.active_release_bundle_hash` =
   that row's `content_hash`) -- the same write `playbook_versions.
   activate_release_bundle` performs, done here with `update_item` rather
   than `put_item` so an admin's own overrides on that row
   (`display_name`, `removed`) are never clobbered.
6. Appends one audit row (actor, action, target, content_hash) to the
   shared `audit` table, on top of whatever `src.playbook_versions` appends
   for the upload/notes/activate steps -- same posture as every other
   mutation in this codebase (ARCHITECTURE.md -> "Audit posture").

Gate 7 (`playbook_versions.activate_release_bundle`'s approved-hash check)
deliberately does NOT apply: Gate 7 means "someone with legal authority
signed off on these exact bytes to put in front of a real counterparty",
and fabricating a `legal_approval` record for shipped sample content nobody
reviewed as a real position would be worse than not having the ceremony.
The seed therefore goes through `activate_playbook_version` (issue #79's
lifecycle primitive) rather than the Gate-7'd wrapper -- the same primitive
that wrapper itself calls.

## Install-once, never re-stomp

The bootstrap re-runs on every container start, so the seed must be
idempotent in the strong sense: it installs on a FRESH deployment and does
nothing at all afterwards. It skips (returning `status: "skipped"`) when

  - the `playbooks` row carries the `removed` tombstone
    `playbook_versions.remove_playbook` writes -- an admin deliberately
    removed the shipped playbook, and a container restart must not
    resurrect it; or
  - any `playbook_versions` row already exists for this playbook_id -- it
    has been installed before, and whatever an admin has done since
    (uploaded a newer version, rolled back, edited the note) is the current
    truth, not the shipped default.

A consequence worth stating plainly: a new image whose on-disk sample
content differs from what was seeded earlier does NOT silently re-seed. The
`playbook_versions` row and `playbooks.active_release_bundle_hash` stay in
sync with each other at the version that was actually installed; picking up
revised content is an ordinary "upload a new version and activate it"
admin action, exactly as it would be for any other playbook. That is the
whole point of issue #433 -- there is no sample-only fast path anymore.

Environment variables consumed:
  PLAYBOOKS_TABLE          playbooks table name (PK: playbook_id) -- same
                           table `backend/src/reviews.py`'s active-bundle
                           resolver reads.
  PLAYBOOK_VERSIONS_TABLE  playbook_versions table name (PK: playbook_id,
                           SK: version) -- the same table
                           `backend/src/playbook_versions.py` reads/writes
                           for every playbook_id's upload/activate/
                           rollback/notes lifecycle.
  AUDIT_TABLE              audit table name (append-only) -- same table/
                           shape as `backend/src/users.py` /
                           `backend/src/retention.py` /
                           `backend/src/model_settings.py`.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

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

# The version identifier stamped on the ONE `playbook_versions` row the
# seed writes. A fixed literal (rather than something derived from the
# registry) is correct here: this is the initial install of the playbook as
# it ships in the image -- once an admin uploads a genuinely new version,
# that upload goes through the normal governed path
# (`src.playbook_versions.record_playbook_version_upload`), which owns
# picking the next version identifier from then on.
_SEED_VERSION = "1.0.0"

# The actor recorded on the seed's audit rows. There is no human caller: a
# deploy step ran, and the trail should say so rather than attribute the
# install to whichever admin happened to sign in first.
SEED_ACTOR = "deploy-bootstrap"


class SampleNotAvailableError(Exception):
    """Raised when `playbook_id` is not something a deployment may install
    -- either unregistered, or a `"test_only": true` fixtures entry. Fail
    closed: the seed never installs a playbook the repo has not shipped as
    a real, non-test contract type."""


class SampleInvalidError(Exception):
    """Raised when the shipped playbook's on-disk content fails the same
    runtime validation every other active-bundle resolution relies on
    (scripts/playbook_validation.py). Should never happen for a committed,
    tested playbook -- this is defense in depth, not the expected path."""


def _write_audit_entry(
    dynamodb_resource: Any,
    actor: str,
    action: str,
    target: str,
    detail: dict[str, Any],
) -> None:
    """Append an immutable audit row -- same table/shape as every other
    mutation in this codebase (ARCHITECTURE.md -> "Audit posture";
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
        "target_type": "shipped_playbook",
        "outcome": "success",
    }
    item.update(detail)
    table.put_item(Item=item)


def _skip(playbook_id: str, reason: str) -> dict[str, Any]:
    return {
        "playbook_id": playbook_id,
        "content_hash": None,
        "status": "skipped",
        "reason": reason,
    }


def seed_shipped_playbook(
    playbook_id: str,
    dynamodb_resource: Any,
    actor_identity: str = SEED_ACTOR,
) -> dict[str, Any]:
    """Install and activate the playbook the image ships with, once, on a
    fresh deployment. See the module docstring for the full contract.

    Raises `SampleNotAvailableError` when `playbook_id` is unregistered or
    is a `"test_only": true` fixtures entry, and `SampleInvalidError` when
    the on-disk content fails runtime validation.

    Returns `{"playbook_id", "content_hash", "status", "reason"}` --
    `status` is `"active"` when this call installed it (with the real
    `content_hash`), or `"skipped"` (with a `reason` and a null
    `content_hash`) when there was nothing to do.
    """
    try:
        entry = playbook_registry.resolve_playbook(playbook_id)
    except playbook_registry.PlaybookNotRegisteredError as exc:
        raise SampleNotAvailableError(
            f"playbook_id {playbook_id!r} is not registered."
        ) from exc

    if entry.test_only:
        raise SampleNotAvailableError(
            f"playbook_id {playbook_id!r} is a test_only registry entry and is "
            "never installed into a deployment."
        )

    # Install-once guards, in this order: `remove_playbook` DELETES a
    # playbook's version rows as part of tombstoning it, so the trail check
    # alone would happily resurrect a playbook an admin removed.
    if playbook_versions.get_playbook_overrides(playbook_id, dynamodb_resource)["removed"]:
        return _skip(playbook_id, "removed_by_admin")

    if playbook_versions.list_playbook_version_trail(playbook_id, dynamodb_resource):
        return _skip(playbook_id, "already_installed")

    try:
        playbook_validation.load_and_validate_playbook(playbook_id)
    except playbook_validation.PlaybookValidationError as exc:
        raise SampleInvalidError(
            f"shipped playbook {playbook_id!r} failed runtime validation: {exc}"
        ) from exc

    content_hash = seed_active_bundle.compute_seed_hash(playbook_id)

    try:
        playbook_versions.record_playbook_version_upload(
            playbook_id,
            _SEED_VERSION,
            actor_identity,
            dynamodb_resource,
            content_hash=content_hash,
        )
    except playbook_versions.PlaybookVersionConflictError:
        # Another bootstrap won the race between the trail check above and
        # this write. Its install is the real one; don't double-write.
        return _skip(playbook_id, "already_installed")

    if entry.seed_notes:
        playbook_versions.update_playbook_version_notes(
            playbook_id, _SEED_VERSION, entry.seed_notes, actor_identity, dynamodb_resource
        )

    playbook_versions.activate_playbook_version(
        playbook_id, _SEED_VERSION, actor_identity, dynamodb_resource
    )

    # Resolver wiring -- the same write `playbook_versions.
    # activate_release_bundle` performs after its Gate 7 check, done with
    # `update_item` (never `put_item`) so an admin's `display_name` /
    # `removed` attributes on this row survive.
    dynamodb_resource.Table(os.environ["PLAYBOOKS_TABLE"]).update_item(
        Key={"playbook_id": playbook_id},
        UpdateExpression="SET active_release_bundle_hash = :h",
        ExpressionAttributeValues={":h": content_hash},
    )

    _write_audit_entry(
        dynamodb_resource=dynamodb_resource,
        actor=actor_identity,
        action="shipped_playbook_seeded",
        target=f"{playbook_id}#{content_hash}",
        detail={
            "playbook_id": playbook_id,
            "version": _SEED_VERSION,
            "content_hash": content_hash,
        },
    )

    return {
        "playbook_id": playbook_id,
        "content_hash": content_hash,
        "status": "active",
        "reason": "",
    }
