"""
Review submission API — issue #59 (async review pipeline, mock-first MVP
scope per epic #123).

Implements the idempotent, atomically-reserved submission path described in
ARCHITECTURE.md -> "Data flow — a single review" (steps 1-8) and the issue
#59 acceptance criteria:

  - Idempotency key derivation: client-supplied key preferred (stable across
    the client's own retries); else derived from owner_sub + file SHA-256 +
    active release-bundle hash + a fixed-width timestamp bucket. The derive
    path checks the CURRENT and PREVIOUS bucket for an existing submission
    before creating one, so a boundary-straddling retry cannot double-run.
  - The API creates or reads a `review_submissions` record with a
    conditional write; that record owns review_id, upload pointer,
    spend-reservation id, execution name/ARN/status, and submission status.
  - Atomic, worst-case, retry-inclusive spend reservation on a conditional
    DynamoDB counter (`daily_spend` table) that fails closed if the day's
    cap would be exceeded.
  - Retry-safe "ensure execution started": if no execution ARN is recorded,
    start Step Functions with the deterministic execution name; on
    ExecutionAlreadyExists (or an ARN already present), record/return the
    existing execution rather than erroring.
  - POST /api/reviews (stub) returns 202 + review id.
  - GET /api/reviews/{id} reflects PENDING -> RUNNING -> DONE/ERROR.

The release bundle is resolved ONCE at submission time and stored on the
submission record (reconciliation note #21) — the pipeline execution reads
and verifies that stored hash; it never re-resolves the active bundle
independently. This module owns the single resolution point; verification
happens in the pipeline (pipeline-stack.ts stage skeleton / #59 execution
step 10, see ARCHITECTURE.md).

No SQS on this path — StartExecution is called directly; Step Functions IS
the durable work queue.

Environment variables consumed:
  REVIEW_SUBMISSIONS_TABLE   DynamoDB review_submissions table name
  REVIEWS_TABLE              DynamoDB reviews table name
  DAILY_SPEND_TABLE          DynamoDB daily_spend counter table name
  PLAYBOOKS_TABLE            DynamoDB playbooks table name (PK: playbook_id;
                             active_release_bundle_hash attribute -- issue #194)
  STATE_MACHINE_ARN          ARN of the contract-toaster-{env} state machine
  DAILY_SPEND_CAP_USD_CENTS  daily spend ceiling in cents (default 2000 = $20)

Issue #194 (active-bundle resolver): the release bundle hash is resolved
from the `playbooks` table's `active_release_bundle_hash` attribute by
`resolve_active_release_bundle_hash` / `resolve_and_submit_review` below --
the previously-missing "single resolution point" caller that
`submit_review` (and its `active_release_bundle_hash` parameter) already
expected. `verify_submission_time_bundle` implements the pipeline's
step-10 verification against that same table -- see each function's
docstring.
"""

import hashlib
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from fastapi import HTTPException, status

try:  # production runs `src.main`; tests put backend/src on sys.path
    from src import config, model_client
except ImportError:  # pragma: no cover
    import config  # type: ignore[no-redef]
    import model_client  # type: ignore[no-redef]

# Cross-directory import (same convention backend/src/pipeline_runner.py and
# scripts/primary_review_pass.py already use) to reach
# scripts/playbook_validation.py -- issue #266's runtime bundle-validation
# seam. Idempotent: harmless if some other module already inserted it.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import playbook_validation  # noqa: E402

# Issue #287 (OPF bind 5/5): resolve v2-bundle OPF §8 lineage
# (opf_content_hash / opf_section_digests / the incoming corpus snapshot
# hash) via the registry's optional `bundle_path` + hash section digests
# with the same canonicalize.content_hash() every other content hash in
# the repo uses.
import canonicalize  # noqa: E402
import playbook_registry  # noqa: E402

# ---------------------------------------------------------------------------
# Idempotency-key derivation constants.
# ---------------------------------------------------------------------------

# Fixed-width timestamp bucket used when deriving an idempotency key from
# owner_sub + file hash + release-bundle hash (no client-supplied key).
# Width is documented here per issue #59 AC ("document the width, default
# 10 min"). A boundary-straddling retry (submitted just before/after a
# bucket edge) is handled by checking BOTH the current and the immediately
# previous bucket for an existing submission before creating one.
BUCKET_WIDTH_MINUTES = 10

# ---------------------------------------------------------------------------
# Cost-model constants (mirrors ARCHITECTURE.md -> Cost shape and
# pipeline-stack.ts context caps; kept in sync with the pinned config there).
# ---------------------------------------------------------------------------
MAX_INPUT_TOKENS = 80_000
MAX_OUTPUT_TOKENS = 8_000
MAX_RETRIES_PER_PASS = 1
PASSES_PER_REVIEW = 2  # primary + adversarial (critic)

# Per-model worst-case rates (issue #189 fix). The reservation must price
# EACH pass at that pass's OWN model's rate, not a single blended
# "most expensive tier" rate applied to every pass's tokens -- the latter
# (the pre-fix WORST_CASE_PRICE_PER_TOKEN_USD = Opus output rate, applied to
# ALL passes) overshot the true worst case by 4.6x: $9.68 reserved per
# review vs the documented $2.11 (ARCHITECTURE.md -> Cost shape), which
# 429'd the third review of any day against the $20/day default cap.
#
# These figures mirror model-policy/bedrock-us-east-1.json's
# models.primary/models.critic cost_per_million_{input,output}_usd (the
# direct-API base rates: $5/$25 Opus, $3/$15 Sonnet) with the ~10%
# regional-endpoint surcharge documented in docs/design-notes.md -> Model
# selection & governance applied ($5.50/$27.50 Opus, $3.30/$16.50 Sonnet) --
# the SAME regional rates ARCHITECTURE.md's Cost shape unit-economics table
# cites for its $2.11 worst-case/review arithmetic. They cannot be loaded
# directly from model-policy/*.json at runtime: this module ships inside the
# backend container (backend/Dockerfile COPYs only src/, built from the
# backend/ directory as its Docker context) and infra/lambda/persist/
# handler.py ships as its own standalone Lambda asset (infra/lambda/persist/
# only) -- neither has model-policy/ on its filesystem. Instead,
# tests/test_spend_reservation_settlement.py cross-checks these hardcoded
# figures against model-policy/bedrock-us-east-1.json (base rate x the
# regional premium below) and against infra/lambda/persist/handler.py's own
# mirrored copy, so a policy change that isn't mirrored here fails CI rather
# than silently drifting.
REGIONAL_PRICING_PREMIUM = 1.10  # ~10% regional-endpoint surcharge (docs/design-notes.md)
PRIMARY_INPUT_RATE_USD_PER_MILLION = 5.50  # Opus 4.8 input, regional rate
PRIMARY_OUTPUT_RATE_USD_PER_MILLION = 27.50  # Opus 4.8 output, regional rate
CRITIC_INPUT_RATE_USD_PER_MILLION = 3.30  # Sonnet 4.6 input, regional rate
CRITIC_OUTPUT_RATE_USD_PER_MILLION = 16.50  # Sonnet 4.6 output, regional rate

DAILY_SPEND_CAP_USD_CENTS_DEFAULT = 2000  # $20.00/day default ceiling


REVIEW_STATUSES_NON_TERMINAL = {"PENDING", "RUNNING"}
REVIEW_STATUSES_TERMINAL = {
    "DONE",
    "ERROR",
    "ERROR_MANUAL_REVIEW_REQUIRED",
    "MANUAL_REVIEW_REQUIRED",
    "QUARANTINED",
    "SUPERSEDED",
}

# ---------------------------------------------------------------------------
# Stage-failure taxonomy (issue #258) -- target-agnostic core shared by both
# deployment targets. Today `failing_stage` is hardcoded to `'pipeline'` in
# the AWS Step Functions error-transition Lambda
# (infra/lib/nested/pipeline-stack.ts) and the DTS in-process runner has its
# own separate hardcoded stage-failure write (pipeline_runner.py). Wiring
# either caller onto `record_stage_failure` below is deliberately OUT OF
# SCOPE here (folds into #244 for AWS's errorTransition, and into the
# DTS-wire ticket for pipeline_runner.py) -- this only establishes the single
# shared mechanism + taxonomy both wirings will call.
#
# `reason` -> reachable terminal status for the two DOCUMENTED manual-review
# outcomes. A `reason` not listed here still records the real failing stage,
# but resolves to the generic `ERROR` status (the same terminal status the
# AWS errorTransition Lambda and the DTS runner's `_fail_review` already use
# for an unmapped/unexpected failure) -- this taxonomy only carves out the
# two statuses that must be specifically reachable, it does not replace the
# generic failure path.
STAGE_FAILURE_REASON_STATUS: dict[str, str] = {
    # Structured-output retry exhausted (a model stage that never produced
    # parseable structured output after its retry budget).
    "structured_output_retry_exhausted": "ERROR_MANUAL_REVIEW_REQUIRED",
    # Document exceeds the size cap enforced ahead of the model stages.
    "document_too_large": "MANUAL_REVIEW_REQUIRED",
}


# ---------------------------------------------------------------------------
# Idempotency key derivation
# ---------------------------------------------------------------------------

def _current_bucket(now_epoch: float) -> int:
    return int(now_epoch // (BUCKET_WIDTH_MINUTES * 60))


def derive_idempotency_key(
    owner_sub: str,
    file_sha256: str,
    release_bundle_hash: str,
    now_epoch: float | None = None,
) -> str:
    """Derive a fallback idempotency key when the client supplies none.

    Key = sha256(owner_sub + file_sha256 + release_bundle_hash + bucket).
    The bucket is a fixed-width (BUCKET_WIDTH_MINUTES) integer window over
    epoch time, so identical retries within the same window collide on the
    same key.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    bucket = _current_bucket(now_epoch)
    raw = f"{owner_sub}:{file_sha256}:{release_bundle_hash}:{bucket}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def candidate_idempotency_keys(
    owner_sub: str,
    file_sha256: str,
    release_bundle_hash: str,
    now_epoch: float | None = None,
) -> list[str]:
    """Return [current_bucket_key, previous_bucket_key].

    Per issue #59 AC: "To avoid a boundary-straddling retry double-running,
    the derive path checks the current AND previous bucket for an existing
    submission before creating one." A retry landing just after a bucket
    edge must still find the submission created just before the edge.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    current = _current_bucket(now_epoch)
    previous_epoch = now_epoch - (BUCKET_WIDTH_MINUTES * 60)
    previous = _current_bucket(previous_epoch)

    keys = []
    for bucket in (current, previous):
        raw = f"{owner_sub}:{file_sha256}:{release_bundle_hash}:{bucket}"
        keys.append(hashlib.sha256(raw.encode("utf-8")).hexdigest())
    return keys


def resolve_idempotency_key(
    client_supplied_key: str | None,
    owner_sub: str,
    file_sha256: str,
    release_bundle_hash: str,
    now_epoch: float | None = None,
) -> str:
    """Client-supplied key is preferred (stable across the client's own
    retries); otherwise derive one from owner/file/bundle/time-bucket."""
    if client_supplied_key:
        return client_supplied_key
    return derive_idempotency_key(owner_sub, file_sha256, release_bundle_hash, now_epoch)


# ---------------------------------------------------------------------------
# Submission record: conditional create-or-fetch
# ---------------------------------------------------------------------------

def find_existing_submission(
    idempotency_key: str,
    owner_sub: str,
    file_sha256: str,
    release_bundle_hash: str,
    dynamodb_resource: Any,
    now_epoch: float | None = None,
) -> dict[str, Any] | None:
    """Look up an existing submission by the resolved key, and — for the
    derived-key (no client key) path — also check the previous bucket so a
    boundary-straddling retry finds the original submission rather than
    creating a duplicate."""
    table = dynamodb_resource.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])

    candidates = [idempotency_key]
    # If this looks like a derived key (not a client-supplied opaque token),
    # also probe the previous bucket. We always compute and check both
    # candidate keys for the derived path regardless of hit/miss on the
    # primary key, since the caller may pass either bucket's key here.
    candidates += [
        k
        for k in candidate_idempotency_keys(owner_sub, file_sha256, release_bundle_hash, now_epoch)
        if k not in candidates
    ]

    for key in candidates:
        resp = table.get_item(Key={"idempotency_key": key})
        item = resp.get("Item")
        if item:
            return item
    return None


def create_submission_record(
    idempotency_key: str,
    owner_sub: str,
    upload_pointer: str,
    release_bundle_hash: str,
    reservation_id: str | None,
    review_id: str,
    execution_name: str,
    execution_input: str,
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """Conditional PutItem — creates the submission record exactly once.

    The record owns review_id, upload pointer, spend-reservation id,
    execution name, execution ARN/status (initially null), the pointer-only
    execution_input payload, and submission status. A retry that races this
    call and loses gets ConditionalCheckFailedException and must re-fetch via
    find_existing_submission instead (the caller's responsibility).

    reservation_id may be None here: the submission record is created BEFORE
    spend is reserved (see submit_review), so only the request that wins the
    conditional create ever calls reserve_spend; the winner then records its
    reservation id via _record_spend_reservation. This avoids a losing
    concurrent request leaking a reservation with no submission record to
    settle it.

    execution_input is persisted here (not just built on the fly by the API
    path) so a crash-recovered re-drive -- e.g. the orphan reconciler's
    ARN-less re-drive path -- has a well-formed pointer-only payload to start
    the execution with, rather than an empty "{}" that would KeyError on the
    first pipeline stage.

    Reconciliation note #21: the resolved release_bundle_hash is stored here
    — the single resolution point. The pipeline execution verifies this
    stored hash; it never re-resolves the active bundle itself.
    """
    table = dynamodb_resource.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
    now = str(int(time.time()))
    item = {
        "idempotency_key": idempotency_key,
        "review_id": review_id,
        "owner_sub": owner_sub,
        "upload_pointer": upload_pointer,
        "release_bundle_hash": release_bundle_hash,
        "spend_reservation_id": reservation_id,
        "execution_name": execution_name,
        "execution_input": execution_input,
        "execution_arn": None,
        "execution_status": None,
        "submission_status": "PENDING",
        "created_at": now,
        "updated_at": now,
    }
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(idempotency_key)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Submission already exists for this idempotency key.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unable to create submission record: {exc!r}",
        ) from exc
    return item


# ---------------------------------------------------------------------------
# Atomic, worst-case, retry-inclusive spend reservation
# ---------------------------------------------------------------------------

def _active_provider_rates() -> tuple[float, float, float, float]:
    """Per-million-token worst-case rates for whichever model provider is
    actually being billed, keyed by `config.model_provider()`
    (`MODEL_PROVIDER` env var) -- issue #268.

    `MODEL_PROVIDER=openrouter` (the DTS deployment target) reads
    `model-policy/openrouter.json`'s `cost_per_million_{input,output}_usd`
    rates, which are already flat/all-in per token (no regional premium --
    see that file's own `_comment`). Any other value (including unset, the
    AWS/Bedrock target's default) returns the existing hardcoded Bedrock
    regional-rate constants, UNCHANGED -- this branch must never perturb
    the Bedrock path's documented $2.11 worst case (issue #189).

    Returns `(primary_input, primary_output, critic_input, critic_output)`
    in USD per million tokens.
    """
    if config.model_provider() == "openrouter":
        policy = model_client.load_openrouter_policy()
        primary = policy["models"]["primary"]
        critic = policy["models"]["critic"]
        return (
            primary["cost_per_million_input_usd"],
            primary["cost_per_million_output_usd"],
            critic["cost_per_million_input_usd"],
            critic["cost_per_million_output_usd"],
        )
    return (
        PRIMARY_INPUT_RATE_USD_PER_MILLION,
        PRIMARY_OUTPUT_RATE_USD_PER_MILLION,
        CRITIC_INPUT_RATE_USD_PER_MILLION,
        CRITIC_OUTPUT_RATE_USD_PER_MILLION,
    )


def compute_worst_case_reservation_usd_cents() -> int:
    """Worst-case spend reservation for a single review.

    Retry-inclusive, per-model formula (issue #189 fix; retry-inclusive
    shape per reconciliation note #14):

        reservation = (1 + max_retries_per_pass) * sum over {primary, critic} of
            (max_input_tokens * that_model's_input_rate_per_token
             + max_output_tokens * that_model's_output_rate_per_token)

    Each pass (primary/Opus, critic/Sonnet) is priced at ITS OWN model's
    rate rather than a single blended "most expensive tier" rate applied to
    both passes (see the constants above for why that overshot 4.6x).

    The rate table itself is provider-aware (`_active_provider_rates`,
    issue #268): `MODEL_PROVIDER=openrouter` prices from
    `model-policy/openrouter.json` instead of the Bedrock constants, so the
    reservation reflects whichever provider is actually being billed.

    Folding the retry budget into the reservation at reserve-time means any
    sequence of attempts within that budget cannot overshoot the reservation
    — only the settled actual spend (ledgered after every model attempt,
    including failures) can come in under it.
    """
    attempts_per_pass = 1 + MAX_RETRIES_PER_PASS
    primary_input_rate, primary_output_rate, critic_input_rate, critic_output_rate = (
        _active_provider_rates()
    )
    primary_usd = MAX_INPUT_TOKENS * (
        primary_input_rate / 1_000_000
    ) + MAX_OUTPUT_TOKENS * (primary_output_rate / 1_000_000)
    critic_usd = MAX_INPUT_TOKENS * (
        critic_input_rate / 1_000_000
    ) + MAX_OUTPUT_TOKENS * (critic_output_rate / 1_000_000)
    usd = attempts_per_pass * (primary_usd + critic_usd)
    return int(round(usd * 100))


def compute_actual_usd_cents_from_usage(
    primary_usage: dict[str, int] | None,
    critic_usage: dict[str, int] | None,
) -> int:
    """Actual settled cost (cents) for one review's primary + critic passes,
    priced from REAL provider-reported token usage rather than the
    worst-case reservation estimate (issue #268).

    Each usage dict is `{"input_tokens": int, "output_tokens": int}` --
    e.g. `OpenRouterModelClient.last_usage`
    (backend/src/model_client.py's `parse_openrouter_usage`), captured from
    the provider's OWN response (`usage.prompt_tokens` /
    `usage.completion_tokens` for OpenRouter's OpenAI-compatible API), not
    estimated from prompt/response text length. A None or missing pass
    (e.g. the critic pass never ran because the primary pass failed closed)
    contributes $0 rather than raising.

    Uses `_active_provider_rates()` -- the SAME provider-aware rate table
    `compute_worst_case_reservation_usd_cents` uses for this review's
    reservation, so a review's reservation and its eventual settlement are
    always priced against the same provider.
    """
    primary_input_rate, primary_output_rate, critic_input_rate, critic_output_rate = (
        _active_provider_rates()
    )
    total_usd = 0.0
    if primary_usage:
        total_usd += primary_usage.get("input_tokens", 0) * (primary_input_rate / 1_000_000)
        total_usd += primary_usage.get("output_tokens", 0) * (primary_output_rate / 1_000_000)
    if critic_usage:
        total_usd += critic_usage.get("input_tokens", 0) * (critic_input_rate / 1_000_000)
        total_usd += critic_usage.get("output_tokens", 0) * (critic_output_rate / 1_000_000)
    return int(round(total_usd * 100))


def reserve_spend(
    review_id: str,
    dynamodb_resource: Any,
    now_epoch: float | None = None,
) -> str:
    """Atomically reserve the worst-case cost for `review_id`, exactly once.

    A SINGLE atomic conditional UpdateExpression increments the day's
    reserved total and fails closed (ConditionalCheckFailedException) if
    that would exceed the configured daily cap — not an optimistic
    read-then-check, so concurrent submissions cannot collectively overshoot
    the cap before settlement (issue #59 AC).

    Returns the reservation id (used as the settlement key later).

    Raises:
        HTTPException(429) — "daily limit reached" — if reserving would
        exceed the cap.
    """
    table = dynamodb_resource.Table(os.environ["DAILY_SPEND_TABLE"])
    now_epoch = time.time() if now_epoch is None else now_epoch
    spend_date = time.strftime("%Y-%m-%d", time.gmtime(now_epoch))
    reservation_amount_cents = compute_worst_case_reservation_usd_cents()
    daily_cap_cents = int(
        os.environ.get("DAILY_SPEND_CAP_USD_CENTS", str(DAILY_SPEND_CAP_USD_CENTS_DEFAULT))
    )
    reservation_id = str(uuid.uuid4())

    try:
        # Single atomic conditional update — the reserve and the cap-check
        # happen in the same DynamoDB request, so no window exists between
        # "check the cap" and "commit the reservation" for a second
        # concurrent submission to race through.
        table.update_item(
            Key={"spend_date": spend_date},
            UpdateExpression=(
                "SET reserved_usd_cents = if_not_exists(reserved_usd_cents, :zero) + :amount, "
                "daily_cap_usd_cents = if_not_exists(daily_cap_usd_cents, :cap)"
            ),
            ConditionExpression=(
                "attribute_not_exists(reserved_usd_cents) OR "
                "reserved_usd_cents + :amount <= if_not_exists(daily_cap_usd_cents, :cap)"
            ),
            ExpressionAttributeValues={
                ":zero": 0,
                ":amount": reservation_amount_cents,
                ":cap": daily_cap_cents,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Daily spend limit reached. Try again after the cap resets (UTC midnight).",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unable to reserve spend: {exc!r}",
        ) from exc

    return reservation_id


def _record_spend_reservation(
    submission: dict[str, Any],
    reservation_id: str,
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """Attach a spend reservation id to an already-created submission record.

    Called only by the winner of the create_submission_record race (see
    submit_review): the submission row is created first with
    spend_reservation_id=None, then reserve_spend runs, then this stamps the
    resulting reservation id onto that same row. A losing concurrent request
    never reaches reserve_spend at all, so it cannot leak a reservation.
    """
    table = dynamodb_resource.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
    table.update_item(
        Key={"idempotency_key": submission["idempotency_key"]},
        UpdateExpression="SET spend_reservation_id = :rid, updated_at = :now",
        ExpressionAttributeValues={
            ":rid": reservation_id,
            ":now": str(int(time.time())),
        },
    )
    submission["spend_reservation_id"] = reservation_id
    return submission


def settle_spend(
    review_id: str,
    reservation_id: str,
    actual_usd_cents: int,
    dynamodb_resource: Any,
    now_epoch: float | None = None,
) -> None:
    """Reconcile the reservation against ledgered actual spend.

    Called from the pipeline's finally path (persist/audit stage) — and by
    the orphan reconciler on the dead-execution path — so a failed or
    retried review still settles (possibly to $0 actual spend) rather than
    silently holding the worst-case reservation forever.
    """
    table = dynamodb_resource.Table(os.environ["DAILY_SPEND_TABLE"])
    now_epoch = time.time() if now_epoch is None else now_epoch
    spend_date = time.strftime("%Y-%m-%d", time.gmtime(now_epoch))
    reservation_amount_cents = compute_worst_case_reservation_usd_cents()
    # Reverse the worst-case reservation, apply the actual settled cost.
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


# ---------------------------------------------------------------------------
# Retry-safe "ensure execution started"
# ---------------------------------------------------------------------------

def deterministic_execution_name(review_id: str) -> str:
    """The execution name IS the dedup mechanism (no SQS on this path).

    Deterministic and stable for a given review_id so a retried
    StartExecution call collides (ExecutionAlreadyExists) instead of
    starting a second execution.
    """
    return f"review-{review_id}"


def ensure_execution_started(
    submission: dict[str, Any],
    execution_input_json: str,
    dynamodb_resource: Any,
    sfn_client: Any,
) -> dict[str, Any]:
    """Idempotently ensure a Step Functions execution exists for this review.

    - If no execution_arn is recorded yet, call StartExecution with the
      deterministic name and record the resulting ARN/status.
    - If StartExecution raises ExecutionAlreadyExists (a concurrent/retried
      caller raced us, or a prior crash left the ARN unrecorded locally),
      look up and record the existing execution instead of erroring.
    - If an execution_arn is already present on the submission record,
      return it as-is (no-op).

    This same function is called both from the API request path and from
    the orphan reconciler's re-drive path, so the two paths cannot diverge
    in behavior.

    The execution_arn is recorded on BOTH the review_submissions row (the
    idempotency/dedup record) AND the reviews row. The reviews-row copy is
    what the orphan reconciler's dead-execution scan
    (_reconcile_dead_executions) filters on -- without it, that scan's
    `attribute_exists(execution_arn)` filter can never match and the
    dead-execution reconciliation path is dead code.
    """
    state_machine_arn = os.environ["STATE_MACHINE_ARN"]
    execution_name = submission["execution_name"]

    if submission.get("execution_arn"):
        return submission

    try:
        resp = sfn_client.start_execution(
            stateMachineArn=state_machine_arn,
            name=execution_name,
            input=execution_input_json,
        )
        execution_arn = resp["executionArn"]
    except sfn_client.exceptions.ExecutionAlreadyExists:
        execution_arn_prefix = state_machine_arn.replace(":stateMachine:", ":execution:")
        execution_arn = f"{execution_arn_prefix}:{execution_name}"

    now = str(int(time.time()))

    submissions_table = dynamodb_resource.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
    submissions_table.update_item(
        Key={"idempotency_key": submission["idempotency_key"]},
        UpdateExpression="SET execution_arn = :arn, execution_status = :status, updated_at = :now",
        ExpressionAttributeValues={
            ":arn": execution_arn,
            ":status": "RUNNING",
            ":now": now,
        },
    )

    reviews_table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    reviews_table.update_item(
        Key={"review_id": submission["review_id"]},
        UpdateExpression="SET execution_arn = :arn, updated_at = :now",
        ExpressionAttributeValues={
            ":arn": execution_arn,
            ":now": now,
        },
    )

    submission["execution_arn"] = execution_arn
    submission["execution_status"] = "RUNNING"
    return submission


# ---------------------------------------------------------------------------
# Active release-bundle resolver (issue #194).
#
# Prior to this, `submit_review`'s `active_release_bundle_hash` parameter
# was a bare parameter with no caller: nothing read
# `playbooks.active_release_bundle_hash`, so a review could only be
# submitted with a hash some other, non-existent caller supplied. These two
# functions are the missing single resolution point:
#
#   - `resolve_active_release_bundle_hash` / `resolve_and_submit_review`
#     implement ARCHITECTURE.md data-flow step 3 ("Resolve the active
#     release bundle ... and derive the idempotency key") for the
#     submission route, INCLUDING the documented no-active-bundle refusal
#     (ARCHITECTURE.md -> "No-active-bundle system state";
#     docs/playbook-governance.md; RUNBOOK.md -> "Suspending intake"):
#     HTTP 503, detail "no active playbook" -- never a faked/fallback hash.
#   - `verify_submission_time_bundle` implements step 10 ("Verify the
#     submission-time bundle; never re-resolve") for the pipeline: it
#     compares the hash stored on the submission/reviews records at step 3
#     against the CURRENT active bundle and QUARANTINEs on mismatch
#     ("Retired-bundle-before-start behavior").
#
# The full release-bundle activation/rollback/deactivate ADMIN API (#41,
# #67, #68) is explicitly deferred past this slice (issue #194 "Suggested
# direction"); only the read side (the resolver + verify step) lands here.
# Activation of a NEW bundle is out of scope; SEEDING the table with the
# eiaa v1.0.0 bundle so the resolver has something real to read is handled
# by scripts/seed_active_bundle.py (uses scripts/canonicalize.py's real
# content_hash -- never a placeholder).
# ---------------------------------------------------------------------------

# User-visible refusal message for the no-active-bundle system state.
# Exact string matched by tests/test_no_active_bundle.py's
# NO_ACTIVE_PLAYBOOK_MESSAGE_PATTERN and by ARCHITECTURE.md /
# docs/playbook-governance.md / RUNBOOK.md.
NO_ACTIVE_PLAYBOOK_DETAIL = "no active playbook"

# Pipeline QUARANTINE reason for a bundle retired/deactivated between
# submission and execution start (ARCHITECTURE.md data-flow step 10,
# "Retired-bundle-before-start behavior").
QUARANTINE_REASON_SUBMISSION_TIME_BUNDLE_RETIRED = "submission_time_bundle_retired"


def _read_active_release_bundle_hash(
    playbook_id: str,
    dynamodb_resource: Any,
) -> str | None:
    """Read `playbooks.active_release_bundle_hash` for `playbook_id`, then
    validate the ON-DISK playbook body that hash is supposed to identify
    (issue #266: runtime validation of the active bundle -- previously
    `playbooks/schema.json` was CI-only, so every reader of this attribute
    trusted the artifact blindly). Returns None -- the SAME "no active
    bundle" signal a genuinely-empty row produces -- if:

      - the playbook row does not exist, or exists but carries no active
        bundle (the documented no-active-bundle state -- e.g. after a
        deactivate action, or before this playbook's first bundle has
        ever been activated), OR
      - the row DOES carry a hash, but the current on-disk playbook body
        for `playbook_id` fails runtime validation (schema-invalid, or a
        covering topic is missing its `our_standard` standard-form text
        -- see `scripts/playbook_validation.py::load_and_validate_playbook`).
        An invalid playbook must never resolve as active: fail closed to
        the exact same refusal a missing bundle produces, never a
        partial/invalid load.

    Never raises, never resolves-and-caches: this is a bare read. Callers
    decide what "no active bundle" means for their step --
    `resolve_active_release_bundle_hash` (submission time) refuses;
    `verify_submission_time_bundle` (execution time) quarantines -- both
    now inherit the same fail-closed validation for free, since both read
    through this single function.
    """
    table = dynamodb_resource.Table(os.environ["PLAYBOOKS_TABLE"])
    resp = table.get_item(Key={"playbook_id": playbook_id})
    item = resp.get("Item")
    if not item:
        return None
    active_hash = item.get("active_release_bundle_hash") or None
    if not active_hash:
        return None

    try:
        playbook_validation.load_and_validate_playbook(playbook_id)
    except playbook_validation.PlaybookValidationError:
        return None

    return active_hash


def resolve_active_release_bundle_hash(
    playbook_id: str,
    dynamodb_resource: Any,
) -> str:
    """The single resolution point (reconciliation note #21;
    ARCHITECTURE.md data-flow step 3): read the CURRENT active release
    bundle hash for `playbook_id` from the `playbooks` table exactly once,
    at submission time. The pipeline (`verify_submission_time_bundle`)
    never re-resolves -- it only verifies the hash this function returned,
    which the submission route stores via `submit_review`.

    Raises HTTPException(503, "no active playbook") -- the documented
    no-active-bundle refusal -- when no bundle is active for this
    playbook, rather than fabricating or falling back to a hash. Per
    ARCHITECTURE.md step 3, this must fire BEFORE any spend is reserved or
    submission record is created; `resolve_and_submit_review` below calls
    this before calling `submit_review` for exactly that reason.
    """
    active_hash = _read_active_release_bundle_hash(playbook_id, dynamodb_resource)
    if not active_hash:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=NO_ACTIVE_PLAYBOOK_DETAIL,
        )
    return active_hash


def resolve_and_submit_review(
    owner_sub: str,
    playbook_id: str,
    file_sha256: str,
    upload_pointer: str,
    dynamodb_resource: Any,
    sfn_client: Any,
    client_supplied_idempotency_key: str | None = None,
) -> dict[str, Any]:
    """The submission route (issue #194): resolves the active release
    bundle for `playbook_id` ONCE (step 3), refusing with 503 "no active
    playbook" if none is active, then hands the resolved hash to
    `submit_review`, which stores it on the submission record and on the
    reviews row's `playbook_hash` (reconciliation note #21) exactly as
    `submit_review`'s own docstring already documented.

    `submit_review`'s signature is intentionally left unchanged -- existing
    callers that already resolve their own hash (tests, the orphan
    reconciler's re-drive path) keep working unmodified. This function is
    the resolving entry point a live `POST /api/reviews` route would call.
    """
    active_release_bundle_hash = resolve_active_release_bundle_hash(
        playbook_id, dynamodb_resource
    )
    return submit_review(
        owner_sub=owner_sub,
        playbook_id=playbook_id,
        file_sha256=file_sha256,
        upload_pointer=upload_pointer,
        active_release_bundle_hash=active_release_bundle_hash,
        dynamodb_resource=dynamodb_resource,
        sfn_client=sfn_client,
        client_supplied_idempotency_key=client_supplied_idempotency_key,
    )


def verify_submission_time_bundle(
    review_id: str,
    playbook_id: str,
    submission_time_bundle_hash: str,
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """Pipeline verify step (ARCHITECTURE.md data-flow step 10): verify the
    release bundle recorded at submission time (step 3) is STILL the
    active bundle for `playbook_id`. Never re-resolves the active bundle --
    reads it ONLY to compare against the hash already resolved once, at
    submission (reconciliation note #21). Replaces the previous
    pass-through stub (issue #194 concern) with a real check.

    - Hash still active -> the review proceeds: returns verified=True and
      does not touch the reviews row.
    - Hash no longer active (a different bundle is now active, or the
      bundle was deactivated and none is active at all) ->
      "Retired-bundle-before-start behavior" (ARCHITECTURE.md step 10):
      the review is refused. Writes reviews.status = QUARANTINED,
      quarantine_reason = submission_time_bundle_retired (docs/
      data-handling.md's documented post-terminal administrative overlay
      fields), quarantine_bundle_hash = the now-stale submission-time
      hash. Returns verified=False so the caller does not proceed to step
      11 (extract).
    """
    current_active_hash = _read_active_release_bundle_hash(playbook_id, dynamodb_resource)

    if current_active_hash == submission_time_bundle_hash:
        return {"review_id": review_id, "verified": True}

    reviews_table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    now = str(int(time.time()))
    reviews_table.update_item(
        Key={"review_id": review_id},
        UpdateExpression=(
            "SET #status = :quarantined, quarantine_reason = :reason, "
            "quarantine_bundle_hash = :hash, updated_at = :now"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":quarantined": "QUARANTINED",
            ":reason": QUARANTINE_REASON_SUBMISSION_TIME_BUNDLE_RETIRED,
            ":hash": submission_time_bundle_hash,
            ":now": now,
        },
    )
    return {
        "review_id": review_id,
        "verified": False,
        "status": "QUARANTINED",
        "reason": QUARANTINE_REASON_SUBMISSION_TIME_BUNDLE_RETIRED,
    }


# ---------------------------------------------------------------------------
# POST /api/reviews (stub) and GET /api/reviews/{id}
# ---------------------------------------------------------------------------

def submit_review(
    owner_sub: str,
    playbook_id: str,
    file_sha256: str,
    upload_pointer: str,
    active_release_bundle_hash: str,
    dynamodb_resource: Any,
    sfn_client: Any,
    client_supplied_idempotency_key: str | None = None,
    review_id: str | None = None,
) -> dict[str, Any]:
    """POST /api/reviews (stub is fine per issue #59 AC).

    Creates a PENDING review through the submission record, reserves spend
    once, stores the upload pointer, ensures the execution is started, and
    returns 202 + review id.

    The release bundle hash is resolved ONCE here (by the caller, passed in
    as active_release_bundle_hash) and stored on the submission record —
    the pipeline never re-resolves it (reconciliation note #21).

    review_id (issue #84): callers that already wrote the uploaded bytes to
    S3 under a specific review_id (see src/review_routes.py -- the route
    writes to ``uploads/{owner_sub}/{review_id}/in.docx`` BEFORE calling
    this function, so it must know review_id in advance) may pass that same
    id here so the fresh-submission path uses it instead of minting a new
    one, keeping the reviews row's identity consistent with the S3 pointer
    already on disk. Ignored on the resumed/duplicate path (the existing
    submission's own review_id and upload_pointer are authoritative there —
    see the `existing` branch below). Defaults to a fresh uuid4 for callers
    that don't pre-allocate one (e.g. the existing test suite).
    """
    idempotency_key = resolve_idempotency_key(
        client_supplied_idempotency_key, owner_sub, file_sha256, active_release_bundle_hash
    )

    existing = find_existing_submission(
        idempotency_key, owner_sub, file_sha256, active_release_bundle_hash, dynamodb_resource
    )
    if existing:
        # Retry: resume from recorded state rather than double-running.
        # ensure_execution_started is idempotent, so calling it again on an
        # already-started submission is a safe no-op.
        execution_input_json = _build_execution_input_json(existing, playbook_id)
        existing = ensure_execution_started(
            existing, execution_input_json, dynamodb_resource, sfn_client
        )
        return {
            "review_id": existing["review_id"],
            "status_code": status.HTTP_202_ACCEPTED,
            "resumed": True,
        }

    review_id = review_id or str(uuid.uuid4())
    execution_name = deterministic_execution_name(review_id)
    # Issue #287: v2-bundle OPF §8 lineage, resolved once alongside the
    # other submission-time facts -- None/absent for a v1 playbook.
    opf_lineage = _resolve_opf_lineage(playbook_id)
    execution_input_json = _build_execution_input_json_from_parts(
        review_id=review_id,
        owner_sub=owner_sub,
        playbook_id=playbook_id,
        upload_s3_key=upload_pointer,
        release_bundle_hash=active_release_bundle_hash,
        opf_content_hash=opf_lineage["opf_content_hash"],
        opf_section_digests_hash=opf_lineage["opf_section_digests_hash"],
        opf_corpus_snapshot_hash=opf_lineage["opf_corpus_snapshot_hash"],
        posture_version=opf_lineage["posture_version"],
    )

    # Create the submission record (conditional write) BEFORE reserving
    # spend. Two concurrent same-derived-key requests both missing
    # find_existing_submission now race on create_submission_record's
    # ConditionalCheckFailedException instead of on reserve_spend: only the
    # winner ever reserves, so a losing request cannot leak a worst-case
    # reservation with no submission record to settle it (the orphan
    # reconciler settles/releases reservations keyed on submission records).
    submission = create_submission_record(
        idempotency_key=idempotency_key,
        owner_sub=owner_sub,
        upload_pointer=upload_pointer,
        release_bundle_hash=active_release_bundle_hash,
        reservation_id=None,
        review_id=review_id,
        execution_name=execution_name,
        execution_input=execution_input_json,
        dynamodb_resource=dynamodb_resource,
    )

    reservation_id = reserve_spend(review_id, dynamodb_resource)
    submission = _record_spend_reservation(submission, reservation_id, dynamodb_resource)

    _create_review_row(
        review_id,
        owner_sub,
        playbook_id,
        active_release_bundle_hash,
        dynamodb_resource,
        opf_content_hash=opf_lineage["opf_content_hash"],
        opf_section_digests_hash=opf_lineage["opf_section_digests_hash"],
        opf_corpus_snapshot_hash=opf_lineage["opf_corpus_snapshot_hash"],
        posture_version=opf_lineage["posture_version"],
    )

    ensure_execution_started(submission, execution_input_json, dynamodb_resource, sfn_client)

    return {
        "review_id": review_id,
        "status_code": status.HTTP_202_ACCEPTED,
        "resumed": False,
    }


# ---------------------------------------------------------------------------
# OPF §8 lineage resolver (issue #287, OPF bind 5/5).
#
# A v2 bundle (playbooks/bundle.schema-v2.json, issue #286) embeds the FULL
# OPF document plus a `lineage` block (opf_content_hash + opf_section_digests,
# copied verbatim from opf.identity by scripts/bind_bundle.py). This resolver
# locates that artifact via the registry's optional per-playbook
# `bundle_path` (scripts/playbook_registry.py's `PlaybookEntry.bundle_path`)
# and reads it. A v1 playbook -- no `bundle_path` registered, today's only
# shape -- resolves every field to None: byte-identical behavior to before
# this issue, never a fabricated value.
#
# `opf_corpus_snapshot_hash` (2026-07 engine #185 update, folded into this
# same slice per the issue's Grind notes) is read directly from the embedded
# OPF's `corpus.snapshot.manifest_hash` -- NOT part of the bundle's
# `lineage` block, which is identity-only per the schema -- and is None
# whenever that field is absent from the embedded OPF (e.g. an OPF authored
# before #185 landed in the engine), never a placeholder.
#
# Kept generic on purpose (a dict of optional fields, not fixed positional
# params) so a future lineage field (#294's `posture_version`) is a
# mechanical addition here, not a call-site rewrite.
#
# #294 update: `posture_version` (int | None) is now that mechanical
# addition -- read from the bundle's `overrides.posture.version` when
# present (None when the bundle carries no posture override, i.e. genesis).
# It is NOT part of `lineage` (identity-only per the bundle schema); it
# lives alongside the other three fields here purely because this resolver
# is the one place that already reads the bundle once per submission.
# ---------------------------------------------------------------------------

_EMPTY_OPF_LINEAGE: dict[str, str | int | None] = {
    "opf_content_hash": None,
    "opf_section_digests_hash": None,
    "opf_corpus_snapshot_hash": None,
    "posture_version": None,
}


def _resolve_opf_lineage(playbook_id: str) -> dict[str, str | int | None]:
    """Resolve OPF §8 lineage for `playbook_id`'s active v2 bundle, if any.

    Returns a dict with keys "opf_content_hash", "opf_section_digests_hash",
    "opf_corpus_snapshot_hash" (each `str | None`), and "posture_version"
    (`int | None`, issue #294). All four are None when the playbook has no
    registry entry, no `bundle_path`, or the bundle_path does not resolve
    to a readable file -- the same "nothing to record" signal as a v1
    playbook, never an error (this resolver never changes submission
    behavior; it is purely additive).
    """
    try:
        entry = playbook_registry.resolve_playbook(playbook_id)
    except playbook_registry.PlaybookNotRegisteredError:
        return dict(_EMPTY_OPF_LINEAGE)

    bundle_path = entry.bundle_path
    if bundle_path is None:
        return dict(_EMPTY_OPF_LINEAGE)

    import json

    try:
        with open(bundle_path, encoding="utf-8") as f:
            bundle = json.load(f)
    except FileNotFoundError:
        return dict(_EMPTY_OPF_LINEAGE)

    lineage = bundle.get("lineage") or {}
    opf_content_hash = lineage.get("opf_content_hash")
    section_digests = lineage.get("opf_section_digests")
    opf_section_digests_hash = (
        canonicalize.content_hash(section_digests) if section_digests is not None else None
    )

    opf = bundle.get("opf") or {}
    corpus = opf.get("corpus") or {}
    snapshot = corpus.get("snapshot") or {}
    opf_corpus_snapshot_hash = snapshot.get("manifest_hash")

    # Issue #294: absent overrides.posture -> None (genesis), never a
    # fabricated 0 -- the schema-normative "absent overrides implies
    # genesis version 0" is a bind_bundle-time monotonic-versioning detail,
    # not something this read-side resolver invents.
    posture_version = (bundle.get("overrides") or {}).get("posture", {}).get("version")

    return {
        "opf_content_hash": opf_content_hash,
        "opf_section_digests_hash": opf_section_digests_hash,
        "opf_corpus_snapshot_hash": opf_corpus_snapshot_hash,
        "posture_version": posture_version,
    }


def _build_execution_input_json(submission: dict[str, Any], playbook_id: str) -> str:
    """Pointer-only execution input (issue #19): S3 keys and hashes only,
    never document text.

    Used on the retry path, where the submission record already exists but
    may predate execution_input being persisted (backward compatibility);
    otherwise the stored submission["execution_input"] (see
    create_submission_record) is the source of truth and this function's
    output must match it byte-for-byte for the same inputs.

    OPF lineage (issue #287) is re-resolved here from `playbook_id` via the
    registry's `bundle_path`, rather than read off `submission` -- it is
    NOT part of the submission record's own persisted fields (only
    release_bundle_hash is), so it must be recomputed the same way the
    original build did, byte-for-byte, for this docstring's guarantee to
    hold.
    """
    lineage = _resolve_opf_lineage(playbook_id)
    return _build_execution_input_json_from_parts(
        review_id=submission["review_id"],
        owner_sub=submission["owner_sub"],
        playbook_id=playbook_id,
        upload_s3_key=submission["upload_pointer"],
        release_bundle_hash=submission["release_bundle_hash"],
        opf_content_hash=lineage["opf_content_hash"],
        opf_section_digests_hash=lineage["opf_section_digests_hash"],
        opf_corpus_snapshot_hash=lineage["opf_corpus_snapshot_hash"],
        posture_version=lineage["posture_version"],
    )


def _build_execution_input_json_from_parts(
    review_id: str,
    owner_sub: str,
    playbook_id: str,
    upload_s3_key: str,
    release_bundle_hash: str,
    opf_content_hash: str | None = None,
    opf_section_digests_hash: str | None = None,
    opf_corpus_snapshot_hash: str | None = None,
    posture_version: int | None = None,
) -> str:
    """Pointer-only execution input (issue #19): S3 keys and hashes only,
    never document text.

    Persisted verbatim on the submission record (create_submission_record)
    so a crash-recovered re-drive -- e.g. the orphan reconciler's ARN-less
    re-drive path -- can start the execution with the same well-formed
    payload the original request would have used, rather than an empty
    "{}" that would KeyError on the first pipeline stage.

    The three `opf_*` params (issue #287) are OPF §8 lineage for a v2
    bundle (see `_resolve_opf_lineage`); `posture_version` (issue #294) is
    the bundle's governed Posture-version override, if any. Each is None
    for a v1 playbook (no `bundle_path` registered) or for a v2 bundle
    carrying no posture override, and OMITTED from the JSON entirely in
    that case -- byte-identical output to before this issue, never a null
    placeholder key.
    """
    import json

    payload = {
        "review_id": review_id,
        "owner_sub": owner_sub,
        "playbook_id": playbook_id,
        "upload_s3_key": upload_s3_key,
        "release_bundle_hash": release_bundle_hash,
    }
    if opf_content_hash is not None:
        payload["opf_content_hash"] = opf_content_hash
    if opf_section_digests_hash is not None:
        payload["opf_section_digests_hash"] = opf_section_digests_hash
    if opf_corpus_snapshot_hash is not None:
        payload["opf_corpus_snapshot_hash"] = opf_corpus_snapshot_hash
    if posture_version is not None:
        payload["posture_version"] = posture_version

    return json.dumps(payload)


DEFAULT_RETENTION_WINDOW_DAYS = 90

# Issue #34: mirrors backend/src/retention.py::RETENTION_WINDOW_FOREVER.
# Duplicated (not imported) per this package's existing convention of each
# module owning its own copy of small shared sentinels/constants (see
# TERMINAL_REVIEW_STATUSES / GLOBAL_SETTING_ID duplicated between
# backend/src/retention.py and infra/lambda/purge_worker/handler.py).
RETENTION_WINDOW_FOREVER = "forever"


def _current_retention_window_days(dynamodb_resource: Any) -> int | str:
    """Read today's global retention window for the snapshot-at-creation
    invariant (issue #61 / docs/data-handling.md purge invariant 2): "the
    window applied to a document is the window in effect when the review
    was created". Falls back to the documented default if the settings row
    is absent (e.g. a fresh environment before any admin has saved a
    setting) rather than failing the whole submission over a missing
    config row.

    Issue #34: the setting may also be the `forever` sentinel (indefinite
    preservation) rather than a numeric day count; that value is snapshotted
    onto the review as-is, never coerced through `int()`.
    """
    settings_table_name = os.environ.get("RETENTION_SETTINGS_TABLE")
    if not settings_table_name:
        return DEFAULT_RETENTION_WINDOW_DAYS
    table = dynamodb_resource.Table(settings_table_name)
    resp = table.get_item(Key={"setting_id": "global"})
    item = resp.get("Item")
    if not item:
        return DEFAULT_RETENTION_WINDOW_DAYS
    value = item.get("retention_window_days", DEFAULT_RETENTION_WINDOW_DAYS)
    if value == RETENTION_WINDOW_FOREVER:
        return RETENTION_WINDOW_FOREVER
    return int(value)


def _create_review_row(
    review_id: str,
    owner_sub: str,
    playbook_id: str,
    release_bundle_hash: str,
    dynamodb_resource: Any,
    opf_content_hash: str | None = None,
    opf_section_digests_hash: str | None = None,
    opf_corpus_snapshot_hash: str | None = None,
    posture_version: int | None = None,
) -> None:
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    now = str(int(time.time()))
    item: dict[str, Any] = {
        "review_id": review_id,
        "owner_sub": owner_sub,
        "playbook_id": playbook_id,
        "playbook_hash": release_bundle_hash,
        "status": "PENDING",
        "created_at": now,
        "updated_at": now,
        # Snapshot-at-creation (issue #61 purge invariant 2): the
        # retention purge worker governs this document by THIS value,
        # never by a later change to the global setting.
        "retention_window_at_creation": _current_retention_window_days(dynamodb_resource),
        # Legal hold defaults to unset; placed/released via the
        # (future) admin hold action -- never set here.
        "legal_hold": False,
    }
    # Issue #287 (OPF bind 5/5): v2-bundle OPF §8 lineage. Absent from the
    # row entirely for a v1 playbook (all three None) -- byte-identical to
    # the row this function wrote before this issue.
    if opf_content_hash is not None:
        item["opf_content_hash"] = opf_content_hash
    if opf_section_digests_hash is not None:
        item["opf_section_digests_hash"] = opf_section_digests_hash
    if opf_corpus_snapshot_hash is not None:
        item["opf_corpus_snapshot_hash"] = opf_corpus_snapshot_hash
    # Issue #294: the bound bundle's governed Posture-version override, if
    # any. Absent from the row entirely when the bundle carries none
    # (genesis) -- same "absent, not null" convention as the three fields
    # above.
    if posture_version is not None:
        item["posture_version"] = posture_version

    table.put_item(Item=item)


def record_stage_failure(
    review_id: str,
    stage_name: str,
    reason: str,
    dynamodb_resource: Any,
    now_epoch: float | None = None,
) -> str:
    """Target-agnostic stage-failure recorder (issue #258).

    Both the AWS Step Functions error-handler Lambda (a Catch target invoked
    for every stage) and the DTS in-process runner's per-stage `except`
    blocks are meant to call this SAME function, so `failing_stage` records
    the real, per-stage name that actually failed -- never a hardcoded
    constant like `'pipeline'` -- regardless of which deployment target is
    running.

    `reason` is looked up in `STAGE_FAILURE_REASON_STATUS` to resolve the
    terminal `status` written to the reviews row: the two documented
    manual-review outcomes (`ERROR_MANUAL_REVIEW_REQUIRED`,
    `MANUAL_REVIEW_REQUIRED`) are reachable this way; any other `reason`
    falls back to the generic `ERROR` status, same as today's unmapped
    failure behavior -- only `failing_stage` becomes accurate.

    Returns the terminal status that was written.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    terminal_status = STAGE_FAILURE_REASON_STATUS.get(reason, "ERROR")
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    table.update_item(
        Key={"review_id": review_id},
        UpdateExpression=(
            "SET #status = :status, failing_stage = :stage, "
            "reason = :reason, updated_at = :now"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": terminal_status,
            ":stage": stage_name,
            ":reason": reason,
            ":now": str(int(now_epoch)),
        },
    )
    return terminal_status


def get_review_status(review_id: str, dynamodb_resource: Any) -> dict[str, Any]:
    """GET /api/reviews/{id} — reflects PENDING -> RUNNING -> DONE/ERROR."""
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    resp = table.get_item(Key={"review_id": review_id})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found.")
    return item


# ---------------------------------------------------------------------------
# GET /api/reviews (list) and GET /api/reviews/{id} (owner/admin-scoped
# detail with the full result payload) — issue #84.
# ---------------------------------------------------------------------------

# Manual-review-state user-facing copy (docs/output-contract.md -> "Manual-
# review states: user-facing next-step copy"). One fixed sentence per
# status, keyed off the pipeline's terminal `status` -- never the specific
# internal `reason` code (e.g. the #18 form-match short-circuit, the #65
# hash-mismatch-at-patch fail-closed path, etc. all surface through the SAME
# MANUAL_REVIEW_REQUIRED copy; `reason` is carried separately as system
# metadata, not rendered as its own message). Both messages are system-
# status copy only -- never a legal verdict -- per that doc section.
STATUS_USER_MESSAGES: dict[str, str] = {
    "MANUAL_REVIEW_REQUIRED": (
        "Your document could not be automatically reviewed — a legal "
        "admin will review it and follow up with you. No action is needed "
        "on your part right now."
    ),
    "ERROR_MANUAL_REVIEW_REQUIRED": (
        "A pipeline error prevented automatic review of your document — "
        "a legal admin will review it and follow up with you. No action is "
        "needed on your part right now."
    ),
}


def _is_admin_caller(caller_user_row: dict[str, Any]) -> bool:
    """`is_admin` is a DynamoDB `users`-row flag, never a JWT claim -- same
    convention as src/users.py::_is_admin / src/download.py::_is_admin."""
    return bool(caller_user_row.get("is_admin", False))


def get_review_detail(
    review_id: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """GET /api/reviews/{id} (issue #84): status + the full result payload
    -- provenance (carried per-issue on `issues[].provenance`), critic
    deltas (`critic_delta`), and confidence band (`confidence_band`) -- for
    #35/#36 to render. Owner-or-admin scoped per ARCHITECTURE.md's Routes
    table.

    A non-owner, non-admin caller gets the SAME HTTP 404 as a review_id that
    does not exist at all -- never a 403 -- so the response cannot be used
    to enumerate other users' review ids (download.py's docstring
    "High-entropy non-enumerable review IDs" invariant, applied here at the
    detail route too; the separate /output route keeps its own existing 403
    behavior, unchanged, since that path already discloses nothing beyond
    "you may not download this").

    Fields not yet populated by the pipeline (e.g. a still-PENDING/RUNNING
    review, or one whose persist stage hasn't landed the real pipeline
    output onto this row yet) are simply absent/null -- this function is a
    faithful, read-only projection of whatever the `reviews` row currently
    holds, never a computation of pipeline state.
    """
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    resp = table.get_item(Key={"review_id": review_id})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found.")

    owner_sub = item.get("owner_sub", "")
    caller_sub = caller_user_row.get("cognito_sub", "")
    if caller_sub != owner_sub and not _is_admin_caller(caller_user_row):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found.")

    status_value = item.get("status", "PENDING")
    reason = item.get("reason") or item.get("quarantine_reason") or item.get("analysis_report_reason")

    return {
        "review_id": review_id,
        "status": status_value,
        "decision": item.get("decision"),
        "confidence_state": item.get("confidence_state"),
        "confidence_band": item.get("confidence_band"),
        "issues": item.get("issues"),
        "critic_delta": item.get("critic_delta"),
        "verdict_summary": item.get("verdict_summary"),
        "reason": reason,
        # Target-agnostic stage-failure taxonomy (issue #258): the specific
        # pipeline stage a failure occurred in, when
        # `record_stage_failure` has written one.
        "failing_stage": item.get("failing_stage"),
        "message": STATUS_USER_MESSAGES.get(status_value),
        "has_output": bool(item.get("output_s3_key")),
        "playbook_id": item.get("playbook_id"),
        "owner_sub": owner_sub,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        # OPF §8 lineage (issue #287): admin/API visibility for a v2-bundle
        # review's opf_content_hash / opf_section_digests_hash /
        # opf_corpus_snapshot_hash. Absent-on-the-row -> None here too, same
        # "faithful projection" convention as every other field above.
        "opf_content_hash": item.get("opf_content_hash"),
        "opf_section_digests_hash": item.get("opf_section_digests_hash"),
        "opf_corpus_snapshot_hash": item.get("opf_corpus_snapshot_hash"),
        # Issue #294: the review's governed Posture-version override, if
        # any. Absent-on-the-row -> None here too, same "faithful
        # projection" convention as the fields above.
        "posture_version": item.get("posture_version"),
    }


_REVIEW_LIST_ITEM_FIELDS = (
    "review_id",
    "owner_sub",
    "playbook_id",
    "status",
    "decision",
    "confidence_band",
    "created_at",
    "updated_at",
)


def _review_list_item(item: dict[str, Any]) -> dict[str, Any]:
    """Lean summary shape for the list view -- confidential per-review
    content (verdict_summary, issues, critic_delta) is reserved for the
    single-review detail route, not the list."""
    return {field: item.get(field) for field in _REVIEW_LIST_ITEM_FIELDS}


def _scan_all_reviews(table: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    resp = table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return items


def _list_reviews_for_owner(table: Any, owner_sub: str) -> list[dict[str, Any]]:
    """Prefer the `owner_sub-index` GSI (see infra/lib/nested/data-stack.ts)
    via a real boto3/moto Table.query(); fall back to scan+filter for a
    lightweight test stand-in that doesn't implement `.query()` (same
    fallback convention as src/disposition.py::_scan_by_owner)."""
    if hasattr(table, "query"):
        from boto3.dynamodb.conditions import Key

        items: list[dict[str, Any]] = []
        query_kwargs: dict[str, Any] = {
            "IndexName": "owner_sub-index",
            "KeyConditionExpression": Key("owner_sub").eq(owner_sub),
        }
        resp = table.query(**query_kwargs)
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = table.query(**query_kwargs, ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        return items

    return [i for i in _scan_all_reviews(table) if i.get("owner_sub") == owner_sub]


def list_reviews(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> list[dict[str, Any]]:
    """GET /api/reviews (issue #84): the caller's own reviews, newest first;
    an admin sees every review (ARCHITECTURE.md Routes table: "List my
    reviews (admin: all reviews)")."""
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])

    if _is_admin_caller(caller_user_row):
        items = _scan_all_reviews(table)
    else:
        owner_sub = caller_user_row.get("cognito_sub", "")
        items = _list_reviews_for_owner(table, owner_sub)

    items.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    return [_review_list_item(i) for i in items]
