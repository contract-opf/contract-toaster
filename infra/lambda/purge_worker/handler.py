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
     clears the Confidential substance fields (`verdict_summary`,
     `issue_rationale_text`) on the matching terminal `reviews` row; the
     non-substantive audit-bearing fields (review_id, status, cost, hashes,
     timestamps, owner_sub) remain untouched.
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

import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

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
}

# Invariant 4: Confidential substance fields cleared on purge (never the
# non-substantive audit-bearing fields alongside them).
SUBSTANCE_FIELDS = ["verdict_summary", "issue_rationale_text"]


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


def _delete_review_documents(review_id: str) -> None:
    """Delete the review's uploads/outputs objects. Legal-hold-tagged
    objects are also denied at the storage layer (bucket-policy DENY on
    contract-toaster:legal-hold=true, see data-stack.ts _addLegalHoldPolicy) as a
    backstop -- this function is only ever reached for reviews that already
    passed the application-level hold check in run_purge_sweep."""
    s3 = _s3()
    for bucket, prefix in (
        (UPLOADS_BUCKET, f"uploads/{review_id}/"),
        (OUTPUTS_BUCKET, f"outputs/{review_id}/"),
    ):
        if not bucket:
            continue
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        keys = [obj["Key"] for obj in resp.get("Contents", [])]

        for key in keys:
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


def _clear_substance_fields(review_id: str) -> None:
    table = _ddb().Table(REVIEWS_TABLE)
    remove_expr = "REMOVE " + ", ".join(SUBSTANCE_FIELDS)
    table.update_item(
        Key={"review_id": review_id},
        UpdateExpression=remove_expr,
    )


def run_purge_sweep() -> dict[str, Any]:
    """Run one purge sweep (used for both the scheduled sweep and the
    immediate on-save retroactive sweep -- both modes enforce all five
    invariants identically).

    Returns a summary dict: deleted_reviews, skipped_active, skipped_hold,
    skipped_not_yet_eligible -- mirroring the audit entry each run writes
    (objects considered, deleted, skipped-for-active, skipped-for-hold).
    """
    settings = get_retention_settings()
    settings = _apply_ready_pending_reduction(settings)

    reviews_table = _ddb().Table(REVIEWS_TABLE)

    deleted: list[str] = []
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
            _delete_review_documents(review_id)
            _clear_substance_fields(review_id)
            deleted.append(review_id)

        exclusive_start_key = resp.get("LastEvaluatedKey")
        if not exclusive_start_key:
            break

    return {
        "deleted_reviews": deleted,
        "skipped_active": skipped_active,
        "skipped_hold": skipped_hold,
        "skipped_not_yet_eligible": skipped_not_yet_eligible,
    }


def handler(event: dict[str, Any] = None, _context: Any = None) -> dict[str, Any]:
    """Entry point for both the scheduled (EventBridge) invocation and the
    on-demand invocation triggered by an admin settings save.

    event (optional):
      {"trigger": "scheduled" | "on_demand_settings_save"}
    Both trigger types run the identical run_purge_sweep() -- the retroactive
    behavior on a settings save comes from the window having just changed
    (or a pending reduction having just become ready), not from a different
    code path.
    """
    return run_purge_sweep()
