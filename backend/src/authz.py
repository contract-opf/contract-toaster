"""The single source of the admin-privilege decision.

WHY THIS MODULE EXISTS (issue #66, diagnostic finding G5):
    The same three-line admin check used to be re-declared 17 times across
    `backend/src` (`_is_admin` / `_require_admin` / `_is_admin_caller` in
    admin_dashboard, audit_queries, bundle_authoring, corpus, demo_auth,
    download, entity_roster, main, model_settings, retention, reviews and
    users). Seventeen copies of an authorization predicate is seventeen
    chances for one of them to drift, and one drifted copy opens a route.
    Every module now imports these two functions instead, and
    `tests/test_authz_single_source.py` AST-parses every module under
    `backend/src` and fails if any of them grows a local copy back.

WHAT IT IS DELIBERATELY NOT:
    Row in, decision out. This module performs no DynamoDB access and knows
    nothing about how the caller's row was fetched -- callers pass the row
    `users.require_active_user()` already read for them. Keeping the I/O
    out is what lets every route share one predicate without dragging a
    table dependency into modules that do not have one.

WHY `is True` AND NOT `bool(...)`:
    `is_admin` is a DynamoDB `users`-row flag, never a JWT claim
    (ARCHITECTURE.md -> "Group-naming misnomer": "The `is_admin` flag in the
    `users` DynamoDB row (not group membership) is the sole admin-privilege
    gate"). The three code paths in `backend/src` that write that attribute
    all store a real boolean: `users.update_user` rejects a non-bool
    `is_admin` with 400, `demo_auth.add_user` coerces the payload field with
    `bool(...)` before persisting it, and `demo_auth.seed_demo_users` writes
    the `SEED_USERS` `True`/`False` literals. The bootstrap reconciliation
    and the break-glass role write `is_admin=true` (ARCHITECTURE.md -> the
    first-admin and break-glass notes). So nothing legitimate is a string or
    an int, and a truthy non-boolean -- `"false"`, `"0"`, `1` in a
    hand-edited or break-glass row -- is evidence of tampering or of a
    writer nobody reviewed, not of admin privilege. `is True` refuses all of
    them; `bool(...)` used to admit `"false"`.
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import HTTPException, status


def is_admin(caller_user_row: Mapping[str, Any]) -> bool:
    """Return True only if the caller's `users` row carries `is_admin is True`.

    `is_admin` is a DynamoDB `users`-row flag, never a JWT claim
    (ARCHITECTURE.md -> "Group-naming misnomer"). Callers pass the caller's
    own row (already fetched by `users.require_active_user` /
    `get_active_user_row`) rather than trusting a token claim, so admin
    privilege cannot be forged by a stale or crafted JWT.
    """
    return caller_user_row.get("is_admin", False) is True


def require_admin(caller_user_row: Mapping[str, Any], detail: str) -> None:
    """Raise HTTPException(403) with `detail` unless the caller is an admin.

    `detail` is the calling route's own 403 copy so an operator who hits
    this knows WHICH surface refused them. It is always a literal owned by
    the route -- never anything caller-supplied, which would echo attacker
    text back through the error body.
    """
    if not is_admin(caller_user_row):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
