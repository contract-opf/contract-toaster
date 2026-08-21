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

Issue #462 closes the matching gap on the rollback side: rollback flipped
`playbook_versions.status` but never wrote the resolver field, so a
rollback was invisible to the review pipeline. `rollback_release_bundle`
wraps `rollback_playbook_version` the same way `activate_release_bundle`
wraps `activate_playbook_version`, and writes
`playbooks.active_release_bundle_hash` through the shared
`_write_active_release_bundle_hash` helper both wrappers now call. Per
docs/playbook-governance.md "Gate 7 on rollback", rollback deliberately
does NOT re-run Gate 7 — instead `rollback_playbook_version` restricts
valid targets to versions carrying a durable `activated_at` fact, written
by `activate_playbook_version` (so both `activate_release_bundle` and the
deploy seed's direct call get it for free).

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

## Gate 7's missing write path (`record_legal_approval`)

Issue #242 landed Gate 7's READ side (`activate_release_bundle` asserts
`content_hash == legal_approval.content_hash`), but nothing in the product
ever WROTE `legal_approval` -- only a test, via a raw `update_item`, ever
had. `record_legal_approval` below is that missing write path: an explicit,
audited admin act that names the exact `content_hash` being approved and
refuses (rather than silently recording a lie) if that hash does not match
the version row's own. See its own docstring for the full contract,
including why it is deliberately never called from `record_playbook_
version_upload` or `activate_playbook_version` themselves -- either would
turn Gate 7 into a rubber stamp.

## Catalog union (`list_all_version_playbook_ids`)

Issue #485/#490: a playbook created purely through `POST /api/admin/
playbooks` has no `playbooks/registry.json` entry at all (that file is
baked into the image). `list_all_version_playbook_ids` is the read
`review_routes._load_playbook_catalog` unions with the registry so a
DB-created playbook_id becomes a selectable contract type without an image
rebuild.

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
    version that has never been successfully activated (no durable
    `activated_at` record — see issue #462's "DECISION" section in this
    ticket, written up in docs/playbook-governance.md "Gate 7 on
    rollback"). Rolling back to a version that was never active is just a
    (first) activation, and callers should use `activate_playbook_version`
    / `activate_release_bundle` for that."""


class PlaybookVersionGate7MismatchError(Exception):
    """Raised by `activate_release_bundle` when Gate 7 fails: the target
    version's `content_hash` does not equal its recorded
    `legal_approval.content_hash` (including the case where no
    `legal_approval` was ever recorded). Per ARCHITECTURE.md / docs/
    playbook-governance.md "Gate 7 (approved hashes match the artifacts
    being promoted)", this means the bytes changed after approval (or were
    never approved) and the bundle cannot be activated."""


class PlaybookVersionApprovalMismatchError(Exception):
    """Raised by `record_legal_approval` when the `content_hash` a caller
    names does not equal the target version's OWN, already-recorded
    `content_hash` (including a target that carries no `content_hash` at
    all). Per docs/playbook-governance.md "Gate 7" step 2 -- "The approver
    reviews the playbook at the hash recorded [at upload] and records that
    exact hash" -- approval names the exact bytes it vouches for; recording
    a hash that does not match what is actually on the row would look like
    an approval while approving nothing real (a typo, or an artifact that
    changed since the approver looked at it)."""


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


def version_already_recorded(
    playbook_id: str,
    version: str,
    dynamodb_resource: Any,
) -> bool:
    """True iff `(playbook_id, version)` already has an audit row.

    A plain read, used by the upload route (issue #478 fix round 1) as a
    pre-check to reject a re-upload BEFORE writing anything to the uploads
    S3 bucket -- a write-then-conditional-put ordering lets the S3 object
    land, then orphans it the moment `record_playbook_version_upload`'s
    `ConditionExpression` fires the 409 below, since nothing ever points
    back at what was written. This is a read, not a substitute for that
    ConditionExpression: a race between this check and the eventual
    `record_playbook_version_upload` call is still resolved by the
    conditional write (unchanged), which remains the sole source of truth
    for append-only enforcement.
    """
    table = _playbook_versions_table(dynamodb_resource)
    resp = table.get_item(Key={"playbook_id": playbook_id, "version": version})
    return "Item" in resp


def record_playbook_version_upload(
    playbook_id: str,
    version: str,
    uploader_identity: str,
    dynamodb_resource: Any,
    content_hash: str | None = None,
    now_epoch_value: float | None = None,
    artifact_kind: str | None = None,
    opf_content_hash: str | None = None,
    storage_key: str | None = None,
    accepted_stub_basis: bool = False,
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

    Issue #478 adds four write-once fields, all optional (absent for a
    caller that never parses/validates the artifact -- e.g. every test that
    calls this function directly rather than through the HTTP route):
      - `artifact_kind` -- `"opf-0.3"` / `"opf-0.2"` / `"v1"`
        (`src.playbook_upload.validate_playbook_upload`'s classification).
      - `opf_content_hash` -- the OPF document's own `identity.content_hash`,
        distinct from `content_hash` above (which is the hash of the RAW
        uploaded bytes, whatever their container). Absent for a `v1` upload,
        which carries no `identity` block.
      - `storage_key` -- the S3 key the validated artifact's canonical text
        was persisted at (`src.playbook_upload.storage_key_for`).
      - `accepted_stub_basis` -- True iff the upload carried the engine's
        `compiler.stub_basis_present` watermark AND the caller explicitly
        accepted it (`accept_stub_basis=true`); False (the default) for
        every non-watermarked upload, never omitted, so the trail always
        answers "was a stub-basis acceptance recorded here?" without an
        absent-vs-false ambiguity.

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
        "accepted_stub_basis": accepted_stub_basis,
    }
    if content_hash is not None:
        item["content_hash"] = content_hash
    if artifact_kind is not None:
        item["artifact_kind"] = artifact_kind
    if opf_content_hash is not None:
        item["opf_content_hash"] = opf_content_hash
    if storage_key is not None:
        item["storage_key"] = storage_key

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


def get_active_version_record(
    playbook_id: str,
    dynamodb_resource: Any,
) -> dict[str, Any] | None:
    """Public read of the currently-active `playbook_versions` row for
    `playbook_id`, or None if nothing is active -- issue #479's runtime OPF
    bind: `backend/src/pipeline_runner.py::_load_playbook_bundle` calls this
    to learn whether the active version's `artifact_kind` is an OPF artifact
    (`opf-0.2` / `opf-0.3`) before deciding whether to load the stored,
    validated artifact (`storage_key`, issue #478) instead of falling
    through to the registry's on-disk v1 read. A thin public wrapper around
    `_find_active_item` -- same query, exposed outside this module for a
    caller that only needs to KNOW what is active, not mutate it.
    """
    return _find_active_item(playbook_id, dynamodb_resource)


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

    Also stamps a durable `activated_at` fact (issue #462) onto the
    activated row — the FIRST epoch second this version was ever activated,
    preserved across any later re-activation or rollback (computed here,
    from the row read at the top of this call, rather than via DynamoDB's
    `if_not_exists()` in the `UpdateExpression` — kept as a plain `SET` so
    this stays a single, ordinary conditionless write, matching every other
    update in this module). Unlike `status` (mutated by every subsequent
    lifecycle transition), `activated_at` never moves once set, which is
    exactly what makes it the durable "was this version ever live?" fact
    `rollback_playbook_version` gates on — see docs/playbook-governance.md
    "Gate 7 on rollback". Both callers of this function get the stamp for
    free: `activate_release_bundle` below, and `sample_playbooks.
    seed_shipped_playbook`'s direct call for the shipped playbook (which
    deliberately bypasses Gate 7 and so cannot go through
    `activate_release_bundle`).

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
    activated_at = target.get("activated_at")
    if activated_at is None:
        activated_at = int(now)

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
        UpdateExpression="SET #status = :active, activated_at = :activated_at",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":active": STATUS_ACTIVE, ":activated_at": activated_at},
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
    result["activated_at"] = activated_at
    return result


def _write_active_release_bundle_hash(
    playbook_id: str,
    content_hash: str,
    dynamodb_resource: Any,
) -> None:
    """Point the resolver at `content_hash` for `playbook_id` — the ONE
    write `reviews.resolve_active_release_bundle_hash` reads (issue #194).

    Shared by `activate_release_bundle`, `rollback_release_bundle`, and
    `sample_playbooks.seed_shipped_playbook` (the deploy-time seed) — every
    caller in the tree that is allowed to change what the review pipeline
    serves goes through this exact same write, never an independently
    maintained copy of it (issue #462 — that drift, one path wired to the
    resolver and others not, is the defect this function exists to make
    impossible).
    """
    playbooks_table = dynamodb_resource.Table(os.environ["PLAYBOOKS_TABLE"])
    playbooks_table.update_item(
        Key={"playbook_id": playbook_id},
        UpdateExpression="SET active_release_bundle_hash = :h",
        ExpressionAttributeValues={":h": content_hash},
    )


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

    _write_active_release_bundle_hash(playbook_id, content_hash, dynamodb_resource)

    return activated


def record_legal_approval(
    playbook_id: str,
    version: str,
    content_hash: str,
    actor_identity: str,
    dynamodb_resource: Any,
    now_epoch_value: float | None = None,
) -> dict[str, Any]:
    """Record legal approval of the EXACT bytes at `content_hash` for
    `(playbook_id, version)` -- the missing product path for Gate 7's step 2
    (docs/playbook-governance.md "Gate 7"). `activate_release_bundle` has
    enforced step 3 (`content_hash == legal_approval.content_hash`) since
    issue #242, but nothing in the product ever WROTE `legal_approval` --
    only a test, via a raw `update_item`, ever had. Every real activation
    therefore hit `PlaybookVersionGate7MismatchError` with no remedy: the
    shipped Activate button could never succeed against a real upload.

    This is a deliberate, explicit, AUDITED operator act -- never a side
    effect of uploading or activating a version. Widening either of those
    to write `legal_approval` on their own would delete Gate 7 rather than
    satisfy it (an upload would then be self-approving). It is also
    distinct from `sample_playbooks.seed_shipped_playbook`'s Gate-7 BYPASS
    (that module's docstring): the seed activates WITHOUT ever calling this
    function, because nobody with real legal authority reviewed shipped
    sample content -- fabricating an approval record for it here would
    widen that bypass rather than honor its reasoning. Nothing in this
    module calls `record_legal_approval` from any other function in this
    file.

    The caller supplies the exact `content_hash` they are approving --
    never "whatever the row currently has" -- and this function REFUSES
    (`PlaybookVersionApprovalMismatchError`) unless that hash equals the
    target row's own recorded `content_hash`. An approval record can
    therefore never be created for a hash that does not match reality (a
    typo, or bytes that changed since the approver looked at them) -- the
    exact governance failure mode Gate 7 exists to catch, just moved one
    step earlier, to the moment the approval itself is recorded rather than
    left to be discovered at activation.

    Appends one audit record (`playbook_version_legal_approval`) naming the
    approver identity and the exact hash approved -- a content hash, from
    which no document is recoverable, the same posture as every other hash
    this module already writes into the audit table (see
    `list_playbook_version_trail`'s docstring).

    Raises `PlaybookVersionNotFoundError` if `(playbook_id, version)` has no
    recorded upload row, `PlaybookVersionApprovalMismatchError` if
    `content_hash` does not equal the row's own `content_hash`.

    Returns the updated row.
    """
    target = _get_version_item(playbook_id, version, dynamodb_resource)
    if target is None:
        raise PlaybookVersionNotFoundError(
            f"no uploaded playbook version to approve: playbook_id={playbook_id!r} "
            f"version={version!r}"
        )

    actual_content_hash = target.get("content_hash")
    if not content_hash or content_hash != actual_content_hash:
        raise PlaybookVersionApprovalMismatchError(
            "content_hash mismatch: approval must name the exact bytes recorded for "
            f"this version (playbook_id={playbook_id!r} version={version!r}, "
            f"supplied={content_hash!r}, recorded={actual_content_hash!r}) -- the "
            "artifact may have changed, or the wrong hash was supplied."
        )

    now = now_epoch_value if now_epoch_value is not None else now_epoch()
    approved_at = int(now)
    table = _playbook_versions_table(dynamodb_resource)
    table.update_item(
        Key={"playbook_id": playbook_id, "version": version},
        UpdateExpression="SET legal_approval = :approval",
        ExpressionAttributeValues={
            ":approval": {
                "content_hash": content_hash,
                "approved_by": actor_identity,
                "approved_at": approved_at,
            }
        },
    )

    _write_audit_entry(
        dynamodb_resource=dynamodb_resource,
        actor=actor_identity,
        action="playbook_version_legal_approval",
        target=f"{playbook_id}#{version}",
        detail={
            "playbook_id": playbook_id,
            "version": version,
            "content_hash": content_hash,
        },
        now_epoch_value=now,
    )

    result = dict(target)
    result["legal_approval"] = {
        "content_hash": content_hash,
        "approved_by": actor_identity,
        "approved_at": approved_at,
    }
    return result


def rollback_playbook_version(
    playbook_id: str,
    version: str,
    actor_identity: str,
    dynamodb_resource: Any,
    now_epoch_value: float | None = None,
) -> dict[str, Any]:
    """Roll back to a previously-active playbook/release-bundle version —
    restore it as active (issue #79 v1 scope, target-eligibility rule
    superseded by issue #462).

    A valid rollback target is a version that carries a durable
    `activated_at` fact — i.e. it was, at some point, successfully
    activated through `activate_playbook_version` (directly, via
    `activate_release_bundle`, or via the deploy seed's
    `sample_playbooks.seed_shipped_playbook`). Rolling back to a version
    that has never been activated is not a "rollback", it is a first
    activation; callers should use `activate_playbook_version` /
    `activate_release_bundle` for that. `status` alone is NOT the
    eligibility test — see docs/playbook-governance.md "Gate 7 on
    rollback" for why the durable `activated_at` fact and not the mutable
    `status` field is what gates this, and why rollback deliberately does
    NOT re-run Gate 7 on the target. Any version currently `active` is
    demoted to `retired` as part of the same rollback, exactly as in
    `activate_playbook_version`. Writes one append-only audit record
    (action `release_bundle_rollback`) to the `audit` table, recording
    that Gate 7 was not re-run and why.

    Raises:
      `PlaybookVersionNotFoundError` if `(playbook_id, version)` has no
        recorded upload row.
      `PlaybookVersionRollbackError` if the target version has no durable
        `activated_at` record (never successfully activated).

    Returns the restored (now active) row.
    """
    target = _get_version_item(playbook_id, version, dynamodb_resource)
    if target is None:
        raise PlaybookVersionNotFoundError(
            f"no uploaded playbook version to roll back to: playbook_id={playbook_id!r} "
            f"version={version!r}"
        )

    if target.get("activated_at") is None:
        raise PlaybookVersionRollbackError(
            "That version has never been activated; there is nothing to roll "
            f"back to. (playbook_id={playbook_id!r} version={version!r})"
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
        action="release_bundle_rollback",
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
            # Issue #462 DECISION: rollback never re-runs Gate 7 -- the
            # restriction to previously-activated targets (see the
            # PlaybookVersionRollbackError check above) is what makes that
            # safe. Recorded explicitly so the trail states the omission
            # rather than leaving it implicit.
            "gate7_reevaluated": False,
            "gate7_skip_reason": "previously_activated_target",
        },
        now_epoch_value=now,
    )

    result = dict(target)
    result["status"] = STATUS_ACTIVE
    return result


def rollback_release_bundle(
    playbook_id: str,
    version: str,
    actor_identity: str,
    dynamodb_resource: Any,
    now_epoch_value: float | None = None,
) -> dict[str, Any]:
    """Real playbook-rollback path (issue #462): rolls back to
    `(playbook_id, version)` the same way `rollback_playbook_version` does
    (issue #79's v1 slice), and closes the gap that left `activate_release_
    bundle` (issue #242) as the ONLY lifecycle action that repointed
    `playbooks.active_release_bundle_hash`. Before this, a rollback flipped
    `playbook_versions.status` back to `active` but never touched the
    resolver -- every review submitted after a rollback kept running under
    the bundle that was just rolled back, while the admin screen, the
    version trail, and the audit row all said the rollback succeeded.

    Deliberately does NOT enforce Gate 7 -- see docs/playbook-governance.md
    "Gate 7 on rollback" for the decision and its rationale. What makes
    that safe is `rollback_playbook_version`'s target-eligibility check:
    only a version with a durable `activated_at` fact (i.e. one that has
    itself already cleared Gate 7, or was seeded, and was actually live at
    some point) is a valid rollback target, so rollback can never confer
    live status on a version that never earned it.

    This wraps, and does not modify, `rollback_playbook_version` -- the
    existing v1 rollback behavior (issue #79, including its own audit
    trail write) is preserved unchanged for callers that still use it
    directly.

    Raises:
      `PlaybookVersionNotFoundError` — no uploaded row for
        `(playbook_id, version)`.
      `PlaybookVersionRollbackError` — the target has never been
        successfully activated (no `activated_at` record), or has no
        recorded `content_hash` to repoint the resolver at.

    Returns the restored (now active) row (same shape as
    `rollback_playbook_version`).
    """
    # Checked up front, before any write, exactly like `activate_release_
    # bundle` checks its own `content_hash` before calling `activate_
    # playbook_version`: a target row with no `content_hash` (a legacy row
    # predating that attribute, or any other row that reached
    # `activated_at` without one) must never be allowed to null out
    # `playbooks.active_release_bundle_hash`. Refusing here -- before
    # `rollback_playbook_version` flips `status` or writes its audit row --
    # keeps a refused rollback a true no-op instead of a rollback that
    # "succeeded" at the status/audit layer while leaving the review
    # pipeline pointed at nothing.
    target = _get_version_item(playbook_id, version, dynamodb_resource)
    if target is None:
        raise PlaybookVersionNotFoundError(
            f"no uploaded playbook version to roll back to: playbook_id={playbook_id!r} "
            f"version={version!r}"
        )
    if not target.get("content_hash"):
        raise PlaybookVersionRollbackError(
            "That version has no recorded content hash; rollback cannot "
            f"repoint the review pipeline at it. (playbook_id={playbook_id!r} "
            f"version={version!r})"
        )

    restored = rollback_playbook_version(
        playbook_id=playbook_id,
        version=version,
        actor_identity=actor_identity,
        dynamodb_resource=dynamodb_resource,
        now_epoch_value=now_epoch_value,
    )

    content_hash = restored.get("content_hash")
    _write_active_release_bundle_hash(playbook_id, content_hash, dynamodb_resource)

    return restored


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

    Also carries `activated_at` (issue #462) — the durable "was this
    version ever successfully activated?" fact `rollback_playbook_version`
    gates on, absent for a version that has never been activated. This is
    the server-side signal issue #476 asks for: a caller can show a
    "Roll back" action for a version only when `activated_at` is present.

    Also carries `artifact_kind` / `opf_content_hash` / `storage_key`
    (issue #478, absent for a row that predates them or was written by a
    caller that never parsed/validated the artifact) and
    `accepted_stub_basis` (issue #478, always present -- see
    `record_playbook_version_upload`'s docstring for why this one is never
    omitted).

    Also carries `legal_approval_content_hash` -- the hash `record_legal_
    approval` most recently approved for this row, absent when no approval
    has ever been recorded. A caller can compute "is this version Gate-7-
    ready?" by comparing it to `content_hash` above, exactly the check
    `activate_release_bundle` itself makes -- surfaced so an admin UI can
    show that state WITHOUT re-deriving it, never as a second source of
    truth for the gate itself (activation still re-checks for real).

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
            "accepted_stub_basis": bool(item.get("accepted_stub_basis", False)),
        }
        # Absent rather than present-and-null for rows that predate
        # content-hash recording — mirrors how the item itself is written.
        content_hash = item.get("content_hash")
        if content_hash is not None:
            row["content_hash"] = content_hash
        activated_at = item.get("activated_at")
        if activated_at is not None:
            row["activated_at"] = int(activated_at)
        artifact_kind = item.get("artifact_kind")
        if artifact_kind is not None:
            row["artifact_kind"] = artifact_kind
        opf_content_hash = item.get("opf_content_hash")
        if opf_content_hash is not None:
            row["opf_content_hash"] = opf_content_hash
        storage_key = item.get("storage_key")
        if storage_key is not None:
            row["storage_key"] = storage_key
        legal_approval = item.get("legal_approval") or {}
        approved_content_hash = legal_approval.get("content_hash")
        if approved_content_hash is not None:
            row["legal_approval_content_hash"] = approved_content_hash
        trail.append(row)

    return trail


# ---------------------------------------------------------------------------
# Catalog union: DB-only playbook_ids (issue #485/#490).
#
# `playbooks/registry.json` is baked into the image, so a playbook created
# purely through `POST /api/admin/playbooks` (a brand-new playbook_id, no
# registry entry at all) has no OTHER on-disk trace. This is the read
# `review_routes._load_playbook_catalog` needs to find it anyway.
# ---------------------------------------------------------------------------


def list_all_version_playbook_ids(dynamodb_resource: Any) -> set[str]:
    """Every DISTINCT playbook_id carrying at least one `playbook_versions`
    row -- registered (`playbooks/registry.json`) or DB-created (issue
    #485's `POST /api/admin/playbooks`) alike.

    A full table scan: `playbook_versions` has no secondary index keyed by
    playbook_id alone (its own key IS `(playbook_id, version)`), and this
    table holds one row per uploaded version across every playbook this
    deployment has ever seen -- small by the same reasoning `_find_active_
    item`'s docstring already gives for a per-playbook query, just summed
    across playbooks rather than scoped to one. Paginates via
    `LastEvaluatedKey` rather than assuming a single `scan()` call returns
    everything.
    """
    table = _playbook_versions_table(dynamodb_resource)
    ids: set[str] = set()
    # Project the one attribute this read needs. A scan pages on the volume
    # of data it READS, so projecting `playbook_id` instead of whole version
    # rows both cuts the read and makes a second page far less likely --
    # `playbook_id` is not a DynamoDB reserved word, so it needs no
    # ExpressionAttributeNames alias.
    scan_kwargs: dict[str, Any] = {"ProjectionExpression": "playbook_id"}
    while True:
        resp = table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            playbook_id = item.get("playbook_id")
            if playbook_id:
                ids.add(playbook_id)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return ids
