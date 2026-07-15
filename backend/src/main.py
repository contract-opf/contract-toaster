"""
ContractToaster Review API — App Runner hello-world container (issue #55).

Endpoints:
  GET /health   — public, liveness only.
                  Returns {"status": "ok"}.  No build details (intentional:
                  liveness probes must not leak version info publicly).

  GET /version  — allowlisted (requires a valid Cognito JWT).
                  Returns version, commit SHA, image digest, and uptime_seconds.

  GET /whoami   — authenticated echo endpoint.
                  Returns the verified Cognito claims so callers can prove JWT
                  verification is working end-to-end (issue #55 AC).

  GET /api/me   — authenticated capability route (issue #235): the caller's
                  own resolved role, e.g. {"is_admin": bool}. Always 200 for
                  any active user (never 403s a non-admin), so the SPA can
                  decide whether to render admin UI before it paints.

  GET /api/users            — admin: the allowlist view (issue #92).
  POST /api/users           — admin: add a user, SSO or username/password (#232).
  PATCH /api/users/{sub}    — admin: set admin flag / lifecycle status (#92).
  DELETE /api/users/{sub}   — admin: remove a user, either type (#232).
  GET /api/users/sync-status — admin: Workspace/SSO sync-job visibility (#92).

  GET  /api/admin/auth-mode  — admin: demo auth-mode setting (sso/password/
                                both) — stored+served (#232).
  POST /api/admin/auth-mode  — admin: set the demo auth-mode setting (#232).
  POST /api/auth/login       — unauthenticated: username/password sign-in for
                                the demo auth feature, gated by the stored
                                auth-mode setting (#232). Cognito SSO sign-in
                                remains the separate, existing hosted-UI flow
                                (unaffected by this route).

  GET  /api/admin/retention                     — admin: retention settings (#94).
  POST /api/admin/retention                     — admin: request a retention change,
                                                    dual-controlled for retroactive
                                                    reductions per #13/#61 (#94).
  POST /api/admin/retention/preview              — admin: pre-sweep purge preview (#94).
  GET  /api/admin/retention/holds                — admin: legal-hold list view (#94).
  POST /api/admin/retention/holds/{review_id}    — admin: place a legal hold (#94).
  DELETE /api/admin/retention/holds/{review_id}  — admin: release a legal hold (#94).

  POST /api/admin/playbooks/{playbook_id}/versions/{version}/activate
                      — admin: activate a playbook release-bundle version,
                        Gate-7-enforced and wired to the resolver (#242).
                        Asserts content_hash == legal_approval.content_hash
                        before activating; on success writes
                        playbooks.active_release_bundle_hash so
                        reviews.resolve_active_release_bundle_hash actually
                        serves the newly activated bundle. A mismatch is
                        rejected with HTTP 409; an unknown version is 404.

  POST /api/corpus — admin: corpus ingestion (#197). Runs the real ingestion
                      pipeline (clause extraction, content-addressed
                      clause_ids, polarity separation, embeddings,
                      staging-index ingestion, manifest hashing) over
                      caller-supplied already-extracted paragraphs and
                      returns the resulting draft/failed staging snapshot.
                      Never activates the snapshot (issue #20's separate
                      admin action). Embeddings use the deterministic
                      hash-based stand-in (see src/corpus.py) until a real
                      Bedrock/Titan client is wired in (follow-up).

  POST /api/reviews                    — multipart .docx upload -> hostile-
                                          file gauntlet -> idempotent
                                          submission (issue #84, mounted by
                                          #186). See src/review_routes.py's
                                          module docstring for the full
                                          route table and rationale; that
                                          module was fully implemented and
                                          tested but deliberately not
                                          mounted here until #186 (this is
                                          the "no user-facing review flow"
                                          fix: #186 mounts the router).
  GET  /api/reviews                    — caller's own reviews; admin: all.
  GET  /api/reviews/{review_id}        — status + result payload.
  GET  /api/reviews/{review_id}/output — scoped presigned download.

Environment variables (set at container build time via Dockerfile ARG/ENV):
  VERSION        — application version (e.g. 0.1.0)
  COMMIT_SHA     — git commit SHA baked in by CI
  IMAGE_DIGEST   — immutable ECR image digest (e.g. sha256:…)

Environment variables (DynamoDB, consumed by src/users.py):
  USERS_TABLE        — users table name (PK: cognito_sub)
  AUDIT_TABLE        — audit table name (append-only)
  SYNC_STATUS_TABLE  — sync_status table name (PK: sync_type)

Environment variables (DynamoDB, consumed by src/demo_auth.py — issue #232):
  AUTH_SETTINGS_TABLE — auth-mode settings table name (PK: setting_id).
                         USERS_TABLE and AUDIT_TABLE above are shared with
                         src/users.py (same tables, no schema change).

Environment variables (DynamoDB/S3, consumed by src/retention.py):
  REVIEWS_TABLE              — reviews table name (PK: review_id)
  RETENTION_SETTINGS_TABLE   — retention_settings table name (PK: setting_id)
  UPLOADS_BUCKET             — uploads S3 bucket name
  OUTPUTS_BUCKET             — outputs S3 bucket name

Security invariants:
  - /health is public and returns ONLY liveness status.  Build details must
    not leak on the unauthenticated path (threat model: information disclosure).
  - /version and /whoami require a verified Cognito Bearer token.
  - The JWT middleware independently re-verifies the email domain and the
    Google 'hd' claim against the configured ALLOWED_EMAIL_DOMAINS
    (two-layer hosted-domain enforcement, backend half — the frontend/Cognito
    edge is the first layer).
  - Every /api/* route additionally re-checks `users.status == active` on
    every request (src.users.require_active_user) — the DynamoDB users row
    is the authoritative, backend-side gate described in ARCHITECTURE.md ->
    "Security defaults", independent of the pre-token Lambda's edge check
    and independent of token TTL. A suspended/deprovisioned user is denied
    on their very next call.
  - CloudWatch must never log document content, rationales, or PII.
    uvicorn is started with --no-access-log to avoid logging request bodies.
"""

import os
import time
from typing import Any

import boto3
from fastapi import Body, Depends, FastAPI, HTTPException, Path, status
from fastapi.responses import JSONResponse

from src import config
from src.auth import get_current_user
from src.corpus import deterministic_embed, run_ingestion_request
from src.demo_auth import (
    add_user,
    get_auth_mode_settings,
    issue_demo_token,
    login_with_password,
    remove_user,
    set_auth_mode,
)
from src.playbook_versions import (
    PlaybookVersionGate7MismatchError,
    PlaybookVersionNotFoundError,
    activate_release_bundle,
)
from src.review_routes import router as review_router
from src.retention import (
    RETENTION_WINDOW_FOREVER,
    get_retention_settings,
    list_legal_holds,
    preview_purge_sweep,
    release_legal_hold,
    request_retention_change,
    set_legal_hold,
)
from src.users import get_sync_status, list_users, require_active_user, update_user

# ---------------------------------------------------------------------------
# Application startup time — used to compute uptime_seconds in /version.
# ---------------------------------------------------------------------------
_START_TIME: float = time.monotonic()


# ---------------------------------------------------------------------------
# DynamoDB resource dependency — lazily constructed so import-time (e.g. unit
# tests that only exercise /health) never requires AWS credentials.
# ---------------------------------------------------------------------------


def get_dynamodb_resource() -> Any:
    return boto3.resource("dynamodb", **config.boto3_client_kwargs("dynamodb"))


def get_s3_client() -> Any:
    return boto3.client("s3", **config.boto3_client_kwargs("s3"))


def get_embed_fn() -> Any:
    """Embedding-function dependency for POST /api/corpus (issue #197).

    Defaults to `corpus.deterministic_embed` (the hash-based stand-in) so
    the pipeline is fully exercisable without a live Bedrock call. The real
    embedding client is a follow-up, injected the same way `AvClient` is
    injected into `upload_validation.run_upload_gauntlet` -- swap this
    dependency, not the call sites.
    """
    return deterministic_embed


def get_active_user_row(
    current_user: dict[str, Any] = Depends(get_current_user),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> dict[str, Any]:
    """FastAPI dependency: re-verify `users.status == active` on every
    request (backend-side gate, independent of the edge/token layers)."""
    return require_active_user(current_user.get("sub", ""), dynamodb_resource)


def _is_admin(caller_user_row: dict[str, Any]) -> bool:
    """`is_admin` is a DynamoDB `users`-row flag, never a JWT claim -- same
    convention as src/users.py::_is_admin / src/retention.py::_is_admin."""
    return bool(caller_user_row.get("is_admin", False))


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ContractToaster Review API",
    description="Contract review tool API — App Runner backend (issue #55).",
    version=os.environ.get("VERSION", "dev"),
    # Disable the default /docs and /redoc on the public path in prod;
    # callers should use /openapi.json only when authenticated.
    # (Phase 0: left enabled for development convenience.)
)

# Review API (issue #84's handlers; mounted here per issue #186 — the
# "no user-facing review flow exists" fix). See src/review_routes.py's
# module docstring for the route table and the auth/idempotency/audit
# contract each route implements.
app.include_router(review_router)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", include_in_schema=True)
async def health() -> JSONResponse:
    """Public liveness probe.

    Returns {"status": "ok"} and nothing else.  Build details must not appear
    on the public liveness path (information disclosure risk).
    """
    return JSONResponse(content={"status": "ok"})


@app.get("/version", include_in_schema=True)
async def version(
    _current_user: dict[str, Any] = Depends(get_current_user),
) -> JSONResponse:
    """Allowlisted (authenticated) version endpoint.

    Returns version, commit SHA, image digest, and uptime_seconds.
    Requires a valid Cognito JWT with an email/hd claim in ALLOWED_EMAIL_DOMAINS.

    Promotion of a new signed digest updates the running service; the
    authenticated /version shows the new commit SHA after a deliberate
    promotion (not on raw push to main).
    """
    uptime = time.monotonic() - _START_TIME
    return JSONResponse(
        content={
            "version": os.environ.get("VERSION", "dev"),
            "commit": os.environ.get("COMMIT_SHA", "unknown"),
            "image_digest": os.environ.get("IMAGE_DIGEST", "unknown"),
            "uptime_seconds": round(uptime, 2),
        }
    )


@app.get("/whoami", include_in_schema=True)
async def whoami(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> JSONResponse:
    """Authenticated echo endpoint — proves JWT verification end-to-end.

    Returns a safe subset of the verified Cognito claims: sub, email, and
    token_use.  The full claims dict is not returned to avoid leaking
    internal claim names to callers.

    Per issue #55 AC: "A /whoami (or equivalent) authenticated echo endpoint
    proves it end-to-end."
    """
    return JSONResponse(
        content={
            "sub": current_user.get("sub", ""),
            "email": current_user.get("email", ""),
            "token_use": current_user.get("token_use", ""),
        }
    )


@app.get("/api/me", include_in_schema=True)
async def get_me(
    caller_row: dict[str, Any] = Depends(get_active_user_row),
) -> JSONResponse:
    """Authenticated capability route (issue #235): resolved role for
    pre-render admin-UI gating.

    Every existing endpoint that would reveal `is_admin` already 403s a
    non-admin caller, so the SPA had no route to call to learn "am I an
    admin?" before rendering (see #234). This route fixes that: it always
    returns 200 for any active user and never 403s a legitimate non-admin.

    `is_admin` is derived from the caller's DynamoDB `users` row (already
    fetched by `get_active_user_row` -> `require_active_user`) via
    `src.users._is_admin` — never a JWT/Cognito claim (ARCHITECTURE.md ->
    "Group-naming misnomer"). No secrets or tokens are included.
    """
    return JSONResponse(content={"is_admin": _is_admin(caller_row)})


@app.get("/api/users", include_in_schema=True)
async def get_users(
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: the allowlist view (issue #92).

    Lists every `users` row — active, suspended, and deprovisioned —
    including JIT-created rows, so an admin can see group-sync status and
    take a lifecycle action. Raises HTTP 403 for a non-admin caller.
    """
    users = list_users(caller_row, dynamodb_resource)
    return JSONResponse(content={"users": users})


@app.post("/api/users", include_in_schema=True)
async def post_users(
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: add a user, either type (issue #232).

    Body (SSO): {"user_type": "sso", "email": str, "is_admin": bool=False}
    Body (password): {"user_type": "password", "username": str,
                       "password": str, "is_admin": bool=False}
    Raises HTTP 403 for a non-admin caller, 400 for a missing field or
    unknown user_type, 409 if the target already exists.
    """
    created = add_user(body, caller_row, dynamodb_resource)
    return JSONResponse(content=created)


@app.get("/api/users/sync-status", include_in_schema=True)
async def get_users_sync_status(
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: Workspace/SSO deprovisioning sync-job visibility (issue #92).

    Read-only surface of the last sync run's outcome (last run, changes
    made, fail-closed state). Raises HTTP 403 for a non-admin caller.

    Registered before the /api/users/{sub} path parameter route so
    "sync-status" is never captured as a `sub` value.
    """
    sync_status = get_sync_status(caller_row, dynamodb_resource)
    return JSONResponse(content=sync_status)


@app.patch("/api/users/{sub}", include_in_schema=True)
async def patch_user(
    sub: str = Path(...),
    updates: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: set admin flag and/or lifecycle status for a user (issue #92).

    Body may contain `is_admin` (bool) and/or `status`
    (active|suspended|deprovisioned). Every mutation is audited. Raises
    HTTP 403 for a non-admin caller, 400 for an invalid payload, 404 if the
    target user does not exist, and 409 if an admin targets their own row.
    """
    updated = update_user(sub, updates, caller_row, dynamodb_resource)
    return JSONResponse(content=updated)


@app.delete("/api/users/{sub}", include_in_schema=True)
async def delete_user_route(
    sub: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: remove a user, either SSO or username/password (issue #232).

    Raises HTTP 403 for a non-admin caller, 404 if the target does not
    exist, 409 if an admin targets their own row.
    """
    result = remove_user(sub, caller_row, dynamodb_resource)
    return JSONResponse(content=result)


@app.get("/api/admin/auth-mode", include_in_schema=True)
async def get_admin_auth_mode(
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: the demo auth-mode setting — sso/password/both, stored+served
    (issue #232). Raises HTTP 403 for a non-admin caller."""
    settings = get_auth_mode_settings(caller_row, dynamodb_resource)
    return JSONResponse(content=settings)


@app.post("/api/admin/auth-mode", include_in_schema=True)
async def post_admin_auth_mode(
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: set the demo auth-mode setting (issue #232).

    Body: {"auth_mode": "sso" | "password" | "both"}. Raises HTTP 403 for a
    non-admin caller, 400 for an invalid mode value.
    """
    result = set_auth_mode(body["auth_mode"], caller_row, dynamodb_resource)
    return JSONResponse(content=result)


@app.post("/api/auth/login", include_in_schema=True)
async def post_auth_login(
    body: dict[str, Any] = Body(...),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Unauthenticated: username/password sign-in for the demo auth feature
    (issue #232), gated by the stored auth-mode setting. This is deliberately
    NOT behind get_active_user_row/get_current_user — a caller attempting to
    log in does not yet hold a Cognito bearer token. The existing Cognito
    hosted-UI SSO flow is unaffected by this route.

    Body: {"username": str, "password": str}. On success returns the user
    summary plus a short-lived `token` the SPA presents as the Bearer on
    subsequent /api/* requests (get_current_user verifies it in `password`/
    `both` mode). Raises HTTP 403 if the stored mode does not permit password
    sign-in, 401 for an unknown user or wrong password, 403 if the matched
    row's lifecycle status is not active.
    """
    result = login_with_password(body["username"], body["password"], dynamodb_resource)
    result["token"] = issue_demo_token(result)
    return JSONResponse(content=result)


@app.get("/api/admin/retention", include_in_schema=True)
async def get_admin_retention(
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: retention settings — the retention slider's current state and
    any in-flight pending retroactive reduction (issue #94). Raises HTTP 403
    for a non-admin caller."""
    settings = get_retention_settings(caller_row, dynamodb_resource)
    return JSONResponse(content=settings)


@app.post("/api/admin/retention", include_in_schema=True)
async def post_admin_retention(
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: request a retention-window change (issue #94).

    Body: {"retention_window_days": int | "forever", "second_admin_confirmation": {"actor": str} | null}
    Forward-looking changes apply immediately, single-admin. Retroactive
    reductions require a second, different admin's confirmation or enter a
    72-hour pending-delay state (#13/#61's dual-control gate). Raises HTTP
    403 for a non-admin caller, 400 for a window outside [0, 1095] days and
    not the `"forever"` / indefinite-preservation sentinel (issue #34).
    """
    raw_window = body["retention_window_days"]
    # "forever" must pass through untouched -- int() on it raises ValueError
    # before request_retention_change's own validation ever runs.
    window_days = raw_window if raw_window == RETENTION_WINDOW_FOREVER else int(raw_window)
    result = request_retention_change(
        window_days,
        caller_row,
        dynamodb_resource,
        second_admin_confirmation=body.get("second_admin_confirmation"),
    )
    return JSONResponse(content=result)


@app.post("/api/admin/retention/preview", include_in_schema=True)
async def post_admin_retention_preview(
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: pre-sweep purge preview — "this change will purge N objects"
    (issue #94). Body: {"proposed_window_days": int}. Raises HTTP 403 for a
    non-admin caller.

    Registered before /api/admin/retention/holds/{review_id} is irrelevant
    here (different sub-path, "preview" vs "holds"), but kept adjacent to
    the settings routes above for readability.
    """
    preview = preview_purge_sweep(
        int(body["proposed_window_days"]), caller_row, dynamodb_resource
    )
    return JSONResponse(content=preview)


@app.get("/api/admin/retention/holds", include_in_schema=True)
async def get_admin_retention_holds(
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: the legal-hold list view (issue #94). Raises HTTP 403 for a
    non-admin caller.

    Registered before /api/admin/retention/holds/{review_id} so "holds"
    (the list) is never captured as a review_id path parameter.
    """
    holds = list_legal_holds(caller_row, dynamodb_resource)
    return JSONResponse(content={"holds": holds})


@app.post("/api/admin/retention/holds/{review_id}", include_in_schema=True)
async def post_admin_retention_hold(
    review_id: str = Path(...),
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    s3_client: Any = Depends(get_s3_client),
) -> JSONResponse:
    """Admin: place a legal hold on a review, mirrored to the storage layer
    (issue #94 / #61). Body: {"reason": str}. Raises HTTP 403 for a
    non-admin caller, 400 for an empty reason, 404 for an unknown review."""
    result = set_legal_hold(
        review_id, body.get("reason", ""), caller_row, dynamodb_resource, s3_client
    )
    return JSONResponse(content=result)


@app.delete("/api/admin/retention/holds/{review_id}", include_in_schema=True)
async def delete_admin_retention_hold(
    review_id: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    s3_client: Any = Depends(get_s3_client),
) -> JSONResponse:
    """Admin: release a legal hold on a review, mirrored to the storage
    layer (issue #94 / #61). Raises HTTP 403 for a non-admin caller, 404 for
    an unknown review."""
    result = release_legal_hold(review_id, caller_row, dynamodb_resource, s3_client)
    return JSONResponse(content=result)


@app.post(
    "/api/admin/playbooks/{playbook_id}/versions/{version}/activate",
    include_in_schema=True,
)
async def post_admin_playbook_version_activate(
    playbook_id: str = Path(...),
    version: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: activate a playbook release-bundle version (issue #242).

    Enforces Gate 7 (`playbook_versions.content_hash ==
    playbook_versions.legal_approval.content_hash`) before activating, and
    on success writes `playbooks.active_release_bundle_hash` so
    `reviews.resolve_active_release_bundle_hash` actually serves the newly
    activated bundle -- see `src.playbook_versions.activate_release_bundle`
    for the full contract.

    Raises HTTP 403 for a non-admin caller, 404 for an unknown
    `(playbook_id, version)`, and 409 for a Gate 7 mismatch (the version's
    content_hash does not match its recorded legal approval -- the bundle
    cannot be activated).
    """
    if not _is_admin(caller_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to activate a playbook version.",
        )
    actor_identity = caller_row.get("cognito_sub", "")
    try:
        result = activate_release_bundle(
            playbook_id=playbook_id,
            version=version,
            actor_identity=actor_identity,
            dynamodb_resource=dynamodb_resource,
        )
    except PlaybookVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PlaybookVersionGate7MismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # Build an explicit, JSON-safe response rather than serializing the raw
    # DynamoDB item -- `uploaded_at` round-trips through boto3's resource
    # API as a Decimal, which json.dumps cannot serialize directly.
    return JSONResponse(
        content={
            "playbook_id": result.get("playbook_id"),
            "version": result.get("version"),
            "status": result.get("status"),
            "content_hash": result.get("content_hash"),
            "uploaded_by": result.get("uploaded_by"),
            "uploaded_at": (
                int(result["uploaded_at"]) if result.get("uploaded_at") is not None else None
            ),
        }
    )


@app.post("/api/corpus", include_in_schema=True)
async def post_corpus(
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    embed_fn: Any = Depends(get_embed_fn),
) -> JSONResponse:
    """Admin: corpus ingestion (issue #197).

    Runs the real ingestion pipeline over caller-supplied, already-extracted
    paragraphs -- clause extraction, content-addressed clause_ids, polarity
    separation, embeddings, staging-index ingestion, manifest hashing -- and
    returns the resulting draft (or failed) staging snapshot. Never
    activates the snapshot; activation is a separate, deliberate admin
    action (issue #20), outside this route's scope. Raises HTTP 403 for a
    non-admin caller.

    Body: {"source_document_id": str, "document_type": str,
    "paragraphs": [{"heading": str, "text": str}, ...],
    "corpus_snapshot_version": str, "playbook_id": str (optional),
    "counterparty_name": str | None (optional), "date": str | None
    (optional)}.

    Real `.docx` paragraph extraction is issue #80's job; this route's input
    is the same already-extracted-paragraphs shape corpus.py's module
    docstring documents as this pipeline's stub seam.
    """
    result = run_ingestion_request(
        caller_user_row=caller_row,
        source_document_id=body["source_document_id"],
        document_type=body["document_type"],
        paragraphs=body["paragraphs"],
        corpus_snapshot_version=body["corpus_snapshot_version"],
        # Issue #289: playbook_id=None -> run_ingestion_request/run_ingestion
        # resolve the registry's current default (playbook_registry.
        # default_playbook_id()) rather than a literal baked in here.
        playbook_id=body.get("playbook_id"),
        counterparty_name=body.get("counterparty_name"),
        date=body.get("date"),
        embed_fn=embed_fn,
    )
    # `_staging_index` is an in-process handle only (a live StagingIndex
    # object) -- never serialized to a response, same invariant as the
    # persisted DynamoDB snapshot record (see corpus.run_ingestion).
    result.pop("_staging_index", None)
    return JSONResponse(content=result)
