"""
Retention and legal-hold admin API — issue #94.

The admin-UI backing for the enforcement built in #61 (retention purge
worker, `infra/lambda/purge_worker/handler.py`) and the dual-control /
72-hour-delay gate required by #13 for retroactive retention reductions.
This module is the API surface an admin actually drives: reading/writing
the same `retention_settings` global row the purge worker reads, running a
pre-sweep preview ("this change will purge N objects") before a retroactive
confirmation, and per-review legal hold set/release mirrored to the
storage layer per #61.

The dual-control state machine here is a thin admin-facing mirror of
`infra/lambda/purge_worker/handler.py::request_retention_change` — same
five purge invariants (RUNBOOK.md -> "Changing document retention" /
"Placing and releasing a legal hold", docs/data-handling.md purge
invariants 1-5): forward-looking changes apply single-admin and
immediately; a retroactive reduction (new window < current window)
requires either a second, different admin's confirmation or is parked in
`pending_reduction` for the mandatory 72h delay (with the GC alarm wired
in CDK reading that same DynamoDB state -- see pipeline-stack.ts /
observability-stack.ts). A lone admin cannot satisfy dual control by
confirming their own request.

Endpoints this module backs (wired in src/main.py):
  GET  /api/admin/retention                    get_retention_settings
  POST /api/admin/retention                     request_retention_change
  POST /api/admin/retention/preview             preview_purge_sweep
  POST /api/admin/retention/holds/{review_id}    set_legal_hold
  DELETE /api/admin/retention/holds/{review_id}  release_legal_hold
  GET  /api/admin/retention/holds               list_legal_holds

Every action here — retention change, hold set, hold release — is audited
via the same `audit` table field dictionary as src/users.py.

Environment variables consumed:
  REVIEWS_TABLE              DynamoDB reviews table name
  RETENTION_SETTINGS_TABLE   DynamoDB retention_settings table name (one
                             row per environment: setting_id="global")
  AUDIT_TABLE                DynamoDB audit table name (append-only)
  UPLOADS_BUCKET             S3 uploads bucket name
  OUTPUTS_BUCKET             S3 outputs bucket name
"""

import logging
import os
import time
import uuid
from typing import Any

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

GLOBAL_SETTING_ID = "global"

MIN_RETENTION_WINDOW_DAYS = 0
MAX_RETENTION_WINDOW_DAYS = 1095  # 3 years (RUNBOOK.md: "0 days-3 years")

DEFAULT_RETENTION_WINDOW_DAYS = 90

# Issue #34: "forever" is an explicit sentinel, not a large number -- the
# purge worker (and this module's own preview/sweep) treats it as "never
# evaluated for purge eligibility", modeled on the existing
# skipped_not_yet_eligible bucket in infra/lambda/purge_worker/handler.py.
# A bounded integer (even MAX_RETENTION_WINDOW_DAYS) always eventually ages
# past its own window; the sentinel is what makes "never purge" actually
# mean never.
RETENTION_WINDOW_FOREVER = "forever"

# Labeled options for the retention-settings surface (admin UI / API
# response). Release de-branding directive: no tenant-brand strings in any label
# here -- "your" voicing only.
RETENTION_WINDOW_OPTIONS: tuple[dict[str, Any], ...] = (
    {"value": DEFAULT_RETENTION_WINDOW_DAYS, "label": "90 days (default)"},
    {"value": 365, "label": "1 year"},
    {"value": MAX_RETENTION_WINDOW_DAYS, "label": "3 years"},
    {"value": RETENTION_WINDOW_FOREVER, "label": "Forever — never purge your records"},
)


def _window_rank(window: int | str) -> float:
    """Order key for comparing retention windows, including the `forever`
    sentinel. `forever` outranks every bounded window, so a change away
    from `forever` to any finite value is always a reduction, and a change
    from a finite value to `forever` is never one."""
    if window == RETENTION_WINDOW_FOREVER:
        return float("inf")
    return float(window)


def _validate_window(window_days: int | str) -> None:
    """Raises HTTPException(400) unless `window_days` is either the
    `forever` sentinel or an int within [MIN_RETENTION_WINDOW_DAYS,
    MAX_RETENTION_WINDOW_DAYS]."""
    if window_days == RETENTION_WINDOW_FOREVER:
        return
    if isinstance(window_days, bool) or not isinstance(window_days, int):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"retention_window_days must be an integer between "
                f"{MIN_RETENTION_WINDOW_DAYS} and {MAX_RETENTION_WINDOW_DAYS}, "
                f"or {RETENTION_WINDOW_FOREVER!r}."
            ),
        )
    if not (MIN_RETENTION_WINDOW_DAYS <= window_days <= MAX_RETENTION_WINDOW_DAYS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"retention_window_days must be between {MIN_RETENTION_WINDOW_DAYS} "
                f"and {MAX_RETENTION_WINDOW_DAYS}."
            ),
        )

# Same terminal-review-status set as the purge worker (invariant 1) --
# reviews outside this set are active executions and must never be counted
# as purge-eligible by the preview, even far past the retention window.
TERMINAL_REVIEW_STATUSES = {
    "DONE",
    "ERROR",
    "ERROR_MANUAL_REVIEW_REQUIRED",
    "MANUAL_REVIEW_REQUIRED",
    "QUARANTINED",
    "SUPERSEDED",
}


def now_epoch() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Table accessors
# ---------------------------------------------------------------------------


def _reviews_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])


def _settings_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["RETENTION_SETTINGS_TABLE"])


def _audit_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["AUDIT_TABLE"])


def _is_admin(caller_user_row: dict[str, Any]) -> bool:
    """`is_admin` is a DynamoDB `users`-row flag, never a JWT claim -- same
    convention as src/users.py::_is_admin."""
    return bool(caller_user_row.get("is_admin", False))


def _require_admin(caller_user_row: dict[str, Any], detail: str) -> None:
    if not _is_admin(caller_user_row):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _write_audit_entry(
    dynamodb_resource: Any,
    actor: str,
    action: str,
    target: str,
    target_type: str,
    detail: dict[str, Any],
) -> None:
    """Append an immutable audit row. Identifiers and setting values only --
    never document substance (same posture as src/users.py::_write_audit_entry
    and ARCHITECTURE.md -> "Audit posture")."""
    table = _audit_table(dynamodb_resource)
    now = now_epoch()
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
        "target_type": target_type,
        "outcome": "success",
    }
    item.update(detail)
    table.put_item(Item=item)


# ---------------------------------------------------------------------------
# Retention settings -- GET (admin)
# ---------------------------------------------------------------------------


def get_retention_settings(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """GET /api/admin/retention.

    Returns the global retention window and any in-flight pending reduction,
    same row the purge worker reads/writes (single source of truth per #61
    Notes: "Keep the stored settings in a small config table/item so both
    the worker and the future admin UI share one source of truth").
    Defaults to the documented 90-day default when no row exists yet.

    Also exposes `default_retention_window_days` and `window_options`
    (issue #34: "expose the default window in settings") so a caller can
    tell the currently-configured window apart from the documented default,
    and render the selectable choices -- including `forever` -- without
    hard-coding them.
    """
    _require_admin(caller_user_row, "Admin privilege required to view retention settings.")

    table = _settings_table(dynamodb_resource)
    resp = table.get_item(Key={"setting_id": GLOBAL_SETTING_ID})
    item = resp.get("Item")
    if not item:
        result = {
            "setting_id": GLOBAL_SETTING_ID,
            "retention_window_days": DEFAULT_RETENTION_WINDOW_DAYS,
            "pending_reduction": None,
        }
    else:
        result = dict(item)
        # A row may exist with only `pending_reduction` set (e.g. the
        # first-ever change on this environment was a retroactive
        # reduction, which writes pending_reduction without first
        # establishing retention_window_days) -- default to the documented
        # 90-day value rather than KeyError.
        result.setdefault("retention_window_days", DEFAULT_RETENTION_WINDOW_DAYS)
        result.setdefault("pending_reduction", None)

    result["default_retention_window_days"] = DEFAULT_RETENTION_WINDOW_DAYS
    result["window_options"] = [dict(opt) for opt in RETENTION_WINDOW_OPTIONS]
    return result


# ---------------------------------------------------------------------------
# request_retention_change -- POST (admin), dual control (#13 / #61)
# ---------------------------------------------------------------------------


def request_retention_change(
    new_window_days: int | str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    second_admin_confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /api/admin/retention.

    Forward-looking changes (raising the window, or an unchanged value)
    apply single-admin, immediately. A retroactive reduction (new window <
    current window, ranked by `_window_rank` so the `forever` sentinel
    always outranks any bounded window) requires either a second, different
    admin's confirmation, or is parked in `pending_reduction` for the
    mandatory 72-hour delay (applied automatically by the purge worker once
    the delay elapses -- see infra/lambda/purge_worker/handler.py). A lone
    admin cannot satisfy dual control by confirming their own request.

    `new_window_days` may be an int in [0, 1095] (RUNBOOK.md: "0 days-3
    years") or the literal string `"forever"` (issue #34: indefinite
    preservation -- the selected records are never purged).

    Raises HTTPException(403) if the caller is not an admin, 400 if
    new_window_days is neither a valid int in range nor `"forever"`.
    """
    _require_admin(caller_user_row, "Admin privilege required to change retention settings.")

    _validate_window(new_window_days)

    actor = caller_user_row.get("cognito_sub", "")
    settings_table = _settings_table(dynamodb_resource)
    settings = get_retention_settings(caller_user_row, dynamodb_resource)
    current_window = settings["retention_window_days"]
    now = now_epoch()

    is_retroactive_reduction = _window_rank(new_window_days) < _window_rank(current_window)

    if not is_retroactive_reduction:
        settings_table.update_item(
            Key={"setting_id": GLOBAL_SETTING_ID},
            UpdateExpression="SET retention_window_days = :w, pending_reduction = :none, updated_at = :now",
            ExpressionAttributeValues={":w": new_window_days, ":none": None, ":now": str(int(now))},
        )
        result = {"status": "APPLIED", "applied_immediately": True}
        _write_audit_entry(
            dynamodb_resource,
            actor=actor,
            action="retention_change",
            target=GLOBAL_SETTING_ID,
            target_type="retention_settings",
            detail={
                "before_retention_window_days": current_window,
                "after_retention_window_days": new_window_days,
                "result_status": result["status"],
            },
        )
        return result

    confirmed_by_different_admin = bool(
        second_admin_confirmation
        and second_admin_confirmation.get("actor")
        and second_admin_confirmation["actor"] != actor
    )

    if confirmed_by_different_admin:
        settings_table.update_item(
            Key={"setting_id": GLOBAL_SETTING_ID},
            UpdateExpression="SET retention_window_days = :w, pending_reduction = :none, updated_at = :now",
            ExpressionAttributeValues={":w": new_window_days, ":none": None, ":now": str(int(now))},
        )
        result = {"status": "APPLIED", "applied_immediately": False}
    else:
        # No valid second-admin confirmation -- enter the 72h pending-delay
        # state. The window itself is NOT lowered yet.
        pending_reduction = {
            "new_window_days": new_window_days,
            "requested_by": actor,
            "requested_at": now,
        }
        settings_table.update_item(
            Key={"setting_id": GLOBAL_SETTING_ID},
            UpdateExpression="SET pending_reduction = :pending, updated_at = :now",
            ExpressionAttributeValues={":pending": pending_reduction, ":now": str(int(now))},
        )
        result = {"status": "PENDING_SECOND_APPROVAL", "applied_immediately": False}

    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="retention_change",
        target=GLOBAL_SETTING_ID,
        target_type="retention_settings",
        detail={
            "before_retention_window_days": current_window,
            "after_retention_window_days": new_window_days,
            "result_status": result["status"],
        },
    )
    return result


# ---------------------------------------------------------------------------
# preview_purge_sweep -- pre-sweep preview ("this change will purge N objects")
# ---------------------------------------------------------------------------


def _is_legal_held(review: dict[str, Any]) -> bool:
    return bool(review.get("legal_hold"))


def _is_past_retention(review: dict[str, Any]) -> bool:
    """Purge-eligibility check mirroring
    infra/lambda/purge_worker/handler.py::_is_past_retention (invariant 2)
    EXACTLY: a review is eligible once it is older than its OWN snapshotted
    `retention_window_at_creation`, never today's global setting and never
    a proposed/hypothetical window. The actual sweep never consults the
    proposed window, so neither does the preview -- otherwise the preview
    can under-report what an immediate sweep would irreversibly delete.

    Issue #34: a review snapshotted at the `forever` sentinel is never past
    retention, at any age -- "forever" means never evaluated for purge
    eligibility, not a large number of days.
    """
    window_days = review.get("retention_window_at_creation", DEFAULT_RETENTION_WINDOW_DAYS)
    if window_days == RETENTION_WINDOW_FOREVER:
        return False
    created_at = float(review.get("created_at", now_epoch()))
    age_seconds = now_epoch() - created_at
    return age_seconds >= (window_days * 86400)


def preview_purge_sweep(
    proposed_window_days: int,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """POST /api/admin/retention/preview.

    Returns {"purge_count": int, "review_ids": [str, ...]} -- the terminal,
    unheld reviews that would be purged by an immediate sweep right now.

    `proposed_window_days` is accepted for API-shape continuity with
    `request_retention_change`, but -- deliberately -- does NOT affect
    eligibility: `run_purge_sweep_now` (and the production
    worker, infra/lambda/purge_worker/handler.py::run_purge_sweep) purge
    strictly on each review's own snapshotted `retention_window_at_creation`
    and never consult a proposed/global window. Applying the proposed
    window here instead of each review's snapshot would let the preview
    report fewer purges than an immediate sweep actually performs --
    exactly backwards for a preview of an irreversible delete. This
    function reuses the identical `_is_past_retention` the sweep uses so
    the preview count is guaranteed to match the actual sweep outcome.

    Raises HTTPException(403) if the caller is not an admin.
    """
    _require_admin(caller_user_row, "Admin privilege required to preview a purge sweep.")

    reviews_table = _reviews_table(dynamodb_resource)
    resp = reviews_table.scan()

    review_ids: list[str] = []
    for review in resp.get("Items", []):
        status_ = review.get("status")
        if status_ not in TERMINAL_REVIEW_STATUSES:
            continue
        if _is_legal_held(review):
            continue
        if not _is_past_retention(review):
            continue
        review_ids.append(review["review_id"])

    return {"purge_count": len(review_ids), "review_ids": review_ids}


# ---------------------------------------------------------------------------
# Object targeting (issue #454)
#
# The input document is written to `uploads/{owner_sub}/{review_id}/in.docx`
# (src/review_routes.py::post_review), and the reviews row records that
# exact key in `upload_s3_key` (src/reviews.py::_create_review_row, issue #449)
# -- the same pointer `GET /api/reviews/{id}/input` presigns. Targeting is
# therefore derived from the ROW's stored pointer, not reconstructed from a
# prefix convention: a future layout change moves both the writer and the
# purger together, instead of silently desyncing them (which is exactly how
# `uploads/{review_id}/` -- a prefix that omits the owner segment and so never
# matched anything -- survived undetected).
#
# The prefix scans are the fallback for rows that predate the recorded
# pointer, plus a belt-and-braces sweep of any sibling object under the
# review's own prefix. Both prefixes are review-scoped, so neither can reach
# another review's objects.
#
# Mirrored EXACTLY in infra/lambda/purge_worker/handler.py (the copy that runs
# on AWS): both must change together or the deployment silently keeps the
# broken behaviour.
# ---------------------------------------------------------------------------

# Row fields that may carry the authoritative input-document pointer.
# `upload_s3_key` is what `_create_review_row` writes on the reviews row (#449)
# and what get_review_input presigns; `upload_pointer` is the name the
# `review_submissions` row uses for the same value and is accepted here so a
# row copied/backfilled from that table is still targeted.
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


def _list_keys(s3_client: Any, bucket: str, prefix: str) -> list[str]:
    listing = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return [obj["Key"] for obj in listing.get("Contents", [])]


def _is_bound_to_review(key: str, review_id: str, root: str) -> bool:
    """A stored pointer is only ever acted on when it sits under the expected
    bucket root AND names this review -- the same key-binding check
    src/download.py::_validate_s3_key_bound_to_review makes before presigning.
    A corrupted or mis-migrated row can therefore never make the sweep delete
    (or the legal-hold tagger tag) another review's object."""
    return key.startswith(f"{root}/") and f"/{review_id}/" in key


def _existing_pointer_keys(
    review: dict[str, Any],
    s3_client: Any,
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
        if key in _list_keys(s3_client, bucket, key):
            keys.append(key)
    return keys


def _review_object_targets(
    review: dict[str, Any],
    s3_client: Any,
    uploads_bucket: str,
    outputs_bucket: str,
) -> list[tuple[str, str]]:
    """Every (bucket, key) belonging to this review: the row's own recorded
    input/output pointers, plus a scan of the review-scoped prefixes for rows
    that predate those fields."""
    review_id = review["review_id"]
    owner_sub = review.get("owner_sub") or ""
    targets: list[tuple[str, str]] = []

    if uploads_bucket:
        keys = _existing_pointer_keys(
            review, s3_client, uploads_bucket, UPLOAD_POINTER_FIELDS, "uploads"
        )
        prefixes = []
        if owner_sub:
            prefixes.append(f"uploads/{owner_sub}/{review_id}/")
        # Pre-owner-segment layout: harmless when nothing matches, and the
        # only reachable target for a row carrying neither pointer nor
        # owner_sub.
        prefixes.append(f"uploads/{review_id}/")
        for prefix in prefixes:
            keys.extend(_list_keys(s3_client, uploads_bucket, prefix))
        targets.extend((uploads_bucket, key) for key in _dedup(keys))

    if outputs_bucket:
        keys = _existing_pointer_keys(
            review, s3_client, outputs_bucket, OUTPUT_POINTER_FIELDS, "outputs"
        )
        keys.extend(_list_keys(s3_client, outputs_bucket, f"outputs/{review_id}/"))
        targets.extend((outputs_bucket, key) for key in _dedup(keys))

    return targets


def _surviving_targets(
    s3_client: Any, targets: list[tuple[str, str]]
) -> list[str]:
    """The targeted keys that are STILL present after the delete calls.

    The sweep's success record must be tied to the outcome, not to having
    issued the calls: before issue #454 a review was appended to
    `deleted_reviews` whether or not anything was actually deleted, which is
    how a sweep that deleted no input document at all reported success for
    years."""
    survivors: list[str] = []
    for bucket, key in targets:
        if key in _list_keys(s3_client, bucket, key):
            survivors.append(key)
    return survivors


def _log_purge_plan(review_id: str, keys: list[str], dry_run: bool) -> None:
    """Itemise what a sweep is about to delete (issue #454 operator gate).

    The first sweep after the prefix fix deletes a BACKLOG of input documents
    that were already past their retention window but never matched the broken
    prefix. Deleting them is the intended behaviour; the volume is the
    surprise, so every sweep names the reviews and the object keys it acts on
    at a level an operator sees, and `dry_run=True` reports the same list
    without deleting anything."""
    prefix = "DRY RUN -- would purge" if dry_run else "purging"
    logger.warning(
        "retention sweep: %s review %s (%d object(s))", prefix, review_id, len(keys)
    )
    for key in keys:
        logger.info("retention sweep: %s %s", prefix, key)


def run_purge_sweep_now(
    s3_client: Any, dynamodb_resource: Any, dry_run: bool = False
) -> dict[str, Any]:
    """Run an immediate purge sweep using each review's own snapshotted
    window (invariant 2), for parity-checking the preview against the real
    sweep outcome. Mirrors
    infra/lambda/purge_worker/handler.py::run_purge_sweep's invariants 1-4
    exactly (that Lambda is the scheduled/on-demand production entry point;
    this function lets the admin-API test suite and any on-demand "sweep
    now" admin action drive the identical logic against the tables this
    module already has handles to).

    dry_run=True (issue #454) is the report-only path: it resolves and
    itemises exactly the objects a real sweep would delete -- so the backlog's
    scale can be measured before it becomes history -- and deletes nothing,
    clears no substance field, and returns an empty `deleted_reviews`.

    Returns, in addition to the skipped_* buckets:
      dry_run           echo of the mode this sweep ran in
      deleted_reviews   reviews whose every targeted object is now gone
                        (empty on a dry run)
      eligible_reviews  reviews that passed invariants 1-3 -- the backlog
      objects_by_review {review_id: [key, ...]} itemisation of what was (or
                        would be) deleted
      object_count      total objects across `objects_by_review`
      failed_reviews    reviews where a targeted object SURVIVED the delete;
                        deliberately NOT in deleted_reviews, and their
                        substance fields are left intact for the next sweep
    """
    reviews_table = _reviews_table(dynamodb_resource)
    resp = reviews_table.scan()

    uploads_bucket = os.environ.get("UPLOADS_BUCKET", "")
    outputs_bucket = os.environ.get("OUTPUTS_BUCKET", "")

    # One purged_at stamp for every row this sweep run purges -- NOT a fresh
    # now_epoch() per row. Mirrors infra/lambda/purge_worker/handler.py's
    # run_purge_sweep exactly: the two purge implementations must stay
    # identical in how they stamp as well as in what they clear, or a
    # cross-target comparison of "when was this purged" reads differently
    # for no reason.
    purged_at = str(int(now_epoch()))

    deleted: list[str] = []
    eligible: list[str] = []
    failed: list[str] = []
    objects_by_review: dict[str, list[str]] = {}
    skipped_active: list[str] = []
    skipped_hold: list[str] = []
    skipped_not_yet_eligible: list[str] = []

    for review in resp.get("Items", []):
        review_id = review["review_id"]
        status_ = review.get("status")

        if status_ not in TERMINAL_REVIEW_STATUSES:
            skipped_active.append(review_id)
            continue
        if _is_legal_held(review):
            skipped_hold.append(review_id)
            continue
        if not _is_past_retention(review):
            skipped_not_yet_eligible.append(review_id)
            continue

        eligible.append(review_id)
        targets = _review_object_targets(review, s3_client, uploads_bucket, outputs_bucket)
        objects_by_review[review_id] = [key for _bucket, key in targets]
        _log_purge_plan(review_id, objects_by_review[review_id], dry_run)

        if dry_run:
            continue

        for bucket, key in targets:
            s3_client.delete_object(Bucket=bucket, Key=key)

        survivors = _surviving_targets(s3_client, targets)
        if survivors:
            logger.error(
                "retention sweep: review %s NOT purged -- %d targeted object(s) "
                "survived deletion: %s",
                review_id,
                len(survivors),
                ", ".join(survivors),
            )
            failed.append(review_id)
            continue

        reviews_table.update_item(
            Key={"review_id": review_id},
            # Issue #563: normalization_notes names the paragraph heading a
            # pending tracked change was accepted-all on -- counterparty
            # clause text, same Confidential-substance reasoning as summary
            # (docs/data-handling.md field table). Mirrors
            # infra/lambda/purge_worker/handler.py's SUBSTANCE_FIELDS list
            # exactly -- this function's own docstring says the two purge
            # implementations must stay identical.
            #
            # Issue #486: attorney_disposition_note is the same shape --
            # docs/data-handling.md's field table has classified it
            # Confidential / "Expires with the document" since before this
            # field had a writer at all. Wiring `record_disposition` behind
            # a real route (this issue) is what turns that from a
            # theoretical gap into a live one: a real attorney note can now
            # land on the row, and a purge that deleted the document while
            # leaving this note behind would not be a purge.
            #
            # Issue #499 adds cover_note_draft -- model-written prose
            # summarizing the counterparty's document, the same Confidential-
            # substance shape as summary (docs/data-handling.md field table).
            # A purge that deleted the document and left this prose behind
            # would not be a purge.
            #
            # `summary`, NOT `verdict_summary`: the narrative summary is
            # persisted under the attribute name `summary` by all three
            # writers (`pipeline_runner._write_terminal` /
            # `._write_real_terminal` and infra/lambda/persist/handler.py) --
            # `scripts/review_spine.py` renames the model's `verdict_summary`
            # key to `summary` when it assembles the result. Nothing has ever
            # written an attribute literally called `verdict_summary`, so
            # this clause was a NO-OP for the field it was supposed to be
            # clearing: the single most substance-bearing prose field on the
            # row survived every purge sweep since the field shipped, on both
            # deployment targets. `tests/test_summary_attribute_roundtrip.py`
            # now drives the real writer into the real sweep so this can
            # never silently regress to a name nothing writes.
            #
            # `issue_rationale_text` is GONE, not renamed: no writer has ever
            # produced an attribute by that literal name, on any deployment
            # target, in the entire history of this repo. Keeping a phantom
            # name in a purge list is exactly the bug this change removes --
            # it clears nothing while reading as if it does. The real
            # per-issue rationale is `issues[].external_rationale_for_
            # footnote`, and it does not live on this row at all: it is
            # persisted only in the S3 analysis artifact
            # (`outputs/{review_id}/analysis.json`), which the prefix-scan
            # delete above already destroys wholesale. Same reasoning is why
            # `issues` and `critic_delta` are deliberately NOT REMOVE targets
            # here: nothing ever writes them to this row either (commit
            # 8b17028, `reviews.load_analysis_artifact`), so naming them
            # would repeat the same mistake for a second and third field.
            #
            # `toaster_guidance` (issue #398) is added here: the submitter's
            # own free-text per-review prose, Confidential per
            # docs/data-handling.md's field table, IS written to this row
            # (`reviews._create_review_row`) and was in NEITHER purge list --
            # unlike `issue_rationale_text` above, this was a real retention
            # leak, not a phantom-name no-op.
            UpdateExpression=(
                "SET purged_at = :now REMOVE summary, original_filename, "
                "normalization_notes, attorney_disposition_note, "
                "cover_note_draft, toaster_guidance"
            ),
            ExpressionAttributeValues={":now": purged_at},
        )
        deleted.append(review_id)

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


# ---------------------------------------------------------------------------
# Legal hold -- set / release, mirrored to storage layer (#61)
# ---------------------------------------------------------------------------


def _tag_review_objects(
    review: dict[str, Any],
    s3_client: Any,
    held: bool,
) -> None:
    """Mirror the review's legal-hold state onto its S3 objects via the
    `contract-toaster:legal-hold` tag, matching the bucket-policy DENY condition in
    infra/lib/nested/data-stack.ts (StringEquals on
    's3:ExistingObjectTag/contract-toaster:legal-hold' = 'true').

    Takes the review ROW (not just its id) because targeting is derived from
    the row's own recorded pointer -- see `_review_object_targets`. Issue #454:
    this tagger previously listed `uploads/{review_id}/`, which never matched
    the layout the API writes, so a held INPUT document never received the tag
    and the storage-layer DENY backstop covered outputs only."""
    uploads_bucket = os.environ.get("UPLOADS_BUCKET", "")
    outputs_bucket = os.environ.get("OUTPUTS_BUCKET", "")

    for bucket, key in _review_object_targets(
        review, s3_client, uploads_bucket, outputs_bucket
    ):
        if held:
            s3_client.put_object_tagging(
                Bucket=bucket,
                Key=key,
                Tagging={"TagSet": [{"Key": "contract-toaster:legal-hold", "Value": "true"}]},
            )
        else:
            s3_client.delete_object_tagging(Bucket=bucket, Key=key)


def _get_review_or_404(review_id: str, dynamodb_resource: Any) -> dict[str, Any]:
    reviews_table = _reviews_table(dynamodb_resource)
    resp = reviews_table.get_item(Key={"review_id": review_id})
    review = resp.get("Item")
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found.")
    return review


def set_legal_hold(
    review_id: str,
    reason: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    s3_client: Any,
) -> dict[str, Any]:
    """POST /api/admin/retention/holds/{review_id}.

    Sets the per-review `legal_hold` flag (RUNBOOK.md -> "Placing and
    releasing a legal hold"), records who placed it and why, and mirrors
    the hold to the storage layer by tagging the review's S3 objects
    `contract-toaster:legal-hold=true` so the bucket-policy backstop denies deletion
    even if application logic is bypassed (#61 storage-layer enforcement).

    Raises HTTPException(403) if the caller is not an admin, 400 if `reason`
    is empty, 404 if the review does not exist.
    """
    _require_admin(caller_user_row, "Admin privilege required to place a legal hold.")

    if not reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A matter reference / reason is required to place a legal hold.",
        )

    review = _get_review_or_404(review_id, dynamodb_resource)
    actor = caller_user_row.get("cognito_sub", "")

    reviews_table = _reviews_table(dynamodb_resource)
    reviews_table.update_item(
        Key={"review_id": review_id},
        UpdateExpression=(
            "SET legal_hold = :held, legal_hold_reason = :reason, "
            "legal_hold_set_by = :actor, legal_hold_set_at = :now"
        ),
        ExpressionAttributeValues={
            ":held": True,
            ":reason": reason,
            ":actor": actor,
            ":now": str(int(now_epoch())),
        },
    )

    _tag_review_objects(review, s3_client, held=True)

    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="legal_hold_set",
        target=review_id,
        target_type="review",
        detail={"legal_hold_reason": reason},
    )

    return {"review_id": review_id, "legal_hold": True, "legal_hold_reason": reason}


def release_legal_hold(
    review_id: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    s3_client: Any,
) -> dict[str, Any]:
    """DELETE /api/admin/retention/holds/{review_id}.

    Releases a legal hold: clears the review row's `legal_hold` flag and
    removes the storage-layer `contract-toaster:legal-hold` tag. Per RUNBOOK.md, release
    does not purge immediately -- the item simply becomes eligible under the
    current window at the next worker run.

    Raises HTTPException(403) if the caller is not an admin, 404 if the
    review does not exist.
    """
    _require_admin(caller_user_row, "Admin privilege required to release a legal hold.")

    review = _get_review_or_404(review_id, dynamodb_resource)
    actor = caller_user_row.get("cognito_sub", "")

    reviews_table = _reviews_table(dynamodb_resource)
    reviews_table.update_item(
        Key={"review_id": review_id},
        UpdateExpression=(
            "SET legal_hold = :held, legal_hold_released_by = :actor, "
            "legal_hold_released_at = :now"
        ),
        ExpressionAttributeValues={
            ":held": False,
            ":actor": actor,
            ":now": str(int(now_epoch())),
        },
    )

    _tag_review_objects(review, s3_client, held=False)

    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="legal_hold_released",
        target=review_id,
        target_type="review",
        detail={},
    )

    return {"review_id": review_id, "legal_hold": False}


# Fields the hold-list view is allowed to return. Deliberately excludes any
# Confidential document-substance field on the reviews row (e.g.
# summary, issue_rationale_text, original_filename (#518) --
# docs/data-handling.md field table) -- the hold list is an identifiers/hold-metadata view, not a
# review-detail view, and this is exactly the surface the "malicious admin
# or compromised session" threat model (threat-model.md) targets: a
# compromised admin session must not be able to exfiltrate document
# substance merely by listing legal holds.
_HOLD_LIST_FIELDS = (
    "review_id",
    "legal_hold",
    "legal_hold_reason",
    "legal_hold_set_by",
    "legal_hold_set_at",
    "legal_hold_released_by",
    "legal_hold_released_at",
    "status",
)


def list_legal_holds(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> list[dict[str, Any]]:
    """GET /api/admin/retention/holds -- the hold list view.

    Returns only hold-relevant identifiers/metadata (see
    `_HOLD_LIST_FIELDS`) -- never the full reviews row, which carries
    Confidential document-substance fields (`summary`,
    `issue_rationale_text`) that have no business being served through a
    hold-list panel (docs/data-handling.md purge invariant 5;
    threat-model.md "Malicious admin or compromised session").

    Raises HTTPException(403) if the caller is not an admin.
    """
    _require_admin(caller_user_row, "Admin privilege required to view legal holds.")

    reviews_table = _reviews_table(dynamodb_resource)
    resp = reviews_table.scan()
    return [
        {field: r[field] for field in _HOLD_LIST_FIELDS if field in r}
        for r in resp.get("Items", [])
        if r.get("legal_hold")
    ]
