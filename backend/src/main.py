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
                  decide whether to render admin UI before it paints. Also
                  carries default_credentials_warning (#469).
  POST /api/me/password — authenticated: change the CALLER'S OWN password
                  (issue #469). Only valid for a username/password row (400
                  for an SSO row); 401 if current_password is wrong, 400 if
                  new_password is under demo_auth.MIN_PASSWORD_LENGTH chars.

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
                                (unaffected by this route). Sets an httpOnly/
                                Secure/SameSite=Strict session cookie rather
                                than returning the token in the body (#468).
  POST /api/auth/logout      — unauthenticated: clears the password-mode
                                session cookie set by /api/auth/login (#468).
                                POST /api/auth/login also 429s a (username,
                                source-IP) currently throttled/locked-out
                                from repeated failures (#469).

  GET  /api/admin/retention                     — admin: retention settings (#94).
  POST /api/admin/retention                     — admin: request a retention change,
                                                    dual-controlled for retroactive
                                                    reductions per #13/#61 (#94).
  POST /api/admin/retention/preview              — admin: pre-sweep purge preview (#94).
  GET  /api/admin/retention/holds                — admin: legal-hold list view (#94).
  POST /api/admin/retention/holds/{review_id}    — admin: place a legal hold (#94).
  DELETE /api/admin/retention/holds/{review_id}  — admin: release a legal hold (#94).

  GET  /api/admin/diagnostics/recent-failures  — admin: why recent reviews
                      failed (#443) — a bounded, newest-first list of recent
                      non-OK terminal reviews carrying ONLY review_id,
                      created_at, failing_stage, the #442 `reason` token, and
                      the terminal status. `?limit=N` is clamped into
                      [1, reviews.RECENT_FAILURES_MAX_LIMIT]. Deliberately not
                      a log viewer: no stack trace, exception message, prompt
                      or document substance, or key material is reachable
                      through it (the response is an explicit field
                      projection, see reviews.list_recent_failures).

  POST /api/admin/playbooks/{playbook_id}/versions/{version}/activate
                      — admin: activate a playbook release-bundle version,
                        Gate-7-enforced and wired to the resolver (#242).
                        Asserts content_hash == legal_approval.content_hash
                        before activating; on success writes
                        playbooks.active_release_bundle_hash so
                        reviews.resolve_active_release_bundle_hash actually
                        serves the newly activated bundle. A mismatch is
                        rejected with HTTP 409; an unknown version is 404.

  PATCH /api/admin/playbooks/{playbook_id}/versions/{version}/notes
                      — admin: set/replace the mutable `notes` string on a
                        playbook version (#411) — the ONE field that may
                        change after upload; content_hash/status stay
                        immutable. Body: {"notes": str}. Appends one
                        audit row (identifiers + notes_length, never the
                        note text) on change; surfaced read-only in
                        GET /api/playbooks (the active version's notes)
                        and in playbook_versions.list_playbook_version_trail.
                        An unknown version is 404.

  POST /api/admin/playbooks/{playbook_id}/pen-rules/validate
                      — admin: validate a candidate pen-rules /
                        posture-override document against a submitted OPF
                        (#432), reusing scripts/bind_bundle.py's fail-closed
                        validators. Returns one machine-readable error per
                        failure (unknown floor_ref, stale
                        parent_section_digest, non-monotonic version, colliding
                        floor_additions id). Read-only: validation only, no
                        persistence, no audit row — and zero runtime effect:
                        no live review consumes a v2 pen-rules/posture
                        *overrides* bundle (the document this route
                        validates), even though issue #479 made the pipeline
                        consume an activated OPF *document*. See
                        src.bundle_authoring.

  POST /api/admin/playbooks/{playbook_id}/versions
                      — admin: multipart upload of a new playbook version's
                        content (#430; parsing/validation/storage added by
                        #478). Rejected outright (413) over
                        `src.upload_validation.MAX_UPLOAD_SIZE_BYTES`, before
                        anything parses the body. The content hash is
                        computed server-side over the uploaded bytes and is
                        the value recorded; a client-supplied `content_hash`
                        form field, if present, is validated against it (400
                        on mismatch) but never trusted as the stored value.
                        The upload is detected (OPF `.opf.html` bundle / bare
                        OPF `.json` / legacy v1 `.json`), schema-validated,
                        agreement-type matched (OPF only), stub-basis
                        watermark checked (refused unless
                        `accept_stub_basis=true`), checked for a
                        `(playbook_id, version)` conflict (409, read BEFORE
                        either S3 write so a conflict can never orphan
                        bytes), then persisted to the uploads S3 bucket —
                        both at a content-addressed key and, for round-trip
                        retrievability, at a second key holding the ORIGINAL
                        uploaded bytes — BEFORE the version row is recorded.
                        Any failure (413/400/409) names the failing check;
                        nothing is written on any of them. New rows land
                        status="draft" (append-only — the version row is
                        itself the upload audit record).
                        Activation is the separate, Gate-7'd route above.

  GET /api/admin/playbooks/{playbook_id}/versions
                      — admin: the full version-upload trail for a playbook,
                        oldest first (#430) — playbook_id, version,
                        uploaded_by, uploaded_at, notes, content_hash,
                        artifact_kind, opf_content_hash, storage_key (#478,
                        each absent when not recorded), accepted_stub_basis
                        only, never document substance. Empty list for a
                        playbook with no uploads.

  POST /api/admin/playbooks/{playbook_id}/versions/{version}/rollback
                      — admin: roll back to a previously-activated version
                        (#430, resolver wiring fixed by #462) — restores it
                        as active, demoting the current active version, and
                        appends one release_bundle_rollback audit row. On
                        success also writes playbooks.active_release_bundle_
                        hash to the restored content_hash, the same write
                        the activate route performs, so reviews.resolve_
                        active_release_bundle_hash actually serves the
                        rolled-back bundle. Does not re-run Gate 7 (docs/
                        playbook-governance.md "Gate 7 on rollback"). An
                        unknown version is 404; a target that has never
                        been successfully activated is 409.

  GET  /api/admin/playbooks/{playbook_id}/instructions
                      — admin: the current standing-instructions version for
                        a playbook plus its append-only history, newest
                        first, capped ~50 (issue #482, epic #481). An
                        unknown playbook_id is 404; a playbook with nothing
                        ever saved returns current=null and an empty
                        history.

  POST /api/admin/playbooks/{playbook_id}/instructions
                      — admin: save a new standing-instructions version
                        (#482). Body: {"text": str, "expected_current_
                        version": int | None}. Append-only — always creates
                        version N+1, never edits a prior version.
                        `expected_current_version` gives compare-and-set
                        semantics: a stale page (or a losing concurrent
                        save) gets HTTP 409 with the actual current version
                        in the body, never a silent overwrite. HTTP 400 for
                        text over 10,000 characters; 404 for an unknown
                        playbook_id. Appends one audit row (identifiers +
                        text length only, never the text itself).

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

import hashlib
import logging
import os
import time
from typing import Any

import boto3
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Path, Request, UploadFile, status
from fastapi.responses import JSONResponse

from src import config
from src.auth import get_current_user
from src.bundle_authoring import validate_pen_rules_document
from src.corpus import deterministic_embed, run_ingestion_request
from src.demo_auth import (
    add_user,
    change_own_password,
    clear_demo_session_cookie,
    client_ip_from_request,
    default_credentials_warning,
    get_auth_mode_settings,
    issue_demo_token,
    login_with_password,
    remove_user,
    set_auth_mode,
    set_demo_session_cookie,
)
from src.model_settings import (
    clear_model_key,
    get_model_key_settings,
    get_model_selection_settings,
    set_model_key,
    set_model_selection,
)
from src.playbook_instructions import (
    PlaybookInstructionsConflictError,
    PlaybookInstructionsTooLargeError,
    get_current_instructions,
    list_instructions_history,
    save_instructions,
)
from src.playbook_upload import (
    PlaybookUploadRejected,
    original_artifact_key,
    storage_key_for,
    validate_playbook_upload,
)
from src.playbook_versions import (
    PlaybookVersionConflictError,
    PlaybookVersionGate7MismatchError,
    PlaybookVersionNotFoundError,
    PlaybookVersionRollbackError,
    activate_release_bundle,
    list_playbook_version_trail,
    record_playbook_version_upload,
    remove_playbook,
    rename_playbook,
    rollback_release_bundle,
    update_playbook_version_notes,
    version_already_recorded,
)
from src.review_routes import router as review_router
from src.reviews import RECENT_FAILURES_DEFAULT_LIMIT, list_recent_failures
from src.upload_validation import MAX_UPLOAD_SIZE_BYTES
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

logger = logging.getLogger(__name__)

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

    `cognito_sub` (issue #473) is the caller's own primary key, already
    known to them (it is the identity behind their own session) — it lets
    the Users & access UI recognize "this row is me" client-side to
    proactively disable/hide self-targeting actions the server would
    refuse, without trusting any client-side claim of *privilege*.

    `username` (issue #468) is present for a password-type row (`None` for
    an SSO row, which has no `username` attribute) — the password-mode SPA
    uses this to restore "Signed in as <username>" from the httpOnly
    session cookie on page load/reload, without ever holding a token to
    decode client-side.

    `default_credentials_warning` (issue #469) is true when the caller is
    STILL signed in with a shipped seed default (admin/admin, user/user) --
    always false for an SSO row. Restoring this on every /api/me call (not
    just the login response) is what makes the banner persist across a
    reload and clear the instant the password is rotated (demo_auth.
    change_own_password rewrites password_hash, so the very next check
    against the seed default stops matching).
    """
    return JSONResponse(
        content={
            "is_admin": _is_admin(caller_row),
            "cognito_sub": caller_row.get("cognito_sub", ""),
            "username": caller_row.get("username"),
            "default_credentials_warning": default_credentials_warning(caller_row),
        }
    )


@app.post("/api/me/password", include_in_schema=True)
async def post_me_password(
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Authenticated: change the CALLER'S OWN password (issue #469).

    Body: {"current_password": str, "new_password": str}. Only valid for a
    username/password-type row -- an SSO row has no password to change
    (HTTP 400). Raises HTTP 401 if current_password does not match the
    stored hash, 400 if new_password is under demo_auth.MIN_PASSWORD_LENGTH
    characters. On success the row's password_hash is rotated (so the OLD
    password stops working on the caller's next login) and one
    `password_changed` audit row is appended -- identifiers only, never a
    plaintext password or the hash itself.
    """
    result = change_own_password(
        body.get("current_password", ""),
        body.get("new_password", ""),
        caller_row,
        dynamodb_resource,
    )
    return JSONResponse(content=result)


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
    target user does not exist, and 409 if the update would strip admin
    access (suspend, deprovision, or revoke-admin) from the LAST active
    admin, self-targeting or not.
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


@app.get("/api/admin/model-key", include_in_schema=True)
async def get_admin_model_key(
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: status of the instance-wide model-provider (OpenRouter) API key.

    Returns whether a key is loaded, which source it came from (admin-set row
    or the OPENROUTER_API_KEY env var), and a last-four hint — never the key
    itself (src/model_settings.py is write-only by design). Raises HTTP 403
    for a non-admin caller.
    """
    settings = get_model_key_settings(caller_row, dynamodb_resource)
    return JSONResponse(content=settings)


@app.post("/api/admin/model-key", include_in_schema=True)
async def post_admin_model_key(
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: set the instance-wide model-provider (OpenRouter) API key.

    Body: {"api_key": str}. Overrides OPENROUTER_API_KEY for every subsequent
    review. Raises HTTP 403 for a non-admin caller, 400 for an invalid key or
    on a deployment with no admin-managed key store (the AWS/Bedrock target).
    """
    result = set_model_key(body.get("api_key", ""), caller_row, dynamodb_resource)
    return JSONResponse(content=result)


@app.delete("/api/admin/model-key", include_in_schema=True)
async def delete_admin_model_key(
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: clear the admin-set model-provider API key, reverting to
    OPENROUTER_API_KEY. Idempotent. Raises HTTP 403 for a non-admin caller.
    """
    result = clear_model_key(caller_row, dynamodb_resource)
    return JSONResponse(content=result)


@app.get("/api/admin/model-selection", include_in_schema=True)
async def get_admin_model_selection(
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: which models reviews run on, and which ones may be chosen
    (issue #445).

    Returns the selectable catalogue from model-policy/openrouter.json (ids,
    tier labels, notes, per-million rates) plus the stored and effective
    primary/critic ids. A SIBLING of /api/admin/model-key rather than an
    extension of it, so the key route's write-only response shape is
    untouched — nothing here reads or returns key material. Raises HTTP 403
    for a non-admin caller.
    """
    settings = get_model_selection_settings(caller_row, dynamodb_resource)
    return JSONResponse(content=settings)


@app.post("/api/admin/model-selection", include_in_schema=True)
async def post_admin_model_selection(
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: set the instance-wide primary/critic model choice (issue #445).

    Body: {"primary_model_id": str|null, "critic_model_id": str|null} — each
    must be on model-policy/openrouter.json's `selectable` allowlist, or
    ""/null to revert that role to the policy default. Takes effect on the
    next review, no redeploy. Raises HTTP 403 for a non-admin caller, 400 for
    an unlisted model or on a deployment with no settings store.
    """
    result = set_model_selection(
        body.get("primary_model_id"),
        body.get("critic_model_id"),
        caller_row,
        dynamodb_resource,
    )
    return JSONResponse(content=result)


@app.post("/api/auth/login", include_in_schema=True)
async def post_auth_login(
    request: Request,
    body: dict[str, Any] = Body(...),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Unauthenticated: username/password sign-in for the demo auth feature
    (issue #232), gated by the stored auth-mode setting. This is deliberately
    NOT behind get_active_user_row/get_current_user — a caller attempting to
    log in does not yet hold a Cognito bearer token. The existing Cognito
    hosted-UI SSO flow is unaffected by this route.

    Body: {"username": str, "password": str}. On success returns the user
    summary and sets a short-lived, httpOnly/Secure/SameSite=Strict session
    cookie (issue #468) that get_current_user verifies on subsequent
    /api/* requests in `password`/`both` mode — the token itself is never
    present in the response body, so it is never reachable from page JS
    (see demo_auth.py's session-cookie posture comment). Raises HTTP 403 if
    the stored mode does not permit password sign-in, 429 if this (username,
    source-IP) is currently throttled/locked out from repeated failures
    (issue #469 — see demo_auth.py's login-throttle comment), 401 for an
    unknown user or wrong password, 403 if the matched row's lifecycle
    status is not active.
    """
    result = login_with_password(
        body["username"],
        body["password"],
        dynamodb_resource,
        client_ip=client_ip_from_request(request),
    )
    token = issue_demo_token(result)
    response = JSONResponse(content=result)
    set_demo_session_cookie(response, token)
    return response


@app.post("/api/auth/logout", include_in_schema=True)
async def post_auth_logout() -> JSONResponse:
    """Unauthenticated: clears the password-mode session cookie (issue
    #468). Safe to call with no session present (e.g. double sign-out, an
    already-expired cookie) — always 200. Cognito/Amplify sign-out is a
    separate client-side flow (Authenticator's own signOut) and is
    unaffected by this route."""
    response = JSONResponse(content={"signed_out": True})
    clear_demo_session_cookie(response)
    return response


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


@app.get("/api/admin/diagnostics/recent-failures", include_in_schema=True)
async def get_admin_diagnostics_recent_failures(
    limit: int = RECENT_FAILURES_DEFAULT_LIMIT,
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: why recent reviews failed (issue #443).

    Returns a BOUNDED list of recent non-OK terminal reviews, newest first,
    each carrying only `review_id`, `created_at`, `failing_stage`, the #442
    `reason` token, and the terminal `status`. `limit` is clamped into
    [1, `reviews.RECENT_FAILURES_MAX_LIMIT`], so no request can turn this
    into a full-table dump.

    This is NOT a log viewer, and deliberately so: no stack trace, exception
    message, prompt or document substance, key material, or raw endpoint is
    reachable through it. The response is projected through
    `reviews._RECENT_FAILURE_FIELDS`; see `reviews.list_recent_failures`.

    Raises HTTP 403 for a non-admin caller.
    """
    failures = list_recent_failures(caller_row, dynamodb_resource, limit=limit)
    return JSONResponse(content={"failures": failures})


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


@app.post(
    "/api/admin/playbooks/{playbook_id}/versions",
    include_in_schema=True,
)
async def post_admin_playbook_version_upload(
    playbook_id: str = Path(...),
    file: UploadFile = File(...),
    version: str = Form(...),
    content_hash: str | None = Form(None),
    accept_stub_basis: bool = Form(False),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
    s3_client: Any = Depends(get_s3_client),
) -> JSONResponse:
    """Admin: upload a new playbook release-bundle version (issue #430,
    extended by issue #478 to parse, validate, and persist the bytes).

    Multipart upload of a new version's content. The content hash is computed
    server-side over the uploaded bytes (`"sha256:" + sha256(bytes)`) and is
    the value recorded -- a client-supplied `content_hash` form field, when
    present, is treated only as an integrity claim to validate against the
    server-computed hash, never trusted as the stored value (a mismatch is
    rejected with HTTP 400). New rows land `status="draft"`; the version row
    is itself the append-only upload audit record (see
    `src.playbook_versions.record_playbook_version_upload`). Activation is a
    separate, Gate-7-enforced admin action
    (`POST .../versions/{version}/activate` above).

    Issue #478: the uploaded bytes are no longer dropped. The body is first
    checked against `src.upload_validation.MAX_UPLOAD_SIZE_BYTES` (413 if
    over-cap, before anything parses it), then detected (OPF 0.2/0.3
    `.opf.html` bundle, bare OPF `.json`, or legacy v1 `.json`), validated
    (`src.playbook_upload.validate_playbook_upload` -- schema,
    `identity.content_hash`, injection scan, agreement-type match, stub-basis
    watermark), and -- only once a `(playbook_id, version)` conflict has
    already been ruled out -- persisted to the uploads S3 bucket at a
    content-addressed key (`playbooks/{playbook_id}/{hash}.json`, plus the
    ORIGINAL uploaded bytes at a second key for every artifact kind, so
    `content_hash` always addresses a retrievable object) BEFORE the version
    row is recorded. A file that fails any check is refused with HTTP
    413/400/409 naming the failing check, and nothing is written -- the
    conflict check runs as a read before either S3 write, precisely so a
    409 never leaves orphaned bytes behind.

    Body (multipart/form-data): `file` (the version content), `version` (the
    new version identifier), optional `content_hash` (an integrity claim, the
    full `"sha256:<hex>"` form), optional `accept_stub_basis` (default false
    -- required to accept an OPF artifact watermarked
    `compiler.stub_basis_present`). Raises HTTP 403 for a non-admin caller,
    413 for a body over `MAX_UPLOAD_SIZE_BYTES`, 400 for a missing/blank
    `version`, a content-hash mismatch, or any artifact-validation failure
    (bad JSON, schema violation, hash mismatch, agreement-type mismatch, or
    an un-accepted stub-basis watermark), 409 if `(playbook_id, version)` was
    already uploaded (append-only -- re-uploads must use a new version
    identifier).
    """
    if not _is_admin(caller_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to upload a playbook version.",
        )
    if not version.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must include a non-empty 'version' field.",
        )
    contents = await file.read()
    # Size cap BEFORE anything else touches the bytes -- parsing, schema
    # validation, canonicalization, and the injection scan all cost more
    # than hashing, so an over-cap body must never reach them (issue #478
    # fix round 1: this route previously read, JSON-parsed, and fully
    # schema-validated an unbounded body before ever checking its size).
    if len(contents) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Upload exceeds the maximum allowed size of "
                f"{MAX_UPLOAD_SIZE_BYTES} bytes."
            ),
        )
    # Server-computed hash is authoritative; a client-supplied hash is only
    # ever validated against it, never trusted as the value we record.
    computed_hash = "sha256:" + hashlib.sha256(contents).hexdigest()
    if content_hash is not None and content_hash != computed_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Content-hash mismatch: the uploaded bytes do not match the "
                "supplied content_hash (the server computes the hash itself; a "
                "client-supplied hash is validated, never trusted)."
            ),
        )

    try:
        validated = validate_playbook_upload(
            filename=file.filename or "",
            contents=contents,
            playbook_id=playbook_id,
            accept_stub_basis=accept_stub_basis,
        )
    except PlaybookUploadRejected as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Reject a (playbook_id, version) conflict BEFORE writing anything to
    # S3 -- issue #478 fix round 1: writing the artifact first and only then
    # discovering the version row already exists (via
    # record_playbook_version_upload's ConditionExpression, below) orphaned
    # the just-written bytes on every re-upload 409, since nothing pointed
    # back at them. This is a plain read; the ConditionExpression on the
    # eventual put_item remains the actual append-only enforcement (races
    # this pre-check cannot see are still caught there).
    if version_already_recorded(playbook_id, version, dynamodb_resource):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"playbook version already recorded: playbook_id={playbook_id!r} "
                f"version={version!r} (append-only — re-uploads must use a new "
                "version identifier)"
            ),
        )

    # Persist the validated artifact to the uploads S3 bucket at a
    # content-addressed key BEFORE recording the version row -- a row is
    # never recorded for bytes that were not (or could not be) written.
    storage_bytes = validated.storage_text.encode("utf-8")
    storage_hash_hex = hashlib.sha256(storage_bytes).hexdigest()
    storage_key = storage_key_for(playbook_id, storage_hash_hex)
    uploads_bucket = os.environ.get("UPLOADS_BUCKET", "")
    if not uploads_bucket:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="UPLOADS_BUCKET not configured.",
        )
    s3_client.put_object(Bucket=uploads_bucket, Key=storage_key, Body=storage_bytes)
    # "...plus the original artifact" (issue #478 step 3): the raw uploaded
    # bytes, kept alongside the canonical text above at a key that can never
    # collide with it -- for EVERY artifact kind (not only `.opf.html`), so
    # the row's `content_hash` (the hash of these exact raw bytes) always
    # addresses a retrievable object (fix round 1, AC1's round-trip check).
    s3_client.put_object(
        Bucket=uploads_bucket,
        Key=original_artifact_key(
            playbook_id, hashlib.sha256(contents).hexdigest(), filename=file.filename or ""
        ),
        Body=contents,
    )

    try:
        result = record_playbook_version_upload(
            playbook_id=playbook_id,
            version=version,
            uploader_identity=caller_row.get("cognito_sub", ""),
            dynamodb_resource=dynamodb_resource,
            content_hash=computed_hash,
            artifact_kind=validated.artifact_kind,
            opf_content_hash=validated.opf_content_hash,
            storage_key=storage_key,
            accepted_stub_basis=validated.accepted_stub_basis,
        )
    except PlaybookVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return JSONResponse(
        content={
            "playbook_id": result.get("playbook_id"),
            "version": result.get("version"),
            "status": result.get("status"),
            "content_hash": result.get("content_hash"),
            "artifact_kind": result.get("artifact_kind"),
            "opf_content_hash": result.get("opf_content_hash"),
            "storage_key": result.get("storage_key"),
            "accepted_stub_basis": result.get("accepted_stub_basis", False),
            "uploaded_by": result.get("uploaded_by"),
            "uploaded_at": (
                int(result["uploaded_at"]) if result.get("uploaded_at") is not None else None
            ),
        }
    )


@app.get(
    "/api/admin/playbooks/{playbook_id}/versions",
    include_in_schema=True,
)
async def get_admin_playbook_versions(
    playbook_id: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: the full version-upload trail for a playbook, oldest first
    (issue #430).

    Returns identifiers, timestamps, and the mutable `notes` field only --
    `playbook_id`, `version`, `uploaded_by`, `uploaded_at`, `notes` -- never
    document substance (see
    `src.playbook_versions.list_playbook_version_trail`). A playbook with no
    uploaded versions returns an empty list. Raises HTTP 403 for a non-admin
    caller.
    """
    if not _is_admin(caller_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to view a playbook's version trail.",
        )
    trail = list_playbook_version_trail(playbook_id, dynamodb_resource)
    return JSONResponse(content={"versions": trail})


@app.post(
    "/api/admin/playbooks/{playbook_id}/versions/{version}/rollback",
    include_in_schema=True,
)
async def post_admin_playbook_version_rollback(
    playbook_id: str = Path(...),
    version: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: roll back to a previously-active playbook version (issue #430,
    resolver wiring fixed by issue #462).

    Restores a version this module previously demoted from `active` to
    `retired` as the active bundle; any currently-active version is demoted to
    `retired` as part of the same rollback, and one append-only audit row
    (`release_bundle_rollback`) is written. On success also writes
    `playbooks.active_release_bundle_hash` to the restored version's
    `content_hash` -- the same resolver write `.../activate` performs -- so
    `reviews.resolve_active_release_bundle_hash` actually serves the
    rolled-back bundle instead of continuing to run reviews under the bundle
    that was just rolled back -- see
    `src.playbook_versions.rollback_release_bundle`. Rolling back to a
    version that was never successfully activated is not a rollback (callers
    should activate it instead); rollback itself deliberately does not
    re-run Gate 7 (docs/playbook-governance.md "Gate 7 on rollback").

    Raises HTTP 403 for a non-admin caller, 404 for an unknown
    `(playbook_id, version)`, and 409 if the target version has never been
    successfully activated -- there is nothing to roll back to.
    """
    if not _is_admin(caller_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to roll back a playbook version.",
        )
    try:
        result = rollback_release_bundle(
            playbook_id=playbook_id,
            version=version,
            actor_identity=caller_row.get("cognito_sub", ""),
            dynamodb_resource=dynamodb_resource,
        )
    except PlaybookVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PlaybookVersionRollbackError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # Same JSON-safe construction as the activate route: `uploaded_at` is a
    # Decimal off the DynamoDB item and must be coerced to int.
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


@app.patch(
    "/api/admin/playbooks/{playbook_id}/versions/{version}/notes",
    include_in_schema=True,
)
async def patch_admin_playbook_version_notes(
    playbook_id: str = Path(...),
    version: str = Path(...),
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: set/replace the `notes` field on a playbook version (issue
    #411) — the one deliberately-mutable field on an otherwise-immutable
    `playbook_versions` row. `content_hash`/`status`/`uploaded_by`/
    `uploaded_at` are untouched by this route; re-uploading a version's
    content is still rejected elsewhere (append-only). See
    `src.playbook_versions.update_playbook_version_notes` for the full
    contract, including its audit posture (identifiers + notes_length
    only — never the note text itself).

    Body: `{"notes": str}`. Raises HTTP 403 for a non-admin caller, 400 if
    `notes` is missing or not a string, 404 for an unknown
    `(playbook_id, version)`.
    """
    if not _is_admin(caller_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to update playbook version notes.",
        )
    notes = body.get("notes")
    if not isinstance(notes, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must include a string 'notes' field.",
        )
    actor_identity = caller_row.get("cognito_sub", "")
    try:
        result = update_playbook_version_notes(
            playbook_id=playbook_id,
            version=version,
            notes=notes,
            actor_identity=actor_identity,
            dynamodb_resource=dynamodb_resource,
        )
    except PlaybookVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return JSONResponse(
        content={
            "playbook_id": result.get("playbook_id"),
            "version": result.get("version"),
            "notes": result.get("notes", ""),
        }
    )


@app.post(
    "/api/admin/playbooks/{playbook_id}/pen-rules/validate",
    include_in_schema=True,
)
async def post_admin_playbook_pen_rules_validate(
    playbook_id: str = Path(...),
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
) -> JSONResponse:
    """Admin: validate a candidate pen-rules / posture-override document for a
    playbook (issue #432) — the backend surface an authoring UI (separate,
    dependent ticket) calls before it can offer to bind one.

    Reuses `scripts/bind_bundle.py`'s own fail-closed validators (never
    reimplements them) and returns one machine-readable error per failure —
    `unknown_floor_ref`, `stale_parent_section_digest`, `non_monotonic_version`,
    `colliding_floor_additions` (plus `playbook_id_mismatch`) — so the frontend
    can point at the specific offending field. See
    `src.bundle_authoring.validate_pen_rules_document` for the request/response
    contract, including why the OPF document is supplied in the body.

    Read-only: validation only, **no persistence** and therefore no audit
    entry. Even a valid document has zero runtime effect — no live review
    consumes a v2 pen-rules/posture *overrides* bundle (the document this
    route validates): `pipeline_runner._load_opf_bundle_if_active` hard-codes
    `"overrides": None` on every OPF bundle it builds, so this route's output
    is never read by `review_knowledge.resolve_knowledge`. This is narrower
    than it used to be — issue #479 made the pipeline consume an activated
    OPF *document* itself — but the overrides bundle stays unconsumed;
    persist/activate is a deliberate follow-up. Raises HTTP 403 for a
    non-admin caller and HTTP 400
    for a malformed body (missing/non-object `opf`, or a wrong-typed field). A
    well-formed document that fails a rule is a 200 with `valid: false`, not an
    HTTP error.
    """
    result = validate_pen_rules_document(playbook_id, body, caller_row)
    return JSONResponse(content=result)


def _require_registered_playbook(playbook_id: str) -> None:
    """404 for a playbook_id the registry does not list. The registry is the
    catalog's source of truth (see `src.review_routes._load_playbook_catalog`),
    so renaming/removing an unlisted id is a client error, not a silent
    no-op that writes an orphan `playbooks` row."""
    import playbook_registry  # local import: same sys.path seam src.sample_playbooks uses

    if playbook_id not in playbook_registry.list_playbook_ids():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown playbook_id: {playbook_id!r}",
        )


@app.get(
    "/api/admin/playbooks/{playbook_id}/instructions",
    include_in_schema=True,
)
async def get_admin_playbook_instructions(
    playbook_id: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: the current standing-instructions version for a playbook plus
    its append-only history (issue #482, epic #481).

    Returns `{"current": {version, text, saved_by, saved_at} | null,
    "history": [...]}` — history newest-first, capped ~50 (see
    `src.playbook_instructions.list_instructions_history`). A playbook with
    nothing ever saved returns `current: null` and an empty history, never
    an error.

    Raises HTTP 403 for a non-admin caller, 404 for a playbook_id the
    registry does not list.
    """
    if not _is_admin(caller_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to view standing instructions.",
        )
    _require_registered_playbook(playbook_id)

    current = get_current_instructions(playbook_id, dynamodb_resource)
    history = list_instructions_history(playbook_id, dynamodb_resource)

    def _shape(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": int(item["version"]),
            "text": item.get("text", ""),
            "saved_by": item.get("saved_by"),
            "saved_at": int(item["saved_at"]) if item.get("saved_at") is not None else None,
        }

    return JSONResponse(
        content={
            "current": _shape(current) if current else None,
            "history": [_shape(item) for item in history],
        }
    )


@app.post(
    "/api/admin/playbooks/{playbook_id}/instructions",
    include_in_schema=True,
)
async def post_admin_playbook_instructions(
    playbook_id: str = Path(...),
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: save a new standing-instructions version for a playbook
    (issue #482, epic #481) — append-only, always creates version N+1.

    Body: `{"text": str, "expected_current_version": int | None}`.
    `expected_current_version` gives compare-and-set semantics: when
    supplied and it no longer matches the actual current version (a stale
    admin page, or a losing concurrent save), the save is refused with
    HTTP 409 and the actual current version in the body, rather than
    silently superseding a save the caller never saw — see
    `src.playbook_instructions.save_instructions` for the full contract,
    including the append-only conditional write that makes two concurrent
    saves unable to both claim the same version number.

    Text is trusted first-party admin input — the same trust class as
    `toaster_guidance` (`src.review_routes.post_review`'s per-review
    free-text box) — never logged in full: this route logs only
    `playbook_id`/`version`/text length at INFO, never the text itself, and
    the audit row `save_instructions` appends carries the same
    identifiers-only shape.

    Raises HTTP 403 for a non-admin caller, 400 if `text` is missing/not a
    string or over 10,000 characters, 404 for an unknown playbook_id, and
    409 for a version conflict (`{"detail": ..., "current_version": int}`).
    """
    if not _is_admin(caller_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to save standing instructions.",
        )
    _require_registered_playbook(playbook_id)

    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must include a string 'text' field.",
        )
    expected_current_version = body.get("expected_current_version")
    if expected_current_version is not None and not isinstance(expected_current_version, int):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'expected_current_version' must be an integer or null.",
        )

    actor_identity = caller_row.get("cognito_sub", "")
    try:
        result = save_instructions(
            playbook_id=playbook_id,
            text=text,
            saved_by=actor_identity,
            dynamodb_resource=dynamodb_resource,
            expected_current_version=expected_current_version,
        )
    except PlaybookInstructionsTooLargeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except PlaybookInstructionsConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "current_version": exc.current_version,
            },
        ) from exc

    # Never the text itself — identifiers + length only (module docstring
    # "Security invariants": CloudWatch must never log document content).
    logger.info(
        "PLAYBOOK_INSTRUCTIONS_SAVE: playbook_id=%s version=%s text_length=%s",
        playbook_id,
        result["version"],
        len(text),
    )

    return JSONResponse(
        content={
            "playbook_id": playbook_id,
            "version": int(result["version"]),
            "saved_by": result.get("saved_by"),
            "saved_at": int(result["saved_at"]) if result.get("saved_at") is not None else None,
        }
    )


@app.patch("/api/admin/playbooks/{playbook_id}", include_in_schema=True)
async def patch_admin_playbook(
    playbook_id: str = Path(...),
    body: dict[str, Any] = Body(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: rename a playbook — set the catalog `display_name` (issue
    #412). A presentation-only override stored on the `playbooks` DB row so
    it survives a deploy (the registry is baked into the image); the
    `playbook_id` every version row and review record is keyed on is NOT
    touched. An empty string restores the registry's shipped name.

    Body: `{"display_name": str}`. Raises HTTP 403 for a non-admin caller,
    400 if `display_name` is missing or not a string, 404 for a
    playbook_id the registry does not list.
    """
    if not _is_admin(caller_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to rename a playbook.",
        )
    display_name = body.get("display_name")
    if not isinstance(display_name, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must include a string 'display_name' field.",
        )
    _require_registered_playbook(playbook_id)
    result = rename_playbook(
        playbook_id=playbook_id,
        display_name=display_name,
        actor_identity=caller_row.get("cognito_sub", ""),
        dynamodb_resource=dynamodb_resource,
    )
    return JSONResponse(content=result)


@app.delete("/api/admin/playbooks/{playbook_id}", include_in_schema=True)
async def delete_admin_playbook(
    playbook_id: str = Path(...),
    caller_row: dict[str, Any] = Depends(get_active_user_row),
    dynamodb_resource: Any = Depends(get_dynamodb_resource),
) -> JSONResponse:
    """Admin: remove a playbook from the catalog (issue #412) — deletes its
    `playbook_versions` rows, clears the active bundle, and writes the
    `removed` tombstone the catalog filters on. See
    `src.playbook_versions.remove_playbook` for why the tombstone is
    load-bearing (the registry is a file baked into the image, so the entry
    would otherwise re-appear on the next request).

    Removal is currently a ONE-WAY DOOR for every playbook alike: nothing in
    this codebase clears the tombstone. Issue #433 retired the bespoke
    re-activate-the-sample path, which used to clear it for the shipped
    sample only, and deliberately did not replace it with a seed that
    re-installs on restart — a container restart resurrecting a playbook an
    admin removed would be worse than removal being irreversible. A generic
    restore belongs with the Playbooks admin surface (issue #434).

    Raises HTTP 403 for a non-admin caller, 404 for a playbook_id the
    registry does not list.
    """
    if not _is_admin(caller_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to remove a playbook.",
        )
    _require_registered_playbook(playbook_id)
    result = remove_playbook(
        playbook_id=playbook_id,
        actor_identity=caller_row.get("cognito_sub", ""),
        dynamodb_resource=dynamodb_resource,
    )
    return JSONResponse(content=result)


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
