"""
Mark-running stage Lambda — issue #188 (status lifecycle: PENDING -> RUNNING).

The mock pipeline (and the real pipeline behind #80-#83) transitions a review
through PENDING -> RUNNING -> DONE/MANUAL_REVIEW_REQUIRED/ERROR. Submission
writes PENDING (backend/src/reviews.py::_create_review_row); the shared error
handler writes ERROR (pipeline-stack.ts TransitionToError); the persist stage
writes the terminal success state (infra/lambda/persist/handler.py). This
stage owns the ONE remaining transition: flipping PENDING -> RUNNING once a
concurrency slot has been acquired and the pipeline has actually started
doing per-review work, so the UI's poll loop observes RUNNING during the
(mock: a few seconds; real: minutes) review window instead of jumping
straight from PENDING to a terminal state.

Placed right after the semaphore-acquired gate and before the extract stage
so it is only reached by an execution that actually holds a slot.

Idempotency / race-safety: the update is conditional on the row currently
being PENDING. A retry of this stage, or a race with the orphan reconciler
having already moved the row to ERROR, hits ConditionalCheckFailedException,
which is swallowed as a no-op -- this stage must never clobber a terminal
(DONE/ERROR/MANUAL_REVIEW_REQUIRED) or already-RUNNING status.

POINTER-ONLY PAYLOAD RULE (issue #19): passes the event through unchanged
(plus the status side effect), same contract as every other Phase-0 stage
stub, so downstream stages are unaffected.

Input event shape (pointer-only; from the semaphore-acquired branch):
  {"review_id": "...", "playbook_id": "...", "upload_s3_key": "...",
   "owner_sub": "...", "semaphore_acquired": true, ...}

Environment variables:
  REVIEWS_TABLE   DynamoDB reviews table name
"""

import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

REVIEWS_TABLE = os.environ.get("REVIEWS_TABLE", "")


def _ddb():
    return boto3.resource("dynamodb")


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Step Functions task entry point: transition PENDING -> RUNNING."""
    review_id = event.get("review_id")
    if not review_id or not REVIEWS_TABLE:
        return event

    table = _ddb().Table(REVIEWS_TABLE)
    now = str(int(time.time()))
    try:
        table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET #status = :running, updated_at = :now",
            # Only PENDING -> RUNNING. A row already RUNNING or in any terminal
            # state (DONE/ERROR/MANUAL_REVIEW_REQUIRED/QUARANTINED) must not be
            # touched -- this stage is not allowed to resurrect a finished or
            # failed review.
            ConditionExpression="#status = :pending",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":running": "RUNNING",
                ":pending": "PENDING",
                ":now": now,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Not PENDING anymore (retry, or the reconciler already moved it):
            # a strict no-op, never an error that would fail the execution.
            return event
        raise

    return event
