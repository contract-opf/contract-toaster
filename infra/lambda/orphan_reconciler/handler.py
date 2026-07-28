"""
Orphan reconciler Lambda — issue #59.

Runs on a short EventBridge schedule (see pipeline-stack.ts) and repairs two
classes of stuck review:

  1. PENDING with no execution_arn — the submission record was written but
     the process crashed (or timed out) before StartExecution ran. This
     re-drives "ensure execution started" for any submission older than a
     short threshold with no execution_arn recorded.

  2. Dead-execution — a review is non-terminal (PENDING or RUNNING) and has
     an execution_arn, but DescribeExecution shows the Step Functions
     execution already reached a terminal status (FAILED, TIMED_OUT, or
     ABORTED) without its Catch/finally states running (a hard kill: process
     kill, Lambda OOM, Fargate SIGKILL, or an execution terminated
     externally). The reconciler transitions the review to ERROR, releases
     the spend reservation, and releases the held concurrency slot.

See ARCHITECTURE.md -> Data flow (execution-level timeout / semaphore lease
recovery) and RUNBOOK.md -> "Reviews are stuck in PENDING / RUNNING" for the
full narrative this handler implements.

Only the re-drive re-attempts StartExecution; if the re-drive itself fails
repeatedly, the stale-PENDING CloudWatch alarm (#57) is the escalation path
to a human -- this handler does not page directly.

Environment variables:
  REVIEWS_TABLE               DynamoDB reviews table name
  REVIEW_SUBMISSIONS_TABLE    DynamoDB review_submissions table name
  SEMAPHORE_TABLE             DynamoDB concurrency-semaphore table name
  DAILY_SPEND_TABLE           DynamoDB daily_spend counter table name
  STATE_MACHINE_ARN           ARN of the contract-toaster-{env} state machine
  STALE_PENDING_THRESHOLD_SECONDS  age (seconds) after which an ARN-less
                               submission is considered stale (default 120)
"""

import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

REVIEWS_TABLE = os.environ.get("REVIEWS_TABLE", "")
REVIEW_SUBMISSIONS_TABLE = os.environ.get("REVIEW_SUBMISSIONS_TABLE", "")
SEMAPHORE_TABLE = os.environ.get("SEMAPHORE_TABLE", "")
DAILY_SPEND_TABLE = os.environ.get("DAILY_SPEND_TABLE", "")
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
STALE_PENDING_THRESHOLD_SECONDS = int(
    os.environ.get("STALE_PENDING_THRESHOLD_SECONDS", "120")
)

# ---------------------------------------------------------------------------
# Cost-model constants (issue #189 fix) -- MIRROR of backend/src/reviews.py
# (see that module's constants block for the full derivation/rationale) and
# of infra/lambda/persist/handler.py's own copy. This Lambda's deployment
# asset is infra/lambda/orphan_reconciler/ only, so it cannot import either
# of those modules; tests/test_spend_reservation_settlement.py cross-checks
# all three copies stay numerically identical.
# ---------------------------------------------------------------------------
MAX_INPUT_TOKENS = 80_000
MAX_OUTPUT_TOKENS = 8_000
MAX_RETRIES_PER_PASS = 1
REGIONAL_PRICING_PREMIUM = 1.10
PRIMARY_INPUT_RATE_USD_PER_MILLION = 5.50
PRIMARY_OUTPUT_RATE_USD_PER_MILLION = 27.50
CRITIC_INPUT_RATE_USD_PER_MILLION = 3.30
CRITIC_OUTPUT_RATE_USD_PER_MILLION = 16.50

# A dead execution is killed before any settlement ledger entry can be
# written, so its actual spend is unknowable -- settle at $0 actual,
# crediting the FULL worst-case reservation back to the day's budget. This
# is conservative in the user's favor (never under-credits) and matches
# settle_spend()'s documented contract ("possibly to $0 actual spend").
DEAD_EXECUTION_ACTUAL_USD_CENTS = 0


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

# Step Functions terminal-but-not-successful statuses. A dead execution in
# any of these statuses without a corresponding terminal `reviews` row means
# the Catch/finally states never ran (hard kill).
DEAD_EXECUTION_STATUSES = {"FAILED", "TIMED_OUT", "ABORTED"}

NON_TERMINAL_REVIEW_STATUSES = {"PENDING", "RUNNING"}


def _ddb():
    return boto3.resource("dynamodb")


def _sfn():
    return boto3.client("stepfunctions")


def _release_reservation(review_id: str, submission: dict[str, Any]) -> None:
    """Release the unspent worst-case spend reservation for this review.

    Settlement on the dead-execution path reconciles the reservation against
    whatever was actually ledgered (possibly $0 if the execution died before
    any model attempt) -- never silently drops the reservation, per the
    "cost ledger in a finally path" AC (issue #189).

    Issue #189 fix: this previously only set a `reservation_released` flag
    on the submission row and never touched daily_spend.reserved_usd_cents,
    so a dead execution's $2.11 (worst-case, per-model rates -- see the
    module constants above) held its slice of the daily cap PERMANENTLY,
    accumulating until UTC midnight regardless of how many reviews actually
    completed. The flag is retained (audit marker + the idempotency guard
    below) but the credit-back to daily_spend now actually happens.

    Idempotent: if a race let the persist stage's normal-completion
    settlement (infra/lambda/persist/handler.py) run first,
    `reservation_released` is already true and this is a no-op -- crediting
    daily_spend twice for the same reservation would corrupt the ledger.
    """
    reservation_id = submission.get("spend_reservation_id")
    if not reservation_id:
        return
    if submission.get("reservation_released"):
        return

    settle_spend(DEAD_EXECUTION_ACTUAL_USD_CENTS, _ddb())

    table = _ddb().Table(REVIEW_SUBMISSIONS_TABLE)
    table.update_item(
        Key={"idempotency_key": submission["idempotency_key"]},
        UpdateExpression="SET reservation_released = :true, updated_at = :now",
        ExpressionAttributeValues={
            ":true": True,
            ":now": str(int(time.time())),
        },
    )


def _release_semaphore_slot(review_id: str) -> None:
    """Reclaim a concurrency-semaphore slot held by a dead execution.

    Slot-reaper half of the lease/TTL design (see ARCHITECTURE.md ->
    "Semaphore lease / slot-leak recovery"): even if the state machine's own
    Catch/finally release state never ran, the reconciler independently
    reconciles held slots against live executions and reclaims any slot
    whose execution is no longer RUNNING.
    """
    if not SEMAPHORE_TABLE:
        return
    table = _ddb().Table(SEMAPHORE_TABLE)
    # Slots are also self-expiring via DynamoDB TTL (see pipeline-stack.ts
    # semaphore table `ttl` attribute); this explicit delete is best-effort
    # immediate reclaim so a burst of retries does not wait out the TTL.
    try:
        table.delete_item(Key={"lock_name": f"review-slot#{review_id}"})
    except ClientError:
        pass


def _transition_review_to_error(review_id: str, failing_stage: str, reason: str) -> None:
    table = _ddb().Table(REVIEWS_TABLE)
    table.update_item(
        Key={"review_id": review_id},
        UpdateExpression=(
            "SET #status = :error, failing_stage = :stage, "
            "error_reason = :reason, updated_at = :now"
        ),
        ConditionExpression="#status IN (:pending, :running)",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":error": "ERROR",
            ":stage": failing_stage,
            ":reason": reason,
            ":now": str(int(time.time())),
            ":pending": "PENDING",
            ":running": "RUNNING",
        },
    )


def _ensure_execution_started(submission: dict[str, Any]) -> str:
    """Idempotent StartExecution wrapper -- same contract as the API path.

    If ExecutionAlreadyExists, records/returns the existing execution rather
    than treating it as an error (the deterministic execution name IS the
    dedup key in the no-SQS design).

    Re-drives with the pointer-only `execution_input` persisted on the
    submission record at create time (backend/src/reviews.py ->
    create_submission_record). There is no "{}" fallback: a submission
    without a stored execution_input predates this field and must not be
    silently re-driven with an empty payload that would KeyError on the
    first pipeline stage (acquireSlot reads event["review_id"]).
    """
    sfn = _sfn()
    execution_name = submission["execution_name"]
    execution_input = submission["execution_input"]
    try:
        resp = sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=execution_name,
            input=execution_input,
        )
        arn = resp["executionArn"]
    except sfn.exceptions.ExecutionAlreadyExists:
        # Deterministic name collision -- look up the existing execution ARN.
        existing = sfn.describe_execution(
            executionArn=(
                f"{STATE_MACHINE_ARN.replace(':stateMachine:', ':execution:')}:"
                f"{execution_name}"
            )
        )
        arn = existing["executionArn"]

    now = str(int(time.time()))

    table = _ddb().Table(REVIEW_SUBMISSIONS_TABLE)
    table.update_item(
        Key={"idempotency_key": submission["idempotency_key"]},
        UpdateExpression="SET execution_arn = :arn, updated_at = :now",
        ExpressionAttributeValues={":arn": arn, ":now": now},
    )

    review_id = submission.get("review_id")
    if review_id:
        reviews_table = _ddb().Table(REVIEWS_TABLE)
        reviews_table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET execution_arn = :arn, updated_at = :now",
            ExpressionAttributeValues={":arn": arn, ":now": now},
        )

    return arn


def _reconcile_arnless_submissions() -> list[str]:
    """Re-drive 'ensure execution started' for stale ARN-less submissions."""
    table = _ddb().Table(REVIEW_SUBMISSIONS_TABLE)
    now = int(time.time())
    redriven: list[str] = []

    # Scan is acceptable here: this table is small (one row per submission
    # attempt) and the reconciler runs on a short schedule, not per-request.
    resp = table.scan(
        FilterExpression="attribute_not_exists(execution_arn)",
    )
    for submission in resp.get("Items", []):
        created_at = int(submission.get("created_at", now))
        age_seconds = now - created_at
        if age_seconds < STALE_PENDING_THRESHOLD_SECONDS:
            continue
        if not submission.get("execution_input"):
            # No stored pointer-only payload to re-drive with (e.g. a
            # submission record written before execution_input persistence
            # existed). Re-driving with an empty "{}" would KeyError on the
            # first pipeline stage -- skip rather than start a broken
            # execution; the stale-PENDING alarm (#57) still covers this row.
            continue
        _ensure_execution_started(submission)
        redriven.append(submission["idempotency_key"])

    return redriven


def _reconcile_dead_executions() -> list[str]:
    """DescribeExecution on non-terminal reviews with an ARN; resolve dead ones.

    Covers both RUNBOOK.md Observation 2 (PENDING with a dead ARN) and
    Observation 3 (stale RUNNING past the state-machine execution-level
    timeout, which Step Functions itself converts to TIMED_OUT).
    """
    sfn = _sfn()
    reviews_table = _ddb().Table(REVIEWS_TABLE)
    resolved: list[str] = []

    resp = reviews_table.scan(
        FilterExpression="#status IN (:pending, :running) AND attribute_exists(execution_arn)",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":pending": "PENDING", ":running": "RUNNING"},
    )
    for review in resp.get("Items", []):
        review_id = review["review_id"]
        execution_arn = review["execution_arn"]
        try:
            desc = sfn.describe_execution(executionArn=execution_arn)
        except ClientError:
            continue

        exec_status = desc.get("status")
        if exec_status not in DEAD_EXECUTION_STATUSES:
            continue

        submission = _find_submission_for_review(review_id)
        _transition_review_to_error(
            review_id,
            failing_stage="unknown_dead_execution",
            reason=f"execution_{exec_status.lower()}",
        )
        if submission:
            _release_reservation(review_id, submission)
        _release_semaphore_slot(review_id)
        resolved.append(review_id)

    return resolved


def _find_submission_for_review(review_id: str) -> dict[str, Any] | None:
    """Keyed lookup-by-review_id (issue #262 pattern; same as
    infra/lambda/persist/handler.py's _find_submission_for_review and
    backend/src/pipeline_runner.py's _find_submission_by_review_id): an
    unpaginated Scan only ever sees its first (<=1MB) page, so a target row
    on a later page is silently invisible and the dead execution's
    reservation is never released. Prefer the `review_id-index` GSI
    (infra/lib/nested/data-stack.ts) via a real boto3/moto Table.query();
    fall back to scan+filter only for a lightweight test stand-in that
    doesn't implement `.query()`."""
    table = _ddb().Table(REVIEW_SUBMISSIONS_TABLE)
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


def handler(_event: dict[str, Any] = None, _context: Any = None) -> dict[str, Any]:
    """EventBridge-scheduled entry point."""
    redriven = _reconcile_arnless_submissions()
    resolved = _reconcile_dead_executions()
    return {
        "redriven_submissions": redriven,
        "resolved_dead_executions": resolved,
    }
