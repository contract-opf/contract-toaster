"""
Retention purge worker — issue #61.

Scheduled + on-demand job that deletes `uploads`/`outputs` documents older
than the configured retention window, and clears the matching Confidential
substance fields on terminal `reviews` rows. Implements the five purge
invariants that are the authoritative statement in
docs/data-handling.md -> "Document retention and purge safety":

  1. Terminal reviews only -- a review in PENDING or RUNNING is an active
     execution and is excluded from every sweep, even a 0-day retroactive
     purge-all.
  2. Snapshot-at-creation -- the window applied to a document is the window
     in effect when its review was created (`retention_window_at_creation`
     on the `reviews` row), not today's global setting.
  3. Legal hold overrides everything -- a review (or corpus document) under
     an active `legal_hold` is never purged, regardless of window or age.
  4. Documents, then matched substance fields -- deleting a document also
     clears the Confidential substance fields (`summary`,
     `issue_rationale_text`) on the matching terminal `reviews` row; the non-substantive
     audit-bearing fields (review_id, status, cost, hashes, timestamps,
     owner_sub) remain untouched.
  5. Dual-control or mandatory delay for retroactive reductions -- lowering
     the global retention window below its current value requires either a
     second admin's confirmation or a 72-hour pending delay (with a GC
     alarm) before the retroactive sweep at the new, lower window is
     permitted to run. Forward-looking changes (raising the window, or a
     future-effective date) apply single-admin, immediately.

See also: RUNBOOK.md -> "Changing document retention" / "Placing and
releasing a legal hold" for the operator narrative this handler implements.

Environment variables:
  REVIEWS_TABLE              DynamoDB reviews table name
  RETENTION_SETTINGS_TABLE   DynamoDB retention_settings table name (one
                              row per environment: setting_id="global")
  UPLOADS_BUCKET             S3 uploads bucket name
  OUTPUTS_BUCKET             S3 outputs bucket name
  RETROACTIVE_REDUCTION_DELAY_SECONDS  delay before a pending reduction may
                              run without a second-admin confirmation
                              (default 259200 = 72 hours)
"""

import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REVIEWS_TABLE = os.environ.get("REVIEWS_TABLE", "")
RETENTION_SETTINGS_TABLE = os.environ.get("RETENTION_SETTINGS_TABLE", "")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")
OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "")
RETROACTIVE_REDUCTION_DELAY_SECONDS = int(
    os.environ.get("RETROACTIVE_REDUCTION_DELAY_SECONDS", str(72 * 3600))
)

GLOBAL_SETTING_ID = "global"

DEFAULT_RETENTION_WINDOW_DAYS = 90

# Issue #34: an explicit sentinel for indefinite preservation -- not a large
# number of days. Mirrors backend/src/retention.py::RETENTION_WINDOW_FOREVER
# (duplicated, not imported, per this file's existing convention of owning
# its own copies of small shared constants -- see GLOBAL_SETTING_ID /
# TERMINAL_REVIEW_STATUSES above -- since this Lambda ships independently of
# backend/src).
RETENTION_WINDOW_FOREVER = "forever"

# Invariant 1: only these review statuses are eligible for purge.
TERMINAL_REVIEW_STATUSES = {
    "DONE",
    "ERROR",
    "ERROR_MANUAL_REVIEW_REQUIRED",
    "MANUAL_REVIEW_REQUIRED",
    "QUARANTINED",
    "SUPERSEDED",
    # Mirrors backend/src/reviews.py's REVIEW_STATUSES_TERMINAL. A cancelled
    # review is terminal, so it must be PURGEABLE: leaving it out would mean a
    # review the user stopped keeps its uploaded contract past the retention
    # window forever, precisely because they stopped it.
    "CANCELLED",
}

# Invariant 4: Confidential substance fields cleared on purge (never the
# non-substantive audit-bearing fields alongside them).
# Issue #518 adds `original_filename`. A contract filename routinely names the
# counterparty -- "Mutual NDA - Acme.docx" is the ordinary case -- so it is
# Confidential substance, not Internal metadata. A purge that deleted the
# document and left the counterparty's name on the row would not be a purge.
# Issue #563 adds `normalization_notes` -- it names the paragraph heading a
# pending tracked change was accepted-all on, which is counterparty clause
# text, the same reasoning that makes summary Confidential rather than
# Internal (docs/data-handling.md field table).
# Issue #486 adds `attorney_disposition_note` -- docs/data-handling.md's
# field table classified it Confidential / "Expires with the document"
# before it had any writer at all; that issue wires `record_disposition`
# behind a real route, so a real attorney note can now land on the row and
# this list must clear it exactly like the substance fields above (mirrored
# in backend/src/retention.py::run_purge_sweep_now -- the two purge
# implementations must stay identical).
# Issue #499 adds `cover_note_draft` -- model-written prose summarizing the
# counterparty's document, the same Confidential-substance shape as summary
# (docs/data-handling.md field table). A purge that deleted the document and
# left this prose behind would not be a purge.
# `summary`, NOT `verdict_summary`: the narrative summary is persisted under
# the attribute name `summary` by every writer -- backend/src/pipeline_runner
# .py's `_write_terminal`/`_write_real_terminal` and infra/lambda/persist/
# handler.py -- because scripts/review_spine.py renames the model's
# `verdict_summary` output key to `summary` when assembling the result.
# Nothing has ever written an attribute literally called `verdict_summary`, so
# listing that name here cleared nothing: the most substance-bearing prose
# field on the row survived every purge on this (the AWS) target too. Kept
# byte-identical to backend/src/retention.py's REMOVE clause, per the
# must-stay-identical rule above.
#
# `issue_rationale_text` is GONE, not renamed: no writer has ever produced an
# attribute by that literal name, on any deployment target, in the entire
# history of this repo. Keeping a phantom name in a purge list is exactly the
# bug this change removes -- it clears nothing while reading as if it does.
# The real per-issue rationale is `issues[].external_rationale_for_footnote`,
# and it does not live on this row at all: it is persisted only in the S3
# analysis artifact (`outputs/{review_id}/analysis.json`), destroyed wholesale
# by this handler's own prefix-scan delete. The same reasoning is why `issues`
# and `critic_delta` are deliberately NOT in this list: nothing ever writes
# them to this row either (commit 8b17028, `reviews.load_analysis_artifact`),
# so listing them would repeat the same mistake for a second and third field.
#
# `toaster_guidance` (issue #398) is added here: the submitter's own
# free-text per-review prose, Confidential per docs/data-handling.md's field
# table, IS written to this row (`reviews._create_review_row`) and was in
# NEITHER purge list -- unlike `issue_rationale_text` above, this was a real
# retention leak, not a phantom-name no-op.
SUBSTANCE_FIELDS = [
    "summary",
    "original_filename",
    "normalization_notes",
    "attorney_disposition_note",
    "cover_note_draft",
    "toaster_guidance",
]


def now_epoch() -> float:
    return time.time()


def _ddb():
    return boto3.resource("dynamodb")


def _s3():
    return boto3.client("s3")


# ---------------------------------------------------------------------------
# Settings: get / dual-control retroactive-reduction gate (invariant 5)
# ---------------------------------------------------------------------------

def get_retention_settings() -> dict[str, Any]:
    table = _ddb().Table(RETENTION_SETTINGS_TABLE)
    resp = table.get_item(Key={"setting_id": GLOBAL_SETTING_ID})
    item = resp.get("Item")
    if not item:
        item = {
            "setting_id": GLOBAL_SETTING_ID,
            "retention_window_days": DEFAULT_RETENTION_WINDOW_DAYS,
            "pending_reduction": None,
        }
        table.put_item(Item=item)
    return item


def _window_rank(window: Any) -> float:
    """Order key for comparing retention windows, including the `forever`
    sentinel (issue #34). `forever` outranks every bounded window: a change
    away from `forever` to any finite value is always a reduction, and a
    change from a finite value to `forever` never is. Mirrors
    backend/src/retention.py::_window_rank."""
    if window == RETENTION_WINDOW_FOREVER:
        return float("inf")
    return float(window)


def is_pending_reduction_ready(pending: dict[str, Any]) -> bool:
    """True once the mandatory delay has elapsed for a reduction awaiting
    second-admin confirmation.

    A pending reduction that has already been confirmed by a second admin
    is applied immediately in request_retention_change and never reaches
    this "waiting on the clock" path.
    """
    requested_at = pending["requested_at"]
    return (now_epoch() - requested_at) >= RETROACTIVE_REDUCTION_DELAY_SECONDS


def request_retention_change(
    new_window_days: int | str,
    actor: str,
    second_admin_confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply, or gate, a change to the global retention window.

    Forward-looking changes (raising the window, or an unchanged value)
    apply single-admin, immediately.

    A retroactive reduction (new_window_days < current window) requires:
      - a second admin's confirmation, where the confirming actor is
        DIFFERENT from the requesting actor (a self-confirmation is
        rejected -- dual control by a single compromised session is not
        dual control); or
      - is placed into a `pending_reduction` state with a 72-hour delay
        and a GC alarm (the alarm-firing side is wired in the CDK
        EventBridge rule / CloudWatch alarm consuming this state; see
        pipeline-stack.ts PurgeWorker construct), applied automatically
        once `is_pending_reduction_ready` returns True on a later sweep.

    Returns a dict with:
      status: "APPLIED" | "PENDING_SECOND_APPROVAL"
      applied_immediately: bool
    """
    settings = get_retention_settings()
    current_window = settings["retention_window_days"]
    table = _ddb().Table(RETENTION_SETTINGS_TABLE)
    now = now_epoch()

    is_retroactive_reduction = _window_rank(new_window_days) < _window_rank(current_window)

    if not is_retroactive_reduction:
        table.update_item(
            Key={"setting_id": GLOBAL_SETTING_ID},
            UpdateExpression="SET retention_window_days = :w, pending_reduction = :none, updated_at = :now",
            ExpressionAttributeValues={":w": new_window_days, ":none": None, ":now": str(int(now))},
        )
        return {"status": "APPLIED", "applied_immediately": True}

    confirmed_by_different_admin = bool(
        second_admin_confirmation
        and second_admin_confirmation.get("actor")
        and second_admin_confirmation["actor"] != actor
    )

    if confirmed_by_different_admin:
        table.update_item(
            Key={"setting_id": GLOBAL_SETTING_ID},
            UpdateExpression="SET retention_window_days = :w, pending_reduction = :none, updated_at = :now",
            ExpressionAttributeValues={":w": new_window_days, ":none": None, ":now": str(int(now))},
        )
        return {"status": "APPLIED", "applied_immediately": False}

    # No valid second-admin confirmation -- enter the 72h pending-delay
    # state. The window itself is NOT lowered yet; only a later sweep that
    # observes is_pending_reduction_ready() == True applies it.
    pending_reduction = {
        "new_window_days": new_window_days,
        "requested_by": actor,
        "requested_at": now,
    }
    table.update_item(
        Key={"setting_id": GLOBAL_SETTING_ID},
        UpdateExpression="SET pending_reduction = :pending, updated_at = :now",
        ExpressionAttributeValues={":pending": pending_reduction, ":now": str(int(now))},
    )
    return {"status": "PENDING_SECOND_APPROVAL", "applied_immediately": False}


def _apply_ready_pending_reduction(settings: dict[str, Any]) -> dict[str, Any]:
    """If a pending reduction's delay has elapsed, apply it and clear the
    pending state. Called at the start of a sweep so a delayed reduction
    takes effect on schedule without a human having to re-click Save."""
    pending = settings.get("pending_reduction")
    if not pending:
        return settings
    if not is_pending_reduction_ready(pending):
        return settings

    table = _ddb().Table(RETENTION_SETTINGS_TABLE)
    now = now_epoch()
    table.update_item(
        Key={"setting_id": GLOBAL_SETTING_ID},
        UpdateExpression="SET retention_window_days = :w, pending_reduction = :none, updated_at = :now",
        ExpressionAttributeValues={
            ":w": pending["new_window_days"],
            ":none": None,
            ":now": str(int(now)),
        },
    )
    settings["retention_window_days"] = pending["new_window_days"]
    settings["pending_reduction"] = None
    return settings


# ---------------------------------------------------------------------------
# Purge sweep (invariants 1-4)
# ---------------------------------------------------------------------------

def _is_legal_held(review: dict[str, Any]) -> bool:
    return bool(review.get("legal_hold"))


def _is_past_retention(review: dict[str, Any]) -> bool:
    """A review is purge-eligible once it is older than ITS OWN snapshotted
    retention_window_at_creation (invariant 2) -- never today's global
    setting, which only governs newly-created reviews and any pending
    retroactive reduction once it takes effect.

    Issue #34: a review snapshotted at the `forever` sentinel is never
    purge-eligible, at any age -- "forever" means never evaluated for purge
    eligibility, not a large number of days (same treatment as the existing
    `skipped_not_yet_eligible` bucket below, just permanently so)."""
    window_days = review.get("retention_window_at_creation", DEFAULT_RETENTION_WINDOW_DAYS)
    if window_days == RETENTION_WINDOW_FOREVER:
        return False
    created_at = float(review.get("created_at", now_epoch()))
    age_seconds = now_epoch() - created_at
    return age_seconds >= (window_days * 86400)


# ---------------------------------------------------------------------------
# Object targeting (issue #454)
#
# The input document is written to `uploads/{owner_sub}/{review_id}/in.docx`
# (backend/src/review_routes.py), and the reviews row records that exact key in
# `upload_s3_key` (backend/src/reviews.py::_create_review_row, issue #449).
# Targeting is derived from the ROW's stored pointer rather than reconstructed
# from a prefix convention, so a future layout change moves the writer and the
# purger together instead of silently desyncing them -- which is exactly how
# `uploads/{review_id}/` (a prefix omitting the owner segment, matching
# nothing) went unnoticed while the sweep reported success.
#
# The prefix scans are the fallback for rows that predate the recorded pointer,
# plus a belt-and-braces sweep of any sibling object under the review's own
# prefix. Both prefixes are review-scoped, so neither can reach another
# review's objects.
#
# Mirrors backend/src/retention.py's block of the same name EXACTLY (this file
# ships independently of backend/src and owns its own copy, per this module's
# existing convention); both must change together.
# ---------------------------------------------------------------------------

UPLOAD_POINTER_FIELDS = ("upload_s3_key", "upload_pointer")
OUTPUT_POINTER_FIELDS = ("output_s3_key",)


def _dedup(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def _list_keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return [obj["Key"] for obj in resp.get("Contents", [])]


def _is_bound_to_review(key: str, review_id: str, root: str) -> bool:
    """A stored pointer is only acted on when it sits under the expected bucket
    root AND names this review, so a corrupted or mis-migrated row can never
    make the sweep delete another review's object."""
    return key.startswith(f"{root}/") and f"/{review_id}/" in key


def _existing_pointer_keys(
    review: dict[str, Any],
    s3: Any,
    bucket: str,
    fields: tuple[str, ...],
    root: str,
) -> list[str]:
    """The row's recorded pointers that are bound to this review AND still
    resolve to an object. A pointer whose object is already gone is not a
    target: the itemised report an operator reads before an irreversible sweep
    must name objects that actually exist, not keys a row happens to carry."""
    review_id = review["review_id"]
    keys = []
    for field in fields:
        key = review.get(field)
        if not key or not _is_bound_to_review(key, review_id, root):
            continue
        if key in _list_keys(s3, bucket, key):
            keys.append(key)
    return keys


def _review_object_targets(review: dict[str, Any]) -> list[tuple[str, str]]:
    """Every (bucket, key) belonging to this review: the row's own recorded
    input/output pointers, plus a scan of the review-scoped prefixes for rows
    that predate those fields."""
    s3 = _s3()
    review_id = review["review_id"]
    owner_sub = review.get("owner_sub") or ""
    targets: list[tuple[str, str]] = []

    if UPLOADS_BUCKET:
        keys = _existing_pointer_keys(
            review, s3, UPLOADS_BUCKET, UPLOAD_POINTER_FIELDS, "uploads"
        )
        prefixes = []
        if owner_sub:
            prefixes.append(f"uploads/{owner_sub}/{review_id}/")
        # Pre-owner-segment layout: harmless when nothing matches, and the only
        # reachable target for a row carrying neither pointer nor owner_sub.
        prefixes.append(f"uploads/{review_id}/")
        for prefix in prefixes:
            keys.extend(_list_keys(s3, UPLOADS_BUCKET, prefix))
        targets.extend((UPLOADS_BUCKET, key) for key in _dedup(keys))

    if OUTPUTS_BUCKET:
        keys = _existing_pointer_keys(
            review, s3, OUTPUTS_BUCKET, OUTPUT_POINTER_FIELDS, "outputs"
        )
        keys.extend(_list_keys(s3, OUTPUTS_BUCKET, f"outputs/{review_id}/"))
        targets.extend((OUTPUTS_BUCKET, key) for key in _dedup(keys))

    return targets


def _surviving_targets(targets: list[tuple[str, str]]) -> list[str]:
    """The targeted keys STILL present after the delete calls. The sweep's
    success record is tied to this outcome, not to having issued the calls:
    before issue #454 a review was recorded as deleted whether or not anything
    was actually deleted."""
    s3 = _s3()
    survivors: list[str] = []
    for bucket, key in targets:
        if key in _list_keys(s3, bucket, key):
            survivors.append(key)
    return survivors


def _log_purge_plan(review_id: str, keys: list[str], dry_run: bool) -> None:
    """Itemise what this sweep is about to delete (issue #454 operator gate).

    The first sweep after the prefix fix deletes a BACKLOG of input documents
    already past their retention window that the broken prefix never matched.
    Deleting them is the intended behaviour; the volume is the surprise, so
    every sweep names the reviews and keys it acts on where an operator sees
    them (CloudWatch), and `dry_run=True` reports the same list without
    deleting anything."""
    prefix = "DRY RUN -- would purge" if dry_run else "purging"
    logger.warning(
        "retention sweep: %s review %s (%d object(s))", prefix, review_id, len(keys)
    )
    for key in keys:
        logger.info("retention sweep: %s %s", prefix, key)


def _delete_review_documents(
    review: dict[str, Any], targets: list[tuple[str, str]] | None = None
) -> list[str]:
    """Delete the review's uploads/outputs objects, returning the targeted keys
    that SURVIVED (empty when the purge fully succeeded). Legal-hold-tagged
    objects are also denied at the storage layer (bucket-policy DENY on
    contract-toaster:legal-hold=true, see data-stack.ts _addLegalHoldPolicy) as a
    backstop -- this function is only ever reached for reviews that already
    passed the application-level hold check in run_purge_sweep.

    Takes the review ROW (not just its id): targeting is derived from the row's
    own recorded pointer -- see `_review_object_targets`. `targets` may be
    passed in by a caller that already resolved them (run_purge_sweep does, to
    itemise the plan before acting on it) so the listing is not repeated."""
    s3 = _s3()
    if targets is None:
        targets = _review_object_targets(review)

    for bucket, key in targets:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "AccessDenied":
                # Storage-layer legal-hold DENY caught something the
                # application-level check missed -- fail loud rather
                # than silently swallow (defense-in-depth working as
                # intended, but this should never happen in practice).
                raise
            raise

    return _surviving_targets(targets)


def _clear_substance_fields(review_id: str, purged_at: str) -> None:
    """Clear every SUBSTANCE_FIELDS attribute and stamp `purged_at` in the
    SAME update -- the durable, checkable fact that this row was purged
    (epoch-seconds string, same convention as `created_at`/`updated_at`).
    `purged_at` is threaded in by the caller (one stamp per sweep run)
    rather than read from anywhere here, so this function never has a
    reason to read the row it is about to clear."""
    table = _ddb().Table(REVIEWS_TABLE)
    remove_expr = "REMOVE " + ", ".join(SUBSTANCE_FIELDS)
    table.update_item(
        Key={"review_id": review_id},
        UpdateExpression=f"SET purged_at = :purged_at {remove_expr}",
        ExpressionAttributeValues={":purged_at": purged_at},
    )


def run_purge_sweep(dry_run: bool = False) -> dict[str, Any]:
    """Run one purge sweep (used for both the scheduled sweep and the
    immediate on-save retroactive sweep -- both modes enforce all five
    invariants identically).

    dry_run=True (issue #454) is the report-only path an operator invokes to
    measure the backlog BEFORE it is irreversibly deleted: it resolves and
    itemises exactly the objects a real sweep would delete, and deletes
    nothing, clears no substance field, and returns an empty `deleted_reviews`.
    Nothing invokes it automatically -- there is no deploy-time or migration
    purge; the existing schedule remains the only mechanism that deletes.

    Returns a summary dict: deleted_reviews, skipped_active, skipped_hold,
    skipped_not_yet_eligible -- mirroring the audit entry each run writes
    (objects considered, deleted, skipped-for-active, skipped-for-hold) --
    plus, per #454: dry_run, eligible_reviews (the backlog), failed_reviews
    (targeted objects survived -> NOT recorded as deleted), objects_by_review
    and object_count (the itemisation).
    """
    settings = get_retention_settings()
    settings = _apply_ready_pending_reduction(settings)

    reviews_table = _ddb().Table(REVIEWS_TABLE)

    # One purged_at stamp for every row this sweep run actually purges --
    # not a fresh now_epoch() per row, so every review swept in the same run
    # reads back the same purge timestamp.
    purged_at = str(int(now_epoch()))

    deleted: list[str] = []
    eligible: list[str] = []
    failed: list[str] = []
    objects_by_review: dict[str, list[str]] = {}
    skipped_active: list[str] = []
    skipped_hold: list[str] = []
    skipped_not_yet_eligible: list[str] = []

    # DynamoDB's scan() returns at most ~1MB of items per call and sets
    # LastEvaluatedKey when more items remain. Keep paging on it until it
    # is absent -- otherwise reviews in the unscanned tail are silently
    # never evaluated for purge eligibility, forever.
    exclusive_start_key = None
    while True:
        if exclusive_start_key is None:
            resp = reviews_table.scan()
        else:
            resp = reviews_table.scan(ExclusiveStartKey=exclusive_start_key)

        for review in resp.get("Items", []):
            review_id = review["review_id"]
            status = review.get("status")

            # Invariant 1: terminal reviews only.
            if status not in TERMINAL_REVIEW_STATUSES:
                skipped_active.append(review_id)
                continue

            # Invariant 3: legal hold overrides everything.
            if _is_legal_held(review):
                skipped_hold.append(review_id)
                continue

            # Invariant 2: snapshot-at-creation eligibility check.
            if not _is_past_retention(review):
                skipped_not_yet_eligible.append(review_id)
                continue

            # Invariant 4: delete documents, then clear matched substance
            # fields.
            eligible.append(review_id)
            targets = _review_object_targets(review)
            objects_by_review[review_id] = [key for _bucket, key in targets]
            _log_purge_plan(review_id, objects_by_review[review_id], dry_run)

            if dry_run:
                continue

            survivors = _delete_review_documents(review, targets)
            if survivors:
                # The success record is tied to the OUTCOME: a review whose
                # targeted object survived is not "purged", so it is neither
                # recorded as deleted nor stripped of its substance fields --
                # the next sweep retries it.
                logger.error(
                    "retention sweep: review %s NOT purged -- %d targeted "
                    "object(s) survived deletion: %s",
                    review_id,
                    len(survivors),
                    ", ".join(survivors),
                )
                failed.append(review_id)
                continue

            _clear_substance_fields(review_id, purged_at)
            deleted.append(review_id)

        exclusive_start_key = resp.get("LastEvaluatedKey")
        if not exclusive_start_key:
            break

    return {
        "dry_run": dry_run,
        "deleted_reviews": deleted,
        "eligible_reviews": eligible,
        "failed_reviews": failed,
        "objects_by_review": objects_by_review,
        "object_count": sum(len(keys) for keys in objects_by_review.values()),
        "skipped_active": skipped_active,
        "skipped_hold": skipped_hold,
        "skipped_not_yet_eligible": skipped_not_yet_eligible,
    }


def handler(event: dict[str, Any] = None, _context: Any = None) -> dict[str, Any]:
    """Entry point for both the scheduled (EventBridge) invocation and the
    on-demand invocation triggered by an admin settings save.

    event (optional):
      {"trigger": "scheduled" | "on_demand_settings_save",
       "dry_run": true | false}
    Both trigger types run the identical run_purge_sweep() -- the retroactive
    behavior on a settings save comes from the window having just changed
    (or a pending reduction having just become ready), not from a different
    code path.

    `"dry_run": true` (issue #454) is the operator's report-only invocation:
    it itemises the backlog a real sweep would delete and deletes nothing.
    It must be requested explicitly -- the scheduled invocation carries no
    `dry_run` key and therefore deletes, exactly as before.
    """
    return run_purge_sweep(dry_run=bool((event or {}).get("dry_run")))
