"""
Persist stage Lambda — issue #189 (spend-reservation settlement fix).

Two jobs (issue #189 spend settlement; issue #188 terminal-state write):

  1. Spend settlement (issue #189): settle the worst-case spend reservation
     taken at submission time (see backend/src/reviews.py -> reserve_spend /
     compute_worst_case_reservation_usd_cents) against actual ledgered
     spend, so a completed review's reservation is released back to the
     day's daily_spend budget instead of being held until UTC midnight (the
     daily cap 429'd the third review of any day because settle_spend() had
     zero callers).

  2. Terminal-state write (issue #188): flip the `reviews` row from RUNNING
     to its terminal success state and land the pipeline result onto it --
     `status` (DONE for REQUEST_CHANGE/ACCEPT, MANUAL_REVIEW_REQUIRED
     otherwise), `decision`, `summary`, `reason`, and -- ONLY when the
     redline stage confirmed it copied the output object
     (`output_object_written`) -- `output_s3_key`. Without this, a review
     never leaves RUNNING and `GET /api/reviews/{id}/output` never has a key
     to serve (issue #188: "nothing ever sets a review to DONE, and no
     output object is ever written").

Data-class split (issue #70 AC B): persist owns the DynamoDB terminal-state
write (it runs on pipelineDynamoDbRole, which has reviews-table read/write
and no S3 grant). Materializing the output .docx object is the REDLINE
stage's job (infra/lambda/redline/handler.py, on the outputs-bucket role) --
persist only records the key the redline stage says it wrote. The full REAL
persist job (real decision/provenance/critic deltas from the live pipeline)
is still #80-#83; this handler writes the mock pipeline's terminal result.

Self-contained, same as every other infra/lambda/* package (issue #59
convention -- see infra/lambda/orphan_reconciler/handler.py's own duplicate
of "ensure execution started"): this Lambda's deployment asset is
infra/lambda/persist/ only (lambda.Code.fromAsset in pipeline-stack.ts), so
it cannot import backend/src/reviews.py (that module ships inside the
separate backend container image, and neither container has the other's
source on its filesystem). settle_spend() and
compute_worst_case_reservation_usd_cents() below are therefore mirrors, not
imports, of backend/src/reviews.py's functions of the same name.
tests/test_spend_reservation_settlement.py cross-checks both copies stay
numerically identical, so a fix to one that isn't mirrored to the other
fails CI rather than silently drifting.

Mock-pipeline scope (issue #59 / #62 eval gate): the mock review stage
(infra/lambda/mock_review/handler.py) makes no real Bedrock calls, so actual
settled cost is $0 for every mock-pipeline review; the real pipeline
(#80-#83) will thread the real per-call ledgered cost through the event
instead of this hardcoded 0.

Pass-through: this returns the event unchanged (plus the settlement and
terminal-state side effects) so the state machine's chain keeps working when
the real persist stage lands.

Input event shape (from the redline stage, pointer-only; see
infra/lambda/mock_review/handler.py's output shape docstring -- redline is
currently also a pass-through stub, so this is the mock_review output
shape unchanged):
  {
    "review_id": "...",
    "decision": "REQUEST_CHANGE" | "MANUAL_REVIEW_REQUIRED",
    "reason": null | "playbook_coming_soon" | "unknown_playbook",
    "output_s3_key": null | "outputs/<review_id>/out.docx",
    "summary": "...",
    "watermark": "..."
  }

Environment variables:
  REVIEW_SUBMISSIONS_TABLE    DynamoDB review_submissions table name
  DAILY_SPEND_TABLE           DynamoDB daily_spend counter table name
"""

import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

REVIEW_SUBMISSIONS_TABLE = os.environ.get("REVIEW_SUBMISSIONS_TABLE", "")
DAILY_SPEND_TABLE = os.environ.get("DAILY_SPEND_TABLE", "")
REVIEWS_TABLE = os.environ.get("REVIEWS_TABLE", "")

# Decisions that mean "the pipeline finished successfully" -> terminal
# status DONE. Anything else (mock: MANUAL_REVIEW_REQUIRED for the
# not-yet-built playbooks / unknown playbook) is its own terminal status.
_DONE_DECISIONS = frozenset({"REQUEST_CHANGE", "ACCEPT"})

# ---------------------------------------------------------------------------
# Cost-model constants -- MIRROR of backend/src/reviews.py (see that
# module's constants block for the full derivation/rationale). Both copies
# are cross-checked by tests/test_spend_reservation_settlement.py.
# ---------------------------------------------------------------------------
MAX_INPUT_TOKENS = 80_000
MAX_OUTPUT_TOKENS = 8_000
MAX_RETRIES_PER_PASS = 1
REGIONAL_PRICING_PREMIUM = 1.10
PRIMARY_INPUT_RATE_USD_PER_MILLION = 5.50
PRIMARY_OUTPUT_RATE_USD_PER_MILLION = 27.50
CRITIC_INPUT_RATE_USD_PER_MILLION = 3.30
CRITIC_OUTPUT_RATE_USD_PER_MILLION = 16.50

# Mock-pipeline scope (see module docstring): no real model calls happen, so
# every mock-pipeline review settles at $0 actual spend.
MOCK_PIPELINE_ACTUAL_USD_CENTS = 0


def _ddb():
    return boto3.resource("dynamodb")


def compute_worst_case_reservation_usd_cents() -> int:
    """MIRROR of backend/src/reviews.py's function of the same name."""
    attempts_per_pass = 1 + MAX_RETRIES_PER_PASS
    primary_usd = MAX_INPUT_TOKENS * (
        PRIMARY_INPUT_RATE_USD_PER_MILLION / 1_000_000
    ) + MAX_OUTPUT_TOKENS * (PRIMARY_OUTPUT_RATE_USD_PER_MILLION / 1_000_000)
    critic_usd = MAX_INPUT_TOKENS * (
        CRITIC_INPUT_RATE_USD_PER_MILLION / 1_000_000
    ) + MAX_OUTPUT_TOKENS * (CRITIC_OUTPUT_RATE_USD_PER_MILLION / 1_000_000)
    usd = attempts_per_pass * (primary_usd + critic_usd)
    return int(round(usd * 100))


def settle_spend(actual_usd_cents: int, dynamodb_resource: Any,
                  now_epoch: float | None = None) -> None:
    """MIRROR of backend/src/reviews.py's settle_spend -- reconciles the
    worst-case reservation against ledgered actual spend on today's
    daily_spend row."""
    table = dynamodb_resource.Table(DAILY_SPEND_TABLE)
    now_epoch = time.time() if now_epoch is None else now_epoch
    spend_date = time.strftime("%Y-%m-%d", time.gmtime(now_epoch))
    reservation_amount_cents = compute_worst_case_reservation_usd_cents()
    delta = actual_usd_cents - reservation_amount_cents
    table.update_item(
        Key={"spend_date": spend_date},
        UpdateExpression=(
            "SET reserved_usd_cents = reserved_usd_cents + :delta, "
            "settled_usd_cents = if_not_exists(settled_usd_cents, :zero) + :actual"
        ),
        ExpressionAttributeValues={
            ":delta": delta,
            ":actual": actual_usd_cents,
            ":zero": 0,
        },
    )


def _find_submission_for_review(review_id: str, dynamodb_resource: Any) -> dict[str, Any] | None:
    """Same keyed lookup-by-review_id as
    infra/lambda/orphan_reconciler/handler.py's _find_submission_for_review
    and backend/src/pipeline_runner.py's _find_submission_by_review_id
    (issue #262) -- review_submissions is keyed by idempotency_key, not
    review_id, and the pointer-only event this stage receives carries only
    review_id.

    An unpaginated Scan only ever sees its first (<=1MB) page, so once the
    table outgrows one page a target row on a later page is silently
    invisible and the reservation it owns never settles. Prefer the
    `review_id-index` GSI (infra/lib/nested/data-stack.ts) via a real
    boto3/moto Table.query(); fall back to scan+filter only for a
    lightweight test stand-in that doesn't implement `.query()`."""
    table = dynamodb_resource.Table(REVIEW_SUBMISSIONS_TABLE)
    if hasattr(table, "query"):
        from boto3.dynamodb.conditions import Key

        resp = table.query(
            IndexName="review_id-index",
            KeyConditionExpression=Key("review_id").eq(review_id),
        )
        items = resp.get("Items", [])
        return items[0] if items else None

    resp = table.scan(
        FilterExpression="review_id = :rid",
        ExpressionAttributeValues={":rid": review_id},
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _write_terminal_reviews_state(event: dict[str, Any], dynamodb_resource: Any) -> None:
    """Issue #188: flip the reviews row RUNNING -> terminal success state and
    land the pipeline result onto it.

    Terminal status: DONE for a REQUEST_CHANGE/ACCEPT decision;
    MANUAL_REVIEW_REQUIRED otherwise (the mock's not-yet-built /
    unknown-playbook paths, and the real pipeline's fail-closed paths).

    Coupling (issue #188 decision): `output_s3_key` is recorded ONLY when the
    redline stage set `output_object_written` -- so the download affordance is
    never advertised for an object that was never materialized.

    Never clobbers ERROR: the write is conditional on the row not already
    being ERROR, so a failure the shared error handler recorded (a race
    between this stage and a Catch, or the orphan reconciler) is not
    overwritten with a spurious success. A ConditionalCheckFailedException is
    swallowed as a no-op.
    """
    if not REVIEWS_TABLE:
        return
    review_id = event.get("review_id")
    if not review_id:
        return

    decision = event.get("decision")
    terminal_status = "DONE" if decision in _DONE_DECISIONS else "MANUAL_REVIEW_REQUIRED"

    set_clauses = ["#status = :status", "updated_at = :now"]
    names = {"#status": "status"}
    values: dict[str, Any] = {
        ":status": terminal_status,
        ":now": str(int(time.time())),
        ":error": "ERROR",
    }
    if decision is not None:
        set_clauses.append("decision = :decision")
        values[":decision"] = decision
    summary = event.get("summary")
    if summary is not None:
        set_clauses.append("summary = :summary")
        values[":summary"] = summary
    reason = event.get("reason")
    if reason is not None:
        set_clauses.append("reason = :reason")
        values[":reason"] = reason
    output_s3_key = event.get("output_s3_key")
    if output_s3_key and event.get("output_object_written"):
        set_clauses.append("output_s3_key = :okey")
        values[":okey"] = output_s3_key
    # Fail-closed internal analysis report (real-pipeline extraction path;
    # the mock pipeline does not produce one).
    if event.get("analysis_report_s3_key"):
        set_clauses.append("has_analysis_report = :has_ar")
        set_clauses.append("analysis_report_reason = :arr")
        values[":has_ar"] = True
        values[":arr"] = reason

    table = dynamodb_resource.Table(REVIEWS_TABLE)
    try:
        table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET " + ", ".join(set_clauses),
            ConditionExpression="attribute_not_exists(#status) OR #status <> :error",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Row is already ERROR -- a failure was recorded first; leave it.
            return
        raise


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Step Functions task entry point for the persist stage.

    Two side effects, then passes the event through unchanged so downstream
    stages (audit) and the eventual real persist implementation (#80-#83) are
    unaffected:

      1. Terminal-state write (issue #188): land the review's terminal
         status/decision/output onto the reviews row. Runs for every
         completed review, independent of whether a spend reservation is
         settled below.
      2. Spend settlement (issue #189): reconcile the worst-case reservation.
         A submission with no recorded spend_reservation_id (a legacy row, or
         one whose reservation the orphan reconciler already released on the
         dead-execution path racing this normal-completion path) is a no-op
         -- settling twice would double-credit the daily_spend row.
    """
    review_id = event.get("review_id")
    if not review_id:
        return event

    dynamodb_resource = _ddb()

    # (1) Terminal-state write -- independent of the spend-settlement guards
    # below, so a review still reaches DONE/MANUAL_REVIEW_REQUIRED even when
    # there is no reservation left to settle.
    _write_terminal_reviews_state(event, dynamodb_resource)

    # (2) Spend settlement.
    submission = _find_submission_for_review(review_id, dynamodb_resource)
    if not submission or not submission.get("spend_reservation_id"):
        return event
    if submission.get("reservation_released"):
        # Already settled/released by the orphan reconciler's dead-execution
        # path (race between this normal-completion path and that
        # recovery path) -- do not double-credit daily_spend.
        return event

    settle_spend(MOCK_PIPELINE_ACTUAL_USD_CENTS, dynamodb_resource)

    submissions_table = dynamodb_resource.Table(REVIEW_SUBMISSIONS_TABLE)
    submissions_table.update_item(
        Key={"idempotency_key": submission["idempotency_key"]},
        UpdateExpression="SET reservation_released = :true, updated_at = :now",
        ExpressionAttributeValues={
            ":true": True,
            ":now": str(int(time.time())),
        },
    )

    return event
