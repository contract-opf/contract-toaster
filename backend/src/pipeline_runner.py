"""
In-process pipeline runner (DTS deployment target, Phase 1).

The AWS deployment drives the review pipeline with Step Functions: the backend
calls `sfn_client.start_execution(...)` (via reviews.ensure_execution_started)
and the Lambda stages carry the review through PENDING -> RUNNING -> terminal.

The DTS deployment has no Step Functions. Rather than change submit_review /
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
built from `OPENROUTER_API_KEY` / `OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID`.
`InProcessStepFunctionsClient`'s default runner picks between the two bodies
per review based on `config.model_provider()` (`MODEL_PROVIDER` env var):
`openrouter` selects the real body, anything else (including unset) keeps
the Phase 1 mock body -- so existing tests/deployments that never set
`MODEL_PROVIDER` are unaffected. On any unhandled exception the real body
calls the SHARED `reviews.record_stage_failure` (issue #258) with the actual
failing stage name, exactly the AWS error-handler Lambda's contract, instead
of leaving the review PENDING/RUNNING forever.

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
    from src import config, model_client, reviews
except ImportError:  # pragma: no cover
    import config  # type: ignore[no-redef]
    import model_client  # type: ignore[no-redef]
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

import playbook_registry  # noqa: E402
import review_spine  # noqa: E402

logger = logging.getLogger(__name__)

_WATERMARK = "tool recommendation only - attorney approval required"

# In-process concurrency cap -- the semaphore equivalent for a single container.
_MAX_CONCURRENCY = int(os.environ.get("PIPELINE_MAX_CONCURRENCY", "5"))


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
        _settle_reservation(review_id, dynamodb_resource)
    except Exception:  # noqa: BLE001 - fail closed to ERROR, never wedge PENDING
        logger.exception("In-process mock pipeline failed for review %s", review_id)
        _fail_review(review_id, dynamodb_resource)
        _settle_reservation(review_id, dynamodb_resource)


def _fail_review(review_id: str, dynamodb_resource: Any) -> None:
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    try:
        table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET #s = :e, failing_stage = :stage, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":e": "ERROR", ":stage": "inprocess_pipeline", ":now": str(int(time.time()))
            },
        )
    except Exception:  # pragma: no cover - best effort
        logger.exception("Failed to mark review %s as ERROR", review_id)


# ---------------------------------------------------------------------------
# Phase 2 real pipeline body (issue #259): the review spine (#239), driven
# by a real OpenRouterModelClient, replacing the mock's canned fixture.
# ---------------------------------------------------------------------------


def _load_playbook_bundle(playbook_id: str) -> dict[str, Any]:
    """Load the active playbook bundle straight off disk by playbook_id,
    mirroring scripts/eval_harness.py's load_playbook. Resolving *which*
    release-bundle version is active (backend/src/playbook_versions.py) is
    a persistence/versioning concern out of scope here -- same as
    review_spine.py's own docstring notes for `bundle` -- so this reads the
    checked-in playbooks/<id>.json content the spine already treats as
    canonical. Raises playbook_registry.PlaybookNotRegisteredError for an
    unregistered playbook_id (e.g. "nda", not yet a real reviewable
    playbook) -- caught by run_real_pipeline's fail-closed except block."""
    entry = playbook_registry.resolve_playbook(playbook_id)
    with open(entry.playbook_path, encoding="utf-8") as f:
        return json.load(f)


def _bundle_with_openrouter_model_ids(bundle: dict[str, Any]) -> dict[str, Any]:
    """review_spine.run_review resolves its primary/critic model ids from
    `bundle["playbook"]["metadata"]` (falling back to the Bedrock policy
    defaults) -- but the on-disk playbook bundle pins Bedrock-form model ids
    (e.g. "anthropic.claude-opus-4-8"), meaningless to OpenRouter's
    provider/model id form. Return a shallow-patched copy pointing at the
    OpenRouter policy's model ids (model-policy/openrouter.json, overridable
    per-deployment via OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID) instead, so the
    real chain calls OpenRouter with ids it actually understands."""
    patched = dict(bundle)
    playbook_section = dict(patched.get("playbook", {}))
    metadata = dict(playbook_section.get("metadata", {}))
    metadata["primary_model_id"] = model_client.openrouter_primary_model_id()
    metadata["critic_model_id"] = model_client.openrouter_critic_model_id()
    playbook_section["metadata"] = metadata
    patched["playbook"] = playbook_section
    return patched


def _fetch_upload_bytes(payload: dict[str, Any], s3_client: Any) -> bytes:
    bucket = os.environ["UPLOADS_BUCKET"]
    key = payload["upload_s3_key"]
    return s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()


def _build_openrouter_client() -> "model_client.OpenRouterModelClient":
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
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


def _write_real_terminal(review_id: str, result: dict[str, Any], output_s3_key: str | None,
                          dynamodb_resource: Any) -> None:
    """Write the terminal reviews-row state from a ReviewResult dict
    (scripts/review_spine.py::run_review's return contract). Unlike
    reviews.record_stage_failure (used only for an actual raised
    exception), the spine's own `status` is ALREADY the correct terminal
    status for every expected fail-closed condition (MANUAL_REVIEW_REQUIRED /
    ERROR_MANUAL_REVIEW_REQUIRED / OK) -- this just persists it verbatim,
    same "never clobbers a terminal/ERROR row" guard as the mock path's
    _write_terminal."""
    status_value = result["status"]
    terminal = "DONE" if status_value == "OK" else status_value
    set_clauses = ["#s = :s", "updated_at = :now"]
    values: dict[str, Any] = {":s": terminal, ":now": str(int(time.time())), ":error": "ERROR"}
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
    leaves it unset and a real OpenRouterModelClient is built from
    OPENROUTER_API_KEY.

    On any unhandled exception (S3/DDB failure, model transport error, an
    unregistered playbook_id, ...) the review is moved to a terminal state
    via the SHARED reviews.record_stage_failure (issue #258), tagged with
    the actual stage that failed, and the reservation is still settled --
    never left wedged in PENDING/RUNNING. An EXPECTED fail-closed result
    from run_review itself (e.g. MANUAL_REVIEW_REQUIRED) is not an
    exception -- it is persisted directly via _write_real_terminal using
    the status run_review already computed.
    """
    playbook_id = payload.get("playbook_id", "")
    stage = "mark_running"
    try:
        _mark_running(review_id, dynamodb_resource)

        stage = "load_playbook"
        bundle = _bundle_with_openrouter_model_ids(_load_playbook_bundle(playbook_id))

        stage = "fetch_upload"
        docx_bytes = _fetch_upload_bytes(payload, s3_client)

        stage = "build_model_client"
        built_client = model_client is None
        client = model_client or _build_openrouter_client()

        stage = "run_review"
        try:
            result = review_spine.run_review(docx_bytes, bundle, client, review_id=review_id)
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
        _write_real_terminal(review_id, result, output_s3_key, dynamodb_resource)
        _settle_reservation(review_id, dynamodb_resource)
    except Exception:  # noqa: BLE001 - fail closed, never wedge PENDING/RUNNING
        logger.exception(
            "In-process real pipeline failed for review %s at stage %s", review_id, stage
        )
        reviews.record_stage_failure(review_id, stage, "unhandled_exception", dynamodb_resource)
        _settle_reservation(review_id, dynamodb_resource)


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
