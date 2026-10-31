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

try:  # production runs `src.main`; tests put backend/src on sys.path
    from src import audit_log, retention_alerts
    from src import reviews as reviews_module
    from src.authz import require_admin
    from src.users import json_safe
except ImportError:  # pragma: no cover
    import audit_log  # type: ignore[no-redef]
    import retention_alerts  # type: ignore[no-redef]
    import reviews as reviews_module  # type: ignore[no-redef]
    from authz import require_admin  # type: ignore[no-redef]
    from users import json_safe  # type: ignore[no-redef]

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

# Invariant 1: only terminal reviews are ever purge-eligible -- reviews
# outside this set are active executions and must never be counted or
# purged, even far past the retention window.
#
# This is `reviews.REVIEW_STATUSES_TERMINAL` itself, not a copy. The copy
# this module used to carry left out CANCELLED, so on DTS a review the user
# stopped kept its uploaded contract past the retention window forever,
# precisely because they stopped it. The purge worker Lambda cannot import
# backend/src and keeps its own copy; tests/test_retention_sweep_w6.py holds
# it equal to this one.
TERMINAL_REVIEW_STATUSES = frozenset(reviews_module.REVIEW_STATUSES_TERMINAL)

# Every status a reviews row can carry (issue #52). The sweep and the hold
# list read the table one `status-index` partition at a time, so "every row"
# is spelled as "every partition in this set" -- a hold can sit on a review
# in any status, and the sweep's `skipped_active` bucket has to see the
# in-flight rows it reports.
_ALL_KNOWN_REVIEW_STATUSES = frozenset(
    TERMINAL_REVIEW_STATUSES | reviews_module.REVIEW_STATUSES_NON_TERMINAL
)


def _reviews_by_status(reviews_table: Any, statuses: Any) -> Any:
    """Every row in each of `statuses`, one full `status-index` query per
    status (issue #52). Replaces the table scan these callers used to make
    -- which read ONE page: past the first megabyte, a purge
    sweep silently never evaluated a row, and the preview under-counted what
    that sweep would delete. `reviews._query_by_status` owns the
    `LastEvaluatedKey` loop, so a row is seen exactly once per sweep."""
    for status_ in sorted(statuses):
        yield from reviews_module._query_by_status(reviews_table, status_)


def now_epoch() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Table accessors
# ---------------------------------------------------------------------------


def _reviews_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])


def _settings_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["RETENTION_SETTINGS_TABLE"])


def _audit(dynamodb_resource: Any, **fields: Any) -> dict[str, Any] | None:
    """Append an immutable audit row through `audit_log`, the one writer.
    Identifiers and setting values only -- never document substance
    (ARCHITECTURE.md -> "Audit posture"). Stamped with this module's own
    `now_epoch` so a test that pins the clock pins the audit row too. A
    review-scoped row (a legal hold set or released) is stamped with
    `review_id` for the `review_id-index` by `audit_log` (issue #253)."""
    return audit_log.write_audit_entry(dynamodb_resource, now=now_epoch(), **fields)


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
    require_admin(caller_user_row, "Admin privilege required to view retention settings.")

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
    pending = result.get("pending_reduction")
    if pending:
        # W6.7: the server, not the browser, says whether THIS viewer may
        # confirm -- the requester never may. `apply_after` is filled in for a
        # row parked before it was recorded.
        pending = dict(pending)
        pending.setdefault(
            "apply_after", int(pending.get("requested_at", 0)) + RETROACTIVE_REDUCTION_DELAY_SECONDS
        )
        viewer = caller_user_row.get("cognito_sub", "")
        pending["viewer_can_confirm"] = bool(
            viewer and pending.get("pending_id") and viewer != pending.get("requested_by")
        )
        result["pending_reduction"] = pending
    # The row goes straight into GET /api/admin/retention's JSONResponse, and
    # boto3 reads a stored bounded window (and a pending reduction's numbers)
    # back as Decimals -- the same 500 GET /api/users had (#440). See
    # tests/test_retention_settings_decimal.py.
    return json_safe(result)


# ---------------------------------------------------------------------------
# request_retention_change -- POST (admin), real dual control (#13 / #61, W6.7)
# ---------------------------------------------------------------------------

# Invariant 5: a retroactive reduction waits this long for a second admin
# before it applies itself. Mirrors the purge worker Lambda's
# RETROACTIVE_REDUCTION_DELAY_SECONDS default.
RETROACTIVE_REDUCTION_DELAY_SECONDS = 72 * 3600

# The actor an automatic, delay-expired application is recorded under.
PENDING_REDUCTION_SYSTEM_ACTOR = "system:retention-delay"

_GLOBAL_KEY = {"setting_id": GLOBAL_SETTING_ID}


def _clean_reason(reason: Any) -> str:
    return reason.strip() if isinstance(reason, str) else ""


def request_retention_change(
    new_window_days: int | str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    reason: str = "",
) -> dict[str, Any]:
    """POST /api/admin/retention. Body: {"retention_window_days", "reason"}.

    Forward-looking changes (raising the window, or an unchanged value)
    apply single-admin, immediately. A retroactive reduction (new window <
    current window, ranked by `_window_rank` so `forever` outranks every
    bounded window) NEVER applies from this request (W6.7). It is parked as
    a pending record -- `pending_id`, requester, required `reason`,
    `apply_after` -- that either a DIFFERENT authenticated admin confirms
    (`confirm_pending_reduction`), any admin cancels
    (`cancel_pending_reduction`), or the purge sweep applies once the 72-hour
    delay has passed (`apply_ready_pending_reduction` on DTS, the Lambda on
    AWS). The free-text "second admin" this request used to accept was
    self-attested, so one admin could apply a 0-day window on their own.

    Raises HTTPException(403) for a non-admin, 400 for an invalid window or
    a reduction without a reason, 409 when a reduction is already pending.
    """
    require_admin(caller_user_row, "Admin privilege required to change retention settings.")

    _validate_window(new_window_days)

    actor = caller_user_row.get("cognito_sub", "")
    settings_table = _settings_table(dynamodb_resource)
    settings = get_retention_settings(caller_user_row, dynamodb_resource)
    current_window = settings["retention_window_days"]
    current_pending = settings.get("pending_reduction") or None
    now = int(now_epoch())

    is_retroactive_reduction = _window_rank(new_window_days) < _window_rank(current_window)

    if not is_retroactive_reduction:
        settings_table.update_item(
            Key=_GLOBAL_KEY,
            UpdateExpression="SET retention_window_days = :w, pending_reduction = :none, updated_at = :now",
            ExpressionAttributeValues={":w": new_window_days, ":none": None, ":now": str(now)},
        )
        result = {"status": "APPLIED", "applied_immediately": True}
        detail: dict[str, Any] = {
            "before_retention_window_days": current_window,
            "after_retention_window_days": new_window_days,
            "result_status": result["status"],
        }
        if current_pending:
            # A forward change supersedes a parked reduction; say so.
            detail["cleared_pending_id"] = current_pending.get("pending_id", "")
        _audit(
            dynamodb_resource,
            actor=actor,
            action="retention_change",
            target=GLOBAL_SETTING_ID,
            target_type="retention_settings",
            detail=detail,
        )
        return result

    reason = _clean_reason(reason)
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A reason is required to request a retention reduction.",
        )
    if current_pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A retention reduction is already pending. Confirm or cancel it "
                "before requesting another."
            ),
        )

    pending_reduction = {
        "pending_id": uuid.uuid4().hex,
        "new_window_days": new_window_days,
        "previous_window_days": current_window,
        "requested_by": actor,
        # Whole epoch seconds: boto3 refuses a Python float on every write
        # (tests/test_retention_pending_reduction_moto.py).
        "requested_at": now,
        "apply_after": now + RETROACTIVE_REDUCTION_DELAY_SECONDS,
        "reason": reason,
    }
    try:
        settings_table.update_item(
            Key=_GLOBAL_KEY,
            UpdateExpression="SET pending_reduction = :pending, updated_at = :now",
            ConditionExpression="attribute_not_exists(pending_reduction) OR pending_reduction = :none",
            ExpressionAttributeValues={
                ":pending": pending_reduction,
                ":now": str(now),
                ":none": None,
            },
        )
    except Exception as exc:
        if _is_conditional_failure(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A retention reduction is already pending.",
            ) from None
        raise

    _audit(
        dynamodb_resource,
        actor=actor,
        action="retention_reduction_requested",
        target=GLOBAL_SETTING_ID,
        target_type="retention_settings",
        detail={
            "pending_id": pending_reduction["pending_id"],
            "before_retention_window_days": current_window,
            "after_retention_window_days": new_window_days,
            "apply_after": pending_reduction["apply_after"],
            "reason": reason,
            "result_status": "PENDING_SECOND_APPROVAL",
        },
    )
    # W6.8: the GC alarm -- a structured log line the AWS metric filter
    # counts, plus the optional DTS push. After the audit row, and never a
    # gate: the reduction is parked whether or not the alarm got through.
    retention_alerts.announce_reduction_request(pending_reduction, current_window)
    return {
        "status": "PENDING_SECOND_APPROVAL",
        "applied_immediately": False,
        "pending_id": pending_reduction["pending_id"],
        "apply_after": pending_reduction["apply_after"],
    }


def _current_pending(dynamodb_resource: Any, pending_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(settings row, pending record) for `pending_id`, or 404."""
    row = (
        _settings_table(dynamodb_resource)
        .get_item(Key=_GLOBAL_KEY, ConsistentRead=True)
        .get("Item")
        or {}
    )
    pending = row.get("pending_reduction") or {}
    if not pending_id or pending.get("pending_id") != pending_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No pending retention reduction with that id.",
        )
    return row, pending


def _apply_pending(dynamodb_resource: Any, pending: dict[str, Any]) -> None:
    """Lower the window to the pending value and clear the record -- only if
    that same record is still the pending one (a concurrent cancel or
    confirm loses cleanly)."""
    _settings_table(dynamodb_resource).update_item(
        Key=_GLOBAL_KEY,
        UpdateExpression="SET retention_window_days = :w, pending_reduction = :none, updated_at = :now",
        ConditionExpression="pending_reduction.pending_id = :pid",
        ExpressionAttributeValues={
            ":w": pending["new_window_days"],
            ":none": None,
            ":now": str(int(now_epoch())),
            ":pid": pending["pending_id"],
        },
    )


def _pending_audit_detail(row: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any]:
    return {
        "pending_id": pending.get("pending_id", ""),
        "requested_by": pending.get("requested_by", ""),
        "requested_at": pending.get("requested_at"),
        "reason": pending.get("reason", ""),
        "before_retention_window_days": row.get(
            "retention_window_days", DEFAULT_RETENTION_WINDOW_DAYS
        ),
        "after_retention_window_days": pending.get("new_window_days"),
    }


def confirm_pending_reduction(
    pending_id: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """POST /api/admin/retention/pending/{pending_id}/confirm.

    The second half of dual control: an authenticated admin (strict
    `is_admin is True`, `authz.require_admin`) who is NOT the requester
    applies the parked reduction now. The confirmer's identity is the
    caller's own row -- never anything in the request body.

    Raises HTTPException(403) for a non-admin or for the requester, 404 for
    an unknown or no-longer-pending id.
    """
    require_admin(caller_user_row, "Admin privilege required to confirm a retention reduction.")
    actor = caller_user_row.get("cognito_sub", "")
    row, pending = _current_pending(dynamodb_resource, pending_id)
    if not actor or actor == pending.get("requested_by"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A retention reduction must be confirmed by a different admin than the one who requested it.",
        )
    try:
        _apply_pending(dynamodb_resource, pending)
    except Exception as exc:
        if _is_conditional_failure(exc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No pending retention reduction with that id.",
            ) from None
        raise
    _audit(
        dynamodb_resource,
        actor=actor,
        action="retention_reduction_confirmed",
        target=GLOBAL_SETTING_ID,
        target_type="retention_settings",
        detail={**_pending_audit_detail(row, pending), "result_status": "APPLIED"},
    )
    return {"status": "APPLIED", "applied_immediately": False, "pending_id": pending_id}


def cancel_pending_reduction(
    pending_id: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    reason: str = "",
) -> dict[str, Any]:
    """POST /api/admin/retention/pending/{pending_id}/cancel. Body:
    {"reason": str} (optional). Any admin -- including the requester -- may
    withdraw a parked reduction; the window is left as it is.

    Raises HTTPException(403) for a non-admin, 404 for an unknown or
    no-longer-pending id.
    """
    require_admin(caller_user_row, "Admin privilege required to cancel a retention reduction.")
    actor = caller_user_row.get("cognito_sub", "")
    row, pending = _current_pending(dynamodb_resource, pending_id)
    try:
        _settings_table(dynamodb_resource).update_item(
            Key=_GLOBAL_KEY,
            UpdateExpression="SET pending_reduction = :none, updated_at = :now",
            ConditionExpression="pending_reduction.pending_id = :pid",
            ExpressionAttributeValues={
                ":none": None,
                ":now": str(int(now_epoch())),
                ":pid": pending_id,
            },
        )
    except Exception as exc:
        if _is_conditional_failure(exc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No pending retention reduction with that id.",
            ) from None
        raise
    _audit(
        dynamodb_resource,
        actor=actor,
        action="retention_reduction_cancelled",
        target=GLOBAL_SETTING_ID,
        target_type="retention_settings",
        detail={
            **_pending_audit_detail(row, pending),
            "cancel_reason": _clean_reason(reason),
            "result_status": "CANCELLED",
        },
    )
    return {"status": "CANCELLED", "pending_id": pending_id}


def apply_ready_pending_reduction(dynamodb_resource: Any) -> bool:
    """Apply a parked reduction whose 72-hour delay has passed, and audit it
    (DTS; the purge worker Lambda does the same on AWS). Called at the start
    of every DTS sweep. Returns True when one was applied.

    `apply_after` is honoured when recorded; a row parked before it was
    recorded falls back to `requested_at` + the delay, as the Lambda does."""
    if not os.environ.get("RETENTION_SETTINGS_TABLE"):
        return False
    row = (
        _settings_table(dynamodb_resource)
        .get_item(Key=_GLOBAL_KEY, ConsistentRead=True)
        .get("Item")
        or {}
    )
    pending = row.get("pending_reduction") or None
    if not pending:
        return False
    apply_after = pending.get("apply_after")
    if apply_after is None:
        apply_after = float(pending.get("requested_at", 0)) + RETROACTIVE_REDUCTION_DELAY_SECONDS
    if now_epoch() < float(apply_after):
        return False
    if not pending.get("pending_id"):
        # A pre-W6.7 record: give it an id so the conditional apply can bind.
        pending = {**pending, "pending_id": "legacy"}
        _settings_table(dynamodb_resource).update_item(
            Key=_GLOBAL_KEY,
            UpdateExpression="SET pending_reduction.pending_id = :pid",
            ExpressionAttributeValues={":pid": "legacy"},
        )
    try:
        _apply_pending(dynamodb_resource, pending)
    except Exception as exc:
        if _is_conditional_failure(exc):
            return False  # confirmed or cancelled meanwhile
        raise
    _audit(
        dynamodb_resource,
        actor=PENDING_REDUCTION_SYSTEM_ACTOR,
        action="retention_reduction_applied_after_delay",
        target=GLOBAL_SETTING_ID,
        target_type="retention_settings",
        detail={**_pending_audit_detail(row, pending), "result_status": "APPLIED"},
    )
    return True


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


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
    require_admin(caller_user_row, "Admin privilege required to preview a purge sweep.")

    reviews_table = _reviews_table(dynamodb_resource)

    review_ids: list[str] = []
    # Only the terminal partitions: a non-terminal review can never be
    # purge-eligible (invariant 1), so there is nothing to read there.
    for review in _reviews_by_status(reviews_table, TERMINAL_REVIEW_STATUSES):
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


# The audit row a sweep writes names the reviews it purged. A daily sweep
# after a long outage could purge thousands; the row is capped well inside
# DynamoDB's 400 KB item limit and says when it was truncated (the counts are
# always exact). Mirrored in infra/lambda/purge_worker/handler.py.
SWEEP_AUDIT_MAX_IDS = 1000

# The audit `actor` for a DTS sweep nobody authenticated to run.
DTS_SWEEP_ACTOR = "system:retention-sweep"


def sweep_audit_detail(result: dict[str, Any]) -> dict[str, Any]:
    """The audit `detail` for one sweep run: its id, mode, exact counts, and
    the purged / failed review ids (capped). Identifiers and counts only --
    never an object key (a key carries the owner's sub) and never substance.
    Mirrored EXACTLY by infra/lambda/purge_worker/handler.py's
    `_sweep_audit_detail`; tests/test_retention_sweep_w6.py checks both."""
    deleted = list(result["deleted_reviews"])
    failed = list(result["failed_reviews"])
    return {
        "sweep_id": result["sweep_id"],
        "dry_run": bool(result["dry_run"]),
        "eligible_count": len(result["eligible_reviews"]),
        "deleted_count": len(deleted),
        "failed_count": len(failed),
        "object_count": int(result["object_count"]),
        "skipped_active_count": len(result["skipped_active"]),
        "skipped_hold_count": len(result["skipped_hold"]),
        "skipped_not_yet_eligible_count": len(result["skipped_not_yet_eligible"]),
        "purged_review_ids": deleted[:SWEEP_AUDIT_MAX_IDS],
        "failed_review_ids": failed[:SWEEP_AUDIT_MAX_IDS],
        "review_ids_truncated": (
            len(deleted) > SWEEP_AUDIT_MAX_IDS or len(failed) > SWEEP_AUDIT_MAX_IDS
        ),
    }


def run_purge_sweep_now(
    s3_client: Any,
    dynamodb_resource: Any,
    dry_run: bool = False,
    actor: str = DTS_SWEEP_ACTOR,
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
      sweep_id          this run's id -- the `target` of the ONE audit row
                        every run (dry runs included) writes, with its counts
                        and purged review ids (`sweep_audit_detail`). That
                        write is not best-effort: a sweep that cannot record
                        itself raises after the fact rather than passing
                        silently.
    """
    if not os.environ.get("AUDIT_TABLE"):
        # Refuse before deleting anything: a sweep must never run unrecorded.
        raise RuntimeError("AUDIT_TABLE is not configured; refusing to sweep unaudited")
    # W6.7: a parked reduction whose 72-hour delay has passed takes effect
    # here, exactly as the purge worker Lambda applies it at the start of
    # its sweep on AWS. Skipped on a dry run, which must change nothing.
    # A failure here does not stop the sweep: this run purges on each
    # review's own snapshotted window either way, and the next run retries.
    if not dry_run:
        try:
            apply_ready_pending_reduction(dynamodb_resource)
        except Exception:  # noqa: BLE001 -- logged by fact, not provider text
            logger.warning("retention sweep: applying a pending reduction failed; will retry")
    reviews_table = _reviews_table(dynamodb_resource)
    sweep_id = uuid.uuid4().hex

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

    # Every known partition, terminal ones first (issue #52): the in-flight
    # partitions are read too, so `skipped_active` still names the reviews
    # invariant 1 protected, exactly as the scan-backed sweep reported them.
    for review in _reviews_by_status(reviews_table, _ALL_KNOWN_REVIEW_STATUSES):
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

        # W6.6: the row above came from a `status-index` query, which is
        # eventually consistent and was read before this candidate's turn. A
        # hold placed since then must still win, so re-read the row strongly
        # consistently immediately before acting on it.
        current = reviews_table.get_item(
            Key={"review_id": review_id}, ConsistentRead=True
        ).get("Item")
        if not current:
            logger.warning("retention sweep: review %s vanished mid-sweep; skipped", review_id)
            continue
        if _is_legal_held(current):
            skipped_hold.append(review_id)
            continue
        if current.get("status") not in TERMINAL_REVIEW_STATUSES:
            skipped_active.append(review_id)
            continue
        review = current

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

    result = {
        "sweep_id": sweep_id,
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
    _audit(
        dynamodb_resource,
        actor=actor,
        action="retention_purge_sweep",
        target=sweep_id,
        target_type="retention_sweep",
        detail=sweep_audit_detail(result),
    )
    return result


# ---------------------------------------------------------------------------
# Legal hold -- set / release, mirrored to storage layer (#61)
# ---------------------------------------------------------------------------


LEGAL_HOLD_TAG_KEY = "contract-toaster:legal-hold"

# The 503 an admin sees when the storage layer refused a hold change. Fixed
# copy: a provider error names ARNs, bucket keys and policy text, none of
# which belongs in a response body (CLAUDE.md -> "Escaped text only").
_HOLD_STORAGE_FAILED = (
    "The legal hold could not be {verb} on this review's stored documents, so "
    "nothing was changed. Try again; if it keeps failing, ask an operator to "
    "check the service's storage permissions."
)
_HOLD_AUDIT_FAILED = (
    "The legal hold change could not be recorded in the audit trail, so it was "
    "undone. Try again; if it keeps failing, ask an operator to check the audit "
    "table."
)


class _HoldTaggingError(Exception):
    """Tagging stopped part-way. `done` is every (bucket, key) already
    changed, so the caller can put them back."""

    def __init__(self, done: list[tuple[str, str]]) -> None:
        super().__init__("legal-hold tagging failed")
        self.done = done


def _hold_targets(review: dict[str, Any], s3_client: Any) -> list[tuple[str, str]]:
    """Every (bucket, key) a hold on this review covers -- derived from the
    row's own recorded pointers plus the review-scoped prefixes, exactly as
    the sweep targets them (`_review_object_targets`, issue #454)."""
    return _review_object_targets(
        review,
        s3_client,
        os.environ.get("UPLOADS_BUCKET", ""),
        os.environ.get("OUTPUTS_BUCKET", ""),
    )


def _apply_hold_tag(s3_client: Any, bucket: str, key: str, held: bool) -> None:
    if held:
        s3_client.put_object_tagging(
            Bucket=bucket,
            Key=key,
            Tagging={"TagSet": [{"Key": LEGAL_HOLD_TAG_KEY, "Value": "true"}]},
        )
    else:
        s3_client.delete_object_tagging(Bucket=bucket, Key=key)


def _tag_targets(
    s3_client: Any, targets: list[tuple[str, str]], held: bool
) -> list[tuple[str, str]]:
    """Mirror `held` onto every target via the `contract-toaster:legal-hold`
    tag the bucket-policy DENY reads (infra/lib/nested/data-stack.ts,
    `s3:ExistingObjectTag/contract-toaster:legal-hold` = 'true'). Raises
    `_HoldTaggingError` carrying what was already changed."""
    done: list[tuple[str, str]] = []
    for bucket, key in targets:
        try:
            _apply_hold_tag(s3_client, bucket, key, held)
        except Exception as exc:
            raise _HoldTaggingError(done) from exc
        done.append((bucket, key))
    return done


def _undo_tags(s3_client: Any, done: list[tuple[str, str]], held: bool, review_id: str) -> None:
    """Best-effort rollback of `_tag_targets(..., held)`. A failure here is
    logged by count only: the row still says what is true, and the storage
    tag is a backstop, not the source of truth."""
    failed = 0
    for bucket, key in done:
        try:
            _apply_hold_tag(s3_client, bucket, key, not held)
        except Exception:  # noqa: BLE001 -- rollback is best-effort
            failed += 1
    if failed:
        logger.warning(
            "LEGAL_HOLD: rollback left %d object tag(s) out of step for review %s",
            failed,
            review_id,
        )


def _tag_or_503(
    s3_client: Any, targets: list[tuple[str, str]], held: bool, review_id: str
) -> list[tuple[str, str]]:
    try:
        return _tag_targets(s3_client, targets, held)
    except _HoldTaggingError as exc:
        _undo_tags(s3_client, exc.done, held, review_id)
        # Deliberately not `logger.exception`: the provider's message can quote
        # a key, and a key names the review's owner.
        logger.warning(
            "LEGAL_HOLD: storage tagging failed for review %s; nothing changed",
            review_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_HOLD_STORAGE_FAILED.format(verb="placed" if held else "released"),
        ) from None



def tag_if_held(review_id: str, dynamodb_resource: Any) -> dict[str, str]:
    """Extra `put_object` / `copy_object` kwargs for an object written under
    `review_id` (W6.6): `{"Tagging": "contract-toaster:legal-hold=true"}` while
    the review is held, else `{}`.

    A hold tags the objects that exist when it is placed; an output written
    afterwards (a pipeline still finishing, a diagnostic artifact) would
    otherwise land untagged, outside the bucket-policy backstop. The read is
    strongly consistent so a hold placed a moment ago is seen. A failed
    lookup never fails the write: the row's flag still protects the review
    from every sweep."""
    try:
        row = (
            _reviews_table(dynamodb_resource)
            .get_item(Key={"review_id": review_id}, ConsistentRead=True)
            .get("Item")
            or {}
        )
    except Exception:  # noqa: BLE001 -- never fail a write over the lookup
        logger.warning("LEGAL_HOLD: hold lookup failed for review %s at write time", review_id)
        return {}
    if _is_legal_held(row):
        return {"Tagging": f"{LEGAL_HOLD_TAG_KEY}=true"}
    return {}


def _get_review_or_404(review_id: str, dynamodb_resource: Any) -> dict[str, Any]:
    reviews_table = _reviews_table(dynamodb_resource)
    resp = reviews_table.get_item(Key={"review_id": review_id}, ConsistentRead=True)
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

    Places a per-review legal hold (RUNBOOK.md -> "Placing and releasing a
    legal hold"), atomically and audited (W6.5). Order:

      1. Tag every object the review owns `contract-toaster:legal-hold=true`
         (the bucket-policy backstop). A failure untags what was tagged and
         answers 503 -- nothing changed.
      2. Conditional row update (the review must still exist). A failure
         untags and answers 404 / 503.
      3. A catch-up tagging pass for any object written between 1 and 2
         (best-effort: the row is already held, and the sweep re-reads it).
      4. The audit row. A failed write puts the row and the tags back and
         answers 503: a hold change never survives unaudited.

    Raises HTTPException(403) for a non-admin, 400 for a blank reason, 404
    for an unknown review, 503 when storage or the audit trail refused.
    """
    require_admin(caller_user_row, "Admin privilege required to place a legal hold.")

    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A matter reference / reason is required to place a legal hold.",
        )

    review = _get_review_or_404(review_id, dynamodb_resource)
    actor = caller_user_row.get("cognito_sub", "")
    was_held = _is_legal_held(review)

    targets = _hold_targets(review, s3_client)
    tagged = _tag_or_503(s3_client, targets, True, review_id)

    reviews_table = _reviews_table(dynamodb_resource)
    try:
        reviews_table.update_item(
            Key={"review_id": review_id},
            UpdateExpression=(
                "SET legal_hold = :held, legal_hold_reason = :reason, "
                "legal_hold_set_by = :actor, legal_hold_set_at = :now"
            ),
            ConditionExpression="attribute_exists(review_id)",
            ExpressionAttributeValues={
                ":held": True,
                ":reason": reason,
                ":actor": actor,
                ":now": str(int(now_epoch())),
            },
        )
    except Exception as exc:  # noqa: BLE001
        if not was_held:
            _undo_tags(s3_client, tagged, True, review_id)
        if _is_conditional_failure(exc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Review not found."
            ) from None
        logger.warning("LEGAL_HOLD: row update failed for review %s; tags undone", review_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_HOLD_STORAGE_FAILED.format(verb="placed"),
        ) from None

    # Catch-up: anything written for this review after step 1 read its row
    # as unheld. Every later write tags itself (`tag_if_held`).
    try:
        latest = _hold_targets(review, s3_client)
        _tag_targets(s3_client, [t for t in latest if t not in targets], True)
    except Exception:  # noqa: BLE001 -- the row is held; this is the backstop
        logger.warning("LEGAL_HOLD: catch-up tagging pass failed for review %s", review_id)

    try:
        _audit(
            dynamodb_resource,
            actor=actor,
            action="legal_hold_set",
            target=review_id,
            target_type="review",
            detail={"legal_hold_reason": reason},
        )
    except Exception:  # noqa: BLE001
        if not was_held:
            _revert_row(reviews_table, review)
            _undo_tags(s3_client, tagged, True, review_id)
        logger.warning("LEGAL_HOLD: audit write failed for review %s; change undone", review_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_HOLD_AUDIT_FAILED
        ) from None

    return {"review_id": review_id, "legal_hold": True, "legal_hold_reason": reason}


_HOLD_ROW_FIELDS = (
    "legal_hold",
    "legal_hold_reason",
    "legal_hold_set_by",
    "legal_hold_set_at",
    "legal_hold_released_by",
    "legal_hold_released_at",
    "legal_hold_release_reason",
)


def _revert_row(reviews_table: Any, before: dict[str, Any]) -> None:
    """Put the hold fields back exactly as `before` had them (best-effort --
    reached only when the audit write already failed)."""
    sets, removes, values = [], [], {}
    for i, field in enumerate(_HOLD_ROW_FIELDS):
        if field in before:
            sets.append(f"{field} = :v{i}")
            values[f":v{i}"] = before[field]
        else:
            removes.append(field)
    expression = ""
    if sets:
        expression += "SET " + ", ".join(sets)
    if removes:
        expression += (" " if expression else "") + "REMOVE " + ", ".join(removes)
    kwargs: dict[str, Any] = {"Key": {"review_id": before["review_id"]}, "UpdateExpression": expression}
    if values:
        kwargs["ExpressionAttributeValues"] = values
    try:
        reviews_table.update_item(**kwargs)
    except Exception:  # noqa: BLE001
        logger.error(
            "LEGAL_HOLD: could not revert hold fields for review %s after an audit failure",
            before["review_id"],
        )


def release_legal_hold(
    review_id: str,
    reason: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    s3_client: Any,
) -> dict[str, Any]:
    """DELETE /api/admin/retention/holds/{review_id}. Body: {"reason": str}.

    Releases a legal hold, atomically and audited (W6.5), in the same order
    as `set_legal_hold`: untag -> conditional row update (`legal_hold =
    true`, so a concurrent release cannot double-record) -> audit. Any
    failure puts back what was already changed. The release `reason` is
    required and is recorded on the row and in the audit row. Release does
    not purge: the review becomes eligible under its own window at the next
    sweep (RUNBOOK.md).

    Raises HTTPException(403) for a non-admin, 400 for a blank reason, 404
    for an unknown review, 409 when the review is not held, 503 when storage
    or the audit trail refused.
    """
    require_admin(caller_user_row, "Admin privilege required to release a legal hold.")

    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A reason is required to release a legal hold.",
        )

    review = _get_review_or_404(review_id, dynamodb_resource)
    if not _is_legal_held(review):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This review has no active legal hold.",
        )
    actor = caller_user_row.get("cognito_sub", "")

    untagged = _tag_or_503(s3_client, _hold_targets(review, s3_client), False, review_id)

    reviews_table = _reviews_table(dynamodb_resource)
    try:
        reviews_table.update_item(
            Key={"review_id": review_id},
            UpdateExpression=(
                "SET legal_hold = :released, legal_hold_released_by = :actor, "
                "legal_hold_released_at = :now, legal_hold_release_reason = :reason"
            ),
            ConditionExpression="legal_hold = :held",
            ExpressionAttributeValues={
                ":released": False,
                ":held": True,
                ":actor": actor,
                ":now": str(int(now_epoch())),
                ":reason": reason,
            },
        )
    except Exception as exc:  # noqa: BLE001
        _undo_tags(s3_client, untagged, False, review_id)
        if _is_conditional_failure(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This review has no active legal hold.",
            ) from None
        logger.warning("LEGAL_HOLD: release row update failed for review %s", review_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_HOLD_STORAGE_FAILED.format(verb="released"),
        ) from None

    try:
        _audit(
            dynamodb_resource,
            actor=actor,
            action="legal_hold_released",
            target=review_id,
            target_type="review",
            detail={
                "legal_hold_release_reason": reason,
                "legal_hold_reason": review.get("legal_hold_reason", ""),
            },
        )
    except Exception:  # noqa: BLE001
        _revert_row(reviews_table, review)
        _undo_tags(s3_client, untagged, False, review_id)
        logger.warning("LEGAL_HOLD: audit write failed for release of %s; undone", review_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_HOLD_AUDIT_FAILED
        ) from None

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
    require_admin(caller_user_row, "Admin privilege required to view legal holds.")

    reviews_table = _reviews_table(dynamodb_resource)
    # Every known partition (issue #52): `set_legal_hold` does not require a
    # terminal review, so a hold on an in-flight one must still be listed.
    return [
        {field: r[field] for field in _HOLD_LIST_FIELDS if field in r}
        for r in _reviews_by_status(reviews_table, _ALL_KNOWN_REVIEW_STATUSES)
        if r.get("legal_hold")
    ]
