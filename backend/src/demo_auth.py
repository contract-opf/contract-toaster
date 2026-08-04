"""
Demo auth-mode config + seeded credentials + user CRUD — issue #232.

Current scope (v1, maintainer-confirmed 2026-07-10): a **demo feature** with
an admin-configurable auth METHOD toggle -- "access only (SSO)", "username +
password", and "both" -- plus seeded demo credentials and a `users` table
that supports BOTH SSO and username/password users with add/remove CRUD.
This module is the BACKEND slice only (moto-tested, offline, no live AWS/
network): it implements

  - `get_auth_mode_settings` / `set_auth_mode`: the stored+served auth-mode
    setting (mirrors the `retention_settings` global-row convention in
    src/retention.py -- one row, `setting_id="global"`, admin read/write).
  - `seed_demo_users`: idempotently seeds `admin`/`admin` (admin role) and
    `user`/`user` (user role) username/password credentials into the SAME
    `users` DynamoDB table SSO rows already live in (src/users.py), keyed by
    a synthetic `local:<username>` value in the existing `cognito_sub`
    partition key -- no table-schema change, since DynamoDB items are
    schemaless beyond the declared key.
  - `add_user` / `remove_user`: admin-gated CRUD covering BOTH user types
    (`user_type` in {"sso", "password"}).
  - `login_with_password` / `sso_admission_allowed`: the credential-path
    gates the stored auth mode controls -- password sign-in is rejected
    outside `password`/`both`, and SSO admission is intact under `sso`/
    `both` (never disabled outside the `password`-only mode).

Explicitly NOT built in this slice (see issue #232 "Current scope (v1)" ->
"Explicitly OUT of this slice (follow-on)" and the maintainer's scope-size
note): the admin toggle UI (frontend; depends on this backend surface) and
Cognito username/password IdP support in infra/lib/nested/auth-stack.ts (the
live SSO path enforced by backend/src/auth.py's Cognito JWT verification is
unaffected by this module -- `sso_admission_allowed` is a pure gating
function this slice exposes for the mode-toggle logic and for the follow-on
to wire into the live auth path, not a change to that path itself). A real
deployment additionally needs a CDK-provisioned `AUTH_SETTINGS_TABLE` (mirror
of `RetentionSettingsTable` in infra/lib/nested/data-stack.ts) -- also a
follow-on; this slice's own test suite (tests/test_demo_auth_232.py) creates
that table via moto.

De-brand (release directive): no tenant-brand strings in any label, error message,
or log line this module emits -- "your" voicing only.

Environment variables consumed:
  USERS_TABLE          DynamoDB users table name (PK: cognito_sub) -- same
                        table and env var as src/users.py.
  AUTH_SETTINGS_TABLE   DynamoDB auth-mode settings table name (PK: setting_id)
  AUDIT_TABLE           DynamoDB audit table name (append-only; PK: partition,
                        SK: timestamp#event_id) -- same table as src/users.py.
  TRUST_PROXY_HEADERS   Issue #469: "1"/"true"/"yes" to trust a caller-supplied
                        `X-Forwarded-For` for login-throttle IP bucketing
                        (client_ip_from_request). Defaults to OFF -- unset
                        anywhere the backend can be reached without going
                        through a hop that sets this header trustworthily.
                        See client_ip_from_request/_trust_proxy_headers.
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
import uuid
from typing import Any

from fastapi import HTTPException, Request, Response, status
from jose import JWTError, jwt

try:  # production runs `src.main`; tests put backend/src on sys.path
    from src.users import public_user_view
except ImportError:  # pragma: no cover
    from users import public_user_view  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth modes
# ---------------------------------------------------------------------------
AUTH_MODE_SSO = "sso"
AUTH_MODE_PASSWORD = "password"
AUTH_MODE_BOTH = "both"
VALID_AUTH_MODES = {AUTH_MODE_SSO, AUTH_MODE_PASSWORD, AUTH_MODE_BOTH}

# Default: SSO-only -- matches the pre-#232 behavior (Google Workspace SSO
# was the only admission path) when no admin has ever touched the toggle.
DEFAULT_AUTH_MODE = AUTH_MODE_SSO

AUTH_MODE_SETTING_ID = "global"

# Labeled options for the auth-mode toggle surface. De-brand directive: no
# tenant-brand strings in any label -- "your" voicing only.
AUTH_MODE_OPTIONS: tuple[dict[str, Any], ...] = (
    {"value": AUTH_MODE_SSO, "label": "Access only (single sign-on)"},
    {"value": AUTH_MODE_PASSWORD, "label": "Username and password"},
    {"value": AUTH_MODE_BOTH, "label": "Both — single sign-on and username/password"},
)

# ---------------------------------------------------------------------------
# User types
# ---------------------------------------------------------------------------
USER_TYPE_SSO = "sso"
USER_TYPE_PASSWORD = "password"
VALID_USER_TYPES = {USER_TYPE_SSO, USER_TYPE_PASSWORD}

# Prefix for the synthetic cognito_sub value used to key password-type users
# in the same sub-keyed `users` table SSO rows already live in.
_LOCAL_SUB_PREFIX = "local:"

# Seeded demo credentials (issue #232 acceptance: "admin/admin (admin role)
# and user/user (user role)"). Passwords are hashed before storage --
# see _hash_password -- never persisted or logged in plaintext.
SEED_USERS: tuple[dict[str, Any], ...] = (
    {"username": "admin", "password": "admin", "role": "admin", "is_admin": True},
    {"username": "user", "password": "user", "role": "user", "is_admin": False},
)

# PBKDF2-HMAC-SHA256 iteration count. stdlib-only (hashlib) -- no bcrypt/
# argon2/passlib dependency is declared in backend/requirements.txt, and this
# is a demo feature, not the production credential store.
_PBKDF2_ITERATIONS = 260_000


def local_user_sub(username: str) -> str:
    """The synthetic `cognito_sub` a username/password user is keyed by in
    the shared `users` table."""
    return f"{_LOCAL_SUB_PREFIX}{username}"


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(candidate.hex(), digest_hex)


# ---------------------------------------------------------------------------
# Table accessors -- same tables/env-vars as src/users.py and src/retention.py.
# ---------------------------------------------------------------------------


def _users_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["USERS_TABLE"])


def _auth_settings_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["AUTH_SETTINGS_TABLE"])


def _audit_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["AUDIT_TABLE"])


def _is_admin(caller_user_row: dict[str, Any]) -> bool:
    """`is_admin` is a DynamoDB `users`-row flag, never a JWT claim -- same
    convention as src/users.py::_is_admin and src/retention.py::_is_admin."""
    return bool(caller_user_row.get("is_admin", False))


def _require_admin(caller_user_row: dict[str, Any], detail: str) -> None:
    if not _is_admin(caller_user_row):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _write_audit_entry(
    dynamodb_resource: Any,
    actor: str,
    action: str,
    target: str,
    target_type: str,
    detail: dict[str, Any],
) -> None:
    """Append an immutable audit row -- identifiers and setting values only,
    never a plaintext password (same posture as src/users.py and
    src/retention.py's own _write_audit_entry)."""
    table = _audit_table(dynamodb_resource)
    now = time.time()
    event_id = uuid.uuid4().hex
    partition = time.strftime("%Y-%m", time.gmtime(now))
    timestamp = f"{int(now)}#{event_id}"

    item: dict[str, Any] = {
        "partition": partition,
        "timestamp": timestamp,
        "event_id": event_id,
        "actor": actor,
        "action": action,
        "target": target,
        "target_type": target_type,
        "outcome": "success",
    }
    item.update(detail)
    table.put_item(Item=item)


# ---------------------------------------------------------------------------
# Auth-mode settings -- GET/POST (admin), mirrors src/retention.py exactly.
# ---------------------------------------------------------------------------


def get_auth_mode_settings(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """GET /api/admin/auth-mode.

    Returns the stored auth mode (defaulting to `sso` when no row exists
    yet -- the pre-#232 SSO-only behavior), plus the selectable
    `auth_mode_options` so a caller can render the toggle without
    hard-coding the choices. Raises HTTPException(403) if the caller is not
    an admin.
    """
    _require_admin(caller_user_row, "Admin privilege required to view the auth-mode setting.")

    table = _auth_settings_table(dynamodb_resource)
    resp = table.get_item(Key={"setting_id": AUTH_MODE_SETTING_ID})
    item = resp.get("Item")
    mode = item.get("auth_mode", DEFAULT_AUTH_MODE) if item else DEFAULT_AUTH_MODE

    return {
        "setting_id": AUTH_MODE_SETTING_ID,
        "auth_mode": mode,
        "default_auth_mode": DEFAULT_AUTH_MODE,
        "auth_mode_options": [dict(opt) for opt in AUTH_MODE_OPTIONS],
    }


def set_auth_mode(
    new_mode: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """POST /api/admin/auth-mode.

    Sets the stored auth mode to one of `sso` / `password` / `both`.
    Applies immediately, single-admin (no dual-control gate -- unlike
    retroactive retention reductions, toggling the auth method is not an
    irreversible data-destructive action). Raises HTTPException(403) if the
    caller is not an admin, 400 if `new_mode` is not a valid mode.
    """
    _require_admin(caller_user_row, "Admin privilege required to change the auth-mode setting.")

    if new_mode not in VALID_AUTH_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"auth_mode must be one of {sorted(VALID_AUTH_MODES)}.",
        )

    before = get_auth_mode_settings(caller_user_row, dynamodb_resource)["auth_mode"]

    table = _auth_settings_table(dynamodb_resource)
    table.update_item(
        Key={"setting_id": AUTH_MODE_SETTING_ID},
        UpdateExpression="SET auth_mode = :m, updated_at = :t",
        ExpressionAttributeValues={":m": new_mode, ":t": str(int(time.time()))},
    )

    actor = caller_user_row.get("cognito_sub", "")
    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="auth_mode_change",
        target=AUTH_MODE_SETTING_ID,
        target_type="auth_settings",
        detail={"before_auth_mode": before, "after_auth_mode": new_mode},
    )

    logger.info("AUTH_MODE_CHANGE: actor=%s before=%s after=%s", actor, before, new_mode)

    return get_auth_mode_settings(caller_user_row, dynamodb_resource)


# ---------------------------------------------------------------------------
# Credential-path gates -- the auth-mode toggle's actual behavior.
# ---------------------------------------------------------------------------


def _current_auth_mode(dynamodb_resource: Any) -> str:
    """Read the stored auth mode WITHOUT an admin gate -- used by the
    pre-authentication login path itself (a caller attempting to log in is,
    by definition, not yet an authenticated admin)."""
    table = _auth_settings_table(dynamodb_resource)
    resp = table.get_item(Key={"setting_id": AUTH_MODE_SETTING_ID})
    item = resp.get("Item")
    return item.get("auth_mode", DEFAULT_AUTH_MODE) if item else DEFAULT_AUTH_MODE


def sso_admission_allowed(mode: str) -> bool:
    """True unless the stored mode is `password`-only. Exposed for the
    live-SSO-path follow-on (infra/backend/src/auth.py's Cognito JWT
    verification) to consult; this slice does not itself wire it into that
    path."""
    return mode in (AUTH_MODE_SSO, AUTH_MODE_BOTH)


def password_login_allowed(mode: str) -> bool:
    return mode in (AUTH_MODE_PASSWORD, AUTH_MODE_BOTH)


# ---------------------------------------------------------------------------
# Demo session tokens (Docker Compose deployment target).
#
# In the Docker Compose deployment there is no Cognito, so a successful password login
# must mint a session token that backend/src/auth.py's get_current_user can
# verify on subsequent /api/* requests. A short-lived HS256 JWT signed with
# DEMO_TOKEN_SECRET carries the same `sub` the users-row lookup keys on
# (`local:<username>`), plus is_admin/role/username. The distinct issuer lets
# get_current_user route a token to the demo verifier vs Cognito in `both`
# mode. NEVER carries or logs a password.
#
# Session-cookie posture (issue #468): the token above is set as an
# httpOnly, Secure, SameSite=Strict cookie (`DEMO_SESSION_COOKIE_NAME`) by
# POST /api/auth/login, rather than returned in the response body for the
# SPA to hold in page-JS memory. The previous in-memory-Bearer approach was
# framed as an XSS mitigation ("never localStorage/sessionStorage"), but an
# in-memory token is exactly as readable by injected page JS as
# localStorage is -- it is not actually stronger, and it forced a re-login
# on every reload/tab-close, training users toward weak passwords on an
# instance with no brute-force protection. An httpOnly cookie IS unreadable
# by page JS (strictly better against XSS) and survives a reload (strictly
# better UX); nothing beyond the cookie itself is persisted anywhere. CSRF
# defense-in-depth comes from `SameSite=Strict` plus the API being
# same-origin-only (nginx reverse-proxy, no CORS) plus the existing strict
# CSP's `form-action 'self'` -- every state-changing route is a `fetch` with
# JSON/multipart from the same origin, never a cross-site HTML form post, so
# a dedicated CSRF token was judged not to add meaningfully more.
# ---------------------------------------------------------------------------
DEMO_TOKEN_ISSUER = "contract-toaster-demo"
DEMO_TOKEN_TTL_SECONDS = int(os.environ.get("DEMO_TOKEN_TTL_SECONDS", str(12 * 3600)))

# httpOnly session-cookie name password-mode login sets/clears (issue #468).
DEMO_SESSION_COOKIE_NAME = "ct_session"


def _demo_token_secret() -> str:
    secret = os.environ.get("DEMO_TOKEN_SECRET", "")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth configuration unavailable (DEMO_TOKEN_SECRET not set).",
        )
    return secret


def issue_demo_token(user_row: dict[str, Any], now: int | None = None) -> str:
    """Mint a short-lived HS256 session token for a logged-in password user.
    `user_row` is the (password-hash-free) dict `login_with_password` returns."""
    issued_at = int(time.time()) if now is None else now
    claims = {
        "sub": user_row["cognito_sub"],
        "username": user_row.get("username"),
        "role": user_row.get("role"),
        "is_admin": bool(user_row.get("is_admin", False)),
        "iss": DEMO_TOKEN_ISSUER,
        "iat": issued_at,
        "exp": issued_at + DEMO_TOKEN_TTL_SECONDS,
    }
    return jwt.encode(claims, _demo_token_secret(), algorithm="HS256")


def looks_like_demo_token(token: str) -> bool:
    """Cheap, signature-unverified check of the `iss` claim, used only to
    ROUTE a token to the right verifier in `both` mode. Never trusted for
    authorization -- verify_demo_token re-checks the signature."""
    try:
        return jwt.get_unverified_claims(token).get("iss") == DEMO_TOKEN_ISSUER
    except JWTError:
        return False


def verify_demo_token(token: str) -> dict[str, Any]:
    """Verify a demo session token (signature, issuer, expiry) and return its
    claims. The returned dict carries `sub`, so get_active_user_row's
    require_active_user lookup works identically to the Cognito path. Raises
    HTTP 401 on any verification failure."""
    try:
        return jwt.decode(
            token,
            _demo_token_secret(),
            algorithms=["HS256"],
            issuer=DEMO_TOKEN_ISSUER,
            options={"verify_aud": False},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Demo token verification failed: {exc!r}",
        ) from exc


def set_demo_session_cookie(response: Response, token: str) -> None:
    """Attach the demo session token to `response` as the httpOnly/Secure/
    SameSite=Strict cookie (issue #468) — called by POST /api/auth/login on
    a successful sign-in, in place of returning the token in the response
    body. `max_age` mirrors DEMO_TOKEN_TTL_SECONDS (the JWT's own `exp`),
    so the cookie does not outlive the token it carries."""
    response.set_cookie(
        DEMO_SESSION_COOKIE_NAME,
        token,
        max_age=DEMO_TOKEN_TTL_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_demo_session_cookie(response: Response) -> None:
    """Clear the demo session cookie (issue #468) — called by POST
    /api/auth/logout. Attributes must match `set_demo_session_cookie`'s or
    the browser will not recognize it as the same cookie to clear."""
    response.delete_cookie(
        DEMO_SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


# ---------------------------------------------------------------------------
# Login throttle (issue #469): POST /api/auth/login has no brute-force
# protection at all -- unlimited, fast, parallel guesses against the seeded
# `admin`/`user` credentials. Failed attempts are tracked per (username,
# source-IP) as a TTL'd item in AUTH_SETTINGS_TABLE (same table/style as the
# auth-mode row above -- no new table, no infra change), keyed
# `login_attempts:<username>:<client_ip>` so unrelated usernames (and
# unrelated source IPs against the SAME username) never share a bucket.
#
# Below the soft threshold: no delay. From the 5th failure: an exponentially
# growing delay. From the 10th: a flat hard-lockout window. A correct
# password submitted while locked is still refused -- the throttle check
# below runs BEFORE the row is even looked up, let alone the password
# verified -- so lockout state, not credential correctness, gates the
# response until `locked_until` passes.
# ---------------------------------------------------------------------------
_LOGIN_ATTEMPT_PREFIX = "login_attempts:"
_THROTTLE_SOFT_FAIL_THRESHOLD = 5  # failures before any delay kicks in
_THROTTLE_HARD_LOCKOUT_THRESHOLD = 10  # failures before the flat lockout
_THROTTLE_HARD_LOCKOUT_SECONDS = 15 * 60
_THROTTLE_BASE_DELAY_SECONDS = 2  # exponential backoff base, from the 5th failure


def _login_attempt_setting_id(username: str, client_ip: str) -> str:
    return f"{_LOGIN_ATTEMPT_PREFIX}{username}:{client_ip}"


def _get_login_attempt_state(
    dynamodb_resource: Any, username: str, client_ip: str
) -> dict[str, int]:
    table = _auth_settings_table(dynamodb_resource)
    resp = table.get_item(Key={"setting_id": _login_attempt_setting_id(username, client_ip)})
    item = resp.get("Item")
    if not item:
        return {"fail_count": 0, "locked_until": 0}
    return {
        "fail_count": int(item.get("fail_count", 0)),
        "locked_until": int(item.get("locked_until", 0)),
    }


def _throttle_delay_seconds(fail_count: int) -> int:
    """Seconds a caller must wait before their next attempt, given
    `fail_count` failures so far. 0 below the soft threshold; an
    exponentially growing delay from the soft threshold; the flat
    hard-lockout window from the hard threshold on."""
    if fail_count >= _THROTTLE_HARD_LOCKOUT_THRESHOLD:
        return _THROTTLE_HARD_LOCKOUT_SECONDS
    if fail_count >= _THROTTLE_SOFT_FAIL_THRESHOLD:
        return _THROTTLE_BASE_DELAY_SECONDS * (2 ** (fail_count - _THROTTLE_SOFT_FAIL_THRESHOLD))
    return 0


def _enforce_login_throttle(dynamodb_resource: Any, username: str, client_ip: str) -> None:
    state = _get_login_attempt_state(dynamodb_resource, username, client_ip)
    now = int(time.time())
    locked_until = state["locked_until"]
    if locked_until > now:
        retry_after = locked_until - now
        logger.info(
            "PASSWORD_LOGIN_DENY: username=%s reason=throttled retry_after=%s",
            username,
            retry_after,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed sign-in attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


def _record_login_failure(dynamodb_resource: Any, username: str, client_ip: str) -> None:
    """Atomically increments the (username, client_ip) failure counter and
    derives the lockout window from the POST-increment count.

    Deliberately an `update_item` with an `ADD` expression rather than the
    read-then-`put_item` this used to be: the threat model is explicitly
    "unlimited, fast, PARALLEL guesses" (issue #469), and a
    read-modify-write here loses updates under concurrency -- N parallel
    callers can each read the same `fail_count`, each compute the same
    `+1`, and each `put_item` the same value, so N failures record as 1.
    `ADD` is DynamoDB's atomic counter primitive: every call increments the
    stored value by exactly 1 regardless of how many other calls race it,
    so `fail_count` after N concurrent failures is always N.
    """
    now = int(time.time())
    table = _auth_settings_table(dynamodb_resource)
    # `locked_until`/`ttl` depend on the POST-increment fail_count, which we
    # don't know until the ADD lands -- a single round trip via
    # ReturnValues="ALL_NEW" gets us the authoritative count instead of a
    # racy second read. `SET updated_at` in the same expression is safe
    # alongside `ADD fail_count` (different attributes, single atomic item
    # update); `locked_until`/`ttl` are computed from the returned count and
    # written in a second, non-counting update below.
    resp = table.update_item(
        Key={"setting_id": _login_attempt_setting_id(username, client_ip)},
        UpdateExpression="ADD fail_count :one SET updated_at = :now",
        ExpressionAttributeValues={":one": 1, ":now": now},
        ReturnValues="ALL_NEW",
    )
    fail_count = int(resp["Attributes"]["fail_count"])
    delay = _throttle_delay_seconds(fail_count)
    table.update_item(
        Key={"setting_id": _login_attempt_setting_id(username, client_ip)},
        UpdateExpression="SET locked_until = :locked_until, #ttl = :ttl",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":locked_until": now + delay if delay else 0,
            # DynamoDB TTL attribute -- best-effort cleanup if/when the table
            # has TTL enabled on `ttl`. Application logic never relies on the
            # sweep itself, only on comparing `locked_until` to wall-clock
            # time above, so this is defense-in-depth against unbounded row
            # growth, not a correctness dependency.
            ":ttl": now + max(_THROTTLE_HARD_LOCKOUT_SECONDS, delay) + 3600,
        },
    )


def _clear_login_attempts(dynamodb_resource: Any, username: str, client_ip: str) -> None:
    table = _auth_settings_table(dynamodb_resource)
    table.delete_item(Key={"setting_id": _login_attempt_setting_id(username, client_ip)})


def _trust_proxy_headers() -> bool:
    """Whether this process is allowed to honor a caller-supplied
    `X-Forwarded-For` header at all.

    Defaults to OFF. `X-Forwarded-For` is not something a FastAPI process
    can validate cryptographically -- it is just a header, and ANY caller
    that can open a TCP connection to the backend can set it to whatever
    they like. Trusting it unconditionally (the pre-#469-fix-round-1
    behavior) let a direct caller pick their own throttle bucket and
    rotate it per request, making the brute-force protection this ticket
    exists to add a no-op -- proved empirically: 60 rotating-XFF wrong-
    password POSTs to a direct connection produced 0x 429 / 60x 401.

    `TRUST_PROXY_HEADERS=1` is an explicit, deployment-scoped opt-in: set
    it ONLY on the backend service in the nginx-fronted topology
    (deploy/dts/docker-compose.yml), where deploy/dts/nginx.conf's
    `/api/` location OVERWRITES (not appends) `X-Forwarded-For` with
    nginx's own view of the peer (`$remote_addr`) before proxying to the
    backend -- so a value reaching the backend from that hop is nginx's,
    never a client's. Pairs with binding the backend's host port to
    `127.0.0.1` (not `0.0.0.0`) in that same compose file, so the backend
    is not additionally reachable directly from outside the host with the
    flag on. Leave unset (the default) for any topology where the backend
    can be reached without going through that trusted hop -- App Runner's
    front door in particular APPENDS to `X-Forwarded-For` rather than
    overwriting it, so a caller there can still prepend a spoofed value
    ahead of the real one; this module does not treat App Runner as a
    trusted hop.
    """
    return os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes"}


def client_ip_from_request(request: Request) -> str:
    """The caller's IP for throttle bucketing (issue #469).

    Only consults `X-Forwarded-For` when `_trust_proxy_headers()` says this
    deployment is behind a hop that sets it trustworthily -- see that
    function's docstring. Everywhere else (including the untrusted-direct-
    caller case this ticket's finding is about, and the FastAPI TestClient
    in unit tests) this returns the direct peer address, then a fixed
    placeholder so throttling degrades to "one shared bucket" rather than
    throwing when neither is present.
    """
    if _trust_proxy_headers():
        forwarded = request.headers.get("x-forwarded-for", "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# ---------------------------------------------------------------------------
# Default-credentials warning (issue #469): the seeded admin/admin and
# user/user rows are a known, published username list. If a row's CURRENT
# password_hash still verifies against the shipped default, the caller (or,
# for an admin looking at the Users & access table, every affected row)
# should see a persistent warning until it is rotated.
#
# Deliberately named without "password"/"hash"/"secret"/"token" in the field
# -- tests/test_users_projection_453.py's SECRET_FIELD_PATTERN scans every
# key on a users-row response for exactly that pattern, and this value is
# safe to return (a boolean, never the hash itself) but must not collide
# with that credential-leak canary.
# ---------------------------------------------------------------------------
MIN_PASSWORD_LENGTH = 8


def default_credentials_warning(user_row: dict[str, Any]) -> bool:
    """True if `user_row`'s CURRENT password_hash still verifies against the
    shipped seed default for its username. False for an SSO row (no
    password_hash at all), a non-seeded username, or a row whose password
    has been rotated away from the shipped default."""
    if user_row.get("user_type") != USER_TYPE_PASSWORD:
        return False
    username = user_row.get("username")
    seed = next((spec for spec in SEED_USERS if spec["username"] == username), None)
    if seed is None:
        return False
    return _verify_password(seed["password"], user_row.get("password_hash", ""))


def login_with_password(
    username: str,
    password: str,
    dynamodb_resource: Any,
    client_ip: str = "unknown",
) -> dict[str, Any]:
    """POST /api/auth/login (unauthenticated -- this IS the login path).

    Validates `username`/`password` against a `user_type == "password"` row
    in the shared `users` table, gated by the stored auth mode. Never logs
    or echoes the plaintext password.

    Raises:
      HTTPException(403) if the stored auth mode does not permit password
        sign-in (mode == `sso`).
      HTTPException(429) if this (username, client_ip) is currently
        throttled or hard-locked-out from prior failures (issue #469) --
        checked before the row is even looked up, so a correct password
        submitted mid-lockout is still refused.
      HTTPException(401) if the username is unknown, is not a password-type
        row, or the password does not match. Counts as a throttle failure.
      HTTPException(403) if the row's lifecycle status is not `active`.
        Does NOT count as a throttle failure -- the credentials themselves
        were correct.
    """
    mode = _current_auth_mode(dynamodb_resource)
    if not password_login_allowed(mode):
        logger.info("PASSWORD_LOGIN_DENY: username=%s reason=mode_disabled mode=%s", username, mode)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Username/password sign-in is not enabled for your organization.",
        )

    _enforce_login_throttle(dynamodb_resource, username, client_ip)

    table = _users_table(dynamodb_resource)
    resp = table.get_item(Key={"cognito_sub": local_user_sub(username)})
    user = resp.get("Item")

    invalid_detail = "Invalid username or password."
    if not user or user.get("user_type") != USER_TYPE_PASSWORD:
        _record_login_failure(dynamodb_resource, username, client_ip)
        logger.info("PASSWORD_LOGIN_DENY: username=%s reason=no_such_user", username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=invalid_detail)

    if not _verify_password(password, user.get("password_hash", "")):
        _record_login_failure(dynamodb_resource, username, client_ip)
        logger.info("PASSWORD_LOGIN_DENY: username=%s reason=bad_password", username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=invalid_detail)

    if user.get("status") != "active":
        logger.info("PASSWORD_LOGIN_DENY: username=%s reason=not_active", username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: user status is {user.get('status')!r}, not 'active'.",
        )

    _clear_login_attempts(dynamodb_resource, username, client_ip)

    table.update_item(
        Key={"cognito_sub": user["cognito_sub"]},
        UpdateExpression="SET last_auth_at = :t",
        ExpressionAttributeValues={":t": int(time.time())},
    )

    logger.info("PASSWORD_LOGIN_ALLOW: username=%s", username)

    return {
        "cognito_sub": user["cognito_sub"],
        "username": user.get("username"),
        "role": user.get("role"),
        "is_admin": bool(user.get("is_admin", False)),
        "default_credentials_warning": default_credentials_warning(user),
    }


def change_own_password(
    current_password: str,
    new_password: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """POST /api/me/password (authenticated): rotate the CALLER'S OWN
    password (issue #469) -- the only way to rotate a password before this
    fix was deleting and re-adding the user.

    Raises:
      HTTPException(400) if the caller's row is not a username/password row
        (nothing to change for an SSO row), or `new_password` is under
        MIN_PASSWORD_LENGTH characters.
      HTTPException(401) if `current_password` does not match the stored
        hash.

    Never logs or echoes plaintext, matching this module's existing
    never-log-plaintext discipline. Appends one `password_changed` audit row
    (identifiers only -- no plaintext, no hash).
    """
    if caller_user_row.get("user_type") != USER_TYPE_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password changes are not available for single sign-on accounts.",
        )

    if not _verify_password(current_password, caller_user_row.get("password_hash", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )

    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"New password must be at least {MIN_PASSWORD_LENGTH} characters.",
        )

    cognito_sub = caller_user_row["cognito_sub"]
    table = _users_table(dynamodb_resource)
    table.update_item(
        Key={"cognito_sub": cognito_sub},
        UpdateExpression="SET password_hash = :h",
        ExpressionAttributeValues={":h": _hash_password(new_password)},
    )

    _write_audit_entry(
        dynamodb_resource,
        actor=cognito_sub,
        action="password_changed",
        target=cognito_sub,
        target_type="user",
        detail={},
    )
    logger.info("PASSWORD_CHANGED: actor=%s", cognito_sub)

    return {"changed": True}


# ---------------------------------------------------------------------------
# Seed demo credentials -- idempotent (never clobbers an existing row, so a
# demo admin's own changes to the seeded rows survive re-seeding).
# ---------------------------------------------------------------------------


def seed_demo_users(dynamodb_resource: Any) -> None:
    """Idempotently seed the `admin`/`admin` (admin role) and `user`/`user`
    (user role) demo credentials into the shared `users` table (issue #232
    acceptance criteria). A row that already exists is left untouched."""
    table = _users_table(dynamodb_resource)
    now = int(time.time())

    for spec in SEED_USERS:
        cognito_sub = local_user_sub(spec["username"])
        existing = table.get_item(Key={"cognito_sub": cognito_sub}).get("Item")
        if existing:
            continue
        table.put_item(Item={
            "cognito_sub": cognito_sub,
            "username": spec["username"],
            "user_type": USER_TYPE_PASSWORD,
            "password_hash": _hash_password(spec["password"]),
            "role": spec["role"],
            "is_admin": spec["is_admin"],
            "status": "active",
            "created_at": now,
            "last_auth_at": None,
            "admission": "seed",
        })
        logger.info("SEED_DEMO_USER: username=%s role=%s", spec["username"], spec["role"])


# ---------------------------------------------------------------------------
# User CRUD -- both types (issue #232: "Users table supporting BOTH user
# types ... with add/remove").
# ---------------------------------------------------------------------------


def add_user(
    payload: dict[str, Any],
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """POST /api/users (admin): create either an SSO user or a
    username/password user, dispatched on `payload["user_type"]`.

    SSO payload: {"user_type": "sso", "email": str, "is_admin": bool=False}
    Password payload: {"user_type": "password", "username": str,
                        "password": str, "is_admin": bool=False}

    Raises HTTPException(403) if the caller is not an admin, 400 for a
    missing/invalid field or unknown user_type, 409 if the target already
    exists.
    """
    _require_admin(caller_user_row, "Admin privilege required to add a user.")

    user_type = payload.get("user_type")
    is_admin = bool(payload.get("is_admin", False))
    role = "admin" if is_admin else "user"

    if user_type == USER_TYPE_SSO:
        email = payload.get("email")
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="email is required to add an SSO user.",
            )
        cognito_sub = payload.get("cognito_sub") or f"pending-sso:{email}"
        item = {
            "cognito_sub": cognito_sub,
            "email": email,
            "user_type": USER_TYPE_SSO,
            "role": role,
            "is_admin": is_admin,
            "status": "active",
            "created_at": int(time.time()),
            "last_auth_at": None,
            "admission": "admin_added",
        }
    elif user_type == USER_TYPE_PASSWORD:
        username = payload.get("username")
        password = payload.get("password")
        if not username or not password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="username and password are required to add a password user.",
            )
        cognito_sub = local_user_sub(username)
        item = {
            "cognito_sub": cognito_sub,
            "username": username,
            "user_type": USER_TYPE_PASSWORD,
            "password_hash": _hash_password(password),
            "role": role,
            "is_admin": is_admin,
            "status": "active",
            "created_at": int(time.time()),
            "last_auth_at": None,
            "admission": "admin_added",
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"user_type must be one of {sorted(VALID_USER_TYPES)}.",
        )

    table = _users_table(dynamodb_resource)
    if table.get_item(Key={"cognito_sub": cognito_sub}).get("Item"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this identity already exists.",
        )
    table.put_item(Item=item)

    actor = caller_user_row.get("cognito_sub", "")
    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="user_added",
        target=cognito_sub,
        target_type="user",
        detail={"user_type": user_type, "role": role},
    )
    logger.info("USER_ADDED: actor=%s target=%s user_type=%s role=%s", actor, cognito_sub, user_type, role)

    # One definition of "safe to return" for a users row, shared with
    # src/users.py's list_users/get_user/update_user (issue #453). This used to
    # be an ad-hoc `result.pop("password_hash", None)` here and nowhere else,
    # which is precisely how the scan-based GET /api/users and the routed
    # PATCH /api/users/{sub} kept leaking hashes.
    return public_user_view(item)


def remove_user(
    cognito_sub: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """DELETE /api/users/{sub} (admin): remove a user row of EITHER type.

    Raises HTTPException(403) if the caller is not an admin, 404 if the
    target does not exist, 409 if the caller targets their own row.

    This is an UNCONDITIONAL self-removal guard, on its own terms — an
    admin can never delete their own row via this route, regardless of how
    many other active admins exist. That is deliberately stricter than
    src/users.py::update_user's last-active-admin guard (issue #473), which
    now permits self-suspend/deprovision/revoke-admin once another active
    admin exists: deletion is destructive (the row and its history are
    gone, not just demoted) and has no reactivate path, so it stays blocked
    for the caller's own row even when a second admin could otherwise
    backstop them. Ask another admin to remove your row instead.
    """
    _require_admin(caller_user_row, "Admin privilege required to remove a user.")

    actor = caller_user_row.get("cognito_sub", "")
    if cognito_sub == actor:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An admin cannot remove their own user row. Ask another admin.",
        )

    table = _users_table(dynamodb_resource)
    existing = table.get_item(Key={"cognito_sub": cognito_sub}).get("Item")
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    table.delete_item(Key={"cognito_sub": cognito_sub})

    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="user_removed",
        target=cognito_sub,
        target_type="user",
        detail={"user_type": existing.get("user_type", USER_TYPE_SSO)},
    )
    logger.info("USER_REMOVED: actor=%s target=%s", actor, cognito_sub)

    return {"cognito_sub": cognito_sub, "removed": True}
