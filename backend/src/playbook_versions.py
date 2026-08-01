"""
Playbook-version upload audit trail, activation, and rollback — issues #9
and #79 (v1 scope, confirmed by the maintainer 2026-07-10).

## Why this exists

The production path for playbooks is the admin UI (download -> edit ->
upload draft -> activate), which never touches git, so `playbooks/` in the
repo and the `PlaybookVersionsTable` (`infra/lib/nested/data-stack.ts:827`)
diverge after the first UI upload. Before this module, nothing recorded who
uploaded a given version, and there was no answer to "who changed the
active playbook, and when?" — and no clean way to switch the active version
or revert a bad one.

## v1 scope (this module)

  - On each new playbook-version upload, write an **append-only** audit
    record capturing the **uploader identity** and a **timestamp**
    (`record_playbook_version_upload`).
  - **Expose** that trail on a read path, records returned in order
    (`list_playbook_version_trail`).
  - **Activate** a specific playbook/release-bundle version — mark it the
    active one (`activate_playbook_version`, issue #79).
  - **Roll back** to a previously-active version — restore it as active
    (`rollback_playbook_version`, issue #79).
  - Activate and rollback both append an actor + timestamp record to the
    same `audit` table field dictionary used by `backend/src/users.py` /
    `backend/src/retention.py` (ARCHITECTURE.md -> "Audit posture": "Release-
    bundle activations and rollbacks").
  - Reuses the existing `PlaybookVersionsTable` (PK: `playbook_id`, SK:
    `version`) and the append-only `audit` posture already used in
    `backend/src/users.py` / `backend/src/retention.py`: upload rows are
    written once and never mutated. A re-upload of an already-recorded
    `(playbook_id, version)` pair is rejected (`ConditionExpression`)
    rather than silently overwriting the prior uploader/timestamp — the
    trail can never be quietly rewritten. Activate/rollback do mutate the
    `status` field of a `playbook_versions` row (that field is the
    documented lifecycle-authority state, `draft -> active -> retired` —
    see docs/playbook-governance.md "Status authority"); the *audit trail*
    of who did it and when is itself append-only, same as every other
    admin action in this codebase.

## Explicitly deferred (not lost — see issue #79 "Explicitly deferred")

The heavier release-lifecycle controls from the original review —
mandatory KMS-signed approval, the two-person rule, full gate-set
orchestration, the deactivate action / no-active-bundle 503 refusal, and
quarantine/supersede wiring (#23/#41/#67/#68) — are **deliberately
deferred by the maintainer** and are out of this v1 slice. They remain
open for a pre-production hardening pass. This module does not implement
them: no signature verification, no approver-role check, no
uploader != approver != activator enforcement, no deactivate-without-a-
successor path, no automatic quarantine of reviews run under a superseded
bundle.

Issue #242 landed two of the pieces this section originally deferred —
Gate 7 (`content_hash == legal_approval.content_hash`) and the resolver
wiring (`playbooks.active_release_bundle_hash`) — via the new
`activate_release_bundle` function below, mounted as an admin HTTP
endpoint in `backend/src/main.py`. `activate_playbook_version` itself is
unchanged; `activate_release_bundle` wraps it. The two-person rule and
quarantine/supersede wiring remain deferred.

## The one mutable field (issue #411)

Every attribute this module writes prior to issue #411 is either
write-once (`uploaded_by`/`uploaded_at`/`content_hash`, set at upload and
never touched again) or a lifecycle-authority transition audited on
change (`status`). Issue #411 adds exactly one more field to the
`playbook_versions` row, `notes` — a free-form string (default `""` at
upload time) an admin may set and later freely replace, entirely outside
the `draft -> active -> retired` lifecycle and independent of Gate 7 —
via `update_playbook_version_notes`, mounted as `PATCH /api/admin/
playbooks/{playbook_id}/versions/{version}/notes` in `backend/src/main.py`.
It is deliberately the ONE mutable field on an otherwise-immutable
version row: re-uploading a version's *content* is still rejected by
`record_playbook_version_upload`'s append-only `ConditionExpression`, and
`update_playbook_version_notes` only ever touches `notes` — it cannot
change `status`, `content_hash`, or any other field. `notes` is surfaced
read-only on `list_playbook_version_trail` and, for the currently `active`
version of a playbook, on `GET /api/playbooks` (the catalog, issue #272)
via `get_active_version_notes`. Because `notes` is free-form admin-
authored text rather than a controlled-vocabulary field, the audit row
`update_playbook_version_notes` appends records identifiers and a
`notes_length` only, never the note text itself — preserving this
module's "never document substance" audit posture (see "De-branding"
below) for the one field that could otherwise carry stray content into
the append-only `audit` table.

## De-branding

Per issue #79's release de-branding requirement, the serialized trail
returned by `list_playbook_version_trail` and the audit records written by
`activate_playbook_version` / `rollback_playbook_version` must never
contain tenant-brand strings branding — they carry only identifiers and
timestamps (uploader/actor identity, version, status, timestamps), never
document substance or brand strings.

Environment variables:
  PLAYBOOK_VERSIONS_TABLE  — playbook_versions table name
                             (PK: playbook_id, SK: version)
  AUDIT_TABLE               — audit table name (append-only; PK: partition,
                             SK: timestamp#event_id) — same table and shape
                             as backend/src/users.py / backend/src/retention.py
  PLAYBOOKS_TABLE            — playbooks table name (PK: playbook_id only),
                             consumed only by `activate_release_bundle`
                             (issue #242) to write `active_release_bundle_hash`
                             — the same table backend/src/reviews.py's
                             `resolve_active_release_bundle_hash` reads.
"""

import os
import time
import uuid
from typing import Any

from boto3.dynamodb.conditions import Key

# Lifecycle statuses (docs/playbook-governance.md -> "Release-bundle
# lifecycle"). `playbook_versions.status` is the sole lifecycle authority;
# see "Status authority" in that doc.
STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"


class PlaybookVersionConflictError(Exception):
    """Raised when a caller attempts to re-upload a (playbook_id, version)
    pair that already has an audit record. The trail is append-only: a
    version, once uploaded, is immutable. Callers that need a new upload
    recorded must supply a new version identifier."""


class PlaybookVersionNotFoundError(Exception):
    """Raised when activate/rollback is asked to act on a (playbook_id,
    version) pair that has no recorded upload row."""


class PlaybookVersionRollbackError(Exception):
    """Raised when `rollback_playbook_version` is asked to restore a
    version that was never previously active (status != "retired"). Only a
    version this module itself demoted from `active` to `retired` is a
    valid rollback target — rolling back to a version that was never
    active is just a (second) activation, and callers should use
    `activate_playbook_version` for that."""


class PlaybookVersionGate7MismatchError(Exception):
    """Raised by `activate_release_bundle` when Gate 7 fails: the target
    version's `content_hash` does not equal its recorded
    `legal_approval.content_hash` (including the case where no
    `legal_approval` was ever recorded). Per ARCHITECTURE.md / docs/
    playbook-governance.md "Gate 7 (approved hashes match the artifacts
    being promoted)", this means the bytes changed after approval (or were
    never approved) and the bundle cannot be activated."""


def _playbook_versions_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])


def _audit_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["AUDIT_TABLE"])


def now_epoch() -> float:
    return time.time()


def _write_audit_entry(
    dynamodb_resource: Any,
    actor: str,
    action: str,
    target: str,
    detail: dict[str, Any],
    now_epoch_value: float | None = None,
) -> None:
    """Append an immutable audit row for a release-bundle activation or
    rollback (ARCHITECTURE.md -> "Audit posture": "Release-bundle
    activations and rollbacks"). Identifiers, statuses, and hashes only —
    never document substance. Same shape as
    backend/src/retention.py::_write_audit_entry / backend/src/users.py.
    """
    table = _audit_table(dynamodb_resource)
    now = now_epoch_value if now_epoch_value is not None else now_epoch()
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
        "target_type": "playbook_version",
        "outcome": "success",
    }
    item.update(detail)
    table.put_item(Item=item)


def record_playbook_version_upload(
    playbook_id: str,
    version: str,
    uploader_identity: str,
    dynamodb_resource: Any,
    content_hash: str | None = None,
    now_epoch_value: float | None = None,
) -> dict[str, Any]:
    """Append-only audit record for a playbook-version upload.

    Writes a new row to the `playbook_versions` table capturing the
    uploader identity and an upload timestamp (`uploaded_by` /
    `uploaded_at` — the field names already named in ARCHITECTURE.md's
    `playbook_versions` field dictionary). New rows land with
    `status = "draft"` (the existing lifecycle-authority convention;
    see docs/playbook-governance.md).

    Append-only: rejects (raises `PlaybookVersionConflictError`) an upload
    for a `(playbook_id, version)` pair that already has a row, rather than
    overwriting the recorded uploader/timestamp.

    New rows land with `notes = ""` (issue #411's one deliberately-mutable
    field, default empty at upload time; set/replaced later via
    `update_playbook_version_notes`).

    Returns the written item.
    """
    table = _playbook_versions_table(dynamodb_resource)
    ts = now_epoch_value if now_epoch_value is not None else now_epoch()
    uploaded_at = int(ts)

    item: dict[str, Any] = {
        "playbook_id": playbook_id,
        "version": version,
        "uploaded_by": uploader_identity,
        "uploaded_at": uploaded_at,
        "status": STATUS_DRAFT,
        "notes": "",
    }
    if content_hash is not None:
        item["content_hash"] = content_hash

    try:
        table.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(playbook_id) AND attribute_not_exists(version)"
            ),
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException as exc:
        raise PlaybookVersionConflictError(
            f"playbook version already recorded: playbook_id={playbook_id!r} "
            f"version={version!r} (append-only — re-uploads must use a new "
            "version identifier)"
        ) from exc

    return item


def _get_version_item(
    playbook_id: str,
    version: str,
    dynamodb_resource: Any,
) -> dict[str, Any] | None:
    table = _playbook_versions_table(dynamodb_resource)
    resp = table.get_item(Key={"playbook_id": playbook_id, "version": version})
    return resp.get("Item")


def _find_active_item(
    playbook_id: str,
    dynamodb_resource: Any,
) -> dict[str, Any] | None:
    """Return the currently `active` row for `playbook_id`, or None.

    Exactly one bundle is active per playbook id at a time (docs/
    playbook-governance.md -> "Release-bundle lifecycle"); this scans the
    (small, per-playbook) version set for the row currently carrying
    `status == "active"`.
    """
    table = _playbook_versions_table(dynamodb_resource)
    resp = table.query(KeyConditionExpression=Key("playbook_id").eq(playbook_id))
    for item in resp.get("Items", []):
        if item.get("status") == STATUS_ACTIVE:
            return item
    return None


def activate_playbook_version(
    playbook_id: str,
    version: str,
    actor_identity: str,
    dynamodb_resource: Any,
    now_epoch_value: float | None = None,
) -> dict[str, Any]:
    """Activate a specific, previously-uploaded playbook/release-bundle
    version — mark it the active one (issue #79 v1 scope).

    Exactly one bundle is active per playbook id: if a different version
    is currently `active`, it is demoted to `retired` (its content-
    addressed snapshot is preserved, never deleted — docs/playbook-
    governance.md -> "Release-bundle lifecycle") as part of the same
    activation. Writes one append-only audit record (actor, action,
    target, before/after status, content hash) to the `audit` table
    (ARCHITECTURE.md -> "Audit posture": "Release-bundle activations and
    rollbacks").

    Raises `PlaybookVersionNotFoundError` if `(playbook_id, version)` has
    no recorded upload row (nothing to activate).

    Returns the activated row.
    """
    target = _get_version_item(playbook_id, version, dynamodb_resource)
    if target is None:
        raise PlaybookVersionNotFoundError(
            f"no uploaded playbook version to activate: playbook_id={playbook_id!r} "
            f"version={version!r}"
        )

    now = now_epoch_value if now_epoch_value is not None else now_epoch()
    before_status = target.get("status")

    prior_active = _find_active_item(playbook_id, dynamodb_resource)
    table = _playbook_versions_table(dynamodb_resource)

    if prior_active is not None and prior_active.get("version") != version:
        table.update_item(
            Key={"playbook_id": playbook_id, "version": prior_active["version"]},
            UpdateExpression="SET #status = :retired",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":retired": STATUS_RETIRED},
        )

    table.update_item(
        Key={"playbook_id": playbook_id, "version": version},
        UpdateExpression="SET #status = :active",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":active": STATUS_ACTIVE},
    )

    _write_audit_entry(
        dynamodb_resource=dynamodb_resource,
        actor=actor_identity,
        action="release_bundle_activate",
        target=f"{playbook_id}#{version}",
        detail={
            "playbook_id": playbook_id,
            "version": version,
            "before_status": before_status,
            "after_status": STATUS_ACTIVE,
            "prior_active_version": (
                prior_active["version"]
                if prior_active is not None and prior_active.get("version") != version
                else None
            ),
            "content_hash": target.get("content_hash"),
        },
        now_epoch_value=now,
    )

    result = dict(target)
    result["status"] = STATUS_ACTIVE
    return result


def activate_release_bundle(
    playbook_id: str,
    version: str,
    actor_identity: str,
    dynamodb_resource: Any,
    now_epoch_value: float | None = None,
) -> dict[str, Any]:
    """Real playbook-activation path (issue #242): activates
    `(playbook_id, version)` the same way `activate_playbook_version` does
    (issue #79's v1 slice), but closes the two gaps that slice's module
    docstring explicitly deferred:

      1. **Gate 7 enforcement** (ARCHITECTURE.md / docs/playbook-
         governance.md "Gate 7 — approved hashes match the artifacts being
         promoted"): asserts `playbook_versions.content_hash ==
         playbook_versions.legal_approval.content_hash` for the target
         version BEFORE activating. A missing `content_hash`, a missing
         `legal_approval`, or a mismatch between the two raises
         `PlaybookVersionGate7MismatchError` and leaves the version
         untouched — the bundle cannot be activated (the bytes changed
         after approval, or were never approved at all).
      2. **Resolver wiring** (issue #194's read side): on success, writes
         `playbooks.active_release_bundle_hash` = the activated version's
         `content_hash`, so `reviews.resolve_active_release_bundle_hash`
         (the pipeline's single resolution point) actually serves the
         newly activated bundle. Before this, activation only flipped
         `playbook_versions.status`, which the resolver never reads —
         activating a bundle had no effect on what the review pipeline
         served.

    This wraps, and does not modify, `activate_playbook_version` — the
    existing v1 activate/rollback behavior (issue #79, including its own
    audit trail write) is preserved unchanged for callers that still use
    it directly.

    Raises:
      `PlaybookVersionNotFoundError` — no uploaded row for
        `(playbook_id, version)`.
      `PlaybookVersionGate7MismatchError` — Gate 7 check fails.

    Returns the activated row (same shape as `activate_playbook_version`).
    """
    target = _get_version_item(playbook_id, version, dynamodb_resource)
    if target is None:
        raise PlaybookVersionNotFoundError(
            f"no uploaded playbook version to activate: playbook_id={playbook_id!r} "
            f"version={version!r}"
        )

    content_hash = target.get("content_hash")
    legal_approval = target.get("legal_approval") or {}
    approved_content_hash = legal_approval.get("content_hash")

    if not content_hash or content_hash != approved_content_hash:
        raise PlaybookVersionGate7MismatchError(
            "Gate 7 mismatch: approved hash does not match the artifact "
            f"being promoted for playbook_id={playbook_id!r} version={version!r} "
            f"(content_hash={content_hash!r}, "
            f"legal_approval.content_hash={approved_content_hash!r}) — "
            "the bundle cannot be activated."
        )

    activated = activate_playbook_version(
        playbook_id=playbook_id,
        version=version,
        actor_identity=actor_identity,
        dynamodb_resource=dynamodb_resource,
        now_epoch_value=now_epoch_value,
    )

    playbooks_table = dynamodb_resource.Table(os.environ["PLAYBOOKS_TABLE"])
    playbooks_table.update_item(
        Key={"playbook_id": playbook_id},
        UpdateExpression="SET active_release_bundle_hash = :h",
        ExpressionAttributeValues={":h": content_hash},
    )

    return activated


def rollback_playbook_version(
    playbook_id: str,
    version: str,
    actor_identity: str,
    dynamodb_resource: Any,
    now_epoch_value: float | None = None,
) -> dict[str, Any]:
    """Roll back to a previously-active playbook/release-bundle version —
    restore it as active (issue #79 v1 scope).

    A valid rollback target is a version this module previously demoted
    from `active` to `retired` (i.e. it really was the active bundle at
    some point) — rolling back to a version that was never active is not
    a "rollback", it is a first activation; callers should use
    `activate_playbook_version` for that. Any version currently `active`
    is demoted to `retired` as part of the same rollback, exactly as in
    `activate_playbook_version`. Writes one append-only audit record
    (action `release_bundle_rollback`) to the `audit` table.

    Raises:
      `PlaybookVersionNotFoundError` if `(playbook_id, version)` has no
        recorded upload row.
      `PlaybookVersionRollbackError` if the target version's current
        status is not `retired` (never previously active).

    Returns the restored (now active) row.
    """
    target = _get_version_item(playbook_id, version, dynamodb_resource)
    if target is None:
        raise PlaybookVersionNotFoundError(
            f"no uploaded playbook version to roll back to: playbook_id={playbook_id!r} "
            f"version={version!r}"
        )

    if target.get("status") != STATUS_RETIRED:
        raise PlaybookVersionRollbackError(
            f"cannot roll back to playbook_id={playbook_id!r} version={version!r}: "
            f"status is {target.get('status')!r}, not {STATUS_RETIRED!r} — only a "
            "previously-active (now retired) version is a valid rollback target"
        )

    now = now_epoch_value if now_epoch_value is not None else now_epoch()
    prior_active = _find_active_item(playbook_id, dynamodb_resource)
    table = _playbook_versions_table(dynamodb_resource)

    if prior_active is not None and prior_active.get("version") != version:
        table.update_item(
            Key={"playbook_id": playbook_id, "version": prior_active["version"]},
            UpdateExpression="SET #status = :retired",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":retired": STATUS_RETIRED},
        )

    table.update_item(
        Key={"playbook_id": playbook_id, "version": version},
        UpdateExpression="SET #status = :active",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":active": STATUS_ACTIVE},
    )

    _write_audit_entry(
        dynamodb_resource=dynamodb_resource,
        actor=actor_identity,
        action="release_bundle_rollback",
        target=f"{playbook_id}#{version}",
        detail={
            "playbook_id": playbook_id,
            "version": version,
            "before_status": STATUS_RETIRED,
            "after_status": STATUS_ACTIVE,
            "prior_active_version": (
                prior_active["version"]
                if prior_active is not None and prior_active.get("version") != version
                else None
            ),
            "content_hash": target.get("content_hash"),
        },
        now_epoch_value=now,
    )

    result = dict(target)
    result["status"] = STATUS_ACTIVE
    return result


def update_playbook_version_notes(
    playbook_id: str,
    version: str,
    notes: str,
    actor_identity: str,
    dynamodb_resource: Any,
    now_epoch_value: float | None = None,
) -> dict[str, Any]:
    """Set (or replace) the admin-editable `notes` string on a previously-
    uploaded playbook version (issue #411).

    `notes` is the ONE deliberately-mutable field on an otherwise-immutable
    `playbook_versions` row: unlike `status` (the lifecycle authority —
    docs/playbook-governance.md "Status authority") or `content_hash`
    (fixed at upload time, never re-hashed), `notes` may be set and later
    freely replaced by an admin without affecting version identity, Gate
    7, or any lifecycle transition. This function only ever touches the
    `notes` attribute of an EXISTING row — re-uploading a version's
    *content* remains rejected by `record_playbook_version_upload`'s
    append-only `ConditionExpression`, entirely unaffected by this
    function.

    Appends one audit record (action `playbook_version_notes_update`) to
    the `audit` table — identifiers and a `notes_length` only, never the
    note text itself, preserving this module's "never document substance"
    audit posture (see module docstring's "De-branding" section) even
    though `notes` — unlike every other field this module writes — is
    free-form admin-authored text that could otherwise carry stray content
    or branding into the append-only trail.

    Raises `PlaybookVersionNotFoundError` if `(playbook_id, version)` has
    no recorded upload row.

    Returns the updated row.
    """
    target = _get_version_item(playbook_id, version, dynamodb_resource)
    if target is None:
        raise PlaybookVersionNotFoundError(
            f"no uploaded playbook version to update notes for: "
            f"playbook_id={playbook_id!r} version={version!r}"
        )

    now = now_epoch_value if now_epoch_value is not None else now_epoch()
    table = _playbook_versions_table(dynamodb_resource)
    table.update_item(
        Key={"playbook_id": playbook_id, "version": version},
        UpdateExpression="SET notes = :notes",
        ExpressionAttributeValues={":notes": notes},
    )

    _write_audit_entry(
        dynamodb_resource=dynamodb_resource,
        actor=actor_identity,
        action="playbook_version_notes_update",
        target=f"{playbook_id}#{version}",
        detail={
            "playbook_id": playbook_id,
            "version": version,
            "notes_length": len(notes),
        },
        now_epoch_value=now,
    )

    result = dict(target)
    result["notes"] = notes
    return result


def get_active_version_notes(
    playbook_id: str,
    dynamodb_resource: Any,
) -> str:
    """The currently-`active` version's `notes` for `playbook_id`, or `""`
    if there is no active version row or the active row simply has no note
    set. (Issue #412/#433: the playbook the image ships with gets a real
    `playbook_versions` row from the deploy seed — precisely so its shipped
    note has somewhere to live and stays admin-editable like any other
    playbook's — so it is not a no-row case either.)

    Thin wrapper over `_find_active_item` so cross-module callers (issue
    #411's `GET /api/playbooks` catalog in `src/review_routes.py`) don't
    reach into this module's private helper directly.
    """
    item = _find_active_item(playbook_id, dynamodb_resource)
    if item is None:
        return ""
    return item.get("notes") or ""


# ---------------------------------------------------------------------------
# Catalog overrides: rename + remove (issue #412).
#
# WHY THESE LIVE IN DYNAMODB AND NOT `playbooks/registry.json`: the registry
# is a file baked into the image and re-read at boot (see
# `src/review_routes.py::_load_playbook_catalog`, which enumerates it). A
# rename or removal written to that file would not survive the next deploy,
# and the file is not writable at runtime anyway. So the catalog enumerates
# the registry and layers these per-playbook DB overrides on top — which is
# what makes a registry-listed playbook (including the one the image ships
# with) genuinely renamable and removable through the normal admin path,
# rather than fixed by whatever the image happened to ship.
# ---------------------------------------------------------------------------


def get_playbook_overrides(
    playbook_id: str,
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """The admin-set catalog overrides on this playbook's `playbooks` row:
    `display_name` (a rename, or None if never renamed) and `removed` (the
    tombstone `remove_playbook` writes). Never raises; a playbook with no
    row yet simply has no overrides."""
    table = dynamodb_resource.Table(os.environ["PLAYBOOKS_TABLE"])
    item = table.get_item(Key={"playbook_id": playbook_id}).get("Item") or {}
    return {
        "display_name": item.get("display_name") or None,
        "removed": bool(item.get("removed", False)),
    }


def rename_playbook(
    playbook_id: str,
    display_name: str,
    actor_identity: str,
    dynamodb_resource: Any,
    now_epoch_value: float | None = None,
) -> dict[str, Any]:
    """Set this playbook's catalog `display_name` (issue #412).

    A pure presentation override: it does not touch the playbook's
    `playbook_id` (the stable key every version row, review record, and
    registry lookup is keyed on — renaming that would orphan history), any
    version row, or the active bundle. Clearing it (empty string) restores
    the registry's shipped name.

    Appends one audit record (`playbook_renamed`) carrying identifiers and
    both names — a display name is admin-authored presentation text, not
    document substance, and the previous value is what makes the rename
    reversible from the trail.
    """
    now = now_epoch_value if now_epoch_value is not None else now_epoch()
    table = dynamodb_resource.Table(os.environ["PLAYBOOKS_TABLE"])
    previous = (table.get_item(Key={"playbook_id": playbook_id}).get("Item") or {}).get(
        "display_name"
    )

    table.update_item(
        Key={"playbook_id": playbook_id},
        UpdateExpression="SET display_name = :d",
        ExpressionAttributeValues={":d": display_name},
    )

    _write_audit_entry(
        dynamodb_resource=dynamodb_resource,
        actor=actor_identity,
        action="playbook_renamed",
        target=playbook_id,
        detail={
            "playbook_id": playbook_id,
            "previous_display_name": previous or "",
            "display_name": display_name,
        },
        now_epoch_value=now,
    )

    return {"playbook_id": playbook_id, "display_name": display_name}


def remove_playbook(
    playbook_id: str,
    actor_identity: str,
    dynamodb_resource: Any,
    now_epoch_value: float | None = None,
) -> dict[str, Any]:
    """Remove a playbook from the catalog (issue #412).

    Deletes every `playbook_versions` row for the playbook, clears
    `playbooks.active_release_bundle_hash` (so it can never resolve as
    active again — `reviews._read_active_release_bundle_hash` returns the
    same no-active-bundle signal as a never-activated playbook), and writes
    a `removed` tombstone the catalog filters on.

    THE TOMBSTONE IS LOAD-BEARING, not bookkeeping: the catalog enumerates
    `playbooks/registry.json`, a file baked into the image, so deleting the
    DB rows alone would leave the playbook re-appearing (as `coming_soon`)
    on the next request. Nothing in this codebase clears the tombstone, so
    removal is a one-way door for every playbook alike — including the one
    the image ships with, whose deploy seed (`src.sample_playbooks`)
    deliberately SKIPS a removed playbook rather than resurrecting it on the
    next container start (issue #433). A generic admin restore belongs with
    the Playbooks admin surface (issue #434), not here.

    Appends one audit record (`playbook_removed`) with identifiers and the
    number of version rows deleted — never document substance.
    """
    now = now_epoch_value if now_epoch_value is not None else now_epoch()

    versions_table = _playbook_versions_table(dynamodb_resource)
    resp = versions_table.query(KeyConditionExpression=Key("playbook_id").eq(playbook_id))
    items = list(resp.get("Items", []))
    for item in items:
        versions_table.delete_item(
            Key={"playbook_id": playbook_id, "version": item["version"]}
        )

    playbooks_table = dynamodb_resource.Table(os.environ["PLAYBOOKS_TABLE"])
    playbooks_table.update_item(
        Key={"playbook_id": playbook_id},
        UpdateExpression="SET active_release_bundle_hash = :empty, removed = :true",
        ExpressionAttributeValues={":empty": "", ":true": True},
    )

    _write_audit_entry(
        dynamodb_resource=dynamodb_resource,
        actor=actor_identity,
        action="playbook_removed",
        target=playbook_id,
        detail={"playbook_id": playbook_id, "versions_deleted": len(items)},
        now_epoch_value=now,
    )

    return {"playbook_id": playbook_id, "removed": True, "versions_deleted": len(items)}


def list_playbook_version_trail(
    playbook_id: str,
    dynamodb_resource: Any,
) -> list[dict[str, Any]]:
    """Read path: the upload audit trail for a playbook, oldest first.

    Returns `playbook_id`, `version`, `uploaded_by`, `uploaded_at`,
    `status`, `content_hash` (only when one was recorded), and the mutable
    `notes` field — never document substance. That is exactly
    `_write_audit_entry`'s posture, which this surface mirrors:
    "Identifiers, statuses, and hashes only — never document substance"
    (ARCHITECTURE.md -> "Audit posture"). A `content_hash` is a digest from
    which no document is recoverable, and it is already written into the
    append-only `audit` table itself by activation/rollback and by the
    shipped-playbook seed — so surfacing it here discloses nothing the
    audit trail does not already hold.

    `notes` is the one free-form admin-authored field (see this module's
    "The one mutable field" section); it is the only value here that could
    carry arbitrary text, which is why it — and not the controlled-
    vocabulary fields around it — is the one the notes audit row records a
    length for rather than a value.

    Records are returned in upload order (ascending `uploaded_at`),
    independent of how the `version` sort-key strings happen to compare
    lexicographically.

    This is the documented assertion point for the "your" (never
    tenant-brand strings) voicing rule on any surface that renders this trail.
    """
    table = _playbook_versions_table(dynamodb_resource)
    resp = table.query(KeyConditionExpression=Key("playbook_id").eq(playbook_id))
    items = list(resp.get("Items", []))
    items.sort(key=lambda item: int(item.get("uploaded_at", 0)))

    trail: list[dict[str, Any]] = []
    for item in items:
        row: dict[str, Any] = {
            "playbook_id": item["playbook_id"],
            "version": item["version"],
            "uploaded_by": item["uploaded_by"],
            "uploaded_at": int(item["uploaded_at"]),
            "status": item.get("status") or STATUS_DRAFT,
            "notes": item.get("notes") or "",
        }
        # Absent rather than present-and-null for rows that predate
        # content-hash recording — mirrors how the item itself is written.
        content_hash = item.get("content_hash")
        if content_hash is not None:
            row["content_hash"] = content_hash
        trail.append(row)

    return trail
