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
    from src import config, model_client, model_settings, playbook_upload, playbook_versions, reviews
except ImportError:  # pragma: no cover
    import config  # type: ignore[no-redef]
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
import playbook_registry  # noqa: E402
import review_spine  # noqa: E402

logger = logging.getLogger(__name__)

_WATERMARK = "tool recommendation only - attorney approval required"

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


def _settle_reservation(review_id: str, dynamodb_resource: Any) -> None:
    """Settle the worst-case spend reservation (persist-stage equivalent),
    reusing reviews.settle_spend. Guarded so a review with no reservation, or
    one already released, is a no-op (no double-credit)."""
    submissions = dynamodb_resource.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
    submission = _find_submission_by_review_id(submissions, review_id)
    if not submission or not submission.get("spend_reservation_id"):
        return
    if submission.get("reservation_released"):
        return
    # Mock pipeline settles at $0 actual spend. settle_spend's signature is
    # (review_id, reservation_id, actual_usd_cents, dynamodb_resource).
    reviews.settle_spend(review_id, submission["spend_reservation_id"], 0, dynamodb_resource)
    submissions.update_item(
        Key={"idempotency_key": submission["idempotency_key"]},
        UpdateExpression="SET reservation_released = :t, updated_at = :now",
        ExpressionAttributeValues={":t": True, ":now": str(int(time.time()))},
    )


def _settle_reservation_safely(review_id: str, dynamodb_resource: Any) -> None:
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
    """
    try:
        _settle_reservation(review_id, dynamodb_resource)
    except Exception:  # noqa: BLE001 - an unsettled reservation must not fail the review
        logger.exception(
            "Failed to settle the spend reservation for review %s. The review's own "
            "result is UNAFFECTED; the reservation is still held against the daily "
            "cap and needs reconciling.",
            review_id,
        )


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


def _build_openrouter_client(dynamodb_resource: Any = None) -> "model_client.OpenRouterModelClient":
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
    return model_client.OpenRouterModelClient(api_key=api_key)


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


def _write_real_terminal(review_id: str, result: dict[str, Any], output_s3_key: str | None,
                          dynamodb_resource: Any,
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
    if output_s3_key is not None:
        set_clauses.append("output_s3_key = :o")
        values[":o"] = output_s3_key
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
    try:
        _mark_running(review_id, dynamodb_resource)

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
        client = model_client or _build_openrouter_client(dynamodb_resource)

        stage = "run_review"
        # Issue #398: the optional per-review free-text guidance threaded
        # from POST /api/reviews (backend/src/reviews.py's execution-input
        # payload) through to the primary + critic passes. `.get(...) or ""`
        # rather than a bare `.get(..., "")` so an explicit `null` in an
        # older/hand-built payload degrades to the same "no guidance"
        # default as a genuinely absent key, never a TypeError downstream.
        toaster_guidance = payload.get("toaster_guidance") or ""
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
        try:
            result = review_spine.run_review(
                docx_bytes,
                bundle,
                client,
                review_id=review_id,
                toaster_guidance=toaster_guidance,
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
        # Issue #479 finding (round-1 fix): the ids-only Floor-coverage
        # projection of THIS result -- present for an OPF review that had
        # invariants to judge, empty otherwise -- computed from `result`
        # itself (never re-derived from `bundle`) so it reflects exactly
        # what `run_review` actually judged for this run, including the
        # `floor_invariant_unjudged` quarantine path.
        floor_coverage = _floor_coverage_for_result(result)
        _write_real_terminal(
            review_id, result, output_s3_key, dynamodb_resource,
            model_ids=model_ids, opf_lineage=opf_lineage, floor_coverage=floor_coverage,
        )
        # Settling is deliberately the LAST thing, and deliberately cannot
        # throw (issue #446): by this point the redline is in object storage
        # and the terminal row is written, so an accounting failure must not
        # reach the fail-closed `except` below and re-terminate a review that
        # already succeeded.
        _settle_reservation_safely(review_id, dynamodb_resource)
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
        _settle_reservation_safely(review_id, dynamodb_resource)


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
