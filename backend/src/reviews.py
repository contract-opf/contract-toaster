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
  PLAYBOOK_VERSIONS_TABLE    DynamoDB playbook_versions table name (PK:
                             playbook_id, SK: version) -- optional; when
                             unset, `_resolve_playbook_version_lineage`
                             (issue #471) resolves to None/None rather than
                             raising
  PLAYBOOK_INSTRUCTIONS_TABLE DynamoDB playbook_instructions table name (PK:
                             playbook_id, SK: version [Number]) -- optional;
                             when unset, `_resolve_instructions_lineage`
                             (issue #482, epic #481) resolves to None/None
                             rather than raising, same discipline as
                             PLAYBOOK_VERSIONS_TABLE above
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

import base64
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from fastapi import HTTPException, status

try:  # production runs `src.main`; tests put backend/src on sys.path
    from src import config, model_client, model_settings
    from src.users import json_safe
except ImportError:  # pragma: no cover
    import config  # type: ignore[no-redef]
    import model_client  # type: ignore[no-redef]
    import model_settings  # type: ignore[no-redef]
    from users import json_safe  # type: ignore[no-redef]

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

logger = logging.getLogger(__name__)


def _is_conditional_check_failed(exc: Exception) -> bool:
    """True for a DynamoDB ConditionalCheckFailedException, however it reached
    us: a real botocore `ClientError`, or a duck-typed stand-in carrying the
    same `response` shape (the convention `pipeline_runner._is_conditional`
    already uses, so test doubles keep working)."""
    resp = getattr(exc, "response", None)
    if not isinstance(resp, dict):
        return False
    return resp.get("Error", {}).get("Code") == "ConditionalCheckFailedException"

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
    # The reviewer asked for this run to stop. Deliberately its OWN terminal
    # status rather than an ERROR with a reason token: nothing failed, and a
    # review the user stopped on purpose must not read -- in the result panel,
    # in History, or in Diagnostics -- like a tool malfunction they should
    # report. Cancellation is cooperative (see pipeline_runner's cancel
    # checkpoints), so a review can also finish normally while the request is
    # in flight; whichever terminal write lands first wins.
    "CANCELLED",
}

# The one terminal status that means "this review SUCCEEDED and its result is
# persisted" -- as opposed to the failure/administrative terminals above. It is
# named rather than spelled inline because it is now load-bearing: issue #446's
# guard in `record_stage_failure` refuses to overwrite a row holding it.
REVIEW_STATUS_SUCCESS_TERMINAL = "DONE"

# ---------------------------------------------------------------------------
# Stage-failure taxonomy (issue #258) -- target-agnostic core shared by both
# deployment targets. Today `failing_stage` is hardcoded to `'pipeline'` in
# the AWS Step Functions error-transition Lambda
# (infra/lib/nested/pipeline-stack.ts) and the Docker Compose in-process runner has its
# own separate hardcoded stage-failure write (pipeline_runner.py). Wiring
# either caller onto `record_stage_failure` below is deliberately OUT OF
# SCOPE here (folds into #244 for AWS's errorTransition, and into the
# Docker Compose wiring ticket for pipeline_runner.py) -- this only establishes the single
# shared mechanism + taxonomy both wirings will call.
#
# `reason` -> reachable terminal status for the two DOCUMENTED manual-review
# outcomes. A `reason` not listed here still records the real failing stage,
# but resolves to the generic `ERROR` status (the same terminal status the
# AWS errorTransition Lambda and the Docker Compose runner's `_fail_review` already use
# for an unmapped/unexpected failure) -- this taxonomy only carves out the
# two statuses that must be specifically reachable, it does not replace the
# generic failure path.
#
# Issue #442 extends this with the model-provider tokens
# `backend/src/pipeline_runner.py::classify_failure_reason` produces. Each
# terminal status below is chosen DELIBERATELY, and every entry is listed
# explicitly -- including the ones that resolve to the generic `ERROR` --
# so "which status does this token land on?" is answered by reading this
# table rather than by inferring it from an absence.
STAGE_FAILURE_REASON_STATUS: dict[str, str] = {
    # Structured-output retry exhausted (a model stage that never produced
    # parseable structured output after its retry budget).
    "structured_output_retry_exhausted": "ERROR_MANUAL_REVIEW_REQUIRED",
    # Document exceeds the size cap enforced ahead of the model stages.
    "document_too_large": "MANUAL_REVIEW_REQUIRED",
    # --- Model-provider conditions (issue #442) ----------------------------
    # OPERATOR problems, not document problems: the document was fine and
    # would review cleanly the moment the account/key/model is fixed. They
    # stay on the generic `ERROR` status precisely so they are NOT filed
    # alongside the documented manual-review outcomes above -- there is no
    # attorney work to queue here, only an admin fix. The `reason` token is
    # what makes them distinguishable, not the status.
    "model_account_out_of_credits": "ERROR",
    "model_key_rejected": "ERROR",
    "model_rate_limited": "ERROR",
    "model_unavailable": "ERROR",
    # Issue #472: a missing/empty key, classified BEFORE any provider call
    # (see model_client.ModelKeyMissingError) -- same "admin fix, not
    # attorney work" reasoning, so the same generic `ERROR` status.
    "model_key_missing": "ERROR",
    # Issue #472: a request that timed out after exhausting retries (see
    # model_client.ModelTimeoutError). Worth resubmitting -- an operator
    # fix at most, never a document problem -- so it stays `ERROR` too.
    "model_timeout": "ERROR",
    # Issue #527: the model response itself was unusable, but the account,
    # key and model are all fine -- worth resubmitting (a raised
    # `reasoning_max_tokens` allowance or a bigger budget fixes it), never a
    # document problem, so these stay `ERROR` too, same reasoning as the
    # provider-condition tokens above.
    "model_empty_content": "ERROR",
    "model_output_truncated": "ERROR",
    # A DOCUMENT problem: the provider itself rejected the assembled prompt
    # as over the model's context window. This is the same condition the
    # step-14 pre-call estimate catches, and it is deliberately given the
    # SAME `MANUAL_REVIEW_REQUIRED` terminal status as `document_too_large`
    # (the reasoning is `model_client.ModelContextLengthExceededError`'s own
    # docstring: a provider-side length rejection is a real occurrence, not a
    # misconfiguration, and must not degrade to a generic pipeline ERROR).
    # It keeps its own token rather than reusing `document_too_large` so the
    # two remain telling-apart-able in the row: one was estimated ahead of
    # the call, the other was measured by the provider.
    "model_context_length_exceeded": "MANUAL_REVIEW_REQUIRED",
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

def _openrouter_rates_for_role(
    policy: dict[str, Any], role: str, effective_model_id: str
) -> tuple[float, float]:
    """The `(input, output)` per-million rates for the model a role will
    ACTUALLY run on, out of model-policy/openrouter.json.

    Looked up in the `selectable` allowlist first (issue #445 -- an admin
    selection resolves to one of those ids), falling back to the role's
    pinned `models.<role>` block. The fallback covers both "nothing is
    selected" (the effective id IS the pin) and "an OPENROUTER_*_MODEL_ID
    break-glass override is in force" -- an off-policy id the artifact
    carries no rates for, so the pin's rates remain the only honest
    estimate available. That second case is the pre-existing behavior and is
    unchanged here.
    """
    for entry in model_client.openrouter_selectable_models(policy):
        if entry.get("model_id") == effective_model_id:
            return (
                entry["cost_per_million_input_usd"],
                entry["cost_per_million_output_usd"],
            )
    block = policy["models"][role]
    return (
        block["cost_per_million_input_usd"],
        block["cost_per_million_output_usd"],
    )


def _active_provider_rates(
    dynamodb_resource: Any = None,
) -> tuple[float, float, float, float]:
    """Per-million-token worst-case rates for whichever model provider is
    actually being billed, keyed by `config.model_provider()`
    (`MODEL_PROVIDER` env var) -- issue #268.

    `MODEL_PROVIDER=openrouter` (the Docker Compose deployment target) reads
    `model-policy/openrouter.json`'s `cost_per_million_{input,output}_usd`
    rates, which are already flat/all-in per token (no regional premium --
    see that file's own `_comment`). Any other value (including unset, the
    AWS/Bedrock target's default) returns the existing hardcoded Bedrock
    regional-rate constants, UNCHANGED -- this branch must never perturb
    the Bedrock path's documented $2.11 worst case (issue #189).

    ADMIN SELECTION (issue #445). On the OpenRouter path the rates are those
    of the models that will actually be invoked -- the admin's stored
    selection resolved through `model_settings.resolve_openrouter_model_ids`,
    which applies the documented admin > env > pin precedence and drops a
    selection that has fallen off the allowlist. Pricing the DEFAULT PINS
    while the review runs on something else makes the daily cap wrong in
    whichever direction the admin chose: reserving the pins' 192c for a pair
    that can cost 256c lets a $20/day cap permit ~$26.67/day of real
    exposure, and reserving 192c for a 17c Budget pair denies the admin the
    headroom the picker just advertised. `dynamodb_resource` is optional so
    a caller with no handle (or a deployment with no settings store) keeps
    exactly the pre-#445 pin behavior, and a DynamoDB blip degrades to the
    pins rather than failing the reservation -- the resolver swallows its own
    read errors.

    Returns `(primary_input, primary_output, critic_input, critic_output)`
    in USD per million tokens.
    """
    if config.model_provider() == "openrouter":
        policy = model_client.load_openrouter_policy()
        effective = model_settings.resolve_openrouter_model_ids(dynamodb_resource)
        primary_input, primary_output = _openrouter_rates_for_role(
            policy, "primary", effective["primary"]
        )
        critic_input, critic_output = _openrouter_rates_for_role(
            policy, "critic", effective["critic"]
        )
        return (primary_input, primary_output, critic_input, critic_output)
    return (
        PRIMARY_INPUT_RATE_USD_PER_MILLION,
        PRIMARY_OUTPUT_RATE_USD_PER_MILLION,
        CRITIC_INPUT_RATE_USD_PER_MILLION,
        CRITIC_OUTPUT_RATE_USD_PER_MILLION,
    )


def compute_worst_case_reservation_usd_cents(dynamodb_resource: Any = None) -> int:
    """Worst-case spend reservation for a single review.

    Retry-inclusive, per-model formula (issue #189 fix; retry-inclusive
    shape per reconciliation note #14):

        reservation = (1 + max_retries_per_pass) * sum over {primary, critic} of
            (max_input_tokens * that_model's_input_rate_per_token
             + max_output_tokens * that_model's_output_rate_per_token)

    Each pass (primary/Opus, critic/Sonnet) is priced at ITS OWN model's
    rate rather than a single blended "most expensive tier" rate applied to
    both passes (see the constants above for why that overshot 4.6x).

    The rate table itself is provider-aware AND selection-aware
    (`_active_provider_rates`, issues #268 and #445):
    `MODEL_PROVIDER=openrouter` prices from `model-policy/openrouter.json`
    instead of the Bedrock constants, at the rates of the models the admin
    has actually selected. Pass `dynamodb_resource` wherever one is in scope
    — without it the reservation falls back to the policy pins, which is the
    right answer only when no admin selection can be in force.

    Folding the retry budget into the reservation at reserve-time means any
    sequence of attempts within that budget cannot overshoot the reservation
    — only the settled actual spend (ledgered after every model attempt,
    including failures) can come in under it.

    Issue #569: when the bounded re-quote repair pass is on
    (`config.requote_enabled()`), the reservation also covers its ONE extra
    model call — priced at the PRIMARY pass's own rate (the model id
    `scripts/review_spine.py` hands `requote_repair.run_requote_repair`)
    and bounded by the SAME per-pass token ceilings, but deliberately NOT
    multiplied by `attempts_per_pass`: that repair call carries no retry
    budget of its own ("one pass ever", `scripts/requote_repair.py`'s own
    docstring). This is a generous over-estimate — the repair prompt is
    just the failed issues' rationale/reasons plus a target paragraph each,
    far smaller than a full-document review — which is the correct
    direction for a WORST-CASE reservation. Added only when the flag is on,
    so a deployment that never turns it on reserves exactly what it always
    has; settlement needs no matching change, since the repair call shares
    the SAME model-client instance the primary/critic passes use and its
    real usage is already folded into that client's own `cumulative_usage`
    total (`backend/src/pipeline_runner.py::_actual_cents_from_client`).
    """
    attempts_per_pass = 1 + MAX_RETRIES_PER_PASS
    primary_input_rate, primary_output_rate, critic_input_rate, critic_output_rate = (
        _active_provider_rates(dynamodb_resource)
    )
    primary_usd = MAX_INPUT_TOKENS * (
        primary_input_rate / 1_000_000
    ) + MAX_OUTPUT_TOKENS * (primary_output_rate / 1_000_000)
    critic_usd = MAX_INPUT_TOKENS * (
        critic_input_rate / 1_000_000
    ) + MAX_OUTPUT_TOKENS * (critic_output_rate / 1_000_000)
    usd = attempts_per_pass * (primary_usd + critic_usd)
    if config.requote_enabled():
        usd += primary_usd
    return int(round(usd * 100))


def compute_actual_usd_cents_from_usage(
    primary_usage: dict[str, int] | None,
    critic_usage: dict[str, int] | None,
    dynamodb_resource: Any = None,
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

    Uses `_active_provider_rates()` -- the SAME provider-aware,
    selection-aware rate table `compute_worst_case_reservation_usd_cents`
    uses for this review's reservation, so a review's reservation and its
    eventual settlement are always priced against the same provider and the
    same chosen models.
    """
    primary_input_rate, primary_output_rate, critic_input_rate, critic_output_rate = (
        _active_provider_rates(dynamodb_resource)
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
    # Priced against the models this review will actually run on (#445), not
    # the policy pins -- the same resource the reservation is written on
    # carries the admin selection.
    reservation_amount_cents = compute_worst_case_reservation_usd_cents(dynamodb_resource)
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
            # DynamoDB ConditionExpressions do not permit arithmetic (only
            # UpdateExpressions do), so the cap check compares the stored
            # reservation against a budget precomputed in Python -- the largest
            # prior total that still leaves room for this amount. Equivalent to
            # `reserved + amount <= cap` for the common case where the stored
            # cap equals the request cap (the SET clause seeds it from :cap);
            # a mid-day change to DAILY_SPEND_CAP_USD_CENTS now takes effect on
            # the next reservation rather than the next UTC day.
            ConditionExpression=(
                "attribute_not_exists(reserved_usd_cents) OR "
                "reserved_usd_cents <= :budget"
            ),
            ExpressionAttributeValues={
                ":zero": 0,
                ":amount": reservation_amount_cents,
                ":cap": daily_cap_cents,
                ":budget": daily_cap_cents - reservation_amount_cents,
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

    The amount to reverse is RECOMPUTED here rather than read back from the
    reservation, exactly as it always has been, and so is priced against the
    admin model selection in force right now (#445). KNOWN RESIDUAL: an admin
    who changes the selection while a review is in flight makes the reversal
    disagree with what that review reserved, leaving the day's
    `reserved_usd_cents` counter off by the difference until the UTC-midnight
    row rolls over. Same shape as the pre-existing mid-day
    DAILY_SPEND_CAP_USD_CENTS window documented in `reserve_spend`. Closing
    it properly means persisting the reserved amount alongside the
    reservation id and reversing the stored figure — issue #459, deliberately
    kept out of #445 rather than smuggled into it.
    """
    table = dynamodb_resource.Table(os.environ["DAILY_SPEND_TABLE"])
    now_epoch = time.time() if now_epoch is None else now_epoch
    spend_date = time.strftime("%Y-%m-%d", time.gmtime(now_epoch))
    reservation_amount_cents = compute_worst_case_reservation_usd_cents(dynamodb_resource)
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
# Preflight spend (issue #491) -- a direct ledger write, not the
# reserve/settle two-phase dance the rest of this module uses.
#
# Preflight runs the moment a file is chosen, before any review_id or
# submission record exists -- there is no reservation to make and, per the
# issue's own wording, nothing is persisted except this ledger row (and,
# optionally, a stamp onto the review row IF the user goes on to submit).
# `reserve_spend`/`settle_spend`'s reservation dance exists to bound a
# review's WORST-CASE cost against the daily cap before an expensive
# multi-pass pipeline runs; preflight is a single cheap-model call with a
# short timeout and no retry budget (see `backend/src/review_routes.py`'s
# preflight route) -- reserving worst-case for that would buy nothing a
# straight settle-after-the-fact does not already give for a fraction of
# the code. Preflight also never enforces the daily cap: it is advisory and
# "never blocks a submission" per the issue's Context, and a check running
# on every file selection (including ones a reviewer abandons) is exactly
# the kind of traffic a hard cap should not be exposed to.
# ---------------------------------------------------------------------------


def compute_preflight_actual_usd_cents(usage: dict[str, int] | None) -> int:
    """The actual settled cost, in USD cents, of one preflight cheap-model
    call, priced against model-policy/openrouter.json's `models.preflight`
    rates -- the SAME real-usage-based pricing `compute_actual_usd_cents_
    from_usage` uses for the primary/critic passes, but against the
    preflight role's own (Budget-tier) rates rather than the primary/critic
    ones, since the preflight role is never admin-selectable (see
    `model_client.openrouter_preflight_model_id`'s docstring).

    `usage` is `None` for a skipped or failed cheap-model call (no request
    was ever billed) -- returns `0` rather than raising, so a caller can
    call this unconditionally regardless of how the preflight attempt went.
    """
    if not usage:
        return 0
    policy = model_client.load_openrouter_policy()
    entry = (policy.get("models") or {}).get("preflight") or {}
    input_rate = float(entry.get("cost_per_million_input_usd", 0.0))
    output_rate = float(entry.get("cost_per_million_output_usd", 0.0))
    total_usd = usage.get("input_tokens", 0) * (input_rate / 1_000_000)
    total_usd += usage.get("output_tokens", 0) * (output_rate / 1_000_000)
    return int(round(total_usd * 100))


def record_preflight_spend(
    usd_cents: int,
    dynamodb_resource: Any,
    now_epoch: float | None = None,
) -> None:
    """Ledger one preflight check's actual settled cost directly onto the
    day's `settled_usd_cents` total -- no reservation, no cap check (see the
    module comment above for why). `usd_cents <= 0` (a skipped, failed, or
    genuinely free-to-round cheap-model call) is a deliberate no-op: a
    preflight that degraded to stats-only never debits a ledger it never
    actually spent from, and there is nothing to reconcile later the way a
    review's worst-case reservation needs `settle_spend` to reverse.
    """
    if usd_cents <= 0:
        return
    table = dynamodb_resource.Table(os.environ["DAILY_SPEND_TABLE"])
    now_epoch = time.time() if now_epoch is None else now_epoch
    spend_date = time.strftime("%Y-%m-%d", time.gmtime(now_epoch))
    table.update_item(
        Key={"spend_date": spend_date},
        UpdateExpression=(
            "SET settled_usd_cents = if_not_exists(settled_usd_cents, :zero) + :amount"
        ),
        ExpressionAttributeValues={":zero": 0, ":amount": usd_cents},
    )


# ---------------------------------------------------------------------------
# Cover-note spend + cache (issue #499, "Butter it") -- same direct-settle,
# no-reservation, no-cap-check shape as the preflight spend above, and for
# the same reason: this is a single cheap-to-mid prose call on an ALREADY
# FINISHED review, not a worst-case multi-pass pipeline run, so a
# reservation would buy nothing a straight settle-after-the-fact does not
# already give for a fraction of the code.
# ---------------------------------------------------------------------------


def compute_cover_note_actual_usd_cents(usage: dict[str, int] | None) -> int:
    """The actual settled cost, in USD cents, of one cover-note generation,
    priced against model-policy/openrouter.json's `models.cover_note`
    rates -- the SAME real-usage-based pricing
    `compute_preflight_actual_usd_cents` uses for the preflight role, but
    against the cover_note role's own rates, since that role is also never
    admin-selectable (see `model_client.openrouter_cover_note_model_id`'s
    docstring).

    `usage` is `None` for a skipped or failed call (no request was ever
    billed) -- returns `0` rather than raising, so a caller can call this
    unconditionally regardless of how the generation attempt went."""
    if not usage:
        return 0
    policy = model_client.load_openrouter_policy()
    entry = (policy.get("models") or {}).get("cover_note") or {}
    input_rate = float(entry.get("cost_per_million_input_usd", 0.0))
    output_rate = float(entry.get("cost_per_million_output_usd", 0.0))
    total_usd = usage.get("input_tokens", 0) * (input_rate / 1_000_000)
    total_usd += usage.get("output_tokens", 0) * (output_rate / 1_000_000)
    return int(round(total_usd * 100))


def record_cover_note_spend(
    usd_cents: int,
    dynamodb_resource: Any,
    now_epoch: float | None = None,
) -> None:
    """Ledger one cover-note generation's actual settled cost directly onto
    the day's `settled_usd_cents` total -- no reservation, no cap check (see
    the module comment above). `usd_cents <= 0` (a failed generation that
    was never cached or billed) is a deliberate no-op, same convention
    `record_preflight_spend` documents for its own field."""
    if usd_cents <= 0:
        return
    table = dynamodb_resource.Table(os.environ["DAILY_SPEND_TABLE"])
    now_epoch = time.time() if now_epoch is None else now_epoch
    spend_date = time.strftime("%Y-%m-%d", time.gmtime(now_epoch))
    table.update_item(
        Key={"spend_date": spend_date},
        UpdateExpression=(
            "SET settled_usd_cents = if_not_exists(settled_usd_cents, :zero) + :amount"
        ),
        ExpressionAttributeValues={":zero": 0, ":amount": usd_cents},
    )


def cover_note_daily_cap_reached(
    dynamodb_resource: Any,
    now_epoch: float | None = None,
) -> bool:
    """True once today's total COMMITTED spend -- `reserved_usd_cents`
    (in-flight review reservations) PLUS `settled_usd_cents` (preflight and
    cover-note actuals already ledgered, including any prior cover-note
    generations this same day) -- has reached the configured daily cap.

    Post-landing review of issue #499 ("Butter it"): a cover note is NOT
    like preflight. Preflight is advisory, "never blocks a submission", and
    fires on every file selection including ones a reviewer abandons -- the
    module comment above `record_preflight_spend` explains why THAT role
    deliberately never cap-checks. A cover note has none of those excuses:
    it is a user-initiated, explicitly priced (the UI shows a priced
    "Regenerate (~$0.03)" button), REPEATABLE action, and nothing on the
    route rate-limits it. Because `record_cover_note_spend` only ever
    settles -- it never reserves, matching the shape it was modeled on --
    this spend never enters `reserved_usd_cents` at all, so `reserve_spend`'s
    own conditional cap check (which reads ONLY that attribute) never sees
    it; an authenticated owner could otherwise loop `{"regenerate": true}`
    indefinitely with zero cap interaction.

    This is a plain read-then-compare, not a conditional write: unlike
    `reserve_spend`'s single atomic UpdateExpression, two concurrent
    cover-note requests racing this check can both read "under the cap" and
    both proceed, so it does not close that race the way review submission's
    reservation does. That is an accepted, narrower gap than the one being
    closed here -- an unbounded, unlimited-concurrency, no-cap-at-all spend
    path becomes a spend path bounded by the same daily ceiling every other
    role already respects. Closing the race too would mean giving cover-note
    spend a real reservation/settlement lifecycle, which is a bigger change
    than a post-landing review finding warrants.

    Deliberately mirrors `reserve_spend`'s own budget arithmetic: the cap
    compared against is the value FRESHLY read from
    `DAILY_SPEND_CAP_USD_CENTS` (env) each call, not the `daily_cap_usd_cents`
    value stored on the row -- that stored value is metadata `reserve_spend`
    seeds for visibility/its own documented mid-day-change caveat, never
    what its own ConditionExpression budget is computed against.
    """
    table = dynamodb_resource.Table(os.environ["DAILY_SPEND_TABLE"])
    now_epoch = time.time() if now_epoch is None else now_epoch
    spend_date = time.strftime("%Y-%m-%d", time.gmtime(now_epoch))
    resp = table.get_item(Key={"spend_date": spend_date})
    row = resp.get("Item") or {}
    daily_cap_cents = int(
        os.environ.get("DAILY_SPEND_CAP_USD_CENTS", str(DAILY_SPEND_CAP_USD_CENTS_DEFAULT))
    )
    committed_cents = row.get("reserved_usd_cents", 0) + row.get("settled_usd_cents", 0)
    return committed_cents >= daily_cap_cents


def record_cover_note_draft(
    review_id: str,
    draft_text: str,
    cost_usd_cents: int,
    served_model_id: str,
    dynamodb_resource: Any,
    *,
    generated_at: str | None = None,
) -> None:
    """Cache a freshly generated cover-note draft onto the `reviews` row
    (issue #499 AC: "cached draft renders free on revisit"; "regenerate ...
    a new ledger row; cached draft renders free on revisit"). Only ever
    called on a SUCCESSFUL generation -- `backend/src/review_routes.py`'s
    route never calls this on a failed/degraded attempt, so a failed
    regenerate leaves the previously cached draft (if any) untouched rather
    than clobbering it with nothing, the same "don't destroy a good value
    with a failed write" discipline issue #486's disposition-note fix
    established for this row.

    Overwrites any previously cached draft -- there is exactly one cached
    draft per review, the most recent generation, matching the Design
    section's "the card stays a faithful record of what was generated"
    (of the LATEST generation, not a history of every one; every
    generation is still ledgered separately via the spend functions above
    and `backend/src/invocation_ledger.py`)."""
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    generated_at = generated_at if generated_at is not None else str(int(time.time()))
    table.update_item(
        Key={"review_id": review_id},
        UpdateExpression=(
            "SET cover_note_draft = :draft, cover_note_generated_at = :at, "
            "cover_note_cost_usd_cents = :cost, cover_note_served_model_id = :model"
        ),
        ExpressionAttributeValues={
            ":draft": draft_text,
            ":at": generated_at,
            ":cost": cost_usd_cents,
            ":model": served_model_id or "",
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
    validate the artifact that hash is supposed to identify (issue #266:
    runtime validation of the active bundle -- previously
    `playbooks/schema.json` was CI-only, so every reader of this attribute
    trusted the artifact blindly). Returns None -- the SAME "no active
    bundle" signal a genuinely-empty row produces -- if:

      - the playbook row does not exist, or exists but carries no active
        bundle (the documented no-active-bundle state -- e.g. after a
        deactivate action, or before this playbook's first bundle has
        ever been activated), OR
      - the active bundle is a v1 registry artifact (no active,
        OPF-artifact-kind `playbook_versions` row -- see the OPF branch
        below) and the current ON-DISK playbook body for `playbook_id`
        fails runtime validation (schema-invalid, or a covering topic is
        missing its `our_standard` standard-form text -- see
        `scripts/playbook_validation.py::load_and_validate_playbook`), OR
      - the active bundle IS an OPF artifact (issue #478's upload flow)
        but its activation record is internally inconsistent (see below).

    An invalid playbook must never resolve as active: fail closed to the
    exact same refusal a missing bundle produces, never a partial/invalid
    load.

    ## OPF branch (issue #485 blocker 3)

    `load_and_validate_playbook` reads and schema-validates
    `playbooks/<playbook_id>.json` off disk via the v1 registry
    (`scripts/playbook_registry.py`). For a playbook activated through
    issue #478's upload flow -- an OPF artifact stored content-addressed in
    S3, `storage_key` on its `playbook_versions` row -- that on-disk v1 body
    is not the thing being served, and validating it is not merely
    redundant, it is WRONG: for a playbook_id that has no registry entry at
    all (every playbook created via `POST /api/admin/playbooks`, issue
    #485's blocker 1, since a DB-created playbook_id is never added to
    `playbooks/registry.json`, which is baked into the image), it doesn't
    validate the wrong document either: `load_and_validate_playbook` itself
    catches `playbook_registry.PlaybookNotRegisteredError` and re-raises
    `PlaybookValidationError`, which THIS function already caught (below)
    and turned into a bare `None` -- "no active bundle" -- indistinguishable
    from a genuinely inactive playbook, so the submission route refused
    with HTTP 503 "no active playbook" for a playbook an admin had, in
    fact, just activated. So: when the active `playbook_versions` row for this
    playbook_id carries an OPF `artifact_kind` (`opf-0.2` / `opf-0.3`), the
    v1 on-disk read is skipped entirely -- it is never reached, and never
    the thing validated.

    Skipping that read is not a blind pass, though (fail-closed still
    applies): this function instead checks that the activation record
    itself is internally consistent -- the row's OWN `content_hash` must
    equal the `active_hash` `playbooks.active_release_bundle_hash` names
    (otherwise activation and the resolver have drifted, and serving either
    hash blind would be wrong), and it must name a `storage_key` a run can
    actually load from. Either check failing returns None, same as any
    other "cannot resolve an active bundle" outcome here.

    What this function deliberately does NOT do is re-fetch and re-validate
    the OPF artifact's bytes from S3 on every read (schema, `identity.
    content_hash`, the injection scan): that full re-validation already
    happens where the content is actually consumed --
    `pipeline_runner._load_opf_bundle_if_active`, read ITS docstring for the
    contract -- called again at execution time via `verify_submission_time_
    bundle` (which reads through this same function). Duplicating an
    S3-fetching check on every resolver read (this function is called once
    per submission AND once per execution start) would not add safety -- a
    byte corrupted between here and execution start would still need to be
    (and is) caught there -- only cost, and would require threading an
    `s3_client` through every caller of this function and `resolve_active_
    release_bundle_hash` (a submission-time HTTP route, easy) as well as
    `verify_submission_time_bundle` (already has one) and every one of this
    function's existing test callers that construct it with only
    `(playbook_id, dynamodb_resource)`.

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

    if os.environ.get("PLAYBOOK_VERSIONS_TABLE"):
        # `ClientError` (e.g. `ResourceNotFoundException`) is caught, not
        # propagated: this function's contract is "never raises" (see
        # above), and plenty of existing callers/tests set the
        # PLAYBOOK_VERSIONS_TABLE env var (a common boilerplate default
        # across this repo's test files) without ever provisioning that
        # table, because their scenario never otherwise touches it. A read
        # failure here just means "the version-row layer isn't usable for
        # this check" -- falls through to the v1 disk path below exactly as
        # if the env var were unset, never a 500 in place of the documented
        # None/hash outcomes.
        # Imported here, not at module scope: `playbook_versions` reaches
        # `boto3.dynamodb.conditions`, and this module is imported by tests
        # that stub `boto3` as a bare module (not a package). Same reason the
        # `Key` imports in this file are function-local.
        try:  # production runs `src.main`; tests put backend/src on sys.path
            from src import playbook_versions
        except ImportError:  # pragma: no cover
            import playbook_versions  # type: ignore[no-redef]

        try:
            active_version = playbook_versions.get_active_version_record(
                playbook_id, dynamodb_resource
            )
        except ClientError:
            active_version = None
        if active_version is not None:
            artifact_kind = active_version.get("artifact_kind") or ""
            if artifact_kind.startswith("opf-"):
                if (
                    active_version.get("content_hash") != active_hash
                    or not active_version.get("storage_key")
                ):
                    return None
                return active_hash

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

# ---------------------------------------------------------------------------
# Notes mode (issue #520, epic #519 item A)
#
# Which audience a review's footnotes are written for. Captured at submission
# because the PROMPT changes by mode -- issue #516 (landed) gates the
# toaster-guidance block's deviation-narration instruction on this value:
# `primary_review_pass._render_toaster_guidance_intro` appends it only when
# the mode puts internal content in scope (`internal`/`both`), so in
# `none`/`external` the model is never told to narrate a playbook deviation
# into `verdict_summary` / `external_rationale_for_footnote` at all. That is
# a PROMPT gate, deliberately not a post-hoc strip: stripping would mean
# internal reasoning was generated into a counterparty-bound field and merely
# filtered on the way out, which is the posture the leakage scan exists to
# prevent.
#
# So this is a pipeline INPUT, and it has to be known before the model call.
# ---------------------------------------------------------------------------

NOTES_MODES = ("none", "external", "internal", "both")

# Today's behaviour. An older client, a hand-built request, or a blank field
# all land here, and `external` reproduces the current output exactly -- which
# is what makes item A landable ahead of the items that make the modes differ.
DEFAULT_NOTES_MODE = "external"


def resolve_notes_mode(value: str | None) -> str:
    """The effective notes mode for a submission.

    Absent, empty and whitespace-only resolve to `external`. Anything else
    that is not one of the four raises `ValueError`, which the route turns
    into a 400.

    The refusal is the point. Silently downgrading a typo'd `internal` to
    `external` would hand someone counterparty-facing output when they asked
    for internal notes -- a quiet wrong answer, which is worse than a loud
    one, and undetectable from the review row afterwards.

    Issue #572: while `config.notes_mode_enabled()` is off (the default),
    `internal` and `both` are refused with that same `ValueError` -> 400
    path -- never silently downgraded to `external` -- because the
    audience-aware leakage scan (#521) that makes internal reasoning safe to
    generate does not exist on `main` yet. `none` and `external` stay
    accepted regardless of the flag: neither can surface internal reasoning,
    so neither carries the risk the gate exists for.
    """
    if value is None:
        return DEFAULT_NOTES_MODE
    normalized = value.strip().lower()
    if not normalized:
        return DEFAULT_NOTES_MODE
    if normalized not in NOTES_MODES:
        raise ValueError(
            f"notes_mode must be one of {', '.join(NOTES_MODES)}; got {value!r}"
        )
    if normalized in ("internal", "both") and not config.notes_mode_enabled():
        raise ValueError(
            f"notes_mode {normalized!r} is not available: NOTES_MODE_ENABLED "
            "is unset (or off) in this deployment"
        )
    return normalized


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
    toaster_guidance: str = "",
    original_filename: str = "",
    notes_mode: str = DEFAULT_NOTES_MODE,
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

    toaster_guidance (issue #398, default ""): the optional per-review
    free-text instructions carried into the execution input (see
    _build_execution_input_json_from_parts) for the pipeline to thread to
    scripts/review_spine.py::run_review's primary + critic passes. NOT part
    of idempotency-key derivation or the dedup lookup — same treatment as
    playbook_id, which already isn't either (resolve_idempotency_key /
    find_existing_submission key only on owner_sub + file_sha256 +
    active_release_bundle_hash). Ignored on the resumed/duplicate path
    exactly like review_id above: the existing submission's own recorded
    execution_input is authoritative there, not whatever this retried call
    happened to pass.

    Issue #431 additionally records it on the reviews row itself (see
    _create_review_row) so get_review_detail can hand it back to the UI --
    the resumed path leaves the original row's value untouched, which is
    correct: the already-started execution runs under the guidance the
    FIRST call supplied, so the row keeps naming what actually governed.
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
        #
        # Issue #482 (fix round 2): prefer the PERSISTED execution_input
        # over rebuilding it here. The submission record's execution_input
        # is what the reviews row's instructions_version was stamped
        # alongside (see resolve_and_submit_review's first-call branch
        # below) -- rebuilding via _build_execution_input_json instead
        # would re-resolve "current" standing instructions at retry time,
        # which can have moved on (e.g. v2 -> v3) since the row was
        # stamped, splitting the row's stamped version from what the
        # pipeline actually runs on. Only fall back to the rebuild for
        # pre-#482 submission records that predate execution_input being
        # persisted at all (see create_submission_record / _build_execution_input_json's
        # own docstring for that backward-compatibility case).
        execution_input_json = existing.get("execution_input") or _build_execution_input_json(
            existing, playbook_id, toaster_guidance=toaster_guidance
        )
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
    # Issue #471: the `playbook_versions` row (admin-facing version +
    # content hash) behind the hash just resolved above -- populated for
    # the live non-OPF playbook too, unlike opf_lineage.
    playbook_version_lineage = _resolve_playbook_version_lineage(
        playbook_id, active_release_bundle_hash, dynamodb_resource
    )
    # Issue #482: the standing-instructions version (+ text hash, + the text
    # itself) governing THIS review, resolved once, alongside the
    # playbook-version lineage above and BEFORE the review row is written --
    # see `_resolve_instructions_lineage`'s docstring for why that ordering
    # is what makes a mid-flight instructions save unable to split-brain
    # what this row records. Merged into the same dict `_create_review_row`
    # already threads through `_recorded_playbook_version_fields` -- no new
    # parameter needed there. `_build_execution_input_json_from_parts` below
    # reads the same dict (via `instructions_lineage`) so the pipeline
    # receives the exact version/hash/text this resolution settled on too --
    # never a second, independent read from inside the pipeline.
    playbook_version_lineage.update(
        _resolve_instructions_lineage(playbook_id, dynamodb_resource)
    )
    execution_input_json = _build_execution_input_json_from_parts(
        review_id=review_id,
        owner_sub=owner_sub,
        playbook_id=playbook_id,
        upload_s3_key=upload_pointer,
        release_bundle_hash=active_release_bundle_hash,
        opf_lineage=opf_lineage,
        toaster_guidance=toaster_guidance,
        notes_mode=notes_mode,
        instructions_lineage=playbook_version_lineage,
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
        opf_lineage=opf_lineage,
        playbook_version_lineage=playbook_version_lineage,
        toaster_guidance=toaster_guidance,
        upload_s3_key=upload_pointer,
        original_filename=original_filename,
        notes_mode=notes_mode,
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
    "policy_version": None,
    "policy_hash": None,
    "policy_approval_status": None,
}

# The governing-input fields recorded on BOTH the review row and the
# execution input: present when resolved, OMITTED (never null) when not.
# Threaded as one dict rather than as fixed kwargs for the reason
# `_resolve_opf_lineage` is itself generic (see the block comment above): a
# future governing input -- e.g. the operator's free-text admin
# instructions, which may supersede the playbook and must therefore pin an
# instruction VERSION on the row exactly the way `policy_hash` pins the
# policy, since "every active version is reconstructable from version
# control" is only a real promise if the row says which version governed --
# joins by being added HERE and to `_EMPTY_OPF_LINEAGE`, with no call-site
# rewrite in `submit_review` and no new builder params.
_RECORDED_LINEAGE_FIELDS: tuple[str, ...] = (
    "opf_content_hash",
    "opf_section_digests_hash",
    "opf_corpus_snapshot_hash",
    "posture_version",
    "policy_version",
    "policy_hash",
    "policy_approval_status",
)


def _resolve_opf_lineage(playbook_id: str) -> dict[str, str | int | None]:
    """Resolve OPF §8 lineage for `playbook_id`'s active v2 bundle, if any.

    Returns a dict with keys "opf_content_hash", "opf_section_digests_hash",
    "opf_corpus_snapshot_hash" (each `str | None`), "posture_version"
    (`int | None`, issue #294), and -- for OPF 0.3 -- "policy_version"
    (`int | None`) / "policy_hash" (`str | None`) / "policy_approval_status"
    (`str | None`) naming the review POLICY document the bundle was bound
    against. All are None when the playbook has no registry entry, no
    `bundle_path`, or the bundle_path does not resolve to a readable file --
    the same "nothing to record" signal as a v1 playbook, never an error
    (this resolver never changes submission behavior; it is purely
    additive).

    Why the policy belongs in lineage: a review's outcome is a function of BOTH
    the corpus-derived playbook (descriptive precedent) and the human-authored
    policy (prescriptive rules). Recording only the playbook hash would leave
    half the governing input unrecorded -- two reviews with identical
    opf_content_hash could legitimately differ because the policy was edited
    between them, and nothing would say so. `policy_hash` covers the policy's
    approval stamp too, so a re-approval is visible as lineage movement.

    Why `policy_approval_status` is recorded ALONGSIDE the hash rather than
    left implicit in it: today's committed policy ships `approval_status:
    "draft"` and still awaits legal review. A hash proves WHICH rules
    governed a contract decision; only the status says whether a human had
    signed off on them. The system may review against unapproved rules, but
    the record must say that it did -- reading that back off the row must not
    require re-deriving it from a bundle that may since have been rebound.
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

    # OPF 0.3: absent `review_policy` -> None/None (the playbook was bound with
    # no prescriptive rules), never a fabricated version 0 -- same discipline as
    # posture_version above.
    review_policy = bundle.get("review_policy") or {}
    policy_version = review_policy.get("version")
    policy_hash = review_policy.get("hash")
    policy_approval_status = review_policy.get("approval_status")

    return {
        "opf_content_hash": opf_content_hash,
        "opf_section_digests_hash": opf_section_digests_hash,
        "opf_corpus_snapshot_hash": opf_corpus_snapshot_hash,
        "posture_version": posture_version,
        "policy_version": policy_version,
        "policy_hash": policy_hash,
        "policy_approval_status": policy_approval_status,
    }


def _build_execution_input_json(
    submission: dict[str, Any], playbook_id: str, toaster_guidance: str = ""
) -> str:
    """Pointer-only execution input (issue #19): S3 keys and hashes only,
    never document text.

    Used on the retry path ONLY as a fallback for submission records that
    predate execution_input being persisted (backward compatibility) --
    the caller (resolve_and_submit_review's retry branch) checks
    submission["execution_input"] FIRST and only calls this function when
    that field is absent. The stored submission["execution_input"] (see
    create_submission_record) is the source of truth whenever present,
    because it is what the reviews row's lineage stamps (including
    instructions_version, issue #482) were recorded alongside; this
    function re-resolves lineage as of NOW, which can disagree with what
    was stamped if e.g. standing instructions were edited between the
    original call and this retry. When it does run, this function's
    output must match the original submission["execution_input"]
    byte-for-byte for the same inputs -- but note it does NOT thread
    `instructions_lineage` through to
    _build_execution_input_json_from_parts, so pre-#482 submission
    records (which never had instructions_lineage to begin with) are the
    only case where that omission is byte-for-byte correct.

    OPF lineage (issue #287) is re-resolved here from `playbook_id` via the
    registry's `bundle_path`, rather than read off `submission` -- it is
    NOT part of the submission record's own persisted fields (only
    release_bundle_hash is), so it must be recomputed the same way the
    original build did, byte-for-byte, for this docstring's guarantee to
    hold.

    `toaster_guidance` (issue #398) is handled the SAME way as
    `playbook_id` above, for the same reason: it is not part of the
    submission record's own persisted fields either, so a caller retrying
    the same logical request re-supplies it fresh rather than this
    function reading a stored copy off `submission`.
    """
    return _build_execution_input_json_from_parts(
        review_id=submission["review_id"],
        owner_sub=submission["owner_sub"],
        playbook_id=playbook_id,
        upload_s3_key=submission["upload_pointer"],
        release_bundle_hash=submission["release_bundle_hash"],
        opf_lineage=_resolve_opf_lineage(playbook_id),
        toaster_guidance=toaster_guidance,
    )


def _build_execution_input_json_from_parts(
    review_id: str,
    owner_sub: str,
    playbook_id: str,
    upload_s3_key: str,
    release_bundle_hash: str,
    opf_lineage: dict[str, str | int | None] | None = None,
    toaster_guidance: str = "",
    notes_mode: str = DEFAULT_NOTES_MODE,
    instructions_lineage: dict[str, str | int | None] | None = None,
) -> str:
    """Pointer-only execution input (issue #19): S3 keys and hashes only,
    never document text.

    Persisted verbatim on the submission record (create_submission_record)
    so a crash-recovered re-drive -- e.g. the orphan reconciler's ARN-less
    re-drive path -- can start the execution with the same well-formed
    payload the original request would have used, rather than an empty
    "{}" that would KeyError on the first pipeline stage.

    `opf_lineage` (issue #287, extended by #294 and OPF 0.3) is a
    `_resolve_opf_lineage` dict: the governing inputs this review ran
    against -- the bound OPF's §8 identity, the Posture-version override,
    and the review policy's version/hash/approval status. Every field of it
    that resolves to None -- all of them, for a v1 playbook with no
    `bundle_path` registered -- is OMITTED from the JSON entirely, never a
    null placeholder key: byte-identical output to before these issues.

    `toaster_guidance` (issue #398, default `""`): the optional per-review
    free-text instructions from POST /api/reviews, read back out of this
    same payload by backend/src/pipeline_runner.py::run_real_pipeline and
    threaded to scripts/review_spine.py::run_review. Unlike `opf_lineage`'s
    fields, this is always included (never omitted at "") -- it is a plain
    review-input field like `playbook_id` above, not optional governance
    metadata.

    `instructions_lineage` (issue #482): a `_resolve_instructions_lineage`
    dict -- `instructions_version`, `instructions_content_hash`, and the
    exact `instructions_text` that hash was computed over. Threaded through
    so the pipeline runs on the SAME text the review row's
    `instructions_version` stamp names, never a second, independent read of
    "current standing instructions" from inside the pipeline -- that
    second read is exactly the mid-flight-save split brain issue #482
    forbids (see `_resolve_instructions_lineage`'s module docstring). Same
    "absent, not null" filter as `opf_lineage`: nothing saved for the
    playbook (or `PLAYBOOK_INSTRUCTIONS_TABLE` unset) omits all three
    fields entirely.
    """
    import json

    payload: dict[str, Any] = {
        "review_id": review_id,
        "owner_sub": owner_sub,
        "playbook_id": playbook_id,
        "upload_s3_key": upload_s3_key,
        "release_bundle_hash": release_bundle_hash,
        "toaster_guidance": toaster_guidance,
    }
    # Issue #520: recorded only when it is NOT the default, following the same
    # "absent, never a placeholder" convention as every other optional field in
    # this payload -- so a submission that says nothing about notes mode
    # produces a byte-identical payload to before this landed. Safe in the
    # direction that matters: `external` and absent both mean
    # counterparty-facing only, and `internal` is never the default, so an
    # internal request can never be lost to omission.
    if notes_mode and notes_mode != DEFAULT_NOTES_MODE:
        payload["notes_mode"] = notes_mode
    payload.update(_recorded_lineage_fields(opf_lineage))
    payload.update(_recorded_instructions_execution_fields(instructions_lineage))

    return json.dumps(payload)


# The instructions-specific fields threaded into `execution_input_json`
# (issue #482) -- a DELIBERATELY separate list from
# `_RECORDED_PLAYBOOK_VERSION_FIELDS` above (which also covers
# `playbook_version` / `playbook_content_hash`, neither of which is part of
# the execution input today): this filter only ever reads the three
# `instructions_*` keys off whatever lineage dict it is given, so passing
# the same merged `playbook_version_lineage` dict used for the review row
# cannot leak the playbook-version fields into the execution input too.
_RECORDED_INSTRUCTIONS_EXECUTION_FIELDS: tuple[str, ...] = (
    "instructions_version",
    "instructions_content_hash",
    "instructions_text",
)


def _recorded_instructions_execution_fields(
    instructions_lineage: dict[str, str | int | None] | None,
) -> dict[str, Any]:
    """The `_RECORDED_INSTRUCTIONS_EXECUTION_FIELDS` that actually
    resolved, in declaration order -- same "absent, not null" filter as
    `_recorded_lineage_fields` / `_recorded_playbook_version_fields`."""
    lineage = instructions_lineage or {}
    return {
        field: lineage[field]
        for field in _RECORDED_INSTRUCTIONS_EXECUTION_FIELDS
        if lineage.get(field) is not None
    }


def _recorded_lineage_fields(
    opf_lineage: dict[str, str | int | None] | None,
) -> dict[str, Any]:
    """The `_RECORDED_LINEAGE_FIELDS` that actually resolved, in declaration
    order -- the shared "absent, not null" filter behind both the review row
    and the execution input, so the two can never disagree about which
    governing inputs a review recorded."""
    lineage = opf_lineage or {}
    return {
        field: lineage[field]
        for field in _RECORDED_LINEAGE_FIELDS
        if lineage.get(field) is not None
    }


# ---------------------------------------------------------------------------
# Playbook-version lineage (issue #471): WHICH `playbook_versions` row (the
# admin-facing version string, e.g. "1.0.0", plus its content hash) gated a
# submission -- distinct from `_resolve_opf_lineage` above, which resolves
# the OPF Section 8 identity for a v2-BUNDLE playbook only (None/None for
# the live, non-OPF `synthetic-nda-sample` playbook every review actually
# runs against today, per the 2026-08-02 audit that opened this issue).
#
# `active_release_bundle_hash` is ALREADY the resolved, submission-time hash
# (ARCHITECTURE.md step 3, reconciliation note #21) -- this resolver never
# re-resolves which bundle is active; it only looks up the admin-facing
# version identifier BEHIND the hash the caller already settled on, by
# finding the `playbook_versions` row (PK playbook_id, SK version) whose
# `content_hash` matches it.
# ---------------------------------------------------------------------------

_EMPTY_PLAYBOOK_VERSION_LINEAGE: dict[str, str | int | None] = {
    "playbook_version": None,
    "playbook_content_hash": None,
    # Standing instructions epic (#481)'s own version stamp -- issue #482
    # populates these two via `_resolve_instructions_lineage` below. Lives
    # here (not in `_EMPTY_OPF_LINEAGE` above) because it is not an OPF
    # Section 8 field either -- it is the operator's free-text
    # instructions, which may supersede the playbook. Never a fabricated
    # value when nothing has ever been saved for the playbook, or when
    # PLAYBOOK_INSTRUCTIONS_TABLE is not configured for this deployment
    # target -- same "absent, not null" discipline as every field here.
    "instructions_version": None,
    "instructions_content_hash": None,
}

_RECORDED_PLAYBOOK_VERSION_FIELDS: tuple[str, ...] = (
    "playbook_version",
    "playbook_content_hash",
    "instructions_version",
    "instructions_content_hash",
)


def _resolve_playbook_version_lineage(
    playbook_id: str,
    active_release_bundle_hash: str,
    dynamodb_resource: Any,
) -> dict[str, str | None]:
    """Resolve the `playbook_versions` row whose `content_hash` matches the
    ALREADY-RESOLVED `active_release_bundle_hash` -- the admin-facing
    version string (e.g. "1.0.0") behind the hash a review actually ran
    against.

    Both fields resolve to None -- never a fabricated version, same
    discipline as `_resolve_opf_lineage` -- when:

      - `PLAYBOOK_VERSIONS_TABLE` is not configured for this deployment
        target, or
      - no `playbook_versions` row for `playbook_id` carries a matching
        `content_hash` -- e.g. a demo/dev environment seeded only via
        `scripts/seed_active_bundle.py`, which writes
        `playbooks.active_release_bundle_hash` directly with no
        `playbook_versions` row at all.

    Never raises: like `_resolve_opf_lineage`, this is purely additive and
    must never change submission behavior.
    """
    table_name = os.environ.get("PLAYBOOK_VERSIONS_TABLE")
    if not table_name:
        return dict(_EMPTY_PLAYBOOK_VERSION_LINEAGE)

    from boto3.dynamodb.conditions import Key

    try:
        table = dynamodb_resource.Table(table_name)
        resp = table.query(KeyConditionExpression=Key("playbook_id").eq(playbook_id))
    except ClientError:
        return dict(_EMPTY_PLAYBOOK_VERSION_LINEAGE)

    for item in resp.get("Items", []):
        if item.get("content_hash") == active_release_bundle_hash:
            result = dict(_EMPTY_PLAYBOOK_VERSION_LINEAGE)
            result["playbook_version"] = item.get("version")
            result["playbook_content_hash"] = active_release_bundle_hash
            return result

    return dict(_EMPTY_PLAYBOOK_VERSION_LINEAGE)


# ---------------------------------------------------------------------------
# Standing-instructions lineage (issue #482, epic #481): WHICH standing-
# instructions version (+ the text's sha256) governed a submission -- the
# per-playbook free-text overrides `src/playbook_instructions.py` stores,
# distinct from both `_resolve_opf_lineage` (OPF Section 8 identity) and
# `_resolve_playbook_version_lineage` (the playbook_versions row) above.
#
# Resolved ONCE here, synchronously, before `_create_review_row` writes the
# review row (same call site as `_resolve_playbook_version_lineage`) -- so a
# standing-instructions save that lands AFTER this resolution (issue #482's
# AC: "even if v3 is saved mid-review") can never change what THIS row
# claims governed it. The version stamped is the version this function
# actually read; the same resolved dict (version, text hash, AND the text
# itself) is also threaded into `execution_input_json` by
# `_build_execution_input_json_from_parts`'s `instructions_lineage` param,
# so the pipeline runs on that same resolution too -- there is no separate
# "read the text" step anywhere downstream for submission purposes, so
# there is nothing left to split-brain.
# ---------------------------------------------------------------------------


def _resolve_instructions_lineage(
    playbook_id: str,
    dynamodb_resource: Any,
) -> dict[str, str | int | None]:
    """Resolve the CURRENT standing-instructions version (+ text hash +
    text) for `playbook_id`, if any.

    All three fields resolve to None -- never a fabricated version, same
    discipline as `_resolve_playbook_version_lineage` -- when:

      - `PLAYBOOK_INSTRUCTIONS_TABLE` is not configured for this deployment
        target, or
      - nothing has ever been saved for `playbook_id` (no row at all).

    `instructions_content_hash` is read off the saved row's own `text_hash`
    attribute (written once, at save time, by
    `playbook_instructions.save_instructions`) rather than recomputed here
    -- every reader of "what hash governed this review" reads the exact
    same value the save wrote, never a second independent hash computation
    that could theoretically disagree with it. `instructions_text` is read
    off that same row's `text` attribute, so the text later threaded into
    `execution_input_json` is the literal text the hash was computed over.

    Never raises: like the other lineage resolvers in this module, this is
    purely additive and must never change submission behavior.
    """
    empty: dict[str, str | int | None] = {
        "instructions_version": None,
        "instructions_content_hash": None,
        "instructions_text": None,
    }

    table_name = os.environ.get("PLAYBOOK_INSTRUCTIONS_TABLE")
    if not table_name:
        return dict(empty)

    from boto3.dynamodb.conditions import Key

    try:
        table = dynamodb_resource.Table(table_name)
        resp = table.query(
            KeyConditionExpression=Key("playbook_id").eq(playbook_id),
            ScanIndexForward=False,
            Limit=1,
        )
    except ClientError:
        return dict(empty)

    items = resp.get("Items", [])
    if not items:
        return dict(empty)

    current = items[0]
    return {
        "instructions_version": int(current["version"]),
        "instructions_content_hash": current.get("text_hash"),
        # Issue #482: the exact text this version's hash was computed over
        # (see playbook_instructions.save_instructions), threaded alongside
        # the version/hash into `execution_input_json` below so the version
        # STAMPED on the row is also the text the pipeline actually runs
        # with -- never a second, independent read of "current" from inside
        # the pipeline, which is what would let a mid-flight save split
        # brain what governed a review.
        "instructions_text": current.get("text"),
    }


def _recorded_playbook_version_fields(
    playbook_version_lineage: dict[str, str | None] | None,
) -> dict[str, Any]:
    """The `_RECORDED_PLAYBOOK_VERSION_FIELDS` that actually resolved, in
    declaration order -- the same "absent, not null" filter
    `_recorded_lineage_fields` applies to the OPF lineage fields."""
    lineage = playbook_version_lineage or {}
    return {
        field: lineage[field]
        for field in _RECORDED_PLAYBOOK_VERSION_FIELDS
        if lineage.get(field) is not None
    }


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
    opf_lineage: dict[str, str | int | None] | None = None,
    playbook_version_lineage: dict[str, str | None] | None = None,
    toaster_guidance: str = "",
    upload_s3_key: str = "",
    original_filename: str = "",
    notes_mode: str = DEFAULT_NOTES_MODE,
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
    # The governing inputs this review ran against: issue #287's v2-bundle
    # OPF §8 lineage, issue #294's Posture-version override, and OPF 0.3's
    # review-policy version/hash/approval status. Each is absent from the
    # row entirely when it did not resolve (a v1 playbook resolves none of
    # them) -- byte-identical to the row this function wrote before those
    # issues, never a null placeholder. This is the audit trail: without it,
    # editing a policy and re-binding leaves two reviews indistinguishable
    # in the record even though different rules governed them.
    # Issue #520: the audience this review's footnotes were written for.
    # Recorded only when it is NOT the default, on the same "absent, never a
    # null placeholder" terms as every field around it -- a submission that
    # says nothing produces a row byte-identical to before this landed.
    #
    # The asymmetry is deliberate and safe: `external` and absent both mean
    # counterparty-facing only, so conflating them costs nothing, while
    # `internal` and `both` are never defaults and are therefore always
    # recorded. The mode that could do harm if lost cannot be lost.
    if notes_mode and notes_mode != DEFAULT_NOTES_MODE:
        item["notes_mode"] = notes_mode

    item.update(_recorded_lineage_fields(opf_lineage))

    # Issue #518: the name the uploader's document arrived under, so the
    # redline can be downloaded under a name that identifies it rather than
    # `out.docx`. Recorded on the same "absent, never a null placeholder"
    # terms as everything above.
    #
    # CLASSIFICATION: this is Confidential, not Internal. A contract filename
    # routinely names the counterparty -- "Mutual NDA - Acme.docx" is the
    # ordinary case, not a corner one -- so it is listed with the substance
    # fields retention clears on purge (backend/src/retention.py,
    # infra/lambda/purge_worker/handler.py). A purge that deleted the document
    # and left the counterparty's name on the row would not be a purge.
    if original_filename and original_filename.strip():
        item["original_filename"] = original_filename.strip()

    # Issue #471: the `playbook_versions` admin-facing version + content
    # hash behind the OPF-independent playbook every review actually runs
    # against today. Same "absent, not null" filter as the OPF lineage
    # fields directly above -- a row for a playbook with no matching
    # `playbook_versions` row (e.g. the bare demo seed) stays byte-identical
    # to the row this function wrote before this issue.
    item.update(_recorded_playbook_version_fields(playbook_version_lineage))

    # Issue #431: the per-review free-text guidance this review actually ran
    # under, recorded on the row so "which instructions governed this
    # review?" is answerable from the review itself (get_review_detail
    # projects it back for the Review tab's read-only readback) rather than
    # only from the submission record's execution_input, which the read path
    # never touches. Recorded on the SAME "absent, never a null placeholder"
    # terms as the lineage fields above -- and whitespace-only guidance is
    # no guidance at all, exactly as scripts/primary_review_pass.py
    # ::render_toaster_guidance_block treats it, so a row for a review that
    # supplied none stays byte-identical to the row written before this
    # issue. The value is stored verbatim (never the stripped copy): the
    # record must be what was submitted.
    if toaster_guidance.strip():
        item["toaster_guidance"] = toaster_guidance

    # Issue #449: the pointer to the INPUT document this review ran against.
    # It already existed on the `review_submissions` row (`upload_pointer`) and
    # inside the execution input, but neither is on the read path -- so "let me
    # re-read what I actually sent you" was unanswerable from the review
    # itself, and there was nothing for a download route to derive a key from
    # without trusting client input. Recorded on the same "absent, never a null
    # placeholder" terms as the fields above, so a row written before this
    # issue stays byte-identical (and renders as "not recorded" rather than
    # offering a download that cannot work).
    if upload_s3_key:
        item["upload_s3_key"] = upload_s3_key

    table.put_item(Item=item)


def record_stage_failure(
    review_id: str,
    stage_name: str,
    reason: str,
    dynamodb_resource: Any,
    now_epoch: float | None = None,
    model_ids: dict[str, str] | None = None,
) -> str:
    """Target-agnostic stage-failure recorder (issue #258).

    Both the AWS Step Functions error-handler Lambda (a Catch target invoked
    for every stage) and the Docker Compose in-process runner's per-stage `except`
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

    NEVER DOWNGRADES A SUCCEEDED REVIEW (issue #446). The write is guarded by
    a ConditionExpression so it cannot overwrite a row that already holds
    `REVIEW_STATUS_SUCCESS_TERMINAL`. It used to be unconditional, and that
    took down every successful review on a live deployment: the persist stage
    wrote the redline object and `status=DONE`, the NEXT statement (settling
    the spend reservation) raised, and this function then clobbered the good
    row with `ERROR` -- destroying a result that was already complete and
    downloadable. The trap was never specific to the settle: ANY exception
    raised after a successful terminal write reached this write. A review that
    finished must stay finished, so `ConditionalCheckFailedException` is
    swallowed as a deliberate no-op.

    Only the SUCCESS terminal is protected -- `ERROR` /
    `MANUAL_REVIEW_REQUIRED` / `QUARANTINED` rows stay writable, so
    reclassifying an already-failed review is unaffected.

    Also stamps `failed_at` (issue #472) -- the moment THIS failure was
    recorded, distinct from `created_at` (when the review was submitted).
    Two Diagnostics rows previously rendered a blank "Failed at" cell because
    no such field existed at all and the column was quietly reading
    `created_at` instead; this is the write side of the fix. Set to the same
    epoch second as `updated_at`, and only ever written alongside a genuine
    failure write -- the guard above means a row that is refused (already
    `DONE`) gets no `failed_at` either, which is correct: it didn't fail.

    Returns the terminal status the row actually holds afterwards: the status
    written, or the untouched `REVIEW_STATUS_SUCCESS_TERMINAL` when the guard
    refused the write (callers log/branch on this and must not be told `ERROR`
    was recorded when it was not).

    `model_ids` (issue #527) is `pipeline_runner._model_ids_for_run`'s dict
    -- `{"primary_model_id": ..., "critic_model_id": ...}` -- whatever
    subset was already known at the point of failure. Before this, an
    unhandled exception from a model call (e.g. `ModelEmptyContentError`,
    `ModelOutputTruncatedError`) landed on the ERROR row with a `reason`
    code but NEITHER model id, unlike `_write_real_terminal`'s success path,
    which has always stamped both -- so the one failure mode most worth
    knowing "which model did this?" for carried the least provenance.
    Omitted keys are simply not written, same "absent, never a null
    placeholder" convention `_write_real_terminal` uses; defaults to `None`
    so every pre-#527 caller (including the AWS error-handler Lambda, which
    has no model concept) keeps writing a row byte-identical to before.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    now_str = str(int(now_epoch))
    terminal_status = STAGE_FAILURE_REASON_STATUS.get(reason, "ERROR")
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    set_clauses = [
        "#status = :status",
        "failing_stage = :stage",
        "reason = :reason",
        "failed_at = :failed_at",
        "updated_at = :now",
    ]
    values: dict[str, Any] = {
        ":status": terminal_status,
        ":stage": stage_name,
        ":reason": reason,
        ":failed_at": now_str,
        ":now": now_str,
        ":succeeded": REVIEW_STATUS_SUCCESS_TERMINAL,
    }
    for index, (field, model_id) in enumerate(sorted((model_ids or {}).items())):
        placeholder = f":m{index}"
        set_clauses.append(f"{field} = {placeholder}")
        values[placeholder] = model_id
    try:
        table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET " + ", ".join(set_clauses),
            ConditionExpression="attribute_not_exists(#status) OR #status <> :succeeded",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values,
        )
    except Exception as exc:  # noqa: BLE001 - only the guard is swallowed
        if not _is_conditional_check_failed(exc):
            raise
        logger.warning(
            "Refusing to mark review %s as %s at stage %s (reason %s): it is already %s "
            "-- the result is persisted and stays intact (issue #446).",
            review_id,
            terminal_status,
            stage_name,
            reason,
            REVIEW_STATUS_SUCCESS_TERMINAL,
        )
        return REVIEW_STATUS_SUCCESS_TERMINAL
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
    # Says what happened and what is true now, with no apology and no
    # troubleshooting: the reviewer chose this, so treating it as an incident
    # to explain away would be both wrong and patronising. It does say there
    # is no redline, because that is the one consequence they might not expect.
    "CANCELLED": (
        "You stopped this review, so no redline was produced. You can submit "
        "the document again whenever you're ready."
    ),
}


def _is_admin_caller(caller_user_row: dict[str, Any]) -> bool:
    """`is_admin` is a DynamoDB `users`-row flag, never a JWT claim -- same
    convention as src/users.py::_is_admin / src/download.py::_is_admin."""
    return bool(caller_user_row.get("is_admin", False))


def load_analysis_artifact(item: dict[str, Any], s3_client: Any) -> dict[str, Any] | None:
    """Read this review's persisted analysis artifact
    (`outputs/{review_id}/analysis.json`, written by
    `pipeline_runner._write_real_analysis`), if one exists.

    This is the ONLY place `findings` / `critic_delta` are ever produced
    (issue #416's `_ANALYSIS_FIELDS`). No writer has ever put either on the
    `reviews` row itself -- not `pipeline_runner._write_terminal`, not
    `._write_real_terminal`, not `infra/lambda/persist/handler.py` -- so
    `get_review_detail` reading `item.get("issues")` /
    `item.get("critic_delta")` returned None on every real review, and the
    cover-note route's 409 gate (`item.get("issues") or []`) never saw a
    real review's findings either.

    Deliberately NOT persisted to DynamoDB instead of fixed by adding a
    writer: the data already lives in S3 and is already destroyed by the
    purge's `outputs/{review_id}/` prefix scan (asserted in
    tests/test_analysis_artifact_persisted.py). Writing the prose-heaviest
    payload on the review into a store with a 35-day PITR retention floor
    (docs/data-handling.md -> "Accepted limitation -- DynamoDB PITR tail")
    would open a new Confidential-substance surface for no gain.

    Sourced from the row's OWN `analysis_s3_key` attribute
    (`pipeline_runner._write_real_terminal` ~L1139-1141) -- never a
    reconstructed path. Bucket is `os.environ["OUTPUTS_BUCKET"]`.

    On the Step Functions target's persist Lambda
    (`infra/lambda/persist/handler.py`), which per its own docstring writes
    only the mock pipeline's terminal result pending issues #80-#83, no
    analysis artifact is ever written and `analysis_s3_key` is simply
    absent -- this degrades to None exactly as it does for a review that
    predates the field. That is a gap in that Lambda's own scope, not a bug
    here; the in-process runner (the live deployment target,
    `config.pipeline_runner() == "inprocess"`) is the one this function
    restores.

    Degrades to None and NEVER raises: `s3_client` is None (this review's
    `get_review_detail` caller didn't construct one --
    `request_cancel`'s two internal calls only need `status` and pass
    none), the row carries no `analysis_s3_key`, the S3 object is missing,
    or the body is not valid JSON. An artifact read must never fail the
    detail route, the same way a missing redline never fails it.
    """
    if s3_client is None:
        return None
    key = item.get("analysis_s3_key")
    if not key:
        return None
    try:
        obj = s3_client.get_object(Bucket=os.environ["OUTPUTS_BUCKET"], Key=key)
        body = obj["Body"].read()
    except Exception as exc:  # noqa: BLE001 -- deliberately total
        # Deliberately broader than ClientError: a missing object, a denied
        # read, an endpoint/connection failure and a malformed response are
        # all the same event here -- "no artifact" -- and this function
        # promises never to fail the detail route. Same posture, and the
        # same noqa, as review_routes.py's "never leak a raw model/network
        # error" handler. Naming botocore's exception hierarchy explicitly
        # would also break the six test files that stub `botocore.exceptions`
        # with ClientError alone.
        logger.warning(
            "ANALYSIS_ARTIFACT: could not read %s for review_id=%s: %s",
            key, item.get("review_id"), exc,
        )
        return None
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        logger.warning(
            "ANALYSIS_ARTIFACT: malformed JSON body at %s for review_id=%s: %s",
            key, item.get("review_id"), exc,
        )
        return None
    if not isinstance(document, dict):
        # Valid JSON is not the same as a usable artifact. A truncated or
        # rewritten object that parses to a list/scalar would sail past the
        # decode guard above and then blow up on `.get(...)` in the caller
        # -- turning "artifact unreadable" into a 500 on the detail route,
        # which is precisely what this function promises never to do.
        logger.warning(
            "ANALYSIS_ARTIFACT: %s for review_id=%s parsed to %s, not an object",
            key, item.get("review_id"), type(document).__name__,
        )
        return None
    return document


def get_review_detail(
    review_id: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    s3_client: Any = None,
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

    `s3_client` (optional, default None): when provided, `issues` /
    `critic_delta` below are read through `load_analysis_artifact` off the
    row's `analysis_s3_key` -- see that function's docstring for why (issue
    #416's `findings`/`critic_delta` are never written to the `reviews` row
    itself). Callers that only need `status` (`request_cancel`'s two
    internal `get_review_detail` calls) pass no `s3_client` and get exactly
    today's behavior -- None for both fields, no S3 round trip.
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

    # `findings`/`critic_delta` are produced by the pipeline
    # (`pipeline_runner._ANALYSIS_FIELDS`, issue #416) but no writer has
    # EVER put either onto the
    # `reviews` row -- they live only in `outputs/{review_id}/analysis.json`.
    # `item.get("issues")` / `item.get("critic_delta")` therefore returned
    # None on every real review; read through the artifact instead of
    # adding a DynamoDB writer (see `load_analysis_artifact`'s docstring for
    # the retention rationale).
    analysis = load_analysis_artifact(item, s3_client)

    return {
        "review_id": review_id,
        "status": status_value,
        "decision": item.get("decision"),
        "confidence_state": item.get("confidence_state"),
        "confidence_band": item.get("confidence_band"),
        "issues": analysis.get("findings") if analysis is not None else None,
        "critic_delta": analysis.get("critic_delta") if analysis is not None else None,
        # The RESPONSE key stays `verdict_summary` (the documented public
        # field name, and the model-output vocabulary docs/output-contract.md
        # specifies) but the DynamoDB ATTRIBUTE it reads is `summary`.
        # `scripts/review_spine.py`'s result dict deliberately renames the
        # model's `verdict_summary` to `summary` (see that file's result
        # assembly and the note at scripts/primary_review_pass.py), and all
        # three writers persist it under that name:
        # `pipeline_runner._write_terminal` / `._write_real_terminal` and the
        # AWS Step Functions persist Lambda (infra/lambda/persist/handler.py).
        # Nothing has EVER written an attribute called `verdict_summary` --
        # confirmed against the full history -- so this read returned None on
        # every real review from the day the field shipped, and no test caught
        # it because every fixture hand-seeds a row with the reader's key
        # instead of going through a writer.
        "verdict_summary": item.get("summary"),
        # Issue #563: disclosure that stage 1 accepted one or more pending
        # tracked changes (single or multi-cluster/multi-author) into the
        # operative draft before review -- names the paragraph heading and
        # cluster/author counts, never silent. Absent-on-the-row -> None
        # here, same faithful-projection convention as every other field.
        "normalization_notes": item.get("normalization_notes"),
        # Issue #569: the bounded re-quote repair pass's outcome, when that
        # pass has run (attempted/recovered/still_failed). Absent-on-the-row
        # -> None here, same faithful-projection convention as every other
        # field -- renders correctly whether or not #569 has landed, since
        # nothing ever writes this key until it does.
        "requote": item.get("requote"),
        "reason": reason,
        # Target-agnostic stage-failure taxonomy (issue #258): the specific
        # pipeline stage a failure occurred in, when
        # `record_stage_failure` has written one.
        "failing_stage": item.get("failing_stage"),
        # Live progress (issue #447): which of the review spine's four
        # user-visible sub-stages (primary_pass / critic_pass /
        # reconciliation / redline -- scripts/review_spine.py's
        # PROGRESS_STAGES) is running RIGHT NOW, written by
        # pipeline_runner._write_progress_stage as each one starts. None on a
        # review that has not reached the spine yet, on one whose runner
        # predates this field, and on any deployment target that does not
        # report progress -- the UI falls back to its honest indeterminate
        # treatment rather than inventing a step. Same faithful-projection
        # convention as every field above: this is a read of the row, never a
        # computation of pipeline state (and emphatically never elapsed
        # time).
        "progress_stage": item.get("progress_stage"),
        "message": STATUS_USER_MESSAGES.get(status_value),
        "has_output": bool(item.get("output_s3_key")),
        # Issue #449: whether the INPUT document is still identifiable from
        # this row (the pointer, never the key itself -- same discipline as
        # `has_output` directly above). False for every review created before
        # the pointer was recorded; that is a truthful "not recorded", not a
        # claim that the document is gone.
        "has_input": bool(item.get("upload_s3_key")),
        # Issue #499 ("Butter it"): whether a cover-note draft is already
        # cached on this row -- a boolean pointer, never the draft text
        # itself, same discipline as `has_output`/`has_input` above. Lets
        # the UI show "cached, free to view" without a billed round trip
        # just to find out.
        "has_cover_note_draft": bool(item.get("cover_note_draft")),
        "playbook_id": item.get("playbook_id"),
        # Issue #449: WHICH MODELS RAN THIS REVIEW. Written at terminal-write
        # time by pipeline_runner._write_real_terminal from the bundle metadata
        # the spine actually resolved its primary/critic calls from. None for a
        # review that predates the field, for the mock pipeline (which invokes
        # no model at all), and for one that failed before the spine ran -- and
        # deliberately NOT back-filled from today's configured model: a review
        # run last week under a different model must never be relabelled with
        # the one configured now (this gets sharper once an admin can change
        # models). "Not recorded" is the honest answer; a guess is not.
        "primary_model_id": item.get("primary_model_id"),
        "critic_model_id": item.get("critic_model_id"),
        # Issue #508/#514: what the PROVIDER said it served, beside what was
        # asked for. Absent on every review predating the field, on the mock
        # pipeline, and wherever the provider omitted it -- and absent is NOT
        # a mismatch, which the reader-side comparison has to honour or the
        # whole history of the product reads as suspicious.
        "served_primary_model_id": item.get("served_primary_model_id"),
        "served_critic_model_id": item.get("served_critic_model_id"),
        # Issue #431: the per-review free-text guidance this review was
        # submitted with, so the Review tab can show back (read-only) which
        # instructions governed it. None for a review submitted without any
        # -- and for every review created before that field was recorded --
        # the same faithful-projection convention as every field above.
        "toaster_guidance": item.get("toaster_guidance"),
        # Issue #520: None on a review predating the field -- which is the
        # honest answer, not a back-filled "external" that would claim the
        # review was submitted under a mode nobody chose.
        "notes_mode": item.get("notes_mode"),
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
        # Issue #479 floor coverage: WHICH invariants were judged, which were
        # violated, and which went unjudged. Projected alongside the lineage
        # hashes above because the point of persisting the ids is that an
        # operator looking at a quarantined row can see which invariant went
        # unjudged, not merely that one did -- and a row-level attribute with
        # no read surface cannot answer that. Absent-on-the-row -> None here,
        # same faithful-projection convention as every other field.
        "floor_judged_invariant_ids": item.get("floor_judged_invariant_ids"),
        "floor_violated_invariant_ids": item.get("floor_violated_invariant_ids"),
        "floor_unjudged_invariant_ids": item.get("floor_unjudged_invariant_ids"),
        # Issue #471: the `playbook_versions` admin-facing version + content
        # hash that gated this submission -- populated for the live,
        # non-OPF playbook too (unlike the OPF-only fields above). Absent-
        # on-the-row -> None here, same faithful-projection convention.
        "playbook_version": item.get("playbook_version"),
        "playbook_content_hash": item.get("playbook_content_hash"),
        # Issue #482 (epic #481): the standing-instructions version + text
        # hash that governed this review, if any -- same faithful-
        # projection convention as every field above.
        "instructions_version": item.get("instructions_version"),
        "instructions_content_hash": item.get("instructions_content_hash"),
        # Issue #294: the review's governed Posture-version override, if
        # any. Absent-on-the-row -> None here too, same "faithful
        # projection" convention as the fields above.
        "posture_version": item.get("posture_version"),
        # OPF 0.3: the review POLICY (prescriptive rules) that governed this
        # document, alongside the playbook (descriptive precedent) above --
        # including whether those rules had been approved when they were
        # applied, which a hash alone does not say.
        "policy_version": item.get("policy_version"),
        "policy_hash": item.get("policy_hash"),
        "policy_approval_status": item.get("policy_approval_status"),
        # Whether a stop has been asked for and not yet taken effect. The UI
        # needs this to say "stopping…" instead of leaving the reviewer
        # pressing a button that looks like it did nothing: cancellation is
        # cooperative, so the gap between the request and the terminal write
        # is real and must be visible rather than hidden.
        "cancel_requested": bool(item.get("cancel_requested_at")),
        # Issue #486: the reviewer's OPTIONAL disposition capture -- NOT an
        # approval gate (owner correction 2026-08-02: a lightweight "what
        # happened with this one" record for the negotiating-history / eval
        # feedback loop, never something this product enforces or nags
        # about). `record_disposition` never touches `status`/`decision`
        # above; these five keys are a faithful projection of whatever the
        # row currently holds -- None on a review with nothing recorded yet,
        # same convention as every field above.
        #
        # The issue's "What to build" asks for the disposition as "(value,
        # who, when)"; this projects value + when
        # (`attorney_disposition_recorded_at`) but deliberately no "who" --
        # this route is owner-scoped (a caller can only ever record a
        # disposition on their OWN review, or as an admin acting on someone
        # else's), so "who recorded this" is never in question for the
        # common case the way it would be on a shared/team surface. The
        # actual actor IS captured, in the one place cross-user visibility
        # matters: `review_routes.post_review_disposition`'s audit row
        # (`actor=caller_sub`). This is a decision, not a miss -- adding
        # `attorney_disposition_by` here would duplicate what the audit
        # trail already answers authoritatively.
        "attorney_disposition": item.get("attorney_disposition"),
        "attorney_disposition_reason_codes": item.get("attorney_disposition_reason_codes"),
        "attorney_disposition_topic_ids": item.get("attorney_disposition_topic_ids"),
        "attorney_disposition_note": item.get("attorney_disposition_note"),
        "attorney_disposition_recorded_at": item.get("attorney_disposition_recorded_at"),
        "legal_triage_status": item.get("legal_triage_status"),
    }


class ReviewNotCancellableError(Exception):
    """Raised by `request_review_cancel` when the review has already reached a
    terminal status. Carries that status so the caller can say WHICH terminal
    it landed on rather than a bare refusal -- "this review already finished"
    and "this review already failed" are different things to be told."""

    def __init__(self, message: str, *, status: str) -> None:
        super().__init__(message)
        self.status = status


def request_review_cancel(
    review_id: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """Ask a running review to stop (owner-or-admin, same scoping as
    `get_review_detail` -- a non-owner gets its 404, never a 403 that would
    confirm the id exists).

    This RECORDS A REQUEST; it does not stop anything itself. The review runs
    on a worker thread inside a model call that cannot be interrupted from
    here, so the runner polls `cancel_requested` at its own checkpoints and
    stops at the next one (see pipeline_runner). Writing an intent and letting
    the owner of the work act on it is the only honest shape: the alternative
    -- marking the row CANCELLED immediately -- would tell the reviewer the
    run had stopped while it was still spending their money.

    The conditional write is what keeps this race-safe: a review that reached
    a terminal status between the caller's read and this update fails the
    condition and raises, rather than stamping a cancel request onto a
    finished review.
    """
    detail = get_review_detail(review_id, caller_user_row, dynamodb_resource)
    current_status = detail.get("status") or ""
    if current_status in REVIEW_STATUSES_TERMINAL:
        raise ReviewNotCancellableError(
            f"Review {review_id} is already {current_status}.", status=current_status
        )

    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    now = str(int(time.time()))
    try:
        table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET cancel_requested_at = :now, updated_at = :now",
            ConditionExpression="attribute_exists(review_id) AND #s IN (:pending, :running)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":now": now,
                ":pending": "PENDING",
                ":running": "RUNNING",
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # It finished under us. Re-read rather than guessing which terminal.
            settled = get_review_detail(review_id, caller_user_row, dynamodb_resource)
            raise ReviewNotCancellableError(
                f"Review {review_id} is already {settled.get('status')}.",
                status=str(settled.get("status") or ""),
            ) from exc
        raise

    return {"review_id": review_id, "status": current_status, "cancel_requested": True}


def cancel_requested(review_id: str, dynamodb_resource: Any) -> bool:
    """Has a stop been asked for? Consulted by the runner at its checkpoints.

    Consistent read: the whole point is to observe a write made moments ago by
    a different request thread, which is exactly the case DynamoDB's default
    eventual consistency is allowed to miss. A read failure returns False --
    an unreachable table must not abort a review that is running fine, and the
    next checkpoint will ask again.
    """
    try:
        table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
        response = table.get_item(
            Key={"review_id": review_id},
            ConsistentRead=True,
            ProjectionExpression="cancel_requested_at",
        )
    except Exception:  # noqa: BLE001 - a failed poll must never fail the review
        logger.warning(
            "Could not read the cancel flag for review %s; treating it as not "
            "cancelled. The review is UNAFFECTED.",
            review_id,
            exc_info=True,
        )
        return False
    return bool((response.get("Item") or {}).get("cancel_requested_at"))


def mark_cancelled(review_id: str, dynamodb_resource: Any) -> None:
    """Terminal write for a review the reviewer stopped.

    Guarded the same way `record_stage_failure` is (issue #446): a review that
    reached a terminal status first keeps it. A run that completed
    successfully in the window between the cancel request and the runner's
    next checkpoint is DONE, with a redline the reviewer can still download --
    relabelling that CANCELLED would destroy real, paid-for work over a race.
    """
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    now = str(int(time.time()))
    try:
        table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET #s = :cancelled, cancelled_at = :now, updated_at = :now",
            ConditionExpression="attribute_exists(review_id) AND #s IN (:pending, :running)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":cancelled": "CANCELLED",
                ":pending": "PENDING",
                ":running": "RUNNING",
                ":now": now,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return
        raise


# A Step Functions execution ARN starts with `arn:`. The in-process Docker
# Compose runner records its own `inprocess:<execution-name>` pseudo-ARN
# (pipeline_runner.InProcessStepFunctionsClient.start_execution), which names
# no AWS resource at all -- so the prefix is what distinguishes "there is a
# real execution to stop" from "this review is running on a worker thread and
# stops via the cooperative checkpoints instead".
#
# Deliberately keyed on the DATA rather than on `config.deploy_target()`: the
# runner that started a review is a fact recorded on its own row, and reading
# it there cannot go stale or disagree with a redeployed env var.
_STEP_FUNCTIONS_ARN_PREFIX = "arn:"


def stop_running_execution(
    review_id: str,
    dynamodb_resource: Any,
    sfn_client: Any,
) -> bool:
    """Abort the Step Functions execution running `review_id`, if there is one.

    Returns True when a real execution was stopped, False when there is
    nothing to stop -- no ARN recorded yet (a cancel that raced submission),
    or an in-process pseudo-ARN.

    This is the AWS target's answer to cancellation, and it is a STRONGER
    guarantee than the in-process one: Step Functions stops scheduling states
    immediately, so no further pipeline stage runs at all. (The stage already
    executing runs to its own completion -- Step Functions cannot kill a
    Lambda mid-invocation any more than the in-process runner can abort a
    blocking HTTP call -- which is why `infra/lambda/persist/handler.py`
    refuses to overwrite a CANCELLED row.)

    The abandoned execution leaks nothing: the concurrency semaphore's slots
    carry a TTL lease precisely so "a hard-killed execution's slot self-
    expires even if the release state never runs" (infra/lib/nested/
    pipeline-stack.ts), and the caller settles the spend reservation.

    Errors PROPAGATE. A swallowed StopExecution is the whole bug this exists
    to fix: the reviews row would say a stop was requested, the UI would show
    "Stopping…", and the pipeline would run happily to completion.
    """
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    response = table.get_item(
        Key={"review_id": review_id},
        ConsistentRead=True,
        ProjectionExpression="execution_arn",
    )
    execution_arn = (response.get("Item") or {}).get("execution_arn") or ""
    if not execution_arn.startswith(_STEP_FUNCTIONS_ARN_PREFIX):
        return False

    sfn_client.stop_execution(
        executionArn=execution_arn,
        cause="Cancelled by the review owner.",
    )
    return True


def settle_reservation_for_cancel(review_id: str, dynamodb_resource: Any) -> None:
    """Credit back the unspent worst-case spend reservation for a cancelled
    review.

    On the AWS target the persist stage is what ordinarily settles the
    reservation, and an aborted execution never reaches it -- so without this
    a stopped review would hold its slice of the daily spend cap until UTC
    midnight, and stopping reviews would quietly starve the cap. Mirrors the
    orphan reconciler's dead-execution settlement (infra/lambda/
    orphan_reconciler/handler.py::_release_reservation), including its
    `reservation_released` idempotency guard so a race with persist cannot
    credit the same reservation twice.

    Settles at 0 actual cents: the ledger, not this function, is the record of
    what was really spent before the stop.
    """
    submissions = dynamodb_resource.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
    submission = _find_submission_row_for_review(submissions, review_id)
    if not submission or not submission.get("spend_reservation_id"):
        return
    if submission.get("reservation_released"):
        return
    settle_spend(review_id, submission["spend_reservation_id"], 0, dynamodb_resource)
    submissions.update_item(
        Key={"idempotency_key": submission["idempotency_key"]},
        UpdateExpression="SET reservation_released = :true, updated_at = :now",
        ExpressionAttributeValues={":true": True, ":now": str(int(time.time()))},
    )


def _find_submission_row_for_review(table: Any, review_id: str) -> dict[str, Any] | None:
    """The submission row for `review_id`, via the review_id GSI when the
    table has one and a scan otherwise -- same shape as
    pipeline_runner._find_submission_by_review_id."""
    try:
        from boto3.dynamodb.conditions import Key

        resp = table.query(
            IndexName="review_id-index",
            KeyConditionExpression=Key("review_id").eq(review_id),
        )
    except Exception:  # noqa: BLE001 - no GSI (or a fake without query): fall back
        resp = table.scan(
            FilterExpression="review_id = :rid",
            ExpressionAttributeValues={":rid": review_id},
        )
    items = resp.get("Items", [])
    return items[0] if items else None


_REVIEW_LIST_ITEM_FIELDS = (
    "review_id",
    "owner_sub",
    "playbook_id",
    "status",
    "decision",
    "confidence_band",
    "created_at",
    "updated_at",
    # Issue #449 -- the provenance the History table renders per row: WHICH
    # version of the governing rules applied, and WHICH models ran each step.
    # None on a row that predates each field; the UI renders "not recorded"
    # rather than substituting today's value (see get_review_detail).
    "policy_version",
    "posture_version",
    "primary_model_id",
    "critic_model_id",
    # Issue #508/#514 -- the response side of the same question, so the
    # History table can answer "asked X, served Y" from the row rather than
    # from a support ticket.
    "served_primary_model_id",
    "served_critic_model_id",
    # Issue #471 -- the playbook version + content hash that actually gated
    # this submission, populated for the live non-OPF playbook too.
    "playbook_version",
    "playbook_content_hash",
    # Issue #486 -- the reviewer's OPTIONAL "what happened with this one"
    # capture (ACCEPTED/EDITED/REJECTED), so History can render a Disposition
    # column without a second per-row detail fetch. None on a review with
    # nothing recorded yet -- the UI renders that as "Not recorded", never a
    # guess, same convention as every other field in this tuple.
    "attorney_disposition",
)


def _review_list_item(item: dict[str, Any]) -> dict[str, Any]:
    """Lean summary shape for the list view -- confidential per-review
    content (summary, issues, critic_delta, toaster_guidance) is
    reserved for the single-review detail route, not the list.

    Issue #449 adds the two DOWNLOAD-AVAILABILITY booleans the History table
    needs. They are booleans, never the S3 keys they are derived from: the
    list is the one response an admin receives for every user's reviews, and
    an object key is storage-layout detail a caller has no use for (the
    detail route has projected `has_output` this way since #84 -- this just
    applies the same rule to the input pointer).
    """
    projection = {field: item.get(field) for field in _REVIEW_LIST_ITEM_FIELDS}
    projection["has_output"] = bool(item.get("output_s3_key"))
    projection["has_input"] = bool(item.get("upload_s3_key"))
    # Issue #499: same boolean-pointer-only convention as has_output/
    # has_input directly above -- History's row action needs to know
    # whether a cached draft exists without a billed round trip.
    projection["has_cover_note_draft"] = bool(item.get("cover_note_draft"))
    return projection


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


# Pagination (issue #488). The listing was unbounded on both axes: every row
# ever written, in one response, sorted in memory -- so the History tab's
# first fetch got heavier every week and the admin path scanned the whole
# table on every load. The cap is not advisory; a caller-supplied `limit` is
# clamped into [1, MAX], so no request can turn this back into a full dump.
# Same posture as the diagnostics route next door.
REVIEWS_PAGE_DEFAULT_LIMIT = 25
REVIEWS_PAGE_MAX_LIMIT = 100


def encode_page_token(last_key: dict[str, Any] | None) -> str | None:
    """DynamoDB's `LastEvaluatedKey` as an opaque string.

    Opaque to the CALLER, not secret: it is a key from a table the caller is
    already authorized to read a page of, and every page re-applies the same
    owner scoping. What the encoding buys is that the client cannot construct
    one by hand and cannot depend on its shape.
    """
    if not last_key:
        return None
    return base64.urlsafe_b64encode(
        json.dumps(last_key, sort_keys=True, default=str).encode("utf-8")
    ).decode("ascii")


def decode_page_token(token: str | None) -> dict[str, Any] | None:
    """The inverse. A token that is not one raises 400 rather than being
    ignored -- silently starting from page one would look to a paging client
    like an endless stream of the same first page."""
    if not token:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
    except Exception:  # noqa: BLE001 - any malformed token is the same answer
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That page token is not valid. Reload the list to start again.",
        ) from None
    if not isinstance(decoded, dict) or not decoded:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That page token is not valid. Reload the list to start again.",
        )
    return decoded


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return REVIEWS_PAGE_DEFAULT_LIMIT
    return max(1, min(int(limit), REVIEWS_PAGE_MAX_LIMIT))


def _page_for_owner(
    table: Any, owner_sub: str, limit: int, start_key: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """One page of an owner's reviews, newest first.

    The `owner_sub-index` GSI is partitioned on `owner_sub` with `created_at`
    as its SORT KEY (infra/lib/nested/data-stack.ts), so `ScanIndexForward=
    False` gives newest-first from the index itself. That is what makes paging
    correct rather than merely bounded: the old in-memory sort could only
    order rows it had already fetched, which is the same thing as fetching
    them all.

    The scan fallback is for a lightweight test stand-in without `.query()`
    (same convention as `_list_reviews_for_owner`). It pages the SCAN and
    filters after, so a page can come back short and still have more behind
    it -- which is exactly how a filtered DynamoDB read behaves. Keeping that
    behaviour identical on both paths is deliberate: a fake that never returns
    a short page would hide the one bug this code can have.
    """
    if hasattr(table, "query"):
        from boto3.dynamodb.conditions import Key

        kwargs: dict[str, Any] = {
            "IndexName": "owner_sub-index",
            "KeyConditionExpression": Key("owner_sub").eq(owner_sub),
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        resp = table.query(**kwargs)
        return resp.get("Items", []), resp.get("LastEvaluatedKey")

    items: list[dict[str, Any]] = []
    key = start_key
    while len(items) < limit:
        kwargs = {"Limit": limit}
        if key:
            kwargs["ExclusiveStartKey"] = key
        resp = table.scan(**kwargs)
        items.extend(i for i in resp.get("Items", []) if i.get("owner_sub") == owner_sub)
        key = resp.get("LastEvaluatedKey")
        if not key:
            break
    items.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    return items[:limit], key


def _page_all(
    table: Any, limit: int, start_key: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """One page of the admin-wide listing.

    A table scan has no global order, so this is bounded but NOT globally
    newest-first: rows are sorted within the page only. That limitation is
    stated rather than papered over -- the honest fix is an index over the
    whole table, which is a schema change in two deploy targets. The
    user-facing History tab asks for `scope=mine` and takes the exact,
    index-ordered path above, so nothing a person actually reads is affected.
    """
    kwargs: dict[str, Any] = {"Limit": limit}
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = table.scan(**kwargs)
    items = resp.get("Items", [])
    items.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    return items, resp.get("LastEvaluatedKey")


def list_reviews(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    owner_scoped: bool = False,
    limit: int | None = None,
    next_token: str | None = None,
) -> dict[str, Any]:
    """GET /api/reviews (issue #84): the caller's own reviews, newest first;
    an admin sees every review (ARCHITECTURE.md Routes table: "List my
    reviews (admin: all reviews)").

    `owner_scoped=True` (issue #449) opts OUT of the admin widening: the
    listing is restricted to the caller's own rows no matter who the caller
    is. The History tab asks for this. An admin's History is their own
    history -- a cross-user history view is a different surface with
    different consequences (it shows every user's contract activity in one
    table) and is explicitly out of scope for that ticket. Nothing about the
    default is changed: an admin calling GET /api/reviews without the
    parameter still sees everything, exactly as documented.

    Issue #488: PAGED. Returns `{"items": [...], "next_token": str | None}`
    rather than a bare list -- a page's worth of rows plus the token for the
    next page, or None when there is nothing behind it. The listing used to
    be unbounded on both axes, so it grew linearly forever; `limit` is
    clamped into [1, REVIEWS_PAGE_MAX_LIMIT] and cannot be opted out of.
    """
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    page_size = _clamp_limit(limit)
    start_key = decode_page_token(next_token)

    if _is_admin_caller(caller_user_row) and not owner_scoped:
        items, last_key = _page_all(table, page_size, start_key)
    else:
        owner_sub = caller_user_row.get("cognito_sub", "")
        items, last_key = _page_for_owner(table, owner_sub, page_size, start_key)

    return {
        "items": [_review_list_item(i) for i in items],
        "next_token": encode_page_token(last_key),
    }


# ---------------------------------------------------------------------------
# Admin diagnostics (issue #443) -- GET /api/admin/diagnostics/recent-failures
# ---------------------------------------------------------------------------

# A terminal status that is NOT a failure, and therefore never a diagnostics
# row. `DONE` is the success terminal; `SUPERSEDED` is a post-terminal
# ADMINISTRATIVE overlay (ARCHITECTURE.md -> QUARANTINED/SUPERSEDED are
# overlays written by an admin action or a rollback sweep, not by the
# pipeline), so a superseded review is not a thing that "failed" and has no
# cause to explain. QUARANTINED deliberately stays IN: it is a review the
# pipeline stopped, and its reason (`submission_time_bundle_retired`, written
# by `verify_submission_time_bundle`) is exactly what an operator is here to
# read. That token is stored under `quarantine_reason`, not `reason`, which is
# why the projection below resolves the reason rather than reading one
# attribute.
_DIAGNOSTIC_NON_FAILURE_STATUSES = frozenset({REVIEW_STATUS_SUCCESS_TERMINAL, "SUPERSEDED"})

# Derived, not hand-listed, on purpose: a terminal failure status added to
# REVIEW_STATUSES_TERMINAL later must show up in diagnostics automatically.
# The safety property of this route does not come from which ROWS are
# selected -- it comes from `_RECENT_FAILURE_FIELDS` below, which is what
# bounds the CONTENT that leaves the backend.
DIAGNOSTIC_FAILURE_STATUSES = frozenset(
    REVIEW_STATUSES_TERMINAL - _DIAGNOSTIC_NON_FAILURE_STATUSES
)

# Default and hard ceiling on how many rows the route will return. The cap is
# not advisory: a caller-supplied `limit` is clamped into [1, MAX], so no
# request can turn this into a full-table dump.
RECENT_FAILURES_DEFAULT_LIMIT = 50
RECENT_FAILURES_MAX_LIMIT = 200

# THE SECURITY BOUNDARY OF THIS ROUTE (issue #443).
#
# This is an ALLOWLIST, and it is the whole reason the Diagnostics tab is not
# a log viewer. A reviews row carries Confidential document substance
# (`summary`, `issues`, the per-review `toaster_guidance` the submitter
# typed) and deployment internals
# (`output_s3_key`, execution names). None of it may reach an operator's
# browser through a diagnostics panel -- the same rule
# `retention._HOLD_LIST_FIELDS` applies to the legal-hold list view, for the
# same reason (docs/data-handling.md purge invariant 5; threat-model.md
# "Malicious admin or compromised session").
#
# So rows are PROJECTED field by field, never spread. Adding a field here is
# a deliberate disclosure decision; forgetting to add one leaks nothing.
# `reason` is safe to serve precisely because issue #442 made it a controlled
# TOKEN vocabulary that contains no status code, endpoint, exception text, or
# key material -- the frontend turns the token into prose.
_RECENT_FAILURE_FIELDS = (
    "review_id",
    "created_at",
    # Issue #472: WHEN the failure was recorded, distinct from `created_at`
    # (submission time). Written by `record_stage_failure` and the Docker
    # Compose mock pipeline's own `_fail_review`; absent on a row that
    # predates this field or was never a failure written through either
    # path (e.g. QUARANTINED, written by verify_submission_time_bundle).
    "failed_at",
    "failing_stage",
    "reason",
    "status",
)


def _resolve_failure_reason(item: dict[str, Any]) -> Any:
    """The failure reason token for a reviews row, wherever it was written.

    The SAME three-field coalesce `get_review_detail` runs, and it has to be:
    the token is not always stored under `reason`. The only QUARANTINED writer
    in this module, `verify_submission_time_bundle`, writes
    `quarantine_reason` -- the documented post-terminal administrative overlay
    field (docs/data-handling.md) -- and writes neither `reason` nor
    `failing_stage`. Reading the bare `reason` attribute therefore returned
    null for EVERY quarantined review: the submitter saw the true cause on
    their own Review tab (which coalesces) while an admin's Diagnostics tab
    said "no cause was recorded". That is exactly the "the system knows, the
    operator cannot see" failure this route exists to remove, and a drift
    between two surfaces the ticket set out to prevent.

    This coalesces INTO the single `reason` output key. `quarantine_reason` is
    deliberately NOT added to `_RECENT_FAILURE_FIELDS`: the allowlist stays at
    exactly the fields listed there, and the disclosure decision is unchanged
    -- every one of these values is an issue-#442 controlled token.
    """
    return (
        item.get("reason") or item.get("quarantine_reason") or item.get("analysis_report_reason")
    )


def list_recent_failures(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    limit: int = RECENT_FAILURES_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """GET /api/admin/diagnostics/recent-failures -- why recent reviews failed.

    Answers, from inside the app, the question that on 2026-08-01 could only
    be answered by driving the Coolify UI into a production container's logs:
    *why* did these reviews fail? Each row carries the #442 `reason` token,
    the stage that failed, the terminal status, and when -- and nothing else.

    NOT A LOG VIEWER (issue #443, explicitly out of scope): no stack trace,
    no exception message, no prompt or document substance, no key material,
    no raw endpoint. That guarantee is structural, not editorial -- the
    response is projected through `_RECENT_FAILURE_FIELDS`, so a field added
    to the reviews row tomorrow cannot appear here by accident.

    Bounded by construction: `limit` is clamped into
    [1, RECENT_FAILURES_MAX_LIMIT], newest first by `created_at`.

    Raises HTTPException(403) for a non-admin caller. This is instance-wide
    visibility -- every user's failures, not the caller's own -- so unlike
    `get_review_detail` (which 404s a non-owner to keep review ids
    non-enumerable) there is no "your own row" case to be coy about: you are
    an admin or you get nothing.
    """
    if not _is_admin_caller(caller_user_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to view diagnostics.",
        )

    try:
        requested = int(limit)
    except (TypeError, ValueError):
        requested = RECENT_FAILURES_DEFAULT_LIMIT
    bounded = max(1, min(requested, RECENT_FAILURES_MAX_LIMIT))

    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    failures = [
        item
        for item in _scan_all_reviews(table)
        if str(item.get("status") or "") in DIAGNOSTIC_FAILURE_STATUSES
    ]
    # Same ordering key as `list_reviews`: `created_at` is written as a
    # fixed-width epoch-second STRING by `_create_review_row`, so a plain
    # reverse string sort is a true newest-first ordering.
    failures.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    return [
        # `json_safe` because these rows come back through boto3's resource
        # API, which deserializes every stored number to `decimal.Decimal` --
        # unserializable by JSONResponse. That took GET /api/users down in
        # production (issue #440, commit df60971) and the unit fakes cannot
        # see it, since they store plain ints.
        json_safe(
            {
                field: (
                    _resolve_failure_reason(item) if field == "reason" else item.get(field)
                )
                for field in _RECENT_FAILURE_FIELDS
            }
        )
        for item in failures[:bounded]
    ]
