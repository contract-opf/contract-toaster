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
                   / DAILY_SPEND_TABLE / PLAYBOOKS_TABLE / STATE_MACHINE_ARN /
                   S3_OUTPUTS_BUCKET -- that infra wiring is a separate,
                   out-of-scope follow-up; #186 or later).
"""

import hashlib
import json
import os
import pathlib
import time
import uuid
from typing import Any

import boto3
from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, UploadFile, status
from fastapi.responses import JSONResponse

from src import config, download, pipeline_runner, reviews, upload_validation
from src.auth import get_current_user
from src.users import require_active_user

router = APIRouter()

# REPO_ROOT / PLAYBOOK_REGISTRY_PATH are defined here (rather than down by
# the /api/playbooks section they also serve, see below) because
# DEFAULT_PLAYBOOK_ID -- post_review()'s Form default just below -- needs
# them at `def` time, which Python evaluates in file order at module load.
# Derived the same way src/model_client.py:41 reaches `model-policy/` --
# two parents up from this file (backend/src/ -> backend/ -> repo root).
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAYBOOK_REGISTRY_PATH = REPO_ROOT / "playbooks" / "registry.json"


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
    # DTS target: an in-process background-worker client that runs the pipeline
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
    table.put_item(Item=item)


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
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    s3_client: Any = Depends(get_s3_client),
    sfn_client: Any = Depends(get_sfn_client),
    av_client: upload_validation.AvClient = Depends(get_av_client),
) -> JSONResponse:
    """Multipart .docx upload -> hostile-file gauntlet -> idempotent
    submission (ARCHITECTURE.md data flow steps 1-8).

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
    contents = await file.read()

    try:
        upload_validation.run_upload_gauntlet(
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
# ---------------------------------------------------------------------------


def _load_playbook_catalog(
    registry_path: pathlib.Path, dynamodb_resource: Any
) -> list[dict[str, str]]:
    """Registered playbook_ids, sorted, each with a display name (the
    registry's optional `display_name` field, falling back to the id
    upper-cased -- issue #272's documented fallback) and active/coming_soon
    status."""
    with open(registry_path, encoding="utf-8") as f:
        registry = json.load(f)
    entries = registry.get("playbooks", {})

    catalog: list[dict[str, str]] = []
    for playbook_id in sorted(entries):
        raw = entries[playbook_id] or {}
        display_name = raw.get("display_name") or playbook_id.upper()
        active_hash = reviews._read_active_release_bundle_hash(playbook_id, dynamodb_resource)
        catalog.append(
            {
                "playbook_id": playbook_id,
                "display_name": display_name,
                "status": "active" if active_hash else "coming_soon",
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


@router.get("/api/reviews", include_in_schema=True)
async def get_reviews(
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """List the caller's own reviews; an admin sees every review
    (ARCHITECTURE.md Routes table)."""
    items = reviews.list_reviews(caller_row, dynamodb_resource)
    return JSONResponse(content={"reviews": items})


# ---------------------------------------------------------------------------
# GET /api/reviews/{review_id}
# ---------------------------------------------------------------------------


@router.get("/api/reviews/{review_id}", include_in_schema=True)
async def get_review(
    review_id: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Status + result payload (provenance / critic deltas / confidence
    band for #35/#36). Owner-or-admin; a non-owner gets the same 404 as an
    unknown review_id (see reviews.get_review_detail's docstring)."""
    detail = reviews.get_review_detail(review_id, caller_row, dynamodb_resource)
    return JSONResponse(content=detail)


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
