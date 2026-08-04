"""
Admin user-management API — issue #92 (allowlist UI, lifecycle actions,
sync visibility; mock-first MVP scope per epic #123).

Implements the authorization-critical pieces described in ARCHITECTURE.md
-> "Authentication — Cognito federated to Google" and "Deprovisioning and
lifecycle", building on the pre-token Lambda's JIT-create path (#53) and
the canonical admission path fixed by #33:

  - `require_active_user`: the per-request backend-side authorization gate.
    ARCHITECTURE.md -> "Security defaults" states that every non-health
    route requires "a valid Cognito token, configured-domain checks,
    allowlist membership, and users.status == active" before route-specific
    owner/admin authorization runs. The pre-token Lambda enforces domain +
    allowlist + JIT-create at sign-in; THIS function is the corresponding
    backend-side, every-request re-check of `status == active` against the
    DynamoDB `users` row — the row is the authoritative gate, and it fails
    closed on any error (missing row, unreadable table).
  - `list_users` / `get_user`: GET /api/users (admin) — the allowlist view,
    including JIT-created rows and their current lifecycle status, plus the
    sync-job visibility panel (last run, changes made, fail-closed state).
  - `update_user`: PATCH /api/users/{sub} (admin) — sets `is_admin` and/or
    `status` (suspend/deprovision/reactivate). Every mutation is audited
    (ARCHITECTURE.md -> "Audit posture": "User admin-flag changes").
  - `get_sync_status`: read-only surface of the `sync_status` table, written
    by the scheduled Workspace/SSO deprovisioning sync worker (that worker's
    own scheduling is a follow-on issue — same mock-first swap-point pattern
    as infra/lambda/mock_review; this module only ever reads the row).

Explicitly NOT built here (see ARCHITECTURE.md -> "Break-glass"): the
break-glass IAM role and its DynamoDB write path. This module surfaces the
break-glass *procedure* read-only (RUNBOOK.md link + summary) so admins know
it exists without exposing a button that bypasses the audited PATCH path —
"stays IAM-side per #53" (issue #92).

Deprovisioning enforcement window (ARCHITECTURE.md -> "Token revocation"):
a suspended/deprovisioned user is denied on their very next backend request
via `require_active_user` (this module), independent of the sync cadence
(<=1h) and access-token TTL (15-60 min) that bound the edge-layer window.

Environment variables consumed:
  USERS_TABLE        DynamoDB users table name (PK: cognito_sub)
  AUDIT_TABLE        DynamoDB audit table name (append-only; PK: partition,
                     SK: timestamp#event_id)
  SYNC_STATUS_TABLE  DynamoDB sync_status table name (PK: sync_type)
"""

import decimal
import os
import time
import uuid
from typing import Any

from fastapi import HTTPException, status


def json_safe(value: Any) -> Any:
    """Coerce boto3's `Decimal`s into plain ints/floats, recursively.

    boto3's DynamoDB *resource* API deserializes every stored number to
    `decimal.Decimal`, which `json.dumps` -- and therefore FastAPI's
    `JSONResponse` -- cannot encode. Any row handed straight to a response
    will 500 the moment it carries a number.

    That is not hypothetical: `GET /api/users` did exactly this in
    production, taking the whole Users & access tab down with
    `TypeError: Object of type Decimal is not JSON serializable`. The
    suite missed it because the in-memory test double stores plain `int`s,
    so the fake and the real client differ in precisely the field that
    breaks (see tests/test_user_management_92.py's Decimal test).

    Integral values become `int` so `last_auth_at` stays an epoch second
    rather than becoming `5000.0`; genuinely fractional ones become
    `float`. Other routes in main.py solve this by hand-building a JSON-safe
    dict per response; this does it once, at the boundary that returns rows.

    PUBLIC (issue #443): the same hazard applies to every route that returns
    raw DynamoDB rows, so `src/reviews.py`'s diagnostics projection imports
    this rather than growing a second copy. It lives here because this is
    where the production outage was found, not because it is users-specific.
    """
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value

# ---------------------------------------------------------------------------
# The API-boundary projection for a `users` row (issue #453).
#
# A users row carries credential material -- password-mode rows store
# `password_hash` ("salt$hash", written by demo_auth._hash_password) -- and
# `GET /api/users` used to return whole scanned rows, so every admin's browser
# received every password user's hash. The UI never read it (`grep
# password_hash frontend/src/` is empty): it was pure over-exposure.
#
# ALLOWLIST, NOT DENYLIST, and applied INSIDE the row-returning functions
# rather than at each caller. Both halves of that are the lesson of the bug:
# the hazard had already been recognised and patched at exactly one of four
# sites (demo_auth.add_user's one-line `result.pop("password_hash", None)`)
# while the scan-based list route -- the one that returns *every* row at once
# -- and the routed PATCH (`update_user`) stayed open. A denylist would
# likewise leak the next secret field someone adds to the row. Anything not
# named here does not cross the boundary.
#
# Every field the admin Users table renders is present: AdminUsers.tsx's
# `UserRow` consumes cognito_sub, email, username, status, is_admin,
# last_auth_at, created_at and admission; user_type and role are included for
# API consumers of the same shape (demo_auth.add_user's response).
# ---------------------------------------------------------------------------
PUBLIC_USER_FIELDS = (
    "cognito_sub",
    "email",
    "username",
    "user_type",
    "status",
    "is_admin",
    "role",
    "admission",
    "created_at",
    "last_auth_at",
    # Derived (issue #469), not a raw row field -- see the synthesis step in
    # public_user_view below. Listed here anyway so this stays the single
    # allowlist tests/test_users_projection_453.py's
    # test_a_future_row_field_is_not_returned_unless_allowlisted checks
    # every returned key against.
    "default_credentials_warning",
)


def public_user_view(row: dict[str, Any]) -> dict[str, Any]:
    """Project a raw `users` row down to the fields safe to return to a client.

    This is the single definition of "safe to return" for a users row; every
    function in this codebase that hands a row to an HTTP response goes
    through it (`list_users`, `get_user`, `update_user`, `demo_auth.add_user`).
    Keep that enumeration true: a new row-returning function must call this,
    not scrub its own output.

    Deliberately separate from `json_safe`, which is a *type* coercion and not
    a security boundary -- overloading that with field filtering would hide
    this decision inside a Decimal helper. Coercion is still applied here, on
    the projected fields, because these dicts go straight into a JSONResponse.

    Keys absent from the row stay absent from the projection (an SSO row has
    no `username`, a never-signed-in row may have no `last_auth_at`), so the
    wire shape is unchanged apart from the removal of non-allowlisted fields.

    `default_credentials_warning` (issue #469) is the one DERIVED field: a
    password-type row gets it synthesized here (true if its CURRENT
    password_hash still verifies against the shipped seed default for its
    username) so an admin can spot an unrotated admin/admin or user/user row
    in the Users & access table. Deferred import -- `demo_auth` already
    imports `public_user_view` from this module at load time, so importing
    `demo_auth` back at module level here would be circular; by call time
    both modules are fully loaded. Never the hash itself, so it stays inside
    the "safe to return" boundary this function draws.
    """
    projected_row: dict[str, Any] = row
    if row.get("user_type") == "password":
        try:
            from src.demo_auth import default_credentials_warning
        except ImportError:  # pragma: no cover
            from demo_auth import default_credentials_warning  # type: ignore[no-redef]
        projected_row = dict(row)
        projected_row["default_credentials_warning"] = default_credentials_warning(row)
    return {field: json_safe(projected_row[field]) for field in PUBLIC_USER_FIELDS if field in projected_row}


# ---------------------------------------------------------------------------
# Lifecycle statuses (ARCHITECTURE.md -> "Deprovisioning and lifecycle").
# There is no separate 'disabled' state — urgent removal maps to 'suspended'
# or 'deprovisioned'.
# ---------------------------------------------------------------------------
VALID_STATUSES = {"active", "suspended", "deprovisioned"}

# The single sync_status row this module reads is keyed by this fixed value.
SYNC_TYPE_USER_DEPROVISION = "user_deprovision"

# Fields a PATCH may set. Anything else is rejected (400) rather than
# silently ignored — this is the admin-privilege/lifecycle mutation path.
PATCHABLE_FIELDS = {"is_admin", "status"}


def _users_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["USERS_TABLE"])


def _audit_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["AUDIT_TABLE"])


def _sync_status_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["SYNC_STATUS_TABLE"])


def _is_admin(claims: dict[str, Any]) -> bool:
    """Return True if the caller's users row (looked up by sub) is an admin.

    NOTE: `is_admin` is a DynamoDB `users`-row flag, never a JWT claim
    (ARCHITECTURE.md -> "Group-naming misnomer": "The `is_admin` flag in the
    users DynamoDB row (not group membership) is the sole admin-privilege
    gate"). Callers of this module pass in the caller's own users row
    (already fetched by `require_active_user`) rather than trusting a token
    claim, so admin privilege cannot be forged by a stale or crafted JWT.
    """
    return bool(claims.get("is_admin", False))


def require_active_user(
    cognito_sub: str,
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """Backend-side authorization gate: re-verify `status == active`.

    This is the every-request check described in ARCHITECTURE.md ->
    "Security defaults": domain and allowlist are enforced at the edge
    (pre-token Lambda), but `users.status == active` must be independently
    re-checked on every backend request so a suspend/deprovision action
    takes effect on the user's very next call, not just at their next
    sign-in.

    Fails closed:
      - No users row for this sub -> HTTP 403 (never treated as "new user,
        allow"; JIT-create is exclusively the pre-token Lambda's job).
      - status != 'active' -> HTTP 403.
      - DynamoDB read failure -> propagates as HTTP 503 (fail closed, never
        silently allow).

    Returns the full users row on success (callers use it for is_admin
    checks so admin privilege is read from DynamoDB, never a JWT claim).
    """
    table = _users_table(dynamodb_resource)
    try:
        resp = table.get_item(Key={"cognito_sub": cognito_sub})
    except Exception as exc:  # fail closed on any DynamoDB error
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unable to verify user status (fail-closed): {exc!r}",
        ) from exc

    user = resp.get("Item")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No allowlist row for this user. Sign in again, or ask an "
            "admin to confirm your access.",
        )

    if user.get("status") != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: user status is {user.get('status')!r}, not 'active'.",
        )

    return user


def list_users(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> list[dict[str, Any]]:
    """GET /api/users (admin): the allowlist view.

    Returns every row in the `users` table — active, suspended, and
    deprovisioned — including JIT-created rows (issue #33), so an admin can
    see group-sync status and take a lifecycle action on any of them.

    Raises HTTPException(403) if the caller is not an admin.
    """
    if not _is_admin(caller_user_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to list users.",
        )

    table = _users_table(dynamodb_resource)
    resp = table.scan()
    # Project before sorting and returning. `public_user_view` both drops
    # everything outside PUBLIC_USER_FIELDS -- a raw scanned row carries
    # `password_hash` for every password-mode user (issue #453) -- and coerces
    # Decimals, since these rows go straight into a JSONResponse and boto3
    # hands back Decimals that json.dumps rejects.
    users = [public_user_view(item) for item in resp.get("Items", [])]
    # Deterministic ordering for a stable UI: most-recently-authenticated first,
    # with never-signed-in rows at the bottom.
    #
    # `or 0` and NOT `.get("last_auth_at", 0)` (issue #452): `dict.get`'s
    # default applies only when the key is ABSENT, and a never-signed-in row
    # has it PRESENT and explicitly `None` -- `demo_auth.py` writes
    # `last_auth_at: None` when it seeds the demo rows and when an admin adds a
    # user. Two such rows made the comparison raise `TypeError: '<' not
    # supported between instances of 'NoneType' and ...` and 500'd the whole
    # Users & access tab. `or 0` collapses `None` (and a stored 0) to a real
    # sort key, which under `reverse=True` puts those rows last -- after every
    # row carrying a genuine epoch.
    #
    # Coerce for the SORT only; the returned value stays `None` so the UI
    # renders "never" rather than an epoch-1970 date (AdminUsers.tsx
    # `formatTimestamp`). Fixing the consumer, not the producer: `None` is the
    # honest stored value for "never signed in".
    users.sort(key=lambda u: u.get("last_auth_at") or 0, reverse=True)
    return users


def get_user(
    target_sub: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """Fetch a single users row (admin only). Raises 403/404 as appropriate.

    Returns the same `public_user_view` projection as `list_users` (issue
    #453): this function is unrouted today, and returning the raw Item would
    leak `password_hash` the moment anyone wires it to a route.
    """
    if not _is_admin(caller_user_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to view a user.",
        )

    table = _users_table(dynamodb_resource)
    resp = table.get_item(Key={"cognito_sub": target_sub})
    user = resp.get("Item")
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return public_user_view(user)


def update_user(
    target_sub: str,
    updates: dict[str, Any],
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    now_epoch: float | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """PATCH /api/users/{sub} (admin): set admin flag and/or lifecycle status.

    `updates` may contain `is_admin` (bool) and/or `status` (one of
    VALID_STATUSES). Any other key is rejected with HTTP 400 — this endpoint
    grants admin privilege and controls access to a legal-document tool, so
    it does not accept an open-ended patch document.

    Every successful mutation writes an audit row (ARCHITECTURE.md ->
    "Audit posture": "User admin-flag changes"), recording actor, action,
    target, and before/after values — never document substance.

    Raises:
      HTTPException(403) if the caller is not an admin.
      HTTPException(400) for an empty or invalid update payload.
      HTTPException(404) if the target user does not exist.
      HTTPException(409) if the update would strip the target's admin
        access (suspending/deprovisioning them, or revoking their admin
        flag) while they are the LAST active admin on the deployment —
        self-targeting or not (issue #473). With at least one other active
        admin, self-demotion is allowed; the second admin retains access.
    """
    if not _is_admin(caller_user_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to update a user.",
        )

    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No updates provided.")

    unknown_fields = set(updates) - PATCHABLE_FIELDS
    if unknown_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported field(s) for user update: {sorted(unknown_fields)}. "
            f"Only {sorted(PATCHABLE_FIELDS)} may be set.",
        )

    if "status" in updates and updates["status"] not in VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status {updates['status']!r}; must be one of {sorted(VALID_STATUSES)}.",
        )

    if "is_admin" in updates and not isinstance(updates["is_admin"], bool):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="is_admin must be a boolean.")

    caller_sub = caller_user_row.get("cognito_sub")

    table = _users_table(dynamodb_resource)
    resp = table.get_item(Key={"cognito_sub": target_sub})
    before = resp.get("Item")
    if not before:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    # Last-active-admin guard (issue #473). The old rule blocked ANY
    # self-targeting update unconditionally, which also blocked legitimate
    # self-demotion when a second active admin exists. The rule that
    # actually matters is narrower and applies regardless of who the caller
    # is: never let an update strip admin access from the target (suspend,
    # deprovision, or revoke-admin) if that leaves the deployment with zero
    # active admins. This covers self-demotion by the sole admin, reading
    # the count fresh at update time rather than from an earlier request.
    # It does NOT close the two-admins-demote-each-other race: this is a
    # `scan()` read followed by an unconditional `update_item`, not a
    # conditional write, so two concurrent PATCHes can each observe the
    # other admin as active, both pass this check, and the deployment can
    # still reach zero active admins. The issue explicitly accepts that
    # residual race at this scale ("re-check inside the update, conditional
    # write on the count if cheap, otherwise re-read-then-write is
    # acceptable at this scale") — this is the re-read-then-write option,
    # knowingly not race-free.
    target_is_active_admin = bool(before.get("is_admin", False)) and before.get("status") == "active"
    would_strip_admin_access = target_is_active_admin and (
        ("status" in updates and updates["status"] != "active")
        or ("is_admin" in updates and updates["is_admin"] is False)
    )
    if would_strip_admin_access:
        other_active_admins = sum(
            1
            for item in table.scan().get("Items", [])
            if item.get("cognito_sub") != target_sub
            and bool(item.get("is_admin", False))
            and item.get("status") == "active"
        )
        if other_active_admins == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This is the only admin account — add another admin first.",
            )

    now = now_epoch if now_epoch is not None else time.time()

    update_expr_parts = []
    expr_values: dict[str, Any] = {":updated_at": int(now)}
    for field, value in updates.items():
        update_expr_parts.append(f"{field} = :{field}")
        expr_values[f":{field}"] = value
    update_expr_parts.append("updated_at = :updated_at")

    table.update_item(
        Key={"cognito_sub": target_sub},
        UpdateExpression="SET " + ", ".join(update_expr_parts),
        ExpressionAttributeValues=expr_values,
    )

    after = dict(before)
    after.update(updates)
    after["updated_at"] = int(now)

    _write_audit_entry(
        dynamodb_resource=dynamodb_resource,
        actor=caller_sub,
        action="user_lifecycle_update",
        target=target_sub,
        before=before,
        after=after,
        now_epoch=now,
        event_id=event_id,
    )

    # Same boundary as list_users/get_user (issue #453). `after` is built from
    # the raw pre-update Item, so it carries `password_hash` for every
    # password-mode row -- and unlike get_user this one IS routed
    # (main.py's PATCH /api/users/{sub} returns this dict verbatim). The audit
    # entry above is written from the unprojected `before`/`after`, which is
    # correct: the audit row records lifecycle values, not a response body.
    return public_user_view(after)


def _write_audit_entry(
    dynamodb_resource: Any,
    actor: str,
    action: str,
    target: str,
    before: dict[str, Any],
    after: dict[str, Any],
    now_epoch: float,
    event_id: str | None = None,
) -> None:
    """Append an immutable audit row for a user admin-flag/status change.

    Follows the `audit` table field dictionary in ARCHITECTURE.md ->
    "Storage" / "Audit posture": actor, action, target, before/after —
    identifiers and lifecycle values only, never document substance (this
    module never touches document content, so there is nothing substantive
    to accidentally leak here).
    """
    table = _audit_table(dynamodb_resource)
    event_id = event_id or uuid.uuid4().hex
    partition = time.strftime("%Y-%m", time.gmtime(now_epoch))
    timestamp = f"{int(now_epoch)}#{event_id}"

    table.put_item(
        Item={
            "partition": partition,
            "timestamp": timestamp,
            "event_id": event_id,
            "actor": actor,
            "action": action,
            "target": target,
            "target_type": "user",
            "before_status": before.get("status"),
            "after_status": after.get("status"),
            "before_is_admin": before.get("is_admin", False),
            "after_is_admin": after.get("is_admin", False),
            "outcome": "success",
        },
    )


def get_sync_status(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """GET /api/users sync-visibility panel: read the sync_status row.

    Read-only. This module never writes sync_status — that is exclusively
    the scheduled sync worker's job, mirroring the "sync only deprovisions,
    never auto-admits" separation of responsibilities fixed by #33.

    Returns a well-formed "never run yet" shape if the row does not exist
    (e.g. before the sync worker's first scheduled run) rather than 404ing —
    the admin UI must always be able to render the panel.

    Raises HTTPException(403) if the caller is not an admin.
    """
    if not _is_admin(caller_user_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to view sync status.",
        )

    table = _sync_status_table(dynamodb_resource)
    resp = table.get_item(Key={"sync_type": SYNC_TYPE_USER_DEPROVISION})
    row = resp.get("Item")
    if not row:
        return {
            "sync_type": SYNC_TYPE_USER_DEPROVISION,
            "last_run_at": None,
            "last_run_outcome": None,
            "users_deprovisioned_count": 0,
            "next_run_at": None,
        }
    # Same Decimal hazard as list_users: this row goes straight into a
    # JSONResponse (main.py's /api/users/sync-status). It is latent rather
    # than live only because the branch above -- the "never run yet" default,
    # all plain ints -- is what a deployment with no completed sync returns.
    # The first real sync worker run writes `last_run_at` and
    # `users_deprovisioned_count` as numbers, and boto3 reads them back as
    # Decimals, which would 500 the panel exactly as GET /api/users did.
    return json_safe(row)
