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

import os
import time
import uuid
from typing import Any

from fastapi import HTTPException, status

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
    users = resp.get("Items", [])
    # Deterministic ordering for a stable UI: most-recently-authenticated first.
    users.sort(key=lambda u: u.get("last_auth_at", 0), reverse=True)
    return users


def get_user(
    target_sub: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """Fetch a single users row (admin only). Raises 403/404 as appropriate."""
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
    return user


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
      HTTPException(409) if the caller targets their own admin flag or
        status — an admin must not be able to lock themselves out or
        de-admin themselves as their last action (self-modification of
        privilege/lifecycle is refused; use another admin or break-glass).
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
    if target_sub == caller_sub:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An admin cannot change their own admin flag or lifecycle status. "
            "Ask another admin, or use the break-glass procedure (see RUNBOOK.md).",
        )

    table = _users_table(dynamodb_resource)
    resp = table.get_item(Key={"cognito_sub": target_sub})
    before = resp.get("Item")
    if not before:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

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

    return after


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
    return row
