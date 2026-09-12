"""
Boot-time recovery for in-process reviews orphaned by a container restart —
issue #62 (2026-09-05 stability/maintainability diagnostic, finding G1,
action B1).

## What was broken

On the Docker Compose target a review runs on an in-process
`ThreadPoolExecutor` inside the API container
(`src/pipeline_runner.py::InProcessStepFunctionsClient`). Nothing outside
that process knows the run exists. When Coolify redeploys — or the container
OOMs, or an operator restarts it — every in-flight review dies mid-pass with
its row still `RUNNING`, and nothing ever writes a terminal:

  * History shows a review that never ends,
  * the worst-case daily-spend reservation is never settled, so the day's
    cap stays consumed by a run that is not running,
  * `GET /api/admin/health` counts it as in-flight forever.

The AWS target has never had this problem: `infra/lambda/orphan_reconciler`
polls `DescribeExecution` and relabels a review whose Step Functions
execution died. That reconciler cannot help here, because an in-process run
has no execution to describe — only a process that is gone. The equivalent
signal on this target is the process boundary itself: a non-terminal review
whose row was last touched BEFORE this process started cannot be owned by
this process, and no other process exists to own it.

## What this module does

`recover_orphaned_reviews` runs once, from `src/main.py::_lifespan`, after
the environment check and before the purge cadence. Each orphan is
relabelled `ERROR` with `reason=runner_restarted`, its reservation is
settled through the same function the cancel route uses, and an audit row
records that it happened. The reader-facing half of the token lives in
`frontend/src/ReviewSubmission.tsx`'s `REASON_EXPLANATIONS`.

## Why the boot-time read is not a scan

Issue #52 landed the `status-index` GSI and moved every live-path reviews
read onto it. This one follows: one `reviews._query_by_status` query per
status in `REVIEW_STATUSES_NON_TERMINAL` (a two-element vocabulary), with
that helper's own `LastEvaluatedKey` loop, so a deployment with more than a
page of in-flight rows recovers all of them rather than the first megabyte.

## Audit posture

Identifiers and counts only, never review substance — the same posture as
`src/retention.py::_write_audit_entry` and ARCHITECTURE.md → "Audit posture".
The log line carries a COUNT, never an id list and never a document detail.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from src import reviews as reviews_module

logger = logging.getLogger(__name__)

# The audit `action` a recovered review is recorded under. Its own verb
# rather than `review_cancel`: nobody asked for this review to stop, and an
# auditor reading the review's history has to be able to tell "the reviewer
# stopped it" from "the service restarted underneath it".
RUNNER_RESTARTED_AUDIT_ACTION = "review_runner_restarted"

# The audit `actor` for a row nobody authenticated to produce. Spelled like
# the other machine actors in this codebase rather than borrowed from a user.
RUNNER_RECOVERY_ACTOR = "system:runner-recovery"


def _reviews_table(dynamodb_resource: Any) -> Any:
    return dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])


def _write_audit_entry(
    dynamodb_resource: Any,
    *,
    review_id: str,
    detail: dict[str, Any],
) -> None:
    """Append one immutable audit row for a recovered review.

    Best-effort and never raised through: this runs inside process startup,
    and an unreachable (or unconfigured) audit table must not stop the API
    from booting — the same posture as
    `src/review_routes.py::_write_audit_row`, including its missing-table
    early return. The warning log is the durable record when the write
    itself fails.
    """
    table_name = os.environ.get("AUDIT_TABLE")
    if not table_name:
        return
    now = time.time()
    event_id = uuid.uuid4().hex
    item: dict[str, Any] = {
        "partition": time.strftime("%Y-%m", time.gmtime(now)),
        "timestamp": f"{int(now)}#{event_id}",
        "event_id": event_id,
        "actor": RUNNER_RECOVERY_ACTOR,
        "action": RUNNER_RESTARTED_AUDIT_ACTION,
        "target": review_id,
        "target_type": "review",
        "outcome": "success",
        # Issue #253: the attribute the audit table's `review_id-index` GSI
        # is keyed by, so docs/audit-queries.md's "full history of one
        # review" query returns this event too.
        "review_id": review_id,
        **detail,
    }
    try:
        dynamodb_resource.Table(table_name).put_item(Item=item)
    except Exception:  # noqa: BLE001 - audit is best-effort; never gate the boot on it
        logger.warning(
            "RUNNER_RECOVERY: failed to write the audit row for a recovered review",
            exc_info=True,
        )


def _row_epoch(row: dict[str, Any]) -> int | None:
    """When this row was last written, as epoch seconds.

    `updated_at` is the field every writer stamps; `created_at` is the
    fallback for a row that somehow carries only the one the creation path
    writes. A row with neither returns None and is deliberately LEFT ALONE:
    "older than this process" is the entire basis for calling a review
    orphaned, and a row that cannot answer that question must not be
    relabelled on a guess.
    """
    for field in ("updated_at", "created_at"):
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return None


def is_orphaned(row: dict[str, Any], *, process_started_at: int) -> bool:
    """Is this non-terminal row a run no live process can still own?

    Three conditions, all required:

      * its `execution_arn` is not a Step Functions one. A row with a real
        `arn:` execution belongs to Step Functions and to
        `infra/lambda/orphan_reconciler`, which can actually ask whether that
        execution is alive. Relabelling it from here would race a healthy
        execution and destroy a review that is running fine.

        CORRECTION to issue #62's own wording, which asked for "no
        `execution_arn` (in-process, not Step Functions)". Those are not the
        same set, and on this codebase the difference is the whole feature:
        `reviews.ensure_execution_started` records the ARN its client
        returns onto the reviews row for BOTH targets, and the in-process
        client returns the pseudo-ARN `inprocess:<execution-name>`
        (`pipeline_runner.InProcessStepFunctionsClient.start_execution`). So
        an in-process review that has actually started — the overwhelming
        majority of what a redeploy strands, and the only kind that has
        spent money — DOES carry an `execution_arn`, and an
        `attribute_not_exists` selector would have skipped every one of them
        while passing a green test suite. The question worth asking is the
        one `stop_running_execution` already asks of the same field, through
        the same predicate: is there a real execution behind this row?
        A missing ARN still qualifies (the submission died before
        `ensure_execution_started` ran) — it is simply not the only shape
        that does.
      * last written BEFORE this process started — a row this process itself
        has touched is one of its own live runs.
      * a readable timestamp — see `_row_epoch`.
    """
    if reviews_module.is_step_functions_execution(row.get("execution_arn")):
        return False
    last_written = _row_epoch(row)
    if last_written is None:
        return False
    return last_written < process_started_at


def _relabel(review_id: str, dynamodb_resource: Any) -> bool:
    """Write the `ERROR` / `runner_restarted` terminal, or report that the
    row moved under us.

    `failing_stage` is deliberately NOT written: whatever stage the run had
    reached is the most useful thing left on the row, and this recovery knows
    nothing about stages. `failed_at` is stamped for the same reason
    `reviews.record_stage_failure` stamps it — the Diagnostics "Failed at"
    column reads it, and `created_at` is a different question.

    The conditional write is the race guard: a review that reached a terminal
    status between the query above and this update keeps it. That is not
    hypothetical here — a review row and its runner die at different moments,
    and a `DONE` row with a persisted redline must never be relabelled a
    failure (the same promise `record_stage_failure` makes for issue #446,
    spelled as "still non-terminal" because this path has no stage to record
    on a row that already failed for a real reason either).
    """
    now = str(int(time.time()))
    try:
        _reviews_table(dynamodb_resource).update_item(
            Key={"review_id": review_id},
            UpdateExpression=(
                "SET #status = :error, #reason = :reason, "
                "failed_at = :now, updated_at = :now"
            ),
            ConditionExpression="attribute_exists(review_id) AND #status IN (:pending, :running)",
            ExpressionAttributeNames={"#status": "status", "#reason": "reason"},
            ExpressionAttributeValues={
                ":error": "ERROR",
                ":reason": reviews_module.RUNNER_RESTARTED_REASON,
                ":now": now,
                ":pending": "PENDING",
                ":running": "RUNNING",
            },
        )
    except Exception as exc:  # noqa: BLE001 - only the race guard is swallowed
        if not reviews_module._is_conditional_check_failed(exc):
            raise
        return False
    return True


def recover_orphaned_reviews(
    dynamodb_resource: Any,
    *,
    process_started_at: int,
) -> list[str]:
    """Relabel every in-process review orphaned by a previous process, and
    return their ids (sorted).

    One review at a time, and one failure at a time: a row whose relabel,
    settlement or audit write raises is logged and the loop continues, so a
    single bad row cannot leave the rest of the backlog `RUNNING` forever —
    which is the exact condition this function exists to clear.

    The returned list is "rows this call RELABELLED", and the id is added the
    moment that write lands rather than after the settlement and audit that
    follow it. Those two can fail independently, and a review that is now
    `ERROR` in the table but missing from this list would be invisible to the
    count the caller logs — the one signal an operator has that a restart
    stranded anything at all.
    """
    orphans: list[str] = []
    table = _reviews_table(dynamodb_resource)
    for status_value in sorted(reviews_module.REVIEW_STATUSES_NON_TERMINAL):
        for row in reviews_module._query_by_status(table, status_value):
            review_id = row.get("review_id")
            if not review_id or not is_orphaned(row, process_started_at=process_started_at):
                continue
            try:
                relabelled = _relabel(str(review_id), dynamodb_resource)
            except Exception:  # noqa: BLE001 - one bad row must not strand the rest
                logger.warning(
                    "RUNNER_RECOVERY: could not relabel one orphaned review",
                    exc_info=True,
                )
                continue
            if not relabelled:
                continue
            orphans.append(str(review_id))
            try:
                # Settled through the cancel route's own function so the
                # reservation is credited back exactly once, guarded by the
                # same `reservation_released` idempotency flag.
                reviews_module.settle_reservation_for_cancel(
                    str(review_id), dynamodb_resource
                )
                _write_audit_entry(
                    dynamodb_resource,
                    review_id=str(review_id),
                    detail={
                        "reason": reviews_module.RUNNER_RESTARTED_REASON,
                        "previous_status": str(status_value),
                    },
                )
            except Exception:  # noqa: BLE001 - the relabel already landed
                logger.warning(
                    "RUNNER_RECOVERY: recovered a review but could not settle or "
                    "audit it",
                    exc_info=True,
                )
    if orphans:
        # COUNT ONLY. An id list here would put review identifiers in the
        # container log on every restart; the audit table is where the
        # per-review record belongs.
        logger.warning(
            "RUNNER_RECOVERY: recovered %d review(s) left running by a previous process",
            len(orphans),
        )
    return sorted(orphans)
