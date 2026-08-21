"""
In-process pipeline runner (Docker Compose deployment target, Phase 1).

The AWS deployment drives the review pipeline with Step Functions: the backend
calls `sfn_client.start_execution(...)` (via reviews.ensure_execution_started)
and the Lambda stages carry the review through PENDING -> RUNNING -> terminal.

The Docker Compose deployment has no Step Functions. Rather than change submit_review /
ensure_execution_started (which inject an `sfn_client`), this module provides a
DUCK-TYPED stand-in -- `InProcessStepFunctionsClient` -- that exposes exactly
the slice of the boto3 Step Functions client that ensure_execution_started uses
(`start_execution(stateMachineArn, name, input)` and
`exceptions.ExecutionAlreadyExists`). On start_execution it enqueues the review
onto a bounded background-worker pool that runs the pipeline in-process. So
`reviews.py` is unchanged; only which client `review_routes.get_sfn_client`
returns changes, selected by PIPELINE_RUNNER.

The bounded ThreadPoolExecutor IS the in-process concurrency semaphore (it
replaces the DynamoDB semaphore + TTL lease, which exist only to recover slots
leaked by hard-killed distributed executions -- a crashed single process
releases everything by dying).

PHASE 1 SCOPE: `run_mock_pipeline` reproduces the *mock* pipeline's observable
contract (PENDING -> RUNNING -> DONE / MANUAL_REVIEW_REQUIRED, with a
downloadable output for the eiaa playbook copied from the seeded fixture), so
the deployment abstraction can be proven end-to-end against known-good
behavior. It is UNCHANGED by Phase 2 below and remains directly callable --
this is the "flag/env var" escape hatch for tests/callers that don't want a
live model call.

PHASE 2 (issue #259): `run_real_pipeline` swaps the canned fixture for a
genuinely computed review -- `scripts/review_spine.py::run_review` (issue
#239), driven by a real `OpenRouterModelClient` (backend/src/model_client.py)
built from the admin-set key (backend/src/model_settings.py) or, failing that,
`OPENROUTER_API_KEY`, against the admin-selected models (issue #445) or,
failing that, `OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID`, or the policy pin.
`InProcessStepFunctionsClient`'s default runner picks between the two bodies
per review based on `config.model_provider()` (`MODEL_PROVIDER` env var):
`openrouter` selects the real body, anything else (including unset) keeps
the Phase 1 mock body -- so existing tests/deployments that never set
`MODEL_PROVIDER` are unaffected. On any unhandled exception the real body
calls the SHARED `reviews.record_stage_failure` (issue #258) with the actual
failing stage name, exactly the AWS error-handler Lambda's contract, instead
of leaving the review PENDING/RUNNING forever -- and, since issue #442, with
a CLASSIFIED reason token (`classify_failure_reason` below) instead of the
constant "unhandled_exception", so the row records WHY it failed and not
merely that it did.

RUNTIME PLAYBOOK LOADING (issue #401, empty-shell foundation): before
`run_real_pipeline` loads any playbook content, it re-resolves the active
release bundle from the runtime activation record (PLAYBOOKS_TABLE, via
`reviews.verify_submission_time_bundle` -- previously wired only into the
AWS error-handler path, issue #194's step 10). A bundle no longer active by
execution time (deactivated/superseded since submission, or -- the
empty-playbook-store case -- never active at all) QUARANTINEs the review
instead of falling through to `_load_playbook_bundle`'s on-disk read: no
code path in this module reads `playbooks/*.json` for the active ruleset
without that runtime check having just passed.

POINTER-ONLY PAYLOAD RULE (issue #19): only S3 keys / review_id / decision flow
here; document bytes move server-side via S3 (CopyObject for the mock path,
GetObject/PutObject for the real path), never through this process's own
state or logs.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import boto3

try:  # production runs `src.main`; tests put backend/src on sys.path
    from src import (
        config,
        invocation_ledger,
        model_client,
        model_settings,
        playbook_upload,
        playbook_versions,
        reviews,
    )
except ImportError:  # pragma: no cover
    import config  # type: ignore[no-redef]
    import invocation_ledger  # type: ignore[no-redef]
    import model_client  # type: ignore[no-redef]
    import model_settings  # type: ignore[no-redef]
    import playbook_upload  # type: ignore[no-redef]
    import playbook_versions  # type: ignore[no-redef]
    import reviews  # type: ignore[no-redef]

# scripts/review_spine.py (issue #239) composes the pipeline-stage modules
# it imports (extraction_normalization_stage, diff_standard_form, ...) via
# its own SCRIPTS_DIR/BACKEND_SRC_DIR sys.path insertion; inserting SCRIPTS_DIR
# here too (idempotent) lets THIS module import review_spine + playbook_registry
# by bare name regardless of which of the two import styles above resolved.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import canonicalize  # noqa: E402
import document_injection_scan  # noqa: E402
import playbook_registry  # noqa: E402
import review_spine  # noqa: E402

logger = logging.getLogger(__name__)

# In-process concurrency cap -- the semaphore equivalent for a single container.
_MAX_CONCURRENCY = int(os.environ.get("PIPELINE_MAX_CONCURRENCY", "5"))

# ---------------------------------------------------------------------------
# Failure classification (issue #442)
# ---------------------------------------------------------------------------
#
# `run_real_pipeline`'s deliberate catch-all used to record the constant
# "unhandled_exception" for EVERY failure, so a review that died because the
# model account was out of credits looked -- to the person who has to fix it
# -- exactly like one that died on a malformed response. The real cause was
# known: it reached `logger.exception` (container log) and was then thrown
# away, which meant diagnosing a two-minute admin fix needed shell access to
# a production container.
#
# This maps the caught exception to a reason TOKEN drawn from the taxonomy in
# `reviews.STAGE_FAILURE_REASON_STATUS`. A token, never a message: the token
# is what crosses the API boundary, and the frontend
# (`frontend/src/ReviewSubmission.tsx`'s REASON_EXPLANATIONS) is what turns
# it into prose. That indirection is exactly what buys BOTH comprehensibility
# and issue #425's rule -- no raw `HTTP <n>`, endpoint, key material, stack
# trace, or prompt/document substance can reach user-facing copy, because
# none of it is in the token.
#
# Classification is by the STRUCTURED `ModelInvocationError.status_code`, not
# by parsing the exception message, which would rot the next time that copy
# changes.
FAILURE_REASON_UNCLASSIFIED = "unhandled_exception"

_MODEL_STATUS_FAILURE_REASONS: dict[int, str] = {
    401: "model_key_rejected",  # key absent/invalid at the provider
    402: "model_account_out_of_credits",  # payment required -- fund the account
    403: "model_key_rejected",  # key present but not permitted
    404: "model_unavailable",  # the selected model id is not served
    429: "model_rate_limited",  # too many requests for this account
    503: "model_unavailable",  # provider/model temporarily out of service
}


def classify_failure_reason(exc: BaseException) -> str:
    """Map a pipeline exception to a stage-failure reason token.

    Anything not recognised -- an unmapped status, a model error with no
    status (transport/malformed-response), or an exception from any other
    stage entirely -- falls back to `unhandled_exception`, i.e. EXACTLY
    today's behavior. This function can therefore only ever make the
    recorded reason more specific, never less.
    """
    if isinstance(exc, model_client.ModelContextLengthExceededError):
        # Its own token rather than `document_too_large`: this one was
        # measured by the provider, not estimated pre-call. Same terminal
        # status (see reviews.STAGE_FAILURE_REASON_STATUS).
        return "model_context_length_exceeded"
    # Issue #472: both of these subclass ModelInvocationError, so they must
    # be checked BEFORE the generic ModelInvocationError/status_code branch
    # below -- exactly the same ordering reason ModelContextLengthExceededError
    # is checked first above.
    if isinstance(exc, model_client.ModelKeyMissingError):
        # A PRE-CALL condition: no key was resolved, so no request was ever
        # sent. Distinct from `model_key_rejected` (a provider 401/403 --
        # a key WAS sent and refused).
        return "model_key_missing"
    if isinstance(exc, model_client.ModelTimeoutError):
        return "model_timeout"
    # Issue #527: both subclass ModelInvocationError, so -- same ordering
    # reason as the branches above -- they must be checked BEFORE the
    # generic ModelInvocationError/status_code branch below.
    # ModelOutputTruncatedError is checked before ModelEmptyContentError
    # because `OpenRouterModelClient.invoke` itself prefers the truncation
    # classification whenever both are true (a truncated response whose
    # content also happens to be empty): that ordering is enforced once,
    # here, at the client -- these two `isinstance` checks below can never
    # both be reachable for the SAME exception instance, but are kept in
    # the same relative order for readability.
    if isinstance(exc, model_client.ModelOutputTruncatedError):
        return "model_output_truncated"
    if isinstance(exc, model_client.ModelEmptyContentError):
        return "model_empty_content"
    if isinstance(exc, model_client.ModelInvocationError):
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return _MODEL_STATUS_FAILURE_REASONS.get(
                status_code, FAILURE_REASON_UNCLASSIFIED
            )
    return FAILURE_REASON_UNCLASSIFIED


class ExecutionAlreadyExists(Exception):
    """Duck-type of botocore's SFN ExecutionAlreadyExists, so
    ensure_execution_started's `except sfn_client.exceptions.ExecutionAlreadyExists`
    branch behaves identically in-process."""


class _InProcExceptions:
    ExecutionAlreadyExists = ExecutionAlreadyExists


def _ddb_resource() -> Any:
    return boto3.resource("dynamodb", **config.boto3_client_kwargs("dynamodb"))


def _s3_client() -> Any:
    return boto3.client("s3", **config.boto3_client_kwargs("s3"))


# ---------------------------------------------------------------------------
# Phase 1 mock pipeline body.
# ---------------------------------------------------------------------------


def _mark_running(review_id: str, dynamodb_resource: Any) -> None:
    """PENDING -> RUNNING, conditional (never clobbers a terminal/ERROR row)."""
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    try:
        table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET #s = :running, updated_at = :now",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":running": "RUNNING",
                ":pending": "PENDING",
                ":now": str(int(time.time())),
            },
        )
    except Exception as exc:  # ConditionalCheckFailed -> not PENDING; no-op
        if type(exc).__name__ != "ConditionalCheckFailedException" and not _is_conditional(exc):
            raise


def _write_progress_stage(review_id: str, stage_token: str, dynamodb_resource: Any) -> None:
    """Record WHICH sub-stage of the review is running right now (issue #447).

    `scripts/review_spine.py::run_review` calls this (via the `on_progress`
    seam) immediately before each of its four user-visible sub-stages, so
    `progress_stage` on the reviews row is a live fact -- "the critic pass is
    running" -- not an elapsed-time guess. `get_review_detail` projects it and
    the polling UI turns it into "Step 2 of 4". Four extra small writes per
    review, against two model calls: negligible.

    NOTHING HERE MAY FAIL THE REVIEW. Progress is cosmetic; the review is not.
    This is the same lesson as #446's `_settle_reservation_safely` -- a
    bookkeeping write that throws must not reach run_real_pipeline's
    fail-closed `except` and terminate a review that is running perfectly
    well. So every exception is swallowed:

      - A ConditionalCheckFailed (the row is no longer RUNNING -- already
        terminal, or never left PENDING) is an expected no-op, silently.
        The condition is what stops a late/racing progress write from
        re-touching a finished row.
      - Anything else (throttling, a transient DDB error) is logged at
        WARNING, once, and dropped. The user simply keeps seeing the
        previous step, which is honest: the pipeline really is still
        somewhere at or after that step.
    """
    try:
        table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
        table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET progress_stage = :p, updated_at = :now",
            ConditionExpression="#s = :running",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":p": stage_token,
                ":running": "RUNNING",
                ":now": str(int(time.time())),
            },
        )
    except Exception as exc:  # noqa: BLE001 - a cosmetic write must never fail the review
        if type(exc).__name__ == "ConditionalCheckFailedException" or _is_conditional(exc):
            return
        logger.warning(
            "Failed to record progress stage %s for review %s. The review itself is "
            "UNAFFECTED; only the live progress indicator is stale.",
            stage_token,
            review_id,
            exc_info=True,
        )


def _is_conditional(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    return bool(resp) and resp.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class ReviewCancelled(Exception):
    """Raised at a cancel checkpoint when the reviewer has asked to stop.

    Deliberately NOT a subclass of anything `classify_failure_reason` maps: a
    cancelled review is not a failed one, and `run_real_pipeline` catches this
    BEFORE its fail-closed handler so no stage-failure row is ever written for
    a stop the user asked for.
    """


def _make_cancel_checkpoint(review_id: str, dynamodb_resource: Any) -> Callable[[], None]:
    """Build the callable the spine, the passes, and the model client all use
    to ask "should I still be running?".

    ONE checkpoint function, threaded into every loop that can spend real time,
    because the seams differ wildly in how long they block:

      - Between the spine's four sub-stages (cheap, but the coarsest).
      - Between a pass's attempts -- where the biggest waits live: a single
        primary attempt was measured at 147s (DeepSeek V4 Pro) and a single
        critic attempt at 205s (Kimi K3), and each pass may take two.
      - Before each of the model client's transport retries, which are what
        turn a wedged provider into minutes of silence.

    The one window this CANNOT interrupt is a single in-flight HTTP request:
    there is no way to abort a blocking `httpx` call from another thread
    without tearing down the connection under it. So a stop takes effect at
    the next checkpoint, bounded by the client's own 120s request timeout --
    not instantly, and the UI says so rather than pretending otherwise.
    """

    def checkpoint() -> None:
        if reviews.cancel_requested(review_id, dynamodb_resource):
            raise ReviewCancelled(f"Review {review_id} was cancelled by its owner.")

    return checkpoint


def _mock_decision(review_id: str, playbook_id: str) -> dict[str, Any]:
    """Canned decision, mirroring infra/lambda/mock_review/handler.py.

    Registry-driven (issue #289) -- no playbook_id literal appears here:
      - registered, with a `mock_output_key` on its registry entry (e.g.
        eiaa) -> the DONE path, copying that pre-baked fixture.
      - registered, with no `mock_output_key` yet (e.g. synthetic-
        knowledge) -> the "playbook coming soon" MANUAL_REVIEW_REQUIRED
        copy.
      - unregistered (playbook_registry.PlaybookNotRegisteredError, a
        KeyError subclass) -> MANUAL_REVIEW_REQUIRED with the generic
        unknown-playbook copy -- caught HERE, deliberately, rather than
        left to propagate: run_mock_pipeline's own broad except-Exception
        would otherwise turn it into status ERROR, not
        MANUAL_REVIEW_REQUIRED (issue #289 AC).
    """
    try:
        entry = playbook_registry.resolve_playbook(playbook_id)
    except playbook_registry.PlaybookNotRegisteredError:
        return {
            "decision": "MANUAL_REVIEW_REQUIRED",
            "reason": "unknown_playbook",
            "output_s3_key": None,
            "summary": f"Unknown playbook_id '{playbook_id}'.",
        }

    if entry.mock_output_key:
        return {
            "decision": "REQUEST_CHANGE",
            "reason": None,
            "output_s3_key": f"outputs/{review_id}/out.docx",
            "pre_baked_source_key": entry.mock_output_key,
            "summary": "Mock review: canned REQUEST_CHANGE result.",
        }

    return {
        "decision": "MANUAL_REVIEW_REQUIRED",
        "reason": "playbook_coming_soon",
        "output_s3_key": None,
        "summary": "playbook coming soon - separate playbook later.",
    }


def _copy_output_object(result: dict[str, Any], s3_client: Any) -> bool:
    """Materialize the output .docx by copying the seeded fixture into the
    review's outputs/ prefix (redline-stage equivalent). Returns True when an
    object was written."""
    output_key = result.get("output_s3_key")
    source_key = result.get("pre_baked_source_key")
    if not output_key or not source_key:
        return False
    bucket = os.environ["OUTPUTS_BUCKET"]
    s3_client.copy_object(
        Bucket=bucket, Key=output_key, CopySource={"Bucket": bucket, "Key": source_key}
    )
    return True


def _write_terminal(review_id: str, result: dict[str, Any], object_written: bool,
                    dynamodb_resource: Any) -> None:
    """Write the terminal reviews-row state (persist-stage equivalent).
    output_s3_key is recorded only when the object was materialized."""
    decision = result["decision"]
    terminal = "DONE" if decision in ("REQUEST_CHANGE", "ACCEPT") else "MANUAL_REVIEW_REQUIRED"
    set_clauses = ["#s = :s", "decision = :d", "updated_at = :now"]
    values: dict[str, Any] = {":s": terminal, ":d": decision, ":now": str(int(time.time())),
                              ":error": "ERROR"}
    if result.get("summary") is not None:
        set_clauses.append("summary = :sum")
        values[":sum"] = result["summary"]
    if result.get("reason") is not None:
        set_clauses.append("reason = :r")
        values[":r"] = result["reason"]
    if result.get("output_s3_key") and object_written:
        set_clauses.append("output_s3_key = :o")
        values[":o"] = result["output_s3_key"]
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    try:
        table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET " + ", ".join(set_clauses),
            ConditionExpression="attribute_not_exists(#s) OR #s <> :error",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except Exception as exc:
        if not _is_conditional(exc):
            raise


def _find_submission_by_review_id(table: Any, review_id: str) -> dict[str, Any] | None:
    """Keyed lookup of the review_submissions row that owns `review_id`
    (issue #262): a full-table Scan only ever sees its first (<=1MB) page,
    so once the table outgrows one page a target row on a later page is
    silently invisible -- the reservation it owns then never settles.

    Prefer the `review_id-index` GSI (see infra/lib/nested/data-stack.ts)
    via a real boto3/moto Table.query() so the lookup is keyed regardless of
    table size or Scan-page ordering. Falls back to scan+filter only for a
    lightweight test stand-in that doesn't implement `.query()` (same
    fallback convention as reviews.py::_list_reviews_for_owner /
    disposition.py::_scan_by_owner)."""
    if hasattr(table, "query"):
        from boto3.dynamodb.conditions import Key

        resp = table.query(
            IndexName="review_id-index",
            KeyConditionExpression=Key("review_id").eq(review_id),
        )
        items = resp.get("Items", [])
        return items[0] if items else None

    resp = table.scan(
        FilterExpression="review_id = :rid", ExpressionAttributeValues={":rid": review_id}
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _settle_reservation(
    review_id: str, dynamodb_resource: Any, actual_usd_cents: int = 0
) -> None:
    """Settle the worst-case spend reservation (persist-stage equivalent),
    reusing reviews.settle_spend. Guarded so a review with no reservation, or
    one already released, is a no-op (no double-credit).

    `actual_usd_cents` (issue #415, default 0) is the REAL settled cost --
    the mock pipeline's call sites never pass one, so they keep settling at
    $0 exactly as before. `run_real_pipeline` passes the cost computed from
    its `OpenRouterModelClient`'s `cumulative_usage` (see
    `_actual_cents_from_client`), so a real review's $20/day guardrail
    finally counts what it actually spent instead of always reserving-and-
    releasing at $0.
    """
    submissions = dynamodb_resource.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
    submission = _find_submission_by_review_id(submissions, review_id)
    if not submission or not submission.get("spend_reservation_id"):
        return
    if submission.get("reservation_released"):
        return
    # settle_spend's signature is (review_id, reservation_id,
    # actual_usd_cents, dynamodb_resource).
    reviews.settle_spend(
        review_id, submission["spend_reservation_id"], actual_usd_cents, dynamodb_resource
    )
    submissions.update_item(
        Key={"idempotency_key": submission["idempotency_key"]},
        UpdateExpression="SET reservation_released = :t, updated_at = :now",
        ExpressionAttributeValues={":t": True, ":now": str(int(time.time()))},
    )


def _settle_reservation_safely(
    review_id: str, dynamodb_resource: Any, actual_usd_cents: int = 0
) -> None:
    """Settle the reservation, but never let settling FAIL THE REVIEW (#446).

    An unsettled spend reservation is an ACCOUNTING problem: the review's
    result is already computed, written to object storage, and recorded on the
    reviews row by the time this runs. Letting a settle error propagate meant
    the caller's fail-closed `except` re-terminated a review that had already
    succeeded -- on the live deployment (a `review_submissions` table missing
    `review_id-index`) that turned EVERY successful review into
    "Failed at stage persist_result", document and all.

    So the failure is logged loudly -- this is real money left reserved
    against the daily cap, and an operator has to see it -- and swallowed.
    The reservation is recoverable (the row keeps its `spend_reservation_id`
    and no `reservation_released` flag); a destroyed result is not.

    `actual_usd_cents` (issue #415, default 0) is threaded straight through
    to `_settle_reservation` -- see its docstring.
    """
    try:
        _settle_reservation(review_id, dynamodb_resource, actual_usd_cents)
    except Exception:  # noqa: BLE001 - an unsettled reservation must not fail the review
        logger.exception(
            "Failed to settle the spend reservation for review %s. The review's own "
            "result is UNAFFECTED; the reservation is still held against the daily "
            "cap and needs reconciling.",
            review_id,
        )


def _actual_cents_from_client(client: Any, dynamodb_resource: Any) -> int:
    """Actual settled cost (cents) for whatever `client` has invoked so far,
    priced from its `cumulative_usage` (issue #415).

    Returns 0 for every case where there is genuinely nothing to price:
    `client` is `None` (a failure before `_build_openrouter_client` ran --
    e.g. the #401 activation gate -- spent nothing), or `client` has no
    `cumulative_usage` attribute at all (a test double like
    `FakeBedrockClient`, or the Bedrock client -- out of scope per this
    issue's Notes, "no production caller"). `getattr` rather than an
    `isinstance` check so any object that happens to expose the attribute
    (a real client or a scripted fake) works identically.

    `cumulative_usage` is the SAME shape `last_usage` always was --
    `{"input_tokens": int, "output_tokens": int}` -- so it drops straight
    into `reviews.compute_actual_usd_cents_from_usage` as its single
    argument, exactly as that function's docstring says it must. This one
    combined total (primary + critic + every successful retry on the
    instance) is passed as the `primary_usage` slot with `critic_usage`
    left `None` -- there is no per-role split once the two passes have
    shared one client instance (issue #270), only the review's grand total,
    which is what actually needs settling.

    Safe to call after `client.close()`: `cumulative_usage` is a plain
    instance attribute, not a transport call -- only the underlying
    httpx.Client is torn down by close().
    """
    usage = getattr(client, "cumulative_usage", None)
    if not usage:
        return 0
    return reviews.compute_actual_usd_cents_from_usage(usage, None, dynamodb_resource)


def run_mock_pipeline(review_id: str, payload: dict[str, Any], *,
                      dynamodb_resource: Any, s3_client: Any) -> None:
    """Phase 1 in-process mock pipeline for one review. Injectable stores make
    it unit-testable offline. On any failure the review is moved to ERROR (the
    shared-error-handler equivalent) and the reservation is still settled."""
    playbook_id = payload.get("playbook_id", "")
    try:
        _mark_running(review_id, dynamodb_resource)
        result = _mock_decision(review_id, playbook_id)
        object_written = _copy_output_object(result, s3_client)
        _write_terminal(review_id, result, object_written, dynamodb_resource)
        _settle_reservation_safely(review_id, dynamodb_resource)
    except Exception:  # noqa: BLE001 - fail closed to ERROR, never wedge PENDING
        logger.exception("In-process mock pipeline failed for review %s", review_id)
        _fail_review(review_id, dynamodb_resource)
        _settle_reservation_safely(review_id, dynamodb_resource)


def _fail_review(review_id: str, dynamodb_resource: Any) -> None:
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    # Issue #472: stamp `failed_at` here too -- this is the mock pipeline's
    # own ERROR write, parallel to (but not routed through)
    # reviews.record_stage_failure, which stamps it for the real pipeline's
    # exception path. Same value as `updated_at`; a distinct field so the
    # Diagnostics tab can show WHEN a review failed rather than reusing
    # `created_at` (when it was submitted) under a "Failed at" header.
    now = str(int(time.time()))
    try:
        table.update_item(
            Key={"review_id": review_id},
            UpdateExpression=(
                "SET #s = :e, failing_stage = :stage, failed_at = :failed_at, updated_at = :now"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":e": "ERROR", ":stage": "inprocess_pipeline",
                ":failed_at": now, ":now": now,
            },
        )
    except Exception:  # pragma: no cover - best effort
        logger.exception("Failed to mark review %s as ERROR", review_id)


# ---------------------------------------------------------------------------
# Phase 2 real pipeline body (issue #259): the review spine (#239), driven
# by a real OpenRouterModelClient, replacing the mock's canned fixture.
# ---------------------------------------------------------------------------


def _load_opf_bundle_if_active(
    playbook_id: str, dynamodb_resource: Any, s3_client: Any
) -> dict[str, Any] | None:
    """Issue #479 step 1: if `playbook_id`'s ACTIVE `playbook_versions` row
    (issue #478's upload+activate flow, distinct from the registry's static
    `bundle_path`) carries an OPF artifact_kind, load and re-validate the
    stored, content-addressed artifact and return an `opf_bundle_v2`-shaped
    dict for `scripts/review_spine.py::run_review`. Returns `None` -- never
    raises for a routine "nothing OPF here" outcome -- when there is no
    active version row at all, or the active row is not an OPF artifact
    (`artifact_kind` absent or `"v1"`): both cases fall through to
    `_load_playbook_bundle`'s unchanged registry-disk read, exactly
    reproducing today's behavior for every playbook that has never been
    uploaded through the new flow (the shipped v1 sample) or was uploaded
    as a v1 JSON document.

    Re-validates (rather than trusting the row) via
    `src.playbook_upload._load_opf_from_bytes` -- the SAME tested,
    Path-based `opf_load.load_opf_document` call the upload route itself
    validated with (schema dispatch, `identity.content_hash` verification,
    the injection scan, sibling-id uniqueness) -- reused rather than
    re-implemented, so a byte later corrupted in object storage is caught
    here exactly as it would be on re-upload, not trusted blindly at
    review-run time.

    Purely additive and never raises for a deployment target that has not
    configured the `playbook_versions` upload flow at all -- mirrors
    `backend/src/reviews.py::_resolve_playbook_version_lineage`'s own
    "PLAYBOOK_VERSIONS_TABLE not configured -> nothing to resolve" guard,
    for the same reason: a target that never set this env var (every test
    fixture and deployment predating issue #478) must keep reading the
    registry disk path exactly as before, not raise a KeyError on an env
    var it never had reason to set.

    `overrides` is always `None` here -- issue #479 DECISION (2026-08-04):
    an earlier attempt threaded a `posture_override_system_prompt` field on
    the active `playbook_versions` row into `overrides.posture
    .system_prompt`, but nothing in the product ever writes that field (no
    upload-route param, no admin UI), so the only place it was ever set was
    a test hand-writing the DynamoDB item directly. That field is NOT
    reinstated. An empty-posture activated artifact is a legitimate,
    supported state (the real/public OPF playbooks ship `posture: {}` on
    purpose) -- `scripts/review_knowledge.py::resolve_knowledge` composes it
    using the playbook's own digest and, when supplied, the deployment's
    standing instructions (issue #482/#483) threaded in by
    `run_real_pipeline` below, never a posture override this function has
    no way to obtain.

    Raises (uncaught, fail-closed via `run_real_pipeline`'s own catch-all)
    if the row claims `artifact_kind: opf-*` but carries no `storage_key`
    (a write that should be impossible per `record_playbook_version_upload`,
    but this function does not silently fall back to the registry for it --
    that would run a review against content that is NOT what was activated)
    or if the stored bytes fail re-validation.
    """
    if not os.environ.get("PLAYBOOK_VERSIONS_TABLE"):
        return None
    active_item = playbook_versions.get_active_version_record(playbook_id, dynamodb_resource)
    if active_item is None:
        return None
    artifact_kind = active_item.get("artifact_kind") or ""
    if not artifact_kind.startswith("opf-"):
        return None
    storage_key = active_item["storage_key"]
    bucket = os.environ["UPLOADS_BUCKET"]
    raw_bytes = s3_client.get_object(Bucket=bucket, Key=storage_key)["Body"].read()
    opf_doc = playbook_upload._load_opf_from_bytes(raw_bytes, suffix=".json")
    return {
        "opf_bundle_v2": {
            "opf": opf_doc,
            "overrides": None,
            # Issue #479 fix round 2: the activated row's own recorded
            # operator decision (main.py's upload route -> `main.py`:1109 ->
            # `playbook_versions.record_playbook_version_upload`) MUST ride
            # along with the bundle it governs, or `review_knowledge
            # .resolve_knowledge`'s `accept_stub_basis` defaults to False on
            # every review and an artifact the operator explicitly accepted
            # at upload time is refused forever, with no remedy, at
            # review-composition time -- the exact "activated artifact the
            # runtime refuses, no redline, ever" failure class the ticket's
            # 2026-08-04 DECISION exists to eliminate, and worse: here the
            # acceptance IS on record and was simply being dropped.
            "accepted_stub_basis": bool(active_item.get("accepted_stub_basis", False)),
        },
        "playbook": {"metadata": {}},
    }


def _load_playbook_bundle(
    playbook_id: str, dynamodb_resource: Any = None, s3_client: Any = None
) -> dict[str, Any]:
    """Resolve `playbook_id`'s review-governing bundle (issue #479 step 1).

    An activated OPF artifact (`_load_opf_bundle_if_active`, issue #478's
    upload flow) takes precedence when one is active; otherwise this reads
    `playbook_id`'s release-bundle body off disk (the checked-in
    `playbooks/<id>.json` artifact `playbook_registry` resolves), mirroring
    scripts/eval_harness.py's load_playbook -- byte-identical to this
    function's pre-#479 behavior, and the ONLY path taken when
    `dynamodb_resource`/`s3_client` are omitted (every caller that has no
    handle to either, e.g. scripts/eval_harness.py itself, keeps working
    unchanged).

    Issue #401 (empty-shell foundation): the registry-disk branch is
    deliberately NOT the runtime-activation gate -- it is a dumb,
    unconditional read, exactly as before. `run_real_pipeline` below is
    what turned "read whichever playbook_id the payload named, no questions
    asked" into "only after reviews.verify_submission_time_bundle has
    confirmed the runtime activation record (PLAYBOOKS_TABLE
    .active_release_bundle_hash, issue #194) still calls this playbook_id's
    bundle active" -- this helper never reads playbooks/*.json (nor an OPF
    artifact) without that gate having already passed. Raises
    playbook_registry.PlaybookNotRegisteredError for a playbook_id with
    neither an active OPF version nor a registry entry -- caught by
    run_real_pipeline's fail-closed except block.
    """
    if dynamodb_resource is not None and s3_client is not None:
        opf_bundle = _load_opf_bundle_if_active(playbook_id, dynamodb_resource, s3_client)
        if opf_bundle is not None:
            return opf_bundle
    entry = playbook_registry.resolve_playbook(playbook_id)
    with open(entry.playbook_path, encoding="utf-8") as f:
        return json.load(f)


def _opf_lineage_for_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Issue #479 step 5: `identity.content_hash` + a hash of
    `identity.section_digests`, read straight off the OPF document
    `_load_opf_bundle_if_active` just loaded -- empty for a v1 bundle (no
    `opf_bundle_v2` key) or for an OPF document whose `identity` carries
    neither field (schema-valid, since only `identity.content_hash` is load-
    bearing for `load_opf_document`'s own hash-verification gate).

    `opf_section_digests_hash` -- a hash of the digests, not the raw dict --
    reuses the exact field-naming and hashing convention
    `backend/src/reviews.py::_resolve_opf_lineage` already established for
    the OTHER (registry `bundle_path`, issue #287) OPF-lineage source, so
    `get_review_detail`'s existing generic `item.get("opf_content_hash")` /
    `item.get("opf_section_digests_hash")` projection surfaces this
    source's values with no reader-side change. That resolver stamps these
    fields at SUBMISSION time from a registry `bundle_path` -- always None
    for a playbook activated purely through the #478 upload flow, since it
    has no registry `bundle_path` entry -- so this function stamping them
    again at TERMINAL time from the actually-loaded artifact never
    clobbers a value `_resolve_opf_lineage` already wrote; it is the first
    (and only) writer for this source. Returned fields are "absent, never a
    null placeholder" -- matching every other lineage resolver in this
    codebase -- so `_write_real_terminal`'s SET clause below only ever adds
    an attribute it has a real value for.
    """
    opf_bundle_v2 = bundle.get("opf_bundle_v2")
    if opf_bundle_v2 is None:
        return {}
    identity = (opf_bundle_v2.get("opf") or {}).get("identity") or {}
    lineage: dict[str, Any] = {}
    content_hash = identity.get("content_hash")
    if isinstance(content_hash, str):
        lineage["opf_content_hash"] = content_hash
    section_digests = identity.get("section_digests")
    if section_digests:
        lineage["opf_section_digests_hash"] = canonicalize.content_hash(section_digests)
    return lineage


def _floor_coverage_for_result(result: dict[str, Any]) -> dict[str, Any]:
    """Issue #479 finding (round-1 fix): an ids-only projection of
    `review_spine.run_review`'s `floor_judgment`
    (`{"verdicts": [...], "unjudged": [...]}`) safe to persist on the
    reviews row.

    Each `verdicts` entry also carries `evidence_quote` -- a short quote FROM
    THE REVIEWED DOCUMENT -- which docs/data-handling.md classifies as
    Confidential document substance, never something recorded outside
    retention-governed S3/`analysis_report` storage. This function drops it
    (and drops the invariant's own `statement`/rationale text, which
    `review_spine.run_review` never even threads this far) and keeps only
    `invariant_id` and its `violated` bool -- the same "opaque identifiers
    only, no clause text" discipline docs/data-handling.md already applies to
    retrieved `clause_ids`.

    Absent (never a null/empty placeholder) whenever `result` carries no
    `floor_judgment` key at all -- a v1 review, or an OPF review with an
    empty Floor -- so `_write_real_terminal`'s SET clause below only ever
    adds an attribute it has a real value for, matching `_opf_lineage_for_
    bundle`'s own convention. A key with an empty list is never written
    either (DynamoDB rejects an empty-list SET the same way it does an empty
    string set), so a Floor with invariants but zero violations still omits
    `floor_violated_invariant_ids` rather than writing `[]`.
    """
    floor_judgment = result.get("floor_judgment")
    if not floor_judgment:
        return {}
    verdicts = floor_judgment.get("verdicts") or []
    judged_ids = [v["invariant_id"] for v in verdicts]
    violated_ids = [v["invariant_id"] for v in verdicts if v.get("violated")]
    unjudged_ids = list(floor_judgment.get("unjudged") or [])
    coverage: dict[str, Any] = {}
    if judged_ids:
        coverage["floor_judged_invariant_ids"] = judged_ids
    if violated_ids:
        coverage["floor_violated_invariant_ids"] = violated_ids
    if unjudged_ids:
        coverage["floor_unjudged_invariant_ids"] = unjudged_ids
    return coverage


def _bundle_with_openrouter_model_ids(
    bundle: dict[str, Any], dynamodb_resource: Any = None
) -> dict[str, Any]:
    """review_spine.run_review resolves its primary/critic model ids from
    `bundle["playbook"]["metadata"]` (falling back to the Bedrock policy
    defaults) -- but the on-disk playbook bundle pins Bedrock-form model ids
    (e.g. "anthropic.claude-opus-4-8"), meaningless to OpenRouter's
    provider/model id form. Return a shallow-patched copy pointing at the
    OpenRouter ids instead, so the real chain calls OpenRouter with ids it
    actually understands.

    Which ids those are is resolved by
    `model_settings.resolve_openrouter_model_ids` (issue #445): the admin's
    selection if one is set, else OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID, else
    the model-policy/openrouter.json pin -- the same precedence the API key
    uses. Resolved PER REVIEW rather than cached at import, so changing the
    models in the admin panel takes effect on the next review instead of the
    next redeploy. `dynamodb_resource` defaults to None so a caller with no
    handle keeps exactly the pre-#445 env-var/policy behavior."""
    patched = dict(bundle)
    playbook_section = dict(patched.get("playbook", {}))
    metadata = dict(playbook_section.get("metadata", {}))
    resolved = model_settings.resolve_openrouter_model_ids(dynamodb_resource)
    metadata["primary_model_id"] = resolved["primary"]
    metadata["critic_model_id"] = resolved["critic"]
    playbook_section["metadata"] = metadata
    patched["playbook"] = playbook_section
    return patched


def _fetch_upload_bytes(payload: dict[str, Any], s3_client: Any) -> bytes:
    bucket = os.environ["UPLOADS_BUCKET"]
    key = payload["upload_s3_key"]
    return s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()


def _build_openrouter_client(
    dynamodb_resource: Any = None,
    *,
    cancel_checkpoint: Callable[[], None] | None = None,
) -> "model_client.OpenRouterModelClient":
    """Build the real OpenRouter client for one review.

    The key comes from `model_settings.resolve_openrouter_api_key`, which
    prefers the admin-set row over `OPENROUTER_API_KEY` -- so an operator can
    rotate the instance key from the admin panel without editing `.env` and
    restarting, while a deploy that only ever set the env var keeps working
    unchanged. Resolved per review rather than cached at import, so a
    rotation takes effect on the next review instead of the next restart.

    Issue #472: a missing/empty key is classified HERE, pre-call, rather than
    left to `OpenRouterModelClient.__init__`'s own defensive `ValueError` --
    that ValueError isn't a `ModelInvocationError` at all, so
    `classify_failure_reason` had no way to tell it apart from any other bug
    and recorded the least-informative `unhandled_exception` for the single
    most common first-run mistake (no key configured yet). Raising
    `ModelKeyMissingError` here means the row instead records
    `model_key_missing`, distinct from a provider-rejected key
    (`model_key_rejected`) -- two different admin fixes, now two different
    tokens.
    """
    api_key = model_settings.resolve_openrouter_api_key(dynamodb_resource)
    if not api_key:
        raise model_client.ModelKeyMissingError(
            "No OpenRouter API key is configured for this deployment."
        )
    return model_client.OpenRouterModelClient(api_key=api_key, cancel_checkpoint=cancel_checkpoint)


def _write_real_output(review_id: str, result: dict[str, Any], s3_client: Any) -> str | None:
    """PUT the spine's computed redline bytes to the same outputs/{review_id}/
    out.docx key convention the mock path uses. Returns the key when an
    object was written (REQUEST_CHANGE, or a partial-delivery
    MANUAL_REVIEW_REQUIRED per redline_generate's #203 "partial delivery,
    never instead of" contract), or None on ACCEPT / a fully fail-closed
    result (no redline_bytes)."""
    redline_bytes = result.get("redline_bytes")
    if not redline_bytes:
        return None
    output_key = f"outputs/{review_id}/out.docx"
    bucket = os.environ["OUTPUTS_BUCKET"]
    s3_client.put_object(Bucket=bucket, Key=output_key, Body=redline_bytes)
    return output_key


# Issue #416: the fields of `run_review`'s result that go into the persisted
# analysis artifact. An explicit allowlist rather than "everything except
# redline_bytes", so a future field added to the result cannot start being
# written to the data plane by accident -- some of these carry counterparty
# substance, and what gets persisted is a decision, not a default.
_ANALYSIS_FIELDS = (
    "status",
    "decision",
    "summary",
    "reason",
    "findings",
    "analysis_report",
    "confidence_band",
    "attempts",
    "critic_delta",
    "floor_judgment",
    # Issue #563: disclosure that stage 1 accepted one or more pending
    # tracked changes into the operative draft before review. Absent (never
    # a null placeholder) for a review with nothing to accept.
    "normalization_notes",
    # Issue #569: the bounded re-quote repair pass's outcome. Absent (never
    # a null placeholder) when the flag was off or nothing was eligible to
    # repair.
    "requote",
    # Issue #582 (Defect 3): `review_knowledge.ReviewKnowledge.lineage_
    # record()` -- what actually governed an OPF review, INCLUDING
    # `prompt_omissions` (substance-free: kinds and counts, never clause
    # text). Absent (never a null placeholder) for a v1 review, matching
    # `floor_judgment`'s own convention above. Was previously computed by
    # `review_spine.run_review` and then discarded -- `opf_prompt.
    # _report_omissions` printed the same information to stderr only, never
    # persisted, so a completed review's own record could not say whether
    # its `posture_source` reflected real playbook content or an empty
    # `posture: {}`/`floor: {}` shape composing to nothing.
    "opf_knowledge_lineage",
)


def _write_real_analysis(review_id: str, result: dict[str, Any], s3_client: Any) -> str:
    """PUT the full review result to `outputs/{review_id}/analysis.json`.

    `run_review` computes findings, the analysis report, the decision, the
    summary and the reason; `run_real_pipeline` kept status/decision/summary/
    redline and threw the rest away. So a review that came out wrong had
    already discarded the evidence needed to say why by the time anyone
    looked -- which is exactly why the stage-1 output-contract bugs and the
    curly-punctuation locate failure both had to be diagnosed by re-running
    the pipeline by hand against real models.

    Written for terminal MANUAL_REVIEW_REQUIRED results too. Those are
    precisely the reviews someone needs to investigate, so writing it only on
    success would miss the case the artifact exists for.

    `redline_bytes` is deliberately excluded: the .docx already lives beside
    this file, and embedding megabytes of base64 would make the artifact
    unopenable for the one job it has.

    Sorted keys and a stable separator, so two runs of the same result produce
    identical bytes and a diff between them is a real difference rather than
    dictionary ordering.

    A `put_object` failure is allowed to raise, exactly as
    `_write_real_output`'s does: the artifact must not be quietly best-effort
    while the redline is not, because a review that silently lost its evidence
    looks identical to one that kept it.

    Retention needs no new machinery -- this shares the `outputs/{review_id}/`
    prefix the redline uses, and BOTH purge implementations resolve their
    targets by listing that prefix (asserted in
    tests/test_analysis_artifact_persisted.py, not assumed).
    """
    document = {field: result.get(field) for field in _ANALYSIS_FIELDS}
    body = json.dumps(document, sort_keys=True, indent=2, default=str).encode("utf-8")
    key = f"outputs/{review_id}/analysis.json"
    s3_client.put_object(
        Bucket=os.environ["OUTPUTS_BUCKET"],
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    return key


def _model_ids_for_run(bundle: dict[str, Any]) -> dict[str, str]:
    """The primary/critic model ids this run's spine will actually resolve
    its calls from (issue #449).

    Read back off the SAME bundle metadata `_bundle_with_openrouter_model_ids`
    just wrote and `review_spine.run_review` reads (`bundle["playbook"]
    ["metadata"]`), rather than re-reading configuration -- so the pair
    recorded on the review row is the pair the review genuinely used, not a
    second, independently-resolved answer that could differ if the
    configuration moved between the two reads.
    """
    metadata = (bundle.get("playbook") or {}).get("metadata") or {}
    return {
        field: metadata[field]
        for field in ("primary_model_id", "critic_model_id")
        if metadata.get(field)
    }


def _served_model_ids_for_result(result: dict[str, Any]) -> dict[str, str]:
    """The RESPONSE-side model provenance `run_review` surfaced, if any
    (issue #514).

    `model_ids` records what each pass was ASKED to run; these record what
    the provider said it actually ran. Keeping both on the row is what makes
    "did the model I selected actually do this review?" answerable from the
    deployment's own data instead of by reading source and simulating a
    request, which is how it had to be answered on 2026-08-02.

    Absent keys are simply not returned -- a client that cannot report what
    it served (every offline fake, the Bedrock path, a provider that omits
    the field) leaves the row byte-identical to before this landed, the same
    "absent, never a null placeholder" convention `_write_real_terminal`
    uses for `model_ids` and `opf_lineage`.
    """
    return {
        field: value
        for field in ("served_primary_model_id", "served_critic_model_id")
        if isinstance(value := result.get(field), str) and value
    }


def _scan_upload_for_injection(docx_bytes: bytes) -> dict[str, Any]:
    """Ids-and-counts summary of anything in the upload addressed to an AI
    (issue #506), or `{}` when the document is clean.

    Wrapped so the scan can never fail a review: it is advisory, the
    hostile-file gauntlet is what refuses a bad package, and a tripwire that
    became a second way for a review to die would be worse than no tripwire.
    """
    try:
        return document_injection_scan.summarise(
            document_injection_scan.scan_document(docx_bytes)
        )
    except Exception:  # noqa: BLE001 - advisory: degrade, never fail the review
        logger.warning("INJECTION_SCAN: scan failed; review continues unaffected")
        return {}


def _write_real_terminal(review_id: str, result: dict[str, Any], output_s3_key: str | None,
                          dynamodb_resource: Any,
                          analysis_s3_key: str | None = None,
                          model_ids: dict[str, str] | None = None,
                          opf_lineage: dict[str, Any] | None = None,
                          floor_coverage: dict[str, Any] | None = None) -> None:
    """Write the terminal reviews-row state from a ReviewResult dict
    (scripts/review_spine.py::run_review's return contract). Unlike
    reviews.record_stage_failure (used only for an actual raised
    exception), the spine's own `status` is ALREADY the correct terminal
    status for every expected fail-closed condition (MANUAL_REVIEW_REQUIRED /
    ERROR_MANUAL_REVIEW_REQUIRED / OK) -- this just persists it verbatim,
    same "never clobbers a terminal/ERROR row" guard as the mock path's
    _write_terminal.

    `model_ids` (issue #449) is `_model_ids_for_run`'s dict: the per-step
    model provenance the History tab reads back. Omitted keys are simply not
    written -- a caller that has none (the mock path, which invokes no model)
    leaves a row byte-identical to the one this function wrote before that
    issue, and the History tab renders "not recorded" rather than guessing.

    `opf_lineage` (issue #479 step 5) is `_opf_lineage_for_bundle`'s dict:
    `opf_content_hash` / `opf_section_digests_hash`, written under the SAME
    field names `backend/src/reviews.py::_resolve_opf_lineage` already
    established for its own (different-source) OPF lineage, so
    `get_review_detail`'s existing generic projection surfaces them with no
    reader-side change. Omitted (never a null placeholder) for a v1 review,
    identically to `model_ids` above.

    `floor_coverage` (issue #479 finding, round-1 fix) is
    `_floor_coverage_for_result`'s ids-only dict: `floor_judged_invariant_
    ids` / `floor_violated_invariant_ids` / `floor_unjudged_invariant_ids`.
    Written on EVERY terminal call that has one -- including the
    `floor_invariant_unjudged` quarantine, whose `result` carries
    `floor_judgment` precisely so this survives that path too (that is the
    whole point: an operator landing on a quarantined row must be able to
    see WHICH invariant went unjudged, not just that one did). Omitted
    (never a null placeholder) for a v1 review or an OPF review with an
    empty Floor, identically to `opf_lineage` above.

    `normalization_notes` (issue #563), when `result` carries one, is
    written the SAME way `decision`/`summary`/`reason` are -- a simple,
    always-present-or-absent field straight off the ReviewResult, not a
    generic dict parameter, since it never needs the coverage-style
    ids-only projection `floor_coverage` does.

    `requote` (issue #569) is written the SAME "simple, always-present-or-
    absent" way as `normalization_notes` above -- the bounded re-quote
    repair pass's `{"attempted", "recovered", "still_failed"}` outcome,
    present only when `scripts/review_spine.py::run_review` actually ran
    that pass.

    Also stamps `failed_at` (issue #472) whenever `terminal` is not
    `reviews.REVIEW_STATUS_SUCCESS_TERMINAL` -- same epoch-second string as
    `updated_at`, same reasoning as `reviews.record_stage_failure`'s own
    stamp: a row a Diagnostics reader lands on must carry the moment IT
    failed, not the submission time it fell back to before this field
    existed. A `DONE` row gets no `failed_at`, which is correct: it didn't
    fail.
    """
    status_value = result["status"]
    terminal = "DONE" if status_value == "OK" else status_value
    now_str = str(int(time.time()))
    set_clauses = ["#s = :s", "updated_at = :now"]
    values: dict[str, Any] = {":s": terminal, ":now": now_str, ":error": "ERROR"}
    if terminal != reviews.REVIEW_STATUS_SUCCESS_TERMINAL:
        set_clauses.append("failed_at = :failed_at")
        values[":failed_at"] = now_str
    for index, (field, model_id) in enumerate(sorted((model_ids or {}).items())):
        placeholder = f":m{index}"
        set_clauses.append(f"{field} = {placeholder}")
        values[placeholder] = model_id
    for index, (field, lineage_value) in enumerate(sorted((opf_lineage or {}).items())):
        placeholder = f":l{index}"
        set_clauses.append(f"{field} = {placeholder}")
        values[placeholder] = lineage_value
    for index, (field, coverage_value) in enumerate(sorted((floor_coverage or {}).items())):
        placeholder = f":fc{index}"
        set_clauses.append(f"{field} = {placeholder}")
        values[placeholder] = coverage_value
    if result.get("decision") is not None:
        set_clauses.append("decision = :d")
        values[":d"] = result["decision"]
    if result.get("summary") is not None:
        set_clauses.append("summary = :sum")
        values[":sum"] = result["summary"]
    if result.get("reason") is not None:
        set_clauses.append("reason = :r")
        values[":r"] = result["reason"]
    # Issue #563: disclosure that stage 1 accepted one or more pending
    # tracked changes into the operative draft before review, same "absent,
    # never a null placeholder" convention as decision/summary/reason above
    # -- run_review only ever sets this key on the result when there is
    # something to disclose.
    if result.get("normalization_notes") is not None:
        set_clauses.append("normalization_notes = :nn")
        values[":nn"] = result["normalization_notes"]
    # Issue #569: the bounded re-quote repair pass's outcome
    # (attempted/recovered/still_failed), same "absent, never a null
    # placeholder" convention as normalization_notes above -- run_review
    # only ever sets this key when the flag was on AND something was
    # eligible to repair. `backend/src/reviews.py::get_review_detail` reads
    # this straight off the row (`item.get("requote")`).
    if result.get("requote") is not None:
        set_clauses.append("requote = :rq")
        values[":rq"] = result["requote"]
    if output_s3_key is not None:
        set_clauses.append("output_s3_key = :o")
        values[":o"] = output_s3_key
    # Issue #416: the pointer to the persisted analysis artifact, stamped the
    # same way `output_s3_key` is. Absent (never a null placeholder) for the
    # mock path, which is out of scope and writes no artifact.
    if analysis_s3_key is not None:
        set_clauses.append("analysis_s3_key = :aj")
        values[":aj"] = analysis_s3_key
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    try:
        table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET " + ", ".join(set_clauses),
            ConditionExpression="attribute_not_exists(#s) OR #s <> :error",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except Exception as exc:
        if not _is_conditional(exc):
            raise


def run_real_pipeline(review_id: str, payload: dict[str, Any], *, dynamodb_resource: Any,
                      s3_client: Any, model_client: Any = None) -> None:
    """Phase 2 in-process real pipeline for one review: drives the composed
    review spine (scripts/review_spine.py::run_review, issue #239) with a
    real model client, replacing run_mock_pipeline's canned fixture with a
    genuinely computed decision + redline. `model_client` is injectable so
    tests drive this fully offline with FakeBedrockClient instead of a live
    OpenRouter call (standing rule 4: no network in any test); production
    leaves it unset and a real OpenRouterModelClient is built by
    _build_openrouter_client (the admin-set key, else OPENROUTER_API_KEY).

    Issue #401 (empty-shell foundation): before loading any playbook
    content, this re-resolves the active release bundle from the runtime
    activation record via reviews.verify_submission_time_bundle -- the
    existing, previously-unwired ARCHITECTURE.md step-10 check
    ("Retired-bundle-before-start behavior") -- rather than trusting the
    submission-time hash blindly. This is what makes it true, for the
    Docker Compose in-process runner, that "no code path reads a
    hard-coded playbooks/*.json for the active ruleset": _load_playbook_bundle
    itself is unchanged (still a dumb disk read), but it is only ever
    reached after the runtime record has confirmed this playbook_id's
    bundle is genuinely still active. Not verified (the bundle was
    deactivated/superseded between submission and this execution starting,
    or -- the empty-playbook-store case -- nothing is active at all) ->
    verify_submission_time_bundle has already written the terminal
    QUARANTINED status + reason to the reviews row itself; this function
    settles the reservation and stops, never falling through to load_playbook
    / run_review against content that is no longer (or never was) active.

    On any unhandled exception (S3/DDB failure, model transport error, an
    unregistered playbook_id, ...) the review is moved to a terminal state
    via the SHARED reviews.record_stage_failure (issue #258), tagged with
    the actual stage that failed, and the reservation is still settled --
    never left wedged in PENDING/RUNNING. An EXPECTED fail-closed result
    from run_review itself (e.g. MANUAL_REVIEW_REQUIRED) is not an
    exception -- it is persisted directly via _write_real_terminal using
    the status run_review already computed.

    Issue #446: that fail-closed handler can no longer destroy a review that
    already SUCCEEDED. Two things changed. `reviews.record_stage_failure` is
    now guarded and refuses to overwrite a `DONE` row, and settling the spend
    reservation goes through `_settle_reservation_safely`, which cannot throw
    at all -- so a settle error (the live incident: a `review_submissions`
    table missing `review_id-index`) is logged as the accounting problem it is
    instead of re-terminating a review whose redline is already written and
    downloadable.

    Issue #447: this is also where LIVE progress comes from. `stage` above is
    a local variable persisted only on FAILURE, and the four sub-stages a
    waiting user cares about are not these stages at all -- they are inside
    `run_review`, which this function sees as one opaque `stage =
    "run_review"`. So the spine's new `on_progress` seam is wired to
    `_write_progress_stage`, publishing primary_pass -> critic_pass ->
    reconciliation -> redline onto the reviews row AS EACH ONE STARTS. The
    UI's "Step 2 of 4" is therefore a fact about the pipeline, never a
    function of elapsed time. That write cannot fail the review.
    """
    playbook_id = payload.get("playbook_id", "")
    stage = "mark_running"
    # Issue #527: declared OUTSIDE the try so the fail-closed `except` below
    # can always pass SOMETHING to `record_stage_failure` -- {} for a
    # failure before `load_playbook` resolves it (no model was ever
    # selected), the real pair once that stage has run. Previously
    # `model_ids` only existed inside the try's local scope, so an
    # unhandled exception (including one raised BY a model call) landed on
    # the reviews row with neither `primary_model_id` nor `critic_model_id`
    # recorded -- an ERROR row with no model provenance is unusable for a
    # "which model did this?" Diagnostics follow-up.
    model_ids: dict[str, str] = {}
    # Issue #479 step 5: same "declared outside the try" reasoning as
    # `model_ids` above -- {} before `load_playbook` resolves the bundle
    # (nothing to stamp yet), the real OPF identity pair once it has, for
    # whichever terminal write actually happens.
    opf_lineage: dict[str, Any] = {}
    # Issue #415: same "declared outside the try" reasoning as `model_ids`/
    # `opf_lineage` above -- None before `build_model_client` runs (nothing
    # has been invoked yet, so nothing was spent), the real client instance
    # once it has, for EVERY settle call site below (success, cancelled, and
    # the fail-closed except alike) to read `cumulative_usage` from via
    # `_actual_cents_from_client`.
    client: Any = None
    try:
        _mark_running(review_id, dynamodb_resource)

        # The review sat in a bounded worker-pool queue before this thread
        # picked it up, so the very first thing worth asking is whether the
        # reviewer already gave up on it -- cancelling a queued review must
        # cost zero model spend, not one full pass.
        cancel_checkpoint = _make_cancel_checkpoint(review_id, dynamodb_resource)
        cancel_checkpoint()

        stage = "verify_active_bundle"
        verify_result = reviews.verify_submission_time_bundle(
            review_id, playbook_id, payload.get("release_bundle_hash") or "", dynamodb_resource
        )
        if not verify_result.get("verified", False):
            _settle_reservation_safely(review_id, dynamodb_resource)
            return

        stage = "load_playbook"
        bundle = _bundle_with_openrouter_model_ids(
            _load_playbook_bundle(playbook_id, dynamodb_resource, s3_client), dynamodb_resource
        )
        # Issue #449: capture the per-step model provenance for the terminal
        # write, from the bundle the spine is about to read it from.
        model_ids = _model_ids_for_run(bundle)
        # Issue #479 step 5: capture the OPF identity lineage (empty for a
        # v1 bundle) from the SAME bundle, before run_review consumes it.
        opf_lineage = _opf_lineage_for_bundle(bundle)

        stage = "fetch_upload"
        docx_bytes = _fetch_upload_bytes(payload, s3_client)

        stage = "build_model_client"
        built_client = model_client is None
        client = model_client or _build_openrouter_client(
            dynamodb_resource, cancel_checkpoint=cancel_checkpoint
        )

        # Issue #506: advisory only, and deliberately NOT threaded into
        # `run_review`. Injecting "this document is suspicious" into the model's
        # context would itself be a manipulation surface -- an attacker who can
        # get text into the document can then aim it at that sentence -- and
        # the untrusted delimiter already carries the instruction-immunity
        # contract. The findings go to the row and to the human; never to the
        # model, and never to a decision.
        injection_summary = _scan_upload_for_injection(docx_bytes)

        # Issue #506: advisory only, and deliberately NOT threaded into
        # `run_review`. Injecting "this document is suspicious" into the model's
        # context would itself be a manipulation surface -- an attacker who can
        # get text into the document can then aim it at that sentence -- and
        # the untrusted delimiter already carries the instruction-immunity
        # contract. The findings go to the row and to the human; never to the
        # model, and never to a decision.
        injection_summary = _scan_upload_for_injection(docx_bytes)

        stage = "run_review"
        # Issue #398: the optional per-review free-text guidance threaded
        # from POST /api/reviews (backend/src/reviews.py's execution-input
        # payload) through to the primary + critic passes. `.get(...) or ""`
        # rather than a bare `.get(..., "")` so an explicit `null` in an
        # older/hand-built payload degrades to the same "no guidance"
        # default as a genuinely absent key, never a TypeError downstream.
        toaster_guidance = payload.get("toaster_guidance") or ""
        # Issue #520: the audience this review's footnotes are written for,
        # read out of the execution payload exactly as `toaster_guidance` is.
        # `or DEFAULT` covers a payload written before the field existed --
        # a re-driven older execution must not crash on a missing key.
        notes_mode = payload.get("notes_mode") or reviews.DEFAULT_NOTES_MODE
        # Issue #483 (epic #481): the playbook's standing instructions, ALREADY
        # resolved once at submission time (backend/src/reviews.py's
        # _resolve_instructions_lineage, issue #482) and carried verbatim in
        # this same execution-input payload -- never re-read from the
        # instructions store here, which would reopen the mid-flight-save
        # split brain #482 forbids. `.get(...) or ""` mirrors toaster_guidance
        # above: an older payload (pre-#482) or a playbook with nothing ever
        # saved has no `instructions_text` key at all, and degrades to the
        # same "no standing instructions" default as an explicit empty string.
        instructions_text = payload.get("instructions_text") or ""
        # Issue #582 investigation (Defect 2): `run_review`'s `policy=` param
        # is deliberately NOT threaded here. This is not a missed wire -- it
        # was checked, end to end, and there is genuinely no review-policy
        # ARTIFACT reachable at review time in this deployment to thread:
        #   - `backend/src/playbook_versions.py`'s upload+activate flow
        #     (issue #478, what `_load_opf_bundle_if_active` above actually
        #     reads) has no policy field anywhere -- `record_playbook_
        #     version_upload` accepts none, and there is no separate policy
        #     upload route in `backend/src/main.py`.
        #   - The registry (`playbooks/registry.json`) has no entry with a
        #     `bundle_path` set today, so `backend/src/reviews.py::
        #     _resolve_opf_lineage` (the ONE place that reads a bound
        #     bundle's `review_policy` LINEAGE metadata -- path/version/
        #     hash/approval_status, never the rule content) always resolves
        #     to its all-None defaults in this deployment.
        #   - `scripts/bind_bundle.py` / `scripts/policy_load.py` (which DO
        #     know how to load a full policy document) are dev-time CLI /
        #     library code with no caller anywhere under `backend/src/` --
        #     confirmed by grep, not assumed.
        #   - The one committed policy artifact, `playbooks/nda-policy-v1
        #     .json`, is for playbook_id `"nda"` -- a registered-but-inactive
        #     stub with no `anchor_map_path`, distinct from
        #     `"synthetic-nda-sample"` (this registry's actual
        #     `default_playbook_id`) -- and its own `approval.note`
        #     documents that it governs no production review.
        # Fabricating a policy load here would invent a source that does not
        # exist. Per the 2026-08-04 #479 DECISION's own doctrine (an empty
        # posture is a valid, honestly-recorded state, not something to
        # paper over), the correct fix is making the absence VISIBLE rather
        # than silently wiring nothing: `scripts/opf_prompt.py`'s Binding/
        # Guidance omissions are now recorded (not just printed to stderr)
        # onto `review_knowledge.ReviewKnowledge.lineage_record()`, surfaced
        # on `run_review`'s own result as `opf_knowledge_lineage`, and
        # persisted via `_ANALYSIS_FIELDS` below -- see that field's own
        # comment. `policy=None` (the default) is passed implicitly by
        # omission, exactly as before this investigation.
        try:
            result = review_spine.run_review(
                docx_bytes,
                bundle,
                client,
                review_id=review_id,
                toaster_guidance=toaster_guidance,
                notes_mode=notes_mode,
                instructions_text=instructions_text,
                # Issue #447: publish the spine's real sub-stage
                # (primary_pass -> critic_pass -> reconciliation -> redline)
                # onto the reviews row as it happens, so the polling UI can
                # say "Step 2 of 4" truthfully instead of animating a bar
                # that carries no information. _write_progress_stage cannot
                # throw -- see its docstring.
                on_progress=lambda stage_token: _write_progress_stage(
                    review_id, stage_token, dynamodb_resource
                ),
                # Issue #414: the only caller that wired this before was the
                # offline eval harness -- the real pipeline itself never
                # supplied one, so `run_review`'s default no-op silently
                # dropped every ModelInvocationRecord the primary/critic
                # passes ledgered. `invocation_ledger.make_ledger_write`
                # persists them into MODEL_INVOCATIONS_TABLE and, like
                # `_write_progress_stage` above, can never fail this review --
                # every exception is caught and logged inside it.
                ledger_write=invocation_ledger.make_ledger_write(review_id, dynamodb_resource),
                # The sub-stage boundary is also the natural cancel checkpoint:
                # the spine already stops here to report progress, so asking
                # "still wanted?" in the same place costs one consistent read
                # per stage and needs no new seam. The passes and the model
                # client take this SAME callable (threaded on from here), which
                # is what makes a stop land mid-pass instead of only between
                # stages -- the difference between cancelling a wedged review
                # in seconds and waiting out both of its attempts.
                cancel_checkpoint=cancel_checkpoint,
            )
        finally:
            # issue #270: a real OpenRouterModelClient now owns a single
            # reused httpx.Client (connection reuse across the primary +
            # critic invoke() calls in one review) instead of one per call --
            # close it once this review is done with it. Only close a client
            # THIS call built; an injected client (tests) is the caller's.
            if built_client:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

        stage = "persist_result"
        output_s3_key = _write_real_output(review_id, result, s3_client)
        # Issue #416: the evidence needed to investigate a wrong review, which
        # `run_review` computes and this runner used to discard. Written
        # BEFORE the terminal row, so a row carrying `analysis_s3_key` always
        # names an object that exists -- the same ordering `_write_real_output`
        # already establishes for the redline.
        analysis_s3_key = _write_real_analysis(review_id, result, s3_client)
        # Issue #479 finding (round-1 fix): the ids-only Floor-coverage
        # projection of THIS result -- present for an OPF review that had
        # invariants to judge, empty otherwise -- computed from `result`
        # itself (never re-derived from `bundle`) so it reflects exactly
        # what `run_review` actually judged for this run, including the
        # `floor_invariant_unjudged` quarantine path.
        floor_coverage = _floor_coverage_for_result(result)
        _write_real_terminal(
            review_id, result, output_s3_key, dynamodb_resource,
            analysis_s3_key=analysis_s3_key,
            # Issue #514: the row records the request side (`model_ids`) and
            # now the response side too. Merged rather than passed
            # separately because `_write_real_terminal` already writes this
            # dict generically, field by field -- a second parameter would
            # buy nothing but another loop. Empty when the client cannot
            # report it, which leaves the row exactly as it was before.
            # Issue #506: the injection summary rides the SAME generic
            # field-by-field write, so it needs no new parameter and no new
            # loop either. Empty for a clean document, which leaves the row
            # byte-identical to before this landed.
            model_ids={
                **model_ids,
                **_served_model_ids_for_result(result),
                **injection_summary,
            },
            opf_lineage=opf_lineage, floor_coverage=floor_coverage,
        )
        # Settling is deliberately the LAST thing, and deliberately cannot
        # throw (issue #446): by this point the redline is in object storage
        # and the terminal row is written, so an accounting failure must not
        # reach the fail-closed `except` below and re-terminate a review that
        # already succeeded.
        #
        # Issue #415: settle at the REAL cost of this review -- the client's
        # cumulative_usage across both passes (and every retry) -- instead
        # of the hardcoded $0 that only the mock pipeline should ever use.
        _settle_reservation_safely(
            review_id, dynamodb_resource, _actual_cents_from_client(client, dynamodb_resource)
        )
    except ReviewCancelled:
        # Caught BEFORE the fail-closed handler below, and handled completely
        # differently: nothing failed here. No `record_stage_failure`, so no
        # `failing_stage`/`reason` lands on the row and Diagnostics does not
        # grow a phantom incident every time someone stops a review they no
        # longer want. The spend reservation is still settled -- the tokens
        # spent before the stop were real, priced from whatever
        # cumulative_usage the client accumulated before the stop landed
        # (issue #415) -- `client` is still `None` if the stop happened
        # before `build_model_client` even ran, which correctly settles at 0.
        logger.info(
            "Review %s cancelled by its owner at stage %s; stopping.", review_id, stage
        )
        reviews.mark_cancelled(review_id, dynamodb_resource)
        _settle_reservation_safely(
            review_id, dynamodb_resource, _actual_cents_from_client(client, dynamodb_resource)
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, never wedge PENDING/RUNNING
        # Still one catch-all -- failing closed is deliberate. What changes
        # (issue #442) is that the cause is CLASSIFIED here instead of being
        # discarded into the container log: the review row now records WHY it
        # failed, not merely that it did. An unrecognised exception still
        # records `unhandled_exception`, so no path is worse than before.
        reason = classify_failure_reason(exc)
        logger.exception(
            "In-process real pipeline failed for review %s at stage %s (reason %s)",
            review_id,
            stage,
            reason,
        )
        # Issue #527: stamp whatever model provenance is known at the point
        # of failure -- {} before `load_playbook` has run, the real pair
        # after -- so an ERROR row (e.g. `model_empty_content`,
        # `model_output_truncated`) still records WHICH models were in play,
        # the same provenance a successful row gets via `_write_real_terminal`.
        reviews.record_stage_failure(
            review_id, stage, reason, dynamodb_resource, model_ids=model_ids
        )
        # Issue #415: a review that burned primary-pass (and/or critic-pass)
        # tokens before failing still spent real money -- settle at whatever
        # `client.cumulative_usage` accumulated before the failure, not $0.
        # `client` is still `None` for a failure before `build_model_client`
        # ran (e.g. `verify_active_bundle`, `load_playbook`, `fetch_upload`),
        # which correctly settles at 0 -- no client, no spend. Reading
        # `cumulative_usage` here is safe even though the inner `finally`
        # above already closed the client (when this call built it): it is a
        # plain instance attribute, not a transport call.
        _settle_reservation_safely(
            review_id, dynamodb_resource, _actual_cents_from_client(client, dynamodb_resource)
        )


# ---------------------------------------------------------------------------
# In-process Step Functions client (duck-typed transport).
# ---------------------------------------------------------------------------


class InProcessStepFunctionsClient:
    """Duck-typed stand-in for the boto3 Step Functions client used by
    reviews.ensure_execution_started. start_execution enqueues the review onto
    a bounded background-worker pool and returns an executionArn.

    `runner` is the per-review pipeline body; it defaults to run_mock_pipeline
    with freshly-constructed config-aware DynamoDB/S3 clients, and is injectable
    for tests. `pool` is injectable so tests can run synchronously.
    """

    exceptions = _InProcExceptions()

    def __init__(
        self,
        *,
        runner: Callable[[str, dict[str, Any]], None] | None = None,
        max_concurrency: int = _MAX_CONCURRENCY,
        pool: Any = None,
    ) -> None:
        self._runner = runner or self._default_runner
        self._pool = pool or ThreadPoolExecutor(
            max_workers=max_concurrency, thread_name_prefix="pipeline"
        )
        self._started: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _default_runner(review_id: str, payload: dict[str, Any]) -> None:
        # config.model_provider() (MODEL_PROVIDER env var) is the "flag/env
        # var" that selects the real pipeline (issue #259): "openrouter"
        # runs the composed review spine against a live OpenRouterModelClient;
        # anything else (including unset, the default) keeps the Phase 1
        # mock body -- so existing deployments/tests that never set
        # MODEL_PROVIDER are unaffected.
        if config.model_provider() == "openrouter":
            run_real_pipeline(
                review_id, payload, dynamodb_resource=_ddb_resource(), s3_client=_s3_client()
            )
        else:
            run_mock_pipeline(
                review_id, payload, dynamodb_resource=_ddb_resource(), s3_client=_s3_client()
            )

    def start_execution(self, *, stateMachineArn: str, name: str, input: str) -> dict[str, Any]:  # noqa: A002,N803
        with self._lock:
            if name in self._started:
                raise ExecutionAlreadyExists(f"execution {name!r} already started")
            self._started.add(name)
        payload = json.loads(input)
        review_id = payload["review_id"]
        self._pool.submit(self._runner, review_id, payload)
        return {"executionArn": f"inprocess:{name}", "startDate": int(time.time())}


# Module-level singleton: one worker pool per process, not per request.
_SINGLETON_LOCK = threading.Lock()
_SINGLETON: InProcessStepFunctionsClient | None = None


def get_inprocess_sfn_client() -> InProcessStepFunctionsClient:
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = InProcessStepFunctionsClient()
    return _SINGLETON
