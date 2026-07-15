"""
Human-review-outcome capture — issue #74 (closes finding 51).

Even though attorney approval stays **outside** this tool (see
ARCHITECTURE.md -> "What we are explicitly not building" and
docs/output-contract.md -> "Attorney-approval framing"), we get no quality
feedback loop unless we record what the attorney did with the tool's
output. This module is that lightweight capture.

What this is NOT: an approval workflow, a legal gate, or anything that
changes a review's pipeline `status` or `decision`. Recording a disposition
is purely a quality signal (docs/evaluation.md -> "Human-review feedback
loop"). `record_disposition` never touches `status` or `decision`.

Field names here follow the canonical `reviews` field dictionary in
docs/data-handling.md -> "Metadata field classification":

  - `attorney_disposition`            ACCEPTED | EDITED | REJECTED
  - `attorney_disposition_reason_codes`  list[str], structured reason codes
  - `attorney_disposition_topic_ids`     list[str], playbook_topic_id values
                                          the disposition relates to
  - `attorney_disposition_note`          optional free text (Confidential;
                                          expires with the document per
                                          docs/data-handling.md)
  - `attorney_disposition_recorded_at`   epoch-seconds string, when captured
  - `legal_triage_status`                PENDING_TRIAGE | TRIAGED | null —
                                          set to PENDING_TRIAGE only for
                                          EDITED/REJECTED outcomes (the
                                          legal triage queue described in
                                          the issue AC); ACCEPTED never
                                          enters the queue.

The disposition nag ("N reviews awaiting disposition") specified in
ARCHITECTURE.md -> "Disposition nag" is served by
`count_reviews_awaiting_disposition` below.

Environment variables consumed:
  REVIEWS_TABLE   DynamoDB reviews table name (same table reviews.py writes)
"""

import os
import time
from typing import Any

from fastapi import HTTPException, status

# ---------------------------------------------------------------------------
# Disposition outcomes
# ---------------------------------------------------------------------------

VALID_OUTCOMES = {"ACCEPTED", "EDITED", "REJECTED"}

# Outcomes that reveal a possible miss or false positive and therefore enter
# the legal triage queue before becoming candidate gold-set changes (issue
# #74 AC). ACCEPTED is a clean pass and never enters triage.
TRIAGE_OUTCOMES = {"EDITED", "REJECTED"}

TRIAGE_STATUS_PENDING = "PENDING_TRIAGE"
TRIAGE_STATUS_TRIAGED = "TRIAGED"

# Review pipeline statuses that must have completed (i.e. produced a result
# the attorney could act on) before a disposition can be recorded against
# them. Recording a disposition on a still-running review would be a
# meaningless signal — there is no tool output yet to accept/edit/reject.
DISPOSITIONABLE_REVIEW_STATUSES = {
    "DONE",
    "MANUAL_REVIEW_REQUIRED",
    "ERROR_MANUAL_REVIEW_REQUIRED",
}


def _reviews_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])


def record_disposition(
    review_id: str,
    outcome: str,
    dynamodb_resource: Any,
    reason_codes: list[str] | None = None,
    topic_ids: list[str] | None = None,
    note: str | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Record the attorney's disposition of a completed review's output.

    outcome must be one of ACCEPTED | EDITED | REJECTED. reason_codes and
    topic_ids are structured signal (issue #74 AC: "structured reason
    codes/topic IDs where applicable"); note is optional free text.

    This function intentionally never writes `status` or `decision` — a
    disposition is metadata about the tool's quality, not a legal verdict,
    and it does not gate anything (issue #74 AC: "does not turn the tool
    into an approval workflow").

    EDITED/REJECTED outcomes set `legal_triage_status = PENDING_TRIAGE`,
    placing the review in the legal triage queue described in the issue AC
    ("Edited/rejected outcomes enter a legal triage queue before becoming
    candidate gold-set changes"). ACCEPTED clears any prior triage status
    is left alone — an ACCEPTED disposition is never enqueued.

    Raises HTTPException(400) for an invalid outcome, HTTPException(404) if
    the review does not exist, and HTTPException(409) if the review has not
    reached a dispositionable (completed) status yet.
    """
    if outcome not in VALID_OUTCOMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid disposition outcome {outcome!r}; must be one of {sorted(VALID_OUTCOMES)}.",
        )

    table = _reviews_table(dynamodb_resource)
    resp = table.get_item(Key={"review_id": review_id})
    review = resp.get("Item")
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found.")

    if review.get("status") not in DISPOSITIONABLE_REVIEW_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot record a disposition until the review has reached a "
                "completed status "
                f"({sorted(DISPOSITIONABLE_REVIEW_STATUSES)}); "
                f"current status is {review.get('status')!r}."
            ),
        )

    now_epoch = time.time() if now_epoch is None else now_epoch
    now = str(int(now_epoch))

    triage_status = TRIAGE_STATUS_PENDING if outcome in TRIAGE_OUTCOMES else None

    update_values: dict[str, Any] = {
        ":disposition": outcome,
        ":reason_codes": list(reason_codes) if reason_codes else [],
        ":topic_ids": list(topic_ids) if topic_ids else [],
        ":note": note,
        ":recorded_at": now,
        ":now": now,
        ":triage_status": triage_status,
    }

    table.update_item(
        Key={"review_id": review_id},
        UpdateExpression=(
            "SET attorney_disposition = :disposition, "
            "attorney_disposition_reason_codes = :reason_codes, "
            "attorney_disposition_topic_ids = :topic_ids, "
            "attorney_disposition_note = :note, "
            "attorney_disposition_recorded_at = :recorded_at, "
            "legal_triage_status = :triage_status, "
            "updated_at = :now"
        ),
        ExpressionAttributeValues=update_values,
    )

    review.update(
        {
            "attorney_disposition": outcome,
            "attorney_disposition_reason_codes": update_values[":reason_codes"],
            "attorney_disposition_topic_ids": update_values[":topic_ids"],
            "attorney_disposition_note": note,
            "attorney_disposition_recorded_at": now,
            "legal_triage_status": triage_status,
            "updated_at": now,
        }
    )
    return review


def count_reviews_awaiting_disposition(
    owner_sub: str,
    dynamodb_resource: Any,
) -> int:
    """Feed the reviewer list-view disposition nag ("N reviews awaiting
    disposition") specified in ARCHITECTURE.md -> "Disposition nag".

    A review is "awaiting disposition" when it has reached a completed
    (dispositionable) status but has no `attorney_disposition` recorded
    yet. Scoped to the reviewer's own reviews (owner_sub), matching the
    reviewer list view the nag is displayed on. The nag is informational
    only — it never blocks access or changes pipeline state.
    """
    table = _reviews_table(dynamodb_resource)
    items = table.query_by_owner(owner_sub) if hasattr(table, "query_by_owner") else _scan_by_owner(table, owner_sub)

    count = 0
    for item in items:
        if item.get("status") in DISPOSITIONABLE_REVIEW_STATUSES and not item.get("attorney_disposition"):
            count += 1
    return count


def _scan_by_owner(table: Any, owner_sub: str) -> list[dict[str, Any]]:
    """Fallback owner-scoped fetch for callers whose table stand-in does not
    implement query_by_owner (e.g. a full boto3 Table would use `query`
    against the owner_sub-index GSI; production callers should pass a
    resource whose Table() exposes that query directly)."""
    if hasattr(table, "scan"):
        resp = table.scan()
        return [i for i in resp.get("Items", []) if i.get("owner_sub") == owner_sub]
    return []


def list_legal_triage_queue(dynamodb_resource: Any) -> list[dict[str, Any]]:
    """Return reviews with an EDITED/REJECTED disposition still pending
    legal triage (issue #74 AC: "Edited/rejected outcomes enter a legal
    triage queue before becoming candidate gold-set changes").

    Triage itself (Legal deciding whether to promote a case to the gold
    set) is out of scope for this issue — see docs/evaluation.md ->
    "Human-review feedback loop" and "Fixture promotion procedure". This
    function only surfaces the queue.
    """
    table = _reviews_table(dynamodb_resource)
    if hasattr(table, "scan"):
        resp = table.scan()
        items = resp.get("Items", [])
    else:
        items = []
    return [i for i in items if i.get("legal_triage_status") == TRIAGE_STATUS_PENDING]


def mark_triaged(review_id: str, dynamodb_resource: Any) -> None:
    """Legal has triaged this review's disposition (accepted/rejected as a
    gold-set candidate, or dismissed). Moves it out of the pending queue.

    This does not itself create a gold-set fixture — see
    docs/evaluation.md -> "Fixture promotion procedure" (a separate,
    GC-signed-off, CODEOWNERS-gated process) — it only records that Legal's
    triage step has happened for this review's disposition.
    """
    table = _reviews_table(dynamodb_resource)
    table.update_item(
        Key={"review_id": review_id},
        UpdateExpression="SET legal_triage_status = :triaged, updated_at = :now",
        ExpressionAttributeValues={
            ":triaged": TRIAGE_STATUS_TRIAGED,
            ":now": str(int(time.time())),
        },
    )
