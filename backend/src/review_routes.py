"""
Review API route handlers — issue #84 (submit/list/detail/download).

Wires the already-implemented, previously-uncalled functions
(`src.reviews.resolve_active_release_bundle_hash` / `submit_review` /
`list_reviews` / `get_review_detail`, `src.upload_validation
.run_upload_gauntlet`, `src.download.generate_presigned_download_url`) into
a real FastAPI `APIRouter`:

  POST   /api/reviews              multipart .docx upload -> hostile-file
                                    gauntlet -> idempotent submission ->
                                    202 + review id (ARCHITECTURE.md data
                                    flow steps 1-8; issue #59's reconciled
                                    idempotency spec).
  GET    /api/reviews              caller's own reviews; admin: all.
  GET    /api/reviews/{review_id}  status + result payload (provenance,
                                    critic deltas, confidence band for
                                    #35/#36); owner-or-admin, non-owner is a
                                    404 (non-enumerable, see
                                    `reviews.get_review_detail`).
  GET    /api/reviews/{review_id}/output   scoped presigned download
                                    (issue #71 AC2/AC5); owner-or-admin,
                                    non-owner is a 403 (unchanged existing
                                    `download.py` behavior); audited.

Issue #186 ("No user-facing review flow exists") depends on THIS ticket and
owns mounting this router onto `src.main.app` plus the minimal
upload/poll/download frontend UI -- see that issue's "Dependencies"
section: "this ticket mounts those implemented handlers into main.py ...
the handlers must exist first." This module is deliberately NOT imported
by `src/main.py` yet; it is fully self-contained and independently
testable (see tests/test_review_api_84.py, which mounts `router` onto its
own local `FastAPI()` app) so #186 only has to add one
`app.include_router(review_routes.router)` line plus the frontend work.

MVP scope note (epic #123 / issue #84's "MVP scope" comment): the pipeline
stages this router's routes front (extraction/primary/critic/redline,
#80-#83) are real and closed, but the Step Functions state machine
(infra/lib/nested/pipeline-stack.ts) is not yet rewired from the mock
review stage to invoke them -- that infra rewiring is explicitly out of
scope here (this ticket is backend/src + scripts + tests only). This
router's job is authorization/idempotency/audit-correct REST plumbing: it
faithfully submits to, and reads back from, whatever the `reviews` /
`review_submissions` DynamoDB rows hold, regardless of which pipeline
variant populated them.

New runtime dependency (issue #84): `python-multipart` is required by
FastAPI/Starlette to parse the multipart POST body this router's upload
route accepts; added to backend/requirements.txt. `boto3` was already an
unconditional import in `src/main.py` but was missing from
backend/requirements.txt (a pre-existing gap); this router also needs it,
so it is declared there now too.

Environment variables consumed (in addition to the ones src/reviews.py,
src/upload_validation.py, and src/download.py already document):
  UPLOADS_BUCKET   S3 bucket the multipart upload is written to, at
                   uploads/{owner_sub}/{review_id}/in.docx.
  AUDIT_TABLE      DynamoDB append-only audit table (same table/shape
                   src/users.py and src/retention.py already write to) --
                   used here for (a) upload-gauntlet rejections (via
                   upload_validation.run_upload_gauntlet's injected
                   audit_write) and (b) successful output downloads.
  ENV_NAME         Deployment environment name (dev/staging/prod), passed
                   through to download.generate_presigned_download_url's
                   per-user daily-limit table-name convention. Defaults to
                   "dev" -- not yet wired as an App Runner env var (see
                   infra/lib/nested/app-stack.ts's runtimeEnvironmentVariables
                   list, which also doesn't yet carry REVIEW_SUBMISSIONS_TABLE
                   / DAILY_SPEND_TABLE / PLAYBOOKS_TABLE / STATE_MACHINE_ARN --
                   that infra wiring is a separate, out-of-scope follow-up;
                   #186 or later). OUTPUTS_BUCKET (see src/download.py) is
                   already wired there.
"""

import hashlib
import json
import os
import logging
import pathlib
import sys
import time
import uuid
from typing import Any, Callable, Iterator

import boto3
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse

from src import (
    config,
    disposition,
    download,
    invocation_ledger,
    model_client,
    model_settings,
    pipeline_runner,
    playbook_versions,
    retention,
    reviews,
    upload_validation,
)
from src.auth import get_current_user
from src.users import require_active_user

logger = logging.getLogger(__name__)

router = APIRouter()

# REPO_ROOT / PLAYBOOK_REGISTRY_PATH are defined here (rather than down by
# the /api/playbooks section they also serve, see below) because
# DEFAULT_PLAYBOOK_ID -- post_review()'s Form default just below -- needs
# them at `def` time, which Python evaluates in file order at module load.
# Derived the same way src/model_client.py:41 reaches `model-policy/` --
# two parents up from this file (backend/src/ -> backend/ -> repo root).
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAYBOOK_REGISTRY_PATH = REPO_ROOT / "playbooks" / "registry.json"

# Issue #491 (preflight): this module previously had no reason to import
# anything under scripts/ -- every scripts/ module it now needs
# (document_injection_scan, preflight_pass, playbook_registry) is a
# bare-name import, same convention backend/src/pipeline_runner.py already
# established for the same reason (see that module's own sys.path comment).
_SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import cover_note_pass  # noqa: E402
import document_injection_scan  # noqa: E402
import leakage_scan  # noqa: E402
import playbook_registry  # noqa: E402
import preflight_pass  # noqa: E402


def get_playbook_registry_path() -> pathlib.Path:
    return PLAYBOOK_REGISTRY_PATH


def _load_default_playbook_id(registry_path: pathlib.Path) -> str:
    """Read playbooks/registry.json's "default_playbook_id" field directly
    (issue #289) -- rather than importing scripts/playbook_registry, per
    this package's existing src/scripts boundary (see the "Dependency
    providers" comment above and the `_load_playbook_catalog` docstring
    below, which reads the same file the same way for the same reason).
    Called once at module load time -- this is config, not per-request
    state (registry.json isn't expected to change without a redeploy)."""
    with open(registry_path, encoding="utf-8") as f:
        registry = json.load(f)
    return registry["default_playbook_id"]


DEFAULT_PLAYBOOK_ID = _load_default_playbook_id(PLAYBOOK_REGISTRY_PATH)

# `fastapi.Path` (path-param declaration, used below by GET
# /api/reviews/{review_id}) shadows `pathlib.Path` -- this module needs
# both, so the filesystem one is referenced via the `pathlib` module
# object rather than a second top-level import.


# ---------------------------------------------------------------------------
# Dependency providers.
#
# Deliberately NOT imported from src/main.py (that would create a circular
# import once #186 does `from src.review_routes import router` inside
# main.py) -- duplicated as small compositions instead, same convention
# already used across this package's module boundaries (see
# infra/lambda/persist/handler.py mirroring src/reviews.py's cost
# constants, or src/download.py/src/reviews.py's each-own-copy of small
# shared sentinels).
# ---------------------------------------------------------------------------


def get_dynamodb_resource() -> Any:
    return boto3.resource("dynamodb", **config.boto3_client_kwargs("dynamodb"))


def get_dynamodb_client() -> Any:
    return boto3.client("dynamodb", **config.boto3_client_kwargs("dynamodb"))


def get_s3_client() -> Any:
    return boto3.client("s3", **config.boto3_client_kwargs("s3"))


def get_sfn_client() -> Any:
    # Docker Compose target: an in-process background-worker client that runs the pipeline
    # in-container (duck-types the boto3 Step Functions slice
    # reviews.ensure_execution_started uses). AWS target: the real client.
    if config.pipeline_runner() == "inprocess":
        return pipeline_runner.get_inprocess_sfn_client()
    return boto3.client("stepfunctions", **config.boto3_client_kwargs("stepfunctions"))


def get_env_name() -> str:
    return os.environ.get("ENV_NAME", "dev")


def get_active_user_row(
    current_user: dict[str, Any] = Depends(get_current_user),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> dict[str, Any]:
    """Re-verify `users.status == active` on every request -- same
    composition as src/main.py::get_active_user_row."""
    return require_active_user(current_user.get("sub", ""), dynamodb_resource)


class NullAvClient:
    """Deterministic, offline AV-client stand-in (issue #84).

    Always returns CLEAN. No in-account ClamAV Lambda exists yet (see
    src/upload_validation.py's module docstring: "the `av_client` parameter
    here is the thin interface that Lambda-based scanner sits behind" --
    building that Lambda is a separate, out-of-scope follow-up). This
    mirrors src/main.py's `get_embed_fn` -> `corpus.deterministic_embed`
    pattern: a real implementation is injected later by swapping this
    dependency, never by changing the call site
    (`upload_validation.run_upload_gauntlet`).
    """

    def scan(self, file_bytes: bytes) -> str:  # noqa: ARG002
        return upload_validation.AV_VERDICT_CLEAN


def get_av_client() -> upload_validation.AvClient:
    return NullAvClient()


# Issue #491's cheap-model pass. Injectable exactly like `get_av_client`
# above -- production builds the real `OpenRouterModelClient`, tests inject
# a fake via `app.dependency_overrides`. Returns None (never raises) when no
# OpenRouter API key is configured, so the route can treat "no client" the
# same way it treats "the client's own call failed": degrade to
# `classification: "unavailable"`, never fail the request.
#
# Timeout ~8s, NO retries (issue #491: "Timeout ~8s ... never fail the
# preflight because the cheap model hiccuped"): `OpenRouterModelClient`'s
# default bounded-retry policy (up to 3 attempts with exponential backoff)
# exists to protect an expensive, already-committed multi-pass review --
# retrying a fast, cheap, EVERY-upload advisory check would burn the whole
# timeout budget re-paying for the same likely-transient failure instead of
# just falling back to stats-only quickly.
def get_preflight_model_client(
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> Iterator[Any | None]:
    api_key = model_settings.resolve_openrouter_api_key(dynamodb_resource)
    if not api_key:
        yield None
        return
    client = model_client.OpenRouterModelClient(
        api_key=api_key, timeout_seconds=8.0, max_retries=0
    )
    try:
        yield client
    finally:
        # Issue #491 fix round 1: mirrors pipeline_runner.py's issue #270
        # precedent -- a real OpenRouterModelClient owns an httpx.Client and
        # its connection pool, and must be closed once this request is done
        # with it. This is a `yield` dependency (rather than closing inline
        # in the route) so every preflight request closes the client it
        # built, even one that raised before reaching the route body. Guard
        # exactly as pipeline_runner does with `getattr(client, "close",
        # None)`: a test double injected via `app.dependency_overrides`
        # bypasses this generator entirely (the override replaces this
        # whole function), so this `finally` only ever sees a client THIS
        # function built.
        close = getattr(client, "close", None)
        if callable(close):
            close()


# Issue #499 ("Butter it"): the cover-note-drafter client, same shape and
# same reason as get_preflight_model_client above -- yields None (never
# raises) when no OpenRouter API key is configured, so the route degrades
# to its own "couldn't butter this one" response instead of a 500, and
# closes the httpx client it built in a `finally` so a test-double override
# (which replaces this whole function) is never double-closed.
def get_cover_note_model_client(
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> Iterator[Any | None]:
    api_key = model_settings.resolve_openrouter_api_key(dynamodb_resource)
    if not api_key:
        yield None
        return
    client = model_client.OpenRouterModelClient(
        api_key=api_key, timeout_seconds=20.0, max_retries=0
    )
    try:
        yield client
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


# Issue #499 fix round 2 (finding 1): the cover-note draft is NEW model
# prose reaching the single most counterparty-bound surface in the product
# and was going straight from the sanitizer to persistence/response with no
# corpus-matching leakage check at all -- `cover_note_pass
# .sanitize_cover_note_text` is a greeting/sign-off/promise/word-cap text
# filter, not a leakage scan. `post_review_cover_note` below scans the
# draft with `scripts/leakage_scan.LeakageScanner` (the same deterministic
# n-gram/pattern mechanism `scripts/review_spine.py` runs `verdict_summary`
# / `external_rationale_for_footnote` through) before EITHER
# `reviews.record_cover_note_draft` or the 200 response can see it.
#
# The corpus to scan against is resolved via this dependency rather than
# built inline so a test can override it with a corpus carrying a planted
# n-gram (see tests/test_cover_note_499.py) without needing a real,
# on-disk playbook fixture engineered to contain one. Production default:
# `_default_cover_note_leakage_corpus` below, keyed on the REVIEWED
# review's own `playbook_id` (not whatever playbook the caller might name
# today) -- best-effort, matching `_resolve_playbook_agreement_type`'s own
# fail-open-on-lookup-error posture, because an unregistered/missing
# playbook_id or a malformed on-disk file must degrade to an empty (never-
# blocking) corpus rather than turning a healthy cover-note request into a
# 502 over a catalog problem unrelated to this draft's content. This only
# ever weakens the scan on a LOOKUP failure -- it never weakens the
# fail-closed behavior on an actual positive detection once a corpus is in
# hand (see post_review_cover_note's leakage-scan block).
#
# Issue #499 fix round 3 (review finding): this MUST resolve the corpus the
# same way `pipeline_runner._load_playbook_bundle` does -- an activated OPF
# artifact (issue #478/#479) takes precedence over the registry's static
# on-disk `playbook_path`, and `ConfidentialCorpus.from_playbook` reads
# `playbook["topics"]`/`["hard_rejections"]`, both ABSENT from an OPF
# bundle, so calling it on OPF content (or on the stale registry file when
# an OPF version is actually active) silently yields an empty, never-
# blocking corpus -- exactly the issue #479 bug, reopened here for this one
# route. `_load_playbook_bundle` is reused (not reimplemented) so this
# route's corpus resolution can never drift from the main pipeline's.
def _default_cover_note_leakage_corpus(
    playbook_id: str | None,
    dynamodb_resource: Any,
    s3_client: Any,
) -> "leakage_scan.ConfidentialCorpus":
    if not playbook_id:
        return leakage_scan.ConfidentialCorpus()
    try:
        bundle = pipeline_runner._load_playbook_bundle(
            playbook_id, dynamodb_resource, s3_client
        )
        opf_bundle_v2 = bundle.get("opf_bundle_v2")
        if opf_bundle_v2 is not None:
            return leakage_scan.ConfidentialCorpus.from_opf_document(
                opf_bundle_v2.get("opf") or {}, overrides=opf_bundle_v2.get("overrides")
            )
        return leakage_scan.ConfidentialCorpus.from_playbook(bundle)
    except Exception:  # noqa: BLE001 -- lookup failure degrades to an empty corpus
        return leakage_scan.ConfidentialCorpus()


def get_cover_note_leakage_corpus_resolver(
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    s3_client: Any = Depends(get_s3_client),
) -> Callable[[str | None], "leakage_scan.ConfidentialCorpus"]:
    def _resolve(playbook_id: str | None) -> "leakage_scan.ConfidentialCorpus":
        return _default_cover_note_leakage_corpus(playbook_id, dynamodb_resource, s3_client)

    return _resolve


# ---------------------------------------------------------------------------
# Audit helpers.
#
# Same `audit` table shape src/users.py::_write_audit_entry and
# src/retention.py::_write_audit_entry already use (PK `partition` =
# "%Y-%m", SK `timestamp` = "{epoch}#{event_id}") -- duplicated here rather
# than imported (those helpers are module-private) per this package's
# existing small-duplication convention.
# ---------------------------------------------------------------------------


def _write_audit_row(
    dynamodb_resource: Any,
    *,
    actor: str,
    action: str,
    target: str,
    target_type: str,
    detail: dict[str, Any] | None = None,
) -> None:
    audit_table_name = os.environ.get("AUDIT_TABLE")
    if not audit_table_name:
        # Best-effort: never gate the request itself on audit-table config,
        # same posture as upload_validation._write_rejection_audit.
        return
    table = dynamodb_resource.Table(audit_table_name)
    now = time.time()
    event_id = uuid.uuid4().hex
    item: dict[str, Any] = {
        "partition": time.strftime("%Y-%m", time.gmtime(now)),
        "timestamp": f"{int(now)}#{event_id}",
        "event_id": event_id,
        "actor": actor,
        "action": action,
        "target": target,
        "target_type": target_type,
        "outcome": "success",
    }
    if detail:
        item.update(detail)
    try:
        # Post-landing review of issue #499, finding 2: this write is
        # called from inside `except leakage_scan.LeakageDetectedError` at
        # the cover-note route's leakage-block branch -- Python does not
        # route an exception raised inside one `except` clause to a sibling
        # `except Exception` of the same `try`, so an unguarded put_item
        # failure here (a DynamoDB throttle/outage) escaped uncaught and
        # turned that route's promised quiet 502 into an unhandled 500,
        # with the leakage block itself going unaudited on top of it. Audit
        # writes are best-effort BY DESIGN (see the missing-AUDIT_TABLE
        # early return above) -- guarding the put_item itself, rather than
        # at any one call site, covers all six call sites the same way,
        # including the success-path write at the end of the cover-note
        # route, which has the identical failure mode. The warning log is
        # the durable record of the event when the write itself fails.
        table.put_item(Item=item)
    except Exception:  # noqa: BLE001 -- audit is best-effort; never gate the request on it
        logger.warning(
            "AUDIT: failed to write audit row action=%s target=%s target_type=%s",
            action,
            target,
            target_type,
        )


def _upload_rejection_audit_write(dynamodb_resource: Any, actor: str):
    """Adapter satisfying upload_validation.AuditWrite's call shape
    (`audit_write(action=..., review_id=..., filename=..., reason_code=...,
    detail=...)`), translated into this package's audit-row shape."""

    def _write(
        *,
        action: str,
        review_id: str | None,
        filename: str,
        reason_code: str,
        detail: str,
    ) -> None:
        _write_audit_row(
            dynamodb_resource,
            actor=actor,
            action=action,
            target=review_id or filename,
            target_type="upload",
            detail={"reason_code": reason_code, "detail": detail, "outcome": "rejected"},
        )

    return _write


# ---------------------------------------------------------------------------
# POST /api/reviews
# ---------------------------------------------------------------------------


@router.post("/api/reviews", status_code=status.HTTP_202_ACCEPTED, include_in_schema=True)
async def post_review(
    file: UploadFile = File(...),
    playbook_id: str = Form(DEFAULT_PLAYBOOK_ID),
    idempotency_key: str | None = Form(None),
    toaster_guidance: str = Form(""),
    notes_mode: str = Form(""),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    s3_client: Any = Depends(get_s3_client),
    sfn_client: Any = Depends(get_sfn_client),
    av_client: upload_validation.AvClient = Depends(get_av_client),
) -> JSONResponse:
    """Multipart .docx upload -> hostile-file gauntlet -> idempotent
    submission (ARCHITECTURE.md data flow steps 1-8).

    toaster_guidance (issue #398, optional, default ""): per-review
    free-text instructions, forwarded verbatim to reviews.submit_review ->
    the pipeline's primary + critic passes (scripts/primary_review_pass.py
    -> assemble_system_blocks). Issue #431 landed the UI input that
    actually sets it (the Review tab's submission form), additively and
    with no change to this signature -- the SPA omits the field entirely
    when the reviewer leaves it blank, so the default above still stands
    for every such submission.

    Order (per issue #84's Context: "multipart upload -> gauntlet ->
    submission record with idempotency ... bundle resolved at submission"):
      1. Read the uploaded bytes.
      2. Run the hostile-file gauntlet (issue #63) -- a rejection here maps
         straight to the gauntlet's own client-facing copy (issue #40:
         format-specific rejection detail carried on HostileFileError,
         converted via `upload_validation.to_http_exception`), and writes a
         rejection audit row.
      3. Resolve the active release bundle (issue #194) -- refuses with the
         documented 503 "no active playbook" (issue #41) before anything
         else is written.
      4. Resolve the idempotency key and probe for an existing submission
         (issue #59's reconciled spec) BEFORE writing to S3, so a
         duplicate/retried submission never orphans an S3 object under an
         unused review_id: the upload is written only when this call is
         about to become a genuinely NEW submission.
      5. Submit (creates the reviews row, reserves spend, starts the
         pipeline execution) and return 202 + review id.
    """
    owner_sub = caller_row.get("cognito_sub", "")

    # Issue #520: validated FIRST, before the file is even read. A bad
    # `notes_mode` is a malformed request, and refusing it before the
    # hostile-file gauntlet, the spend reservation and the submission record
    # means a typo costs nothing -- the same reasoning that puts the
    # no-active-playbook 503 ahead of any reservation.
    try:
        resolved_notes_mode = reviews.resolve_notes_mode(notes_mode)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    contents = await file.read()

    try:
        # Reassign `contents` to the gauntlet's return value (issue #63 /
        # docs/threat-model.md): on the ordinary path this is the SAME
        # bytes object handed in, but a document carrying an
        # attachedTemplate relationship comes back SANITIZED. Everything
        # below -- the hash, the S3 object, and (on the idempotent-retry
        # path) what extraction eventually reads -- must derive from this
        # one canonical value, never from the pre-gauntlet upload, or the
        # stored artifact and its own hash would silently disagree.
        contents = upload_validation.run_upload_gauntlet(
            contents,
            filename=file.filename or "upload.docx",
            declared_content_type=file.content_type or "application/octet-stream",
            av_client=av_client,
            audit_write=_upload_rejection_audit_write(dynamodb_resource, owner_sub),
        )
    except upload_validation.HostileFileError as exc:
        raise upload_validation.to_http_exception(exc) from exc

    file_sha256 = hashlib.sha256(contents).hexdigest()

    # Step 3 (issue #194): refuses with 503 "no active playbook" before any
    # spend reservation or submission record — see
    # reviews.resolve_active_release_bundle_hash's docstring.
    active_release_bundle_hash = reviews.resolve_active_release_bundle_hash(
        playbook_id, dynamodb_resource
    )

    resolved_key = reviews.resolve_idempotency_key(
        idempotency_key, owner_sub, file_sha256, active_release_bundle_hash
    )
    existing = reviews.find_existing_submission(
        resolved_key, owner_sub, file_sha256, active_release_bundle_hash, dynamodb_resource
    )

    if existing:
        # Duplicate/retry: the existing submission's own review_id and
        # upload_pointer are authoritative (submit_review's `existing`
        # branch ignores the review_id/upload_pointer arguments below), so
        # nothing is re-uploaded to S3.
        review_id = existing["review_id"]
        upload_pointer = existing["upload_pointer"]
    else:
        review_id = str(uuid.uuid4())
        upload_pointer = f"uploads/{owner_sub}/{review_id}/in.docx"
        _put_upload_object(s3_client, upload_pointer, contents)

    result = reviews.submit_review(
        owner_sub=owner_sub,
        playbook_id=playbook_id,
        file_sha256=file_sha256,
        upload_pointer=upload_pointer,
        active_release_bundle_hash=active_release_bundle_hash,
        dynamodb_resource=dynamodb_resource,
        sfn_client=sfn_client,
        client_supplied_idempotency_key=resolved_key,
        review_id=review_id,
        toaster_guidance=toaster_guidance,
        # Issue #518: recorded so the deliverable can be downloaded under a
        # name that identifies it. `file.filename` is counterparty-supplied
        # and is sanitised at DOWNLOAD time (download.content_disposition_for),
        # not here -- storing the raw value keeps the record faithful to what
        # was actually uploaded, and puts the escaping at the sink where the
        # header is built rather than trusting a stored value to stay safe.
        original_filename=(file.filename or "")[:512],
        notes_mode=resolved_notes_mode,
    )

    return JSONResponse(
        status_code=result["status_code"],
        content={"review_id": result["review_id"], "resumed": result["resumed"]},
    )


def _put_upload_object(s3_client: Any, key: str, contents: bytes) -> None:
    bucket = os.environ.get("UPLOADS_BUCKET", "")
    if not bucket:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="UPLOADS_BUCKET not configured.",
        )
    s3_client.put_object(Bucket=bucket, Key=key, Body=contents)


# ---------------------------------------------------------------------------
# POST /api/reviews/preflight -- issue #491
#
# A cheap, fast, ADVISORY check run the moment a file is chosen -- BEFORE
# "Upload for review" -- so a reviewer sees word count/page estimate/title
# and a does-this-match-the-dial signal without waiting for the full
# two-pass review. Never blocks a submission: every failure mode below
# degrades to a smaller response rather than an error the frontend would
# have to gate the Upload button on.
#
# Nothing is persisted except the spend-ledger row for an actual cheap-
# model call (`reviews.record_preflight_spend`) -- no S3 write, no reviews/
# review_submissions row. Stamping the result onto the review row if the
# user proceeds is explicitly a "nice-to-have" in the issue and is NOT
# implemented here -- a documented, deliberate scope cut, not an oversight.
# ---------------------------------------------------------------------------


def _resolve_playbook_agreement_type(
    playbook_id: str,
) -> tuple[str | None, list[str]]:
    """The SELECTED playbook's own `agreement_type` + `agreement_aliases`,
    read straight off its on-disk playbook JSON via `playbook_registry` --
    the comparison target `preflight_pass.compute_match_verdict` needs.

    Best-effort and fail-open to `(None, [])` on ANY problem (unregistered
    playbook_id, missing file, malformed JSON): this only feeds an
    ADVISORY match verdict (`compute_match_verdict` already treats a
    missing `playbook_agreement_type` as "unclear", never a refusal), so a
    catalog problem here must never turn into a 500 on an otherwise-healthy
    preflight request the way it would if this propagated.
    """
    try:
        entry = playbook_registry.resolve_playbook(playbook_id)
        if entry.playbook_path is None:
            return None, []
        with open(entry.playbook_path, encoding="utf-8") as f:
            data = json.load(f)
        playbook_section = data.get("playbook") or {}
        agreement_type = playbook_section.get("agreement_type")
        aliases = playbook_section.get("agreement_aliases") or []
        return (
            agreement_type if isinstance(agreement_type, str) else None,
            [a for a in aliases if isinstance(a, str)],
        )
    except Exception:  # noqa: BLE001 -- advisory lookup, never fail preflight over it
        return None, []


@router.post(
    "/api/reviews/preflight", status_code=status.HTTP_200_OK, include_in_schema=True
)
async def post_review_preflight(
    file: UploadFile = File(...),
    playbook_id: str = Form(DEFAULT_PLAYBOOK_ID),
    caller_row: dict[str, Any] = Depends(get_active_user_row),  # noqa: ARG001 -- auth gate only
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    av_client: upload_validation.AvClient = Depends(get_av_client),
    preflight_model_client: Any = Depends(get_preflight_model_client),
) -> JSONResponse:
    """Deterministic stats + a cheap-model type/side guess + a server-side
    match verdict against `playbook_id`'s own agreement type.

    Order:
      1. The SAME hostile-file gauntlet + 25 MiB cap `POST /api/reviews`
         runs (`upload_validation.run_upload_gauntlet`) -- this doubles as
         pre-submit validation, so a bad file's rejection reason is visible
         before the reviewer ever clicks Upload, not after.
      2. Deterministic stats (`preflight_pass.compute_document_stats`) --
         no model call, cannot fail on a cheap-model outage.
      3. The document-injection scan (issue #506), run BEFORE the cheap-
         model call and carried into this SAME response -- "one flag, not
         two" (issue #491's injection-defense rider, item 4).
      4. The cheap-model classification, iff a client is available
         (`get_preflight_model_client`) -- any failure (no client, timeout,
         malformed response) degrades to `classification: "unavailable"`
         with the deterministic stats still returned.
      5. The match verdict, computed HERE (never left to the model), against
         `playbook_id`'s own `agreement_type`/`agreement_aliases`.
    """
    contents = await file.read()
    try:
        # Reassign to the gauntlet's return value -- see the matching
        # comment in post_review_by_playbook. The stats/injection-scan/
        # classification below must all read the SAME (possibly
        # attachedTemplate-sanitized) bytes the gauntlet approved, not the
        # raw pre-gauntlet upload.
        contents = upload_validation.run_upload_gauntlet(
            contents,
            filename=file.filename or "upload.docx",
            declared_content_type=file.content_type or "application/octet-stream",
            av_client=av_client,
            audit_write=_upload_rejection_audit_write(
                dynamodb_resource, caller_row.get("cognito_sub", "")
            ),
        )
    except upload_validation.HostileFileError as exc:
        raise upload_validation.to_http_exception(exc) from exc

    try:
        stats = preflight_pass.compute_document_stats(contents)
    except preflight_pass.DocumentStatsError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Could not read this document. Please check the file and "
                "try again."
            ),
        ) from exc

    # Issue #506, run before the model call and folded into this same
    # response -- never a second, separate flag.
    try:
        injection_scan = document_injection_scan.summarise(
            document_injection_scan.scan_document(contents)
        )
    except Exception:  # noqa: BLE001 -- advisory: never fail preflight over the scan
        logger.warning("PREFLIGHT: injection scan failed; preflight continues unaffected")
        injection_scan = {}

    classification = "unavailable"
    agreement_type_guess: str | None = None
    paper_side: str | None = None
    confidence: float | None = None
    one_line_summary: str | None = None
    usage: dict[str, int] | None = None
    served_model_id: str | None = None

    if preflight_model_client is not None and stats["excerpt"]:
        try:
            known_types = preflight_pass.known_agreement_types()
            model_id = model_client.openrouter_preflight_model_id()
            raw_response = preflight_model_client.invoke(
                model_id=model_id,
                system_prompt=preflight_pass.PREFLIGHT_SYSTEM_PROMPT,
                user_prompt=preflight_pass.render_preflight_user_prompt(stats["excerpt"]),
                max_output_tokens=200,
                output_schema=preflight_pass.build_preflight_output_schema(known_types),
            )
            parsed = json.loads(preflight_pass.extract_json_object(raw_response))
            sanitized = preflight_pass.sanitize_classification(parsed, known_types)
            agreement_type_guess = sanitized["agreement_type_guess"]
            paper_side = sanitized["paper_side"]
            confidence = sanitized["confidence"]
            one_line_summary = sanitized["one_line_summary"]
            classification = "ok"
            usage = getattr(preflight_model_client, "last_usage", None)
            served_model_id = getattr(preflight_model_client, "last_served_model", None)
        except Exception:  # noqa: BLE001 -- degrade to stats-only, never fail
            logger.warning(
                "PREFLIGHT: cheap-model classification failed; degrading to stats-only"
            )

    # Never blocks on ledger errors -- a DynamoDB blip must not turn into a
    # visible preflight failure over accounting. Cost is logged as a bare
    # cents figure -- never the document text, prompt text, or the model's
    # response body -- so a per-check spend trail exists without leaking
    # counterparty content into logs (issue #491 AC: "per-check cost
    # logged"). A skipped/degraded check (usage is None) logs 0 rather than
    # skipping the log line, so "no cost" is visible too, not just silent.
    preflight_cost_cents = reviews.compute_preflight_actual_usd_cents(usage)
    logger.info("PREFLIGHT: cheap-model check cost %d cent(s)", preflight_cost_cents)
    try:
        reviews.record_preflight_spend(preflight_cost_cents, dynamodb_resource)
    except Exception:  # noqa: BLE001
        logger.warning("PREFLIGHT: spend ledger write failed; preflight continues unaffected")

    match: str | None = None
    if classification == "ok":
        playbook_agreement_type, playbook_aliases = _resolve_playbook_agreement_type(
            playbook_id
        )
        match = preflight_pass.compute_match_verdict(
            agreement_type_guess, playbook_agreement_type, playbook_aliases
        )

    response: dict[str, Any] = {
        "word_count": stats["word_count"],
        "page_estimate": stats["page_estimate"],
        "paragraph_count": stats["paragraph_count"],
        "title": stats["title"],
        "classification": classification,
        "agreement_type_guess": agreement_type_guess,
        "paper_side": paper_side,
        "confidence": confidence,
        "one_line_summary": one_line_summary,
        "match": match,
        "injection_scan": injection_scan,
    }
    if served_model_id:
        # Issue #491 AC: "a route test pins the preflight model to the
        # policy's preflight role (never the primary)" -- surfaced so a
        # test (or an operator) can see which model actually served the
        # request, same provenance discipline issue #514 established for
        # the primary/critic passes.
        response["served_preflight_model_id"] = served_model_id
    return JSONResponse(status_code=status.HTTP_200_OK, content=response)


# ---------------------------------------------------------------------------
# POST /api/reviews/preflight/match -- issue #491 fix round 1
#
# `render_preflight_user_prompt` (called above) receives only the document
# excerpt -- `playbook_id` never reaches the cheap-model prompt at all, and
# ONLY the match verdict below reads it. A dial change therefore never needs
# to re-upload the file or re-pay for the cheap-model call the full
# `/api/reviews/preflight` route above already ran once for this document:
# `ReviewSubmission.tsx` calls THIS route instead on a dial change, keeping
# the already-classified `agreement_type_guess` from the full response and
# asking only "does THIS type match THIS (possibly new) playbook now."
# ---------------------------------------------------------------------------


@router.post(
    "/api/reviews/preflight/match", status_code=status.HTTP_200_OK, include_in_schema=True
)
async def post_review_preflight_match(
    agreement_type_guess: str = Form(""),
    playbook_id: str = Form(DEFAULT_PLAYBOOK_ID),
    caller_row: dict[str, Any] = Depends(get_active_user_row),  # noqa: ARG001 -- auth gate only
) -> JSONResponse:
    """Recompute just the match verdict against `playbook_id`, the exact
    same way `post_review_preflight` computes it
    (`preflight_pass.compute_match_verdict` against the selected playbook's
    own `agreement_type`/`agreement_aliases`) -- no file, no gauntlet, no
    cheap-model call, nothing persisted. Advisory only, exactly like the
    full preflight route: an empty/unrecognized `agreement_type_guess` or an
    unresolvable `playbook_id` degrades to `match: "unclear"`, never an
    error.
    """
    playbook_agreement_type, playbook_aliases = _resolve_playbook_agreement_type(playbook_id)
    match = preflight_pass.compute_match_verdict(
        agreement_type_guess or None, playbook_agreement_type, playbook_aliases
    )
    return JSONResponse(status_code=status.HTTP_200_OK, content={"match": match})


# ---------------------------------------------------------------------------
# GET /api/playbooks
#
# Issue #272: the contract-type picker's data source -- thin, read-only
# slice of #77 (the full CRUD/versioning registry API, still out of
# scope) + #85 (reviewer UI, which renders the picker from this). The
# backend already accepted `playbook_id` on POST /api/reviews with no way
# for the frontend to discover what values are valid; this closes that
# gap.
#
# Source of truth: `playbooks/registry.json` (the same file
# `scripts/playbook_registry.py::list_playbook_ids` reads) -- read
# directly here rather than importing `scripts/playbook_registry`, per
# this package's existing src/scripts boundary (review_routes.py's own
# "Dependency providers" comment above; ARCHITECTURE.md's src/ vs scripts/
# split). REPO_ROOT / PLAYBOOK_REGISTRY_PATH / get_playbook_registry_path
# are defined near the top of this module (post_review()'s Form default
# needs them at `def` time) -- reused here rather than redefined.
#
# Status per playbook_id:
#   "active"      `reviews._read_active_release_bundle_hash` resolves a
#                  non-empty hash for it (a registered id with a runtime-
#                  valid, currently-active release bundle).
#   "coming_soon"  registered in the catalog but no active bundle yet
#                  (no `playbooks` table row, an empty
#                  `active_release_bundle_hash`, or an on-disk playbook
#                  that fails validation -- see that function's docstring;
#                  all three fail closed to the same "not active" signal
#                  here, exactly as they do for submission).
#
# Those two are the ONLY statuses (issue #433): the playbook the image
# ships with is installed by a deploy-time seed (src.sample_playbooks)
# through the same functions any admin-uploaded version goes through, so it
# is an ordinary catalog entry with an ordinary status -- there is no
# sample-only third state, and no `has_bundled_sample` flag, to synthesize
# here.
#
# `notes` (issue #411): the currently-active playbook_versions row's
# admin-editable `notes` field (src.playbook_versions.get_active_version_notes),
# or "" if there is no active version row or the row has no note set. This
# is a live read at request time, same as `status` above -- never cached or
# denormalized onto the playbooks table row.
#
# `test_only` (issue #412): a registry entry carrying `"test_only": true`
# (e.g. "synthetic-generic", the renamed former "eiaa" fixture the anchor/
# detector test suite still resolves through) exists purely so the test
# suite has a real, on-disk playbook_id to resolve -- it is never a shipped
# contract type, so it is filtered out of this catalog entirely, never
# surfaced as "active" or "coming_soon". This is the toaster's "ships and
# loads exactly ONE playbook" invariant: the catalog is the shipped-playbook
# list, not the registered-playbook_id list.
# ---------------------------------------------------------------------------


def _load_playbook_catalog(
    registry_path: pathlib.Path, dynamodb_resource: Any
) -> list[dict[str, Any]]:
    """Registered OR DB-created, non-`test_only` playbook_ids, sorted, each
    with a display name (the registry's optional `display_name` field,
    falling back to the id upper-cased -- issue #272's documented
    fallback), active/coming_soon status, and the active version's
    admin-editable notes (issue #411). A `"test_only": true` entry (issue
    #412) is never included -- it is not a shipped contract type.

    Admin overrides from the `playbooks` DB row are layered on top of the
    registry (issue #412's rename/remove): a `removed` playbook is omitted
    entirely, and an admin-set `display_name` wins over the registry's
    shipped one. The registry is a file baked into the image, so these have
    to live in the DB to survive a deploy -- see
    `src.playbook_versions.get_playbook_overrides`.

    Issue #485/#490: the id set iterated here is the UNION of the registry
    file's ids and `src.playbook_versions.list_all_version_playbook_ids` --
    every playbook_id that carries at least one `playbook_versions` row,
    which includes a playbook created purely through `POST /api/admin/
    playbooks` (issue #485's create-playbook route), never added to
    `playbooks/registry.json` at all. Before this, a DB-created playbook_id
    never appeared here, so it was never a selectable contract type on the
    review dial no matter how many versions it had or how many were
    activated. A DB-only id has no registry entry (`raw` below is `{}` for
    it), so it is simply never `test_only` and its display name falls back
    to the id upper-cased exactly like any other unnamed registry entry --
    every other rule (removed tombstone, display_name override, active/
    coming_soon resolution, live notes) applies identically regardless of
    which set an id came from."""
    with open(registry_path, encoding="utf-8") as f:
        registry = json.load(f)
    entries = registry.get("playbooks", {})
    all_ids = set(entries) | playbook_versions.list_all_version_playbook_ids(dynamodb_resource)

    catalog: list[dict[str, Any]] = []
    for playbook_id in sorted(all_ids):
        raw = entries.get(playbook_id) or {}
        if raw.get("test_only"):
            continue
        overrides = playbook_versions.get_playbook_overrides(playbook_id, dynamodb_resource)
        if overrides["removed"]:
            continue
        display_name = (
            overrides["display_name"] or raw.get("display_name") or playbook_id.upper()
        )
        active_hash = reviews._read_active_release_bundle_hash(playbook_id, dynamodb_resource)
        catalog.append(
            {
                "playbook_id": playbook_id,
                "display_name": display_name,
                "status": "active" if active_hash else "coming_soon",
                "notes": playbook_versions.get_active_version_notes(
                    playbook_id, dynamodb_resource
                ),
            }
        )
    return catalog


@router.get("/api/playbooks", status_code=status.HTTP_200_OK, include_in_schema=True)
async def get_playbooks(
    caller_row: dict[str, Any] = Depends(get_active_user_row),  # noqa: ARG001 -- auth gate only
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    registry_path: pathlib.Path = Depends(get_playbook_registry_path),
) -> JSONResponse:
    """The contract-type catalog (issue #272): any authenticated active
    user may read it (same `get_active_user_row` gate every other route in
    this router uses). Read-only -- the CRUD/versioning admin surface for
    the registry itself stays #77."""
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"playbooks": _load_playbook_catalog(registry_path, dynamodb_resource)},
    )


# ---------------------------------------------------------------------------
# GET /api/reviews
# ---------------------------------------------------------------------------


SCOPE_MINE = "mine"


@router.get("/api/reviews", include_in_schema=True)
async def get_reviews(
    scope: str = Query(
        "all",
        description=(
            "'mine' restricts the listing to the caller's own reviews even for "
            "an admin; anything else keeps the default (admin: all reviews)."
        ),
    ),
    limit: int | None = Query(
        None,
        description=(
            "Page size. Clamped into [1, 100]; omitted means 25. The clamp is "
            "not advisory -- there is no way to ask for the whole table."
        ),
    ),
    next_token: str | None = Query(
        None,
        description=(
            "Opaque token from a previous response's `next_token`. A token "
            "that is not one is a 400, never a silent restart from page one."
        ),
    ),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """List the caller's own reviews; an admin sees every review
    (ARCHITECTURE.md Routes table).

    `?scope=mine` (issue #449) opts out of that admin widening -- the History
    tab is a personal surface ("what did I toast, and how?"), so an admin
    opening it gets their OWN history rather than a table of every user's
    contract activity. It can only ever NARROW what the caller may see, so it
    is not an authorization decision: `reviews.list_reviews` remains the sole
    authority for what any caller is allowed to read.
    """
    page = reviews.list_reviews(
        caller_row,
        dynamodb_resource,
        owner_scoped=(scope == SCOPE_MINE),
        limit=limit,
        next_token=next_token,
    )
    # `reviews` keeps its name and its meaning (issue #488): the response is
    # additive, so a client that predates paging still reads the key it always
    # read -- it now reads a bounded first page of it, which is the point.
    # `next_token` is null when there is nothing behind this page.
    return JSONResponse(
        content={"reviews": page["items"], "next_token": page["next_token"]}
    )


# ---------------------------------------------------------------------------
# GET /api/reviews/{review_id}
# ---------------------------------------------------------------------------


@router.get("/api/reviews/{review_id}", include_in_schema=True)
async def get_review(
    review_id: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    s3_client: Any = Depends(get_s3_client),
) -> JSONResponse:
    """Status + result payload (provenance / critic deltas / confidence
    band for #35/#36). Owner-or-admin; a non-owner gets the same 404 as an
    unknown review_id (see reviews.get_review_detail's docstring).

    `s3_client` lets `get_review_detail` read `issues`/`critic_delta` off
    the persisted analysis artifact (`reviews.load_analysis_artifact`) --
    neither field is ever written to the `reviews` row itself."""
    detail = reviews.get_review_detail(review_id, caller_row, dynamodb_resource, s3_client)
    return JSONResponse(content=detail)


# ---------------------------------------------------------------------------
# POST /api/reviews/{review_id}/cancel
# ---------------------------------------------------------------------------


@router.post("/api/reviews/{review_id}/cancel", include_in_schema=True)
async def post_review_cancel(
    review_id: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    sfn_client: Any = Depends(get_sfn_client),
) -> JSONResponse:
    """Ask a running review to stop. Owner-or-admin; a non-owner gets the same
    404 as an unknown review_id (reviews.get_review_detail's scoping).

    Both deployment targets, two different mechanisms, one promise:

      - Docker Compose (in-process): recording the intent IS the mechanism.
        The runner polls it at cancel checkpoints inside the spine, both model
        passes, and the model client's retry loop, then writes the terminal
        row itself. 202 with the review still running is the honest answer.

      - AWS (Step Functions): `StopExecution` on the ARN recorded at
        submission. That is immediate and preemptive -- no further pipeline
        stage runs -- so this route also owns the terminal write and the spend
        settlement the aborted execution will never reach.

    Which one applies is decided by the recorded execution ARN, not by a
    deployment flag (see reviews.stop_running_execution).

    202, not 200: even on the AWS path the stage already executing runs to its
    own completion. Claiming 200/"cancelled" would be a lie the reviewer could
    act on -- they'd close the tab believing nothing more would be spent.

    409 when the review already reached a terminal status, carrying WHICH one,
    so the UI can say "it already finished" rather than "cancel failed".

    502 when the abort itself fails. This route must NEVER answer 202 for a
    stop it could not deliver: on the AWS target a swallowed StopExecution
    leaves the UI showing "Stopping…" over a pipeline running happily to
    completion, which is the precise false promise this endpoint exists to
    avoid. The recorded intent is deliberately left in place -- it is what the
    reviewer asked for, it is harmless on a review that goes on to finish, and
    clearing it would race the retry they are about to make.
    """
    try:
        result = reviews.request_review_cancel(review_id, caller_row, dynamodb_resource)
    except reviews.ReviewNotCancellableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "status": exc.status},
        ) from exc

    try:
        stopped = reviews.stop_running_execution(review_id, dynamodb_resource, sfn_client)
    except Exception as exc:  # noqa: BLE001 - the caller must learn this failed
        logger.exception("StopExecution failed for review %s", review_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": (
                    "We recorded your request but could not stop the review. "
                    "It may still be running — please try again."
                ),
                "status": result.get("status"),
            },
        ) from exc

    if stopped:
        # The aborted execution reaches neither the persist stage's terminal
        # write nor its spend settlement, so this route owns both. Ordered
        # terminal-write-first so that even if settlement fails the review is
        # already correctly CANCELLED rather than left RUNNING for the orphan
        # reconciler to relabel ERROR (ABORTED is one of its dead-execution
        # statuses).
        reviews.mark_cancelled(review_id, dynamodb_resource)
        reviews.settle_reservation_for_cancel(review_id, dynamodb_resource)
        result = {**result, "status": "CANCELLED"}

    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=result)


# ---------------------------------------------------------------------------
# POST /api/reviews/{review_id}/disposition -- reason_codes/topic_ids/note
# body validation (fix-round-1 on issue #486).
#
# docs/data-handling.md's field table classifies `attorney_disposition_
# reason_codes` and `attorney_disposition_topic_ids` as "structured codes
# only ... not document substance" with Indefinite (audit) retention -- the
# purge sweep (backend/src/retention.py, infra/lambda/purge_worker/
# handler.py's SUBSTANCE_FIELDS) deliberately never clears them, unlike
# `attorney_disposition_note`. That classification is only true if the
# values landing in those columns actually ARE short structured tokens.
# Before this fix the route forwarded whatever list the caller sent with
# only an `isinstance(list)` check on the list itself -- never on its
# elements -- straight into `record_disposition` and from there into the
# audit row below. An authenticated caller could park arbitrary clause text
# in two stores the purge promise never clears by POSTing it as a "reason
# code" or "topic id". The enumeration these are meant to be drawn from
# does not exist yet (#91/#252 -- the admin triage queue), so this bounds
# shape/size rather than whitelisting values; revisit with a real
# enumeration once #91/#252 lands.
MAX_DISPOSITION_LIST_ITEMS = 20
MAX_DISPOSITION_CODE_CHARS = 64
MAX_DISPOSITION_NOTE_CHARS = 4000


def _validate_disposition_code_list(value: Any, field_name: str) -> list[str] | None:
    """Validate `reason_codes`/`topic_ids` from the disposition POST body.

    None (the field was omitted) passes through unchanged. Anything present
    must be a list of short, non-empty strings -- HTTPException(400)
    otherwise. This is a bound on shape/size, not a whitelist of values (see
    the block comment above): it exists so "structured codes only, not
    document substance" stays true of whatever actually lands in these
    columns, not just of their intended use.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a list of strings.",
        )
    if len(value) > MAX_DISPOSITION_LIST_ITEMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} may not contain more than {MAX_DISPOSITION_LIST_ITEMS} items.",
        )
    for element in value:
        if not isinstance(element, str) or not element:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_name} elements must be non-empty strings.",
            )
        if len(element) > MAX_DISPOSITION_CODE_CHARS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{field_name} elements must be at most "
                    f"{MAX_DISPOSITION_CODE_CHARS} characters (structured "
                    "codes/ids only, not document text)."
                ),
            )
    return list(value)


def _validate_disposition_note(value: Any) -> str | None:
    """Validate the disposition POST body's optional `note`.

    None (omitted) passes through as None -- the note is free text and has
    never been required. A present-but-non-string value, or a
    present-but-too-long string, is HTTPException(400) rather than a
    silent type coercion or truncation: either would be a
    quietly-corrupted governance record, which is worse than telling the
    caller to fix the type or shorten it.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="note must be a string.",
        )
    if len(value) > MAX_DISPOSITION_NOTE_CHARS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"note may not exceed {MAX_DISPOSITION_NOTE_CHARS} characters.",
        )
    return value


# ---------------------------------------------------------------------------
# POST /api/reviews/{review_id}/disposition
# ---------------------------------------------------------------------------


@router.post("/api/reviews/{review_id}/disposition", include_in_schema=True)
async def post_review_disposition(
    review_id: str = Path(...),
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Record the reviewer's OPTIONAL disposition of a finished review's
    output (issue #486) -- wires the already-implemented, previously
    unreachable `src.disposition.record_disposition` behind a real route.

    Body: {"disposition": "ACCEPTED" | "EDITED" | "REJECTED",
    "reason_codes"?: list[str], "topic_ids"?: list[str], "note"?: str}.
    `reason_codes`/`topic_ids` elements are bounded (see
    `_validate_disposition_code_list`: at most `MAX_DISPOSITION_LIST_ITEMS`
    short strings each, at most `MAX_DISPOSITION_CODE_CHARS` characters) and
    `note` is length-capped (`_validate_disposition_note`,
    `MAX_DISPOSITION_NOTE_CHARS`) -- fix-round-1 on issue #486: these two
    fields are classified "structured codes only, not document substance"
    with Indefinite (audit) retention (docs/data-handling.md), a
    classification that unvalidated free-form strings would make false.

    Owner-or-admin -- same scoping as GET /output and /input above (a
    non-owner gets 403, not the detail route's non-enumerable 404: this
    route is only ever reached from a screen the caller already has the id
    from, exactly like the two download routes it copies the check from).

    NOT AN APPROVAL GATE (owner correction on issue #486, 2026-08-02):
    disposition capture is an optional "what happened with this one" record
    for the negotiating-history / eval feedback loop, never something the
    product enforces, gates, or nags about. This is a single idempotent
    write -- latest value wins, prior value kept in the audit trail below --
    and `record_disposition` itself never touches the review's pipeline
    `status` or `decision` (see that function's own docstring). An
    EDITED/REJECTED outcome additionally sets `legal_triage_status =
    PENDING_TRIAGE`; the cross-user triage queue UI that consumes that is
    explicitly deferred (#91/#252), not built here.

    Raises HTTPException(400) for an invalid `disposition` value (via
    `disposition.record_disposition`) and HTTPException(409) if the review
    has not yet reached a dispositionable (completed) status.
    """
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    resp = table.get_item(Key={"review_id": review_id})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found.")

    # Authorization BEFORE anything else is disclosed -- same ordering and
    # reasoning as get_review_output/get_review_input above.
    owner_sub = item.get("owner_sub", "")
    caller_sub = caller_row.get("cognito_sub", "")
    if caller_sub != owner_sub and not caller_row.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You are not the owner of this review and do not have admin "
                "privileges."
            ),
        )

    reason_codes = _validate_disposition_code_list(body.get("reason_codes"), "reason_codes")
    topic_ids = _validate_disposition_code_list(body.get("topic_ids"), "topic_ids")
    if "note" in body:
        note = _validate_disposition_note(body.get("note"))
    else:
        # An ABSENT `note` key (e.g. History's change-of-mind call, which
        # never sends one) means "leave the note alone", not "clear it" --
        # an explicit `"note": null` still clears it. Preserve whatever is
        # already stored on the row (`item`, already fetched above) rather
        # than letting `record_disposition`'s unconditional SET overwrite
        # it with None (issue #486 fix-round-2 finding #1).
        note = item.get("attorney_disposition_note")
    updated = disposition.record_disposition(
        review_id,
        body.get("disposition", ""),
        dynamodb_resource,
        reason_codes=reason_codes,
        topic_ids=topic_ids,
        note=note,
    )

    # Structured tokens only -- the free-text note is Confidential,
    # document-adjacent substance (disposition.py's own field dictionary)
    # that follows the REVIEWS row's retention, not the audit table's, so it
    # is never copied into this audit entry.
    _write_audit_row(
        dynamodb_resource,
        actor=caller_sub,
        action="review_disposition_recorded",
        target=review_id,
        target_type="review",
        detail={
            "disposition": updated.get("attorney_disposition"),
            "reason_codes": updated.get("attorney_disposition_reason_codes") or [],
            "topic_ids": updated.get("attorney_disposition_topic_ids") or [],
        },
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "review_id": review_id,
            "attorney_disposition": updated.get("attorney_disposition"),
            "attorney_disposition_recorded_at": updated.get("attorney_disposition_recorded_at"),
            "legal_triage_status": updated.get("legal_triage_status"),
        },
    )


# ---------------------------------------------------------------------------
# GET /api/reviews/{review_id}/output
# ---------------------------------------------------------------------------


@router.get("/api/reviews/{review_id}/output", include_in_schema=True)
async def get_review_output(
    review_id: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    dynamodb_client: Any = Depends(get_dynamodb_client),
    s3_client: Any = Depends(get_s3_client),
    env_name: str = Depends(get_env_name),
) -> JSONResponse:
    """Scoped presigned download (issue #71 AC2/AC5) -- owner-or-admin
    (HTTP 403 for anyone else, unchanged existing `download.py`
    behavior), short-lived, no-store, audited.

    The owner_sub and s3_key are derived from the authoritative `reviews`
    row here, never taken from client input (download.py's own docstring
    invariant): a client cannot request an arbitrary key by crafting the
    request.
    """
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    resp = table.get_item(Key={"review_id": review_id})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found.")

    # Authorization BEFORE existence-of-output is disclosed: checking
    # "has output" first would let a non-owner distinguish "exists, no
    # output yet" from "doesn't exist" without ever being authorized to
    # know either. Same owner-or-admin rule download.py's own
    # _check_owner_or_admin enforces; duplicated as a plain check here
    # rather than reaching into that module's private helper.
    owner_sub = item.get("owner_sub", "")
    caller_sub = caller_row.get("cognito_sub", "")
    if caller_sub != owner_sub and not caller_row.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Download denied: you are not the owner of this review and "
                "do not have admin privileges."
            ),
        )

    output_s3_key = item.get("output_s3_key")
    if not output_s3_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No output is available for this review yet.",
        )

    response = download.generate_presigned_download_url(
        review_id,
        owner_sub,
        output_s3_key,
        caller_row,
        env_name,
        s3_client,
        dynamodb_client,
        # Issue #449: a retention purge deletes the OBJECT and leaves the row's
        # pointer in place, so `output_s3_key` alone stopped being proof that
        # anything is still downloadable. Without this the History tab would
        # hand out a valid-looking URL that 404s on click.
        require_object_exists=True,
        # Issue #518: name the deliverable after the document it came from.
        # `original_filename` is absent on every review created before that
        # field existed, and after a retention purge clears it -- both resolve
        # to the review-id fallback rather than failing.
        download_filename=download.content_disposition_for(
            item.get("original_filename"), review_id
        ),
    )

    # Audit only a SUCCESSFUL download-URL issuance (generate_presigned_download_url
    # already raised for an unauthorized/over-limit caller before reaching here).
    _write_audit_row(
        dynamodb_resource,
        actor=caller_row.get("cognito_sub", ""),
        action="review_output_downloaded",
        target=review_id,
        target_type="review",
        detail={"s3_key": output_s3_key},
    )

    return response


# ---------------------------------------------------------------------------
# GET /api/reviews/{review_id}/input
#
# Issue #449: the History tab links each past review back to the document it
# reviewed. There was no such route -- `/api/reviews/{id}/download` appears in
# src/download.py's module docstring as a USAGE EXAMPLE and has never been
# registered, a false lead recorded in that ticket's audit.
#
# Deliberately a sibling of /output rather than a parameterised variant of it:
# the two differ in bucket, in key layout (the uploads prefix carries
# `owner_sub`, the outputs prefix does not), and in audit action, and the
# owner-or-admin gate is short enough that duplicating it keeps each route
# readable in one screen -- this module's existing small-duplication
# convention.
# ---------------------------------------------------------------------------


@router.get("/api/reviews/{review_id}/input", include_in_schema=True)
async def get_review_input(
    review_id: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    dynamodb_client: Any = Depends(get_dynamodb_client),
    s3_client: Any = Depends(get_s3_client),
    env_name: str = Depends(get_env_name),
) -> JSONResponse:
    """Scoped presigned download of the review's INPUT document -- the same
    owner-or-admin gate, short TTL, no-store and audit trail as /output.

    The owner_sub and s3_key are derived from the authoritative `reviews` row,
    never from client input. The key's prefix is built from that row's OWN
    `owner_sub` (not the caller's) so an admin downloading another user's
    input still presigns the correct object, and `download
    ._validate_s3_key_bound_to_review` independently re-checks that the stored
    key really does sit under it.

    Three distinct absences, three distinct answers:
      * no review row              -> 404 (unchanged /output behavior)
      * no recorded upload pointer -> 404 ("not recorded": every review created
        before this issue, which the History tab renders as such)
      * pointer recorded, object gone -> 410 Gone, raised inside
        `generate_presigned_download_url`

    The 410 is reachable on both routes: issue #454 fixed the retention
    sweep's uploads targeting (it listed `uploads/{review_id}/`, a prefix that
    omitted the owner segment and so matched nothing, while still reporting
    the review purged), so a past-window input document is genuinely deleted
    and this route reports it Gone rather than handing back a document
    retention believed it had deleted.
    """
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    resp = table.get_item(Key={"review_id": review_id})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found.")

    # Authorization BEFORE existence-of-input is disclosed -- same ordering
    # and same reasoning as get_review_output above.
    owner_sub = item.get("owner_sub", "")
    caller_sub = caller_row.get("cognito_sub", "")
    if caller_sub != owner_sub and not caller_row.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Download denied: you are not the owner of this review and "
                "do not have admin privileges."
            ),
        )

    upload_s3_key = item.get("upload_s3_key")
    if not upload_s3_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No input document is recorded for this review.",
        )

    response = download.generate_presigned_download_url(
        review_id,
        owner_sub,
        upload_s3_key,
        caller_row,
        env_name,
        s3_client,
        dynamodb_client,
        expected_key_prefix=f"uploads/{owner_sub}/{review_id}/",
        bucket_name=download.get_uploads_bucket(),
        require_object_exists=True,
        # The INPUT document downloads under its own name, with no redline
        # marker -- it is not a redline. Same sanitising, same fallback.
        download_filename=download.content_disposition_for_input(
            item.get("original_filename"), review_id
        ),
    )

    _write_audit_row(
        dynamodb_resource,
        actor=caller_row.get("cognito_sub", ""),
        action="review_input_downloaded",
        target=review_id,
        target_type="review",
        detail={"s3_key": upload_s3_key},
    )

    return response


# ---------------------------------------------------------------------------
# POST /api/reviews/{review_id}/cover-note -- issue #499, "Butter it"
#
# Drafts the counterparty cover email from a FINISHED review's own analysis
# artifact (issues[] + verdict_summary already on the row) -- never re-reads
# the document, never re-runs review. Copy-only: the response is body text a
# reviewer pastes into their own email client. Nothing is ever sent by the
# toaster.
#
# Owner-or-admin, same scoping and disclosure ordering as GET /output,
# GET /input, and POST /disposition above. Refuses a non-DONE review (409)
# and a review with no requested changes to describe (409) -- there is
# nothing to butter on either. `regenerate: false` (the default) returns a
# previously cached draft for FREE (no model call, no new spend, no new
# ledger row) when one exists; `regenerate: true` always pays for a fresh
# draft and overwrites the cache -- but ONLY on success: a failed
# regenerate leaves whatever was cached before untouched (see
# `reviews.record_cover_note_draft`'s docstring).
# ---------------------------------------------------------------------------


@router.post(
    "/api/reviews/{review_id}/cover-note", status_code=status.HTTP_200_OK, include_in_schema=True
)
async def post_review_cover_note(
    review_id: str = Path(...),
    body: dict[str, Any] = Body(default={}),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    cover_note_model_client: Any = Depends(get_cover_note_model_client),
    s3_client: Any = Depends(get_s3_client),
    leakage_corpus_resolver: Callable[
        [str | None], "leakage_scan.ConfidentialCorpus"
    ] = Depends(get_cover_note_leakage_corpus_resolver),
) -> JSONResponse:
    """Draft (or return the cached draft of) the counterparty cover email
    for a finished review. Body: `{"regenerate"?: bool}`, default False.

    Raises HTTPException(404) for an unknown review_id, (403) for a
    non-owner/non-admin caller, (409) for a non-DONE review or one with no
    issues to describe, and (502) when the cover-note model client is
    unavailable, the generation attempt itself fails, or the freshly
    generated draft trips the leakage scan (issue #499 fix round 2, finding
    1) -- the frontend turns that into the quiet "Couldn't butter this one
    -- the redline is unaffected" copy with a retry, per the issue's own
    AC. A leakage-scan block is never distinguishable from any other
    generation failure in the response body (same as every other model
    error this route degrades) precisely so a blocked draft is never
    disclosed to the very channel it was blocked from reaching.
    """
    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    resp = table.get_item(Key={"review_id": review_id})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found.")

    # Authorization BEFORE anything else is disclosed -- same ordering and
    # reasoning as get_review_output/get_review_input/post_review_disposition
    # above.
    owner_sub = item.get("owner_sub", "")
    caller_sub = caller_row.get("cognito_sub", "")
    if caller_sub != owner_sub and not caller_row.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You are not the owner of this review and do not have admin "
                "privileges."
            ),
        )

    if item.get("status") != reviews.REVIEW_STATUS_SUCCESS_TERMINAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This review has not finished successfully yet; there is nothing to butter.",
        )

    # `issues` is never written to the `reviews` row itself (no writer ever
    # has -- see `reviews.load_analysis_artifact`'s docstring); it lives
    # only in the persisted analysis artifact
    # (`outputs/{review_id}/analysis.json`). Reading `item.get("issues")`
    # directly meant this gate 409'd on EVERY real review. Uses the SAME
    # shared helper `get_review_detail` uses -- a second independent reader
    # is exactly how this drift started.
    analysis = reviews.load_analysis_artifact(item, s3_client)
    issues = (analysis.get("findings") if analysis is not None else None) or []
    if not issues:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This review has no requested changes to describe.",
        )

    # Issue #499 fix round 3 (review finding), extended by the purge-parity
    # wave that follows it: neither purge implementation clears `issues`/
    # `status` (only `summary`, `original_filename`, `normalization_notes`,
    # `attorney_disposition_note`, `cover_note_draft`, `toaster_guidance` --
    # see retention.py/purge_worker/handler.py's SUBSTANCE_FIELDS;
    # `issue_rationale_text` was removed from that list, as no writer has
    # ever produced it), so a purged review still reads status=DONE with
    # `issues` intact and this route had no gate of its own -- a purge is
    # supposed to mean this review's substance is gone, but the endpoint
    # would still read `issues`/`summary` and mint a fresh, billed draft
    # from it.
    #
    # `purged_at` is checked FIRST and is DEFINITIVE: both purge
    # implementations stamp it (epoch-seconds string) in the same update as
    # their REMOVE clause, so its presence means this row demonstrably WAS
    # purged -- no timestamp arithmetic required, and none is done: a
    # corrupt `created_at` on an already-purged row can never even reach
    # `_is_past_retention`'s `float()` call, because that function is never
    # called once `purged_at` is set.
    #
    # Only when `purged_at` is ABSENT does this fall back to the prediction
    # below (`retention._is_past_retention` / `_is_legal_held` -- the EXACT
    # eligibility predicate the sweep itself runs) rather than inventing a
    # second one, so this gate can never drift from what the sweep actually
    # purges. That fallback is deliberately MORE conservative than
    # `purged_at`: it refuses a review that has crossed its retention window
    # even if the sweep itself hasn't physically run yet. `purged_at` ADDS a
    # definitive signal on top of that prediction; it does not replace it.
    if item.get("purged_at"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This review is past its retention window; there is nothing left to butter.",
        )
    try:
        past_retention = retention._is_past_retention(item) and not retention._is_legal_held(
            item
        )
    except (TypeError, ValueError):
        # `_is_past_retention` does `float(review.get("created_at", ...))` --
        # a corrupted row with a non-numeric `created_at` would otherwise
        # turn this gate's own robustness check into an unhandled 500.
        # Fails CLOSED (same posture as the leakage scan and the retention
        # check itself): if the row's retention state can't be determined
        # safely, refuse rather than risk serving from a row that may
        # already be past its window.
        logger.warning(
            "COVER_NOTE: could not evaluate retention eligibility for review_id=%s; "
            "refusing rather than risk serving a possibly-purged row",
            review_id,
        )
        past_retention = True
    if past_retention:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This review is past its retention window; there is nothing left to butter.",
        )

    regenerate = bool(body.get("regenerate", False))

    if not regenerate and item.get("cover_note_draft"):
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "review_id": review_id,
                "draft": item["cover_note_draft"],
                "cost_usd_cents": 0,
                "cached": True,
                "generated_at": item.get("cover_note_generated_at"),
                "served_model_id": item.get("cover_note_served_model_id"),
                # Issue #499 fix round 1: the stored cost of the generation
                # that produced this cached draft, as its OWN field --
                # `cost_usd_cents` above stays 0 (this response cost
                # nothing) but the frontend's Regenerate button needs the
                # last REAL cost to render its cost hint on the common real
                # path (reload / History revisit), not just right after a
                # fresh generation in the same session.
                "last_generation_cost_usd_cents": item.get("cover_note_cost_usd_cents"),
            },
        )

    # Post-landing review of issue #499, finding 1: this is the ONLY spend
    # gate on this route. `record_cover_note_spend` (below) is settle-only --
    # no reservation -- so this cost never enters `reserved_usd_cents` and
    # `reserve_spend`'s own conditional check (submission path) never sees
    # it; without this, an authenticated owner could loop
    # `{"regenerate": true}` and bill `models.cover_note` with zero cap
    # interaction. Deliberately placed AFTER the free cached-draft return
    # above (a revisit that never calls the model costs nothing and must not
    # be refused just because unrelated spend elsewhere today exhausted the
    # cap) and BEFORE every path that can reach the model invocation below --
    # never at `record_cover_note_spend`'s own call site further down, which
    # sits inside a block whose `except Exception` would silently swallow an
    # HTTPException raised there (see that call site's own comment).
    if reviews.cover_note_daily_cap_reached(dynamodb_resource):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily spend limit reached. Try again after the cap resets (UTC midnight).",
        )

    if cover_note_model_client is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't butter this one — the redline is unaffected.",
        )

    model_id = model_client.openrouter_cover_note_model_id()
    usage: dict[str, int] | None = None
    served_model_id = model_id
    try:
        raw_text = cover_note_model_client.invoke(
            model_id=model_id,
            system_prompt=cover_note_pass.COVER_NOTE_SYSTEM_PROMPT,
            user_prompt=cover_note_pass.render_cover_note_user_prompt(
                # The persisted attribute is `summary`, not `verdict_summary`
                # -- see `reviews.get_review_detail`'s note on the same read.
                # Reading the wrong name here meant the drafter never saw the
                # review's narrative summary, so the "Overall: ..." line
                # `cover_note_pass.render_cover_note_user_prompt` appends was
                # silently absent from every real cover note.
                issues, item.get("summary")
            ),
            max_output_tokens=350,
        )
        draft = cover_note_pass.sanitize_cover_note_text(raw_text)
        if not draft:
            raise ValueError("cover-note draft was empty after sanitizing")

        # Issue #499 fix round 2, finding 1: this is NEW model-generated
        # prose bound for the counterparty channel, exactly the shape
        # `docs/output-contract.md`'s "Leakage scan scope" table requires a
        # scan for (same as `verdict_summary` / `external_rationale_for_
        # footnote`) -- `sanitize_cover_note_text` above is a tone/length
        # filter only and does no corpus matching. Runs BEFORE
        # `reviews.record_cover_note_draft` and BEFORE the 200 response
        # below (both further down this function), and BEFORE `usage`/
        # `served_model_id` are read, so a positive detection short-circuits
        # into the `except` block below with nothing about the blocked
        # draft persisted or returned.
        leakage_corpus = leakage_corpus_resolver(item.get("playbook_id"))
        scan_result = leakage_scan.LeakageScanner(leakage_corpus).scan(
            draft, field_name="cover_note_draft"
        )
        if scan_result.blocked:
            raise leakage_scan.LeakageDetectedError(
                field_name="cover_note_draft",
                category=scan_result.category or "",
                rule_id=scan_result.rule_id,
            )

        usage = getattr(cover_note_model_client, "last_usage", None)
        served_model_id = (
            getattr(cover_note_model_client, "last_served_model", None) or model_id
        )
    except leakage_scan.LeakageDetectedError as exc:
        # Same fail-closed posture as the pipeline's own
        # `leakage_scan.run_leakage_gate` callers: an audit row carrying
        # only non-substantive facts (field name, category, rule id --
        # never the matched text), then a quiet failure. Caught ahead of
        # the generic `except Exception` below so this gets its own audit
        # action rather than being logged as an undifferentiated
        # generation failure.
        logger.warning(
            "COVER_NOTE: leakage scan blocked draft for review_id=%s field=%s category=%s",
            review_id,
            exc.field_name,
            exc.category,
        )
        _write_audit_row(
            dynamodb_resource,
            actor=caller_sub,
            action="leakage_scan_blocked",
            target=review_id,
            target_type="review",
            detail={
                "field_name": exc.field_name,
                "category": exc.category,
                "rule_id": exc.rule_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't butter this one — the redline is unaffected.",
        ) from None
    except Exception:  # noqa: BLE001 -- never leak a raw model/network error
        logger.warning(
            "COVER_NOTE: generation failed for review_id=%s; degrading to a quiet failure",
            review_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't butter this one — the redline is unaffected.",
        ) from None

    cost_cents = reviews.compute_cover_note_actual_usd_cents(usage)
    generated_at = str(int(time.time()))

    # Cache the draft on the row BEFORE the ledger/spend writes below: those
    # are best-effort accounting (never fail the response), but the draft
    # itself is the reason this call was billed at all -- if only one write
    # can succeed, it must be this one.
    try:
        reviews.record_cover_note_draft(
            review_id, draft, cost_cents, served_model_id, dynamodb_resource,
            generated_at=generated_at,
        )
    except Exception:  # noqa: BLE001
        logger.warning("COVER_NOTE: failed to cache draft for review_id=%s", review_id)

    # Issue #414's per-attempt ledger -- "Spend ledger row per generation"
    # (issue #499 AC). Never blocks the response: a ledger write failure
    # (including MODEL_INVOCATIONS_TABLE not being configured on this
    # deployment) is swallowed by invocation_ledger's own ledger_write
    # callable, same "never fail a review over accounting" posture that
    # module's docstring documents.
    usage_for_ledger = usage or {}
    invocation_ledger.make_ledger_write(review_id, dynamodb_resource)(
        model_client.ModelInvocationRecord(
            review_id=review_id,
            pass_name="cover_note",
            model_id=model_id,
            attempt_number=1,
            outcome="success",
            input_tokens_est=usage_for_ledger.get("input_tokens", 0),
            output_tokens_est=usage_for_ledger.get("output_tokens", 0),
            actual_input_tokens=usage_for_ledger.get("input_tokens") if usage else None,
            actual_output_tokens=usage_for_ledger.get("output_tokens") if usage else None,
            served_model_id=served_model_id or "",
        )
    )

    # Never blocks on ledger errors either -- same posture as the preflight
    # route's own spend write.
    try:
        reviews.record_cover_note_spend(cost_cents, dynamodb_resource)
    except Exception:  # noqa: BLE001
        logger.warning("COVER_NOTE: daily-spend ledger write failed; response unaffected")

    _write_audit_row(
        dynamodb_resource,
        actor=caller_sub,
        action="review_cover_note_generated",
        target=review_id,
        target_type="review",
        detail={"cost_usd_cents": cost_cents},
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "review_id": review_id,
            "draft": draft,
            "cost_usd_cents": cost_cents,
            "cached": False,
            "generated_at": generated_at,
            "served_model_id": served_model_id,
        },
    )
