"""
Per-user preferences store — issue #523 (epic #519 item F).

## Why this is its own table

Epic #519 decision 3 is a hard requirement: the notes-mode preference is
**per-user, never an admin-over-others setting**. An admin's preference
governs their own reviews, because many admins are also users; nobody —
admin included — reads or writes anybody else's row.

`backend/src/users.py` is therefore the wrong home for it. That module
exposes exactly one "safe to return" projection, `PUBLIC_USER_FIELDS` ->
`public_user_view`, and that projection is what the **admin** Users table
renders. A preference living on the users row would either be invisible
(not on the allowlist, which `tests/test_users_projection_453.py`
enforces) or, once allowlisted, admin-readable — directly defeating this
module's own requirement. So: a separate table, keyed on `cognito_sub`,
with no route and no function anywhere in this module that accepts a
target subject.

That last sentence is the security design, not a detail. There is no
`get_preferences_for(sub)`. Every entry point derives the key from the
CALLER'S OWN row (`caller_user_row["cognito_sub"]`, already re-verified
active by `src.main.get_active_user_row` -> `users.require_active_user`),
the same owner-scoping shape `src/download.py` uses for review artifacts.
A caller who nonetheless names a subject in the request body gets a 403
(`_require_self`) rather than having it quietly ignored — a silently
ignored cross-user write reads like a successful one to whoever sent it.

## Storage shape

One row per user, PK `cognito_sub`::

    {
        "cognito_sub": "user-abc",     # the caller's own identity
        "notes_mode": "internal",      # a known preference key
        "updated_at": 1785000000,      # epoch seconds
    }

Preferences are stored as top-level attributes rather than a nested map so
a future consumer can `UpdateItem` a single key without a read-modify-write
over the whole blob.

## A GENERAL store, not a notes-only one

Issue #523: "Keep it a general prefs store, not a notes-only one — #489
(remember mute + last contract type) is the obvious second consumer."
`PREFERENCE_SPECS` below is the whole schema: one entry per known key, with
its default and its validator. Adding #489's mute flag is a new entry
there, no route change and no storage change. Unknown keys are refused
(400) rather than stored: an unbounded key space on a per-user row is how a
preferences table becomes an untyped dumping ground, and a typo'd key that
silently "saves" but never reads back is a bug the user cannot see.

Nothing about a review's CONTENT is ever stored here — only the user's own
settings — so this table carries no document substance and needs no audit
row (unlike `src/retention.py` / `src/users.py`, whose writes change what
OTHER people can do).

Environment variables:
  USER_PREFERENCES_TABLE — this module's table (PK: cognito_sub)
"""

import os
import time
from typing import Any, Callable

from fastapi import HTTPException, status

from src import config
from src import reviews


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


def _validate_notes_mode(value: Any) -> str:
    """The stored default footnote audience (issue #519 axis 1).

    Validation is delegated to `src.reviews.resolve_notes_mode` — the SAME
    function `src/review_routes.py` runs on the per-review form field — so a
    preference can never be saved that a submission would then refuse. That
    includes the #572 kill switch: while `NOTES_MODE_ENABLED` is off,
    `internal` and `both` are refused here exactly as they are refused at
    submission, instead of being stored as a default that 400s every review
    the user starts.

    Note the deliberate asymmetry with `resolve_notes_mode`: blank/absent is
    a legitimate "no opinion" at submission time, but here the caller is
    explicitly writing a preference, so an empty value is a malformed write,
    not a request for the default.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"notes_mode must be one of {', '.join(reviews.NOTES_MODES)}")
    return reviews.resolve_notes_mode(value)


class PreferenceSpec:
    """One known preference key: its default, and the validator that decides
    whether an incoming value may be stored at all."""

    def __init__(self, key: str, default: Any, validate: Callable[[Any], Any]) -> None:
        self.key = key
        self.default = default
        self.validate = validate


PREFERENCE_SPECS: dict[str, PreferenceSpec] = {
    "notes_mode": PreferenceSpec(
        key="notes_mode",
        # The stored default is today's behaviour (issue #520's
        # DEFAULT_NOTES_MODE), so a user who has never touched the control
        # submits exactly the request they submitted before it existed.
        default=reviews.DEFAULT_NOTES_MODE,
        validate=_validate_notes_mode,
    ),
}

KNOWN_PREFERENCE_KEYS = tuple(sorted(PREFERENCE_SPECS))


# ---------------------------------------------------------------------------
# Table accessor
# ---------------------------------------------------------------------------


def _preferences_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["USER_PREFERENCES_TABLE"])


def _caller_sub(caller_user_row: dict[str, Any]) -> str:
    sub = str(caller_user_row.get("cognito_sub") or "").strip()
    if not sub:
        # An active users row always has its own primary key; an empty one
        # here means the caller row was hand-built. Fail closed rather than
        # reading or writing a row keyed on "".
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Preferences are per-user and this session has no identity.",
        )
    return sub


def _require_self(caller_sub: str, requested_sub: Any) -> None:
    """Refuse a body that names somebody else's row.

    No route exposes a target subject, so this only fires for a caller who
    put one in the body anyway — an admin included. Refusing loudly (403)
    rather than ignoring the field is the point: epic #519 decision 3 gives
    an admin no override in either direction, and a silently dropped
    `cognito_sub` would look, to the sender, exactly like a cross-user write
    that worked.
    """
    if requested_sub is None:
        return
    if str(requested_sub).strip() != caller_sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A user may only read or change their own preferences.",
        )


# ---------------------------------------------------------------------------
# Read / write — the caller's OWN row, always
# ---------------------------------------------------------------------------


def _apply_defaults(item: dict[str, Any] | None) -> dict[str, Any]:
    stored = item or {}
    resolved: dict[str, Any] = {}
    for key, spec in PREFERENCE_SPECS.items():
        value = stored.get(key)
        resolved[key] = spec.default if value is None else value
    return resolved


def get_preferences(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """GET /api/me/preferences — the CALLER'S own preferences.

    Never 404s: a user who has never saved anything gets the documented
    defaults, the same way `retention.get_retention_settings` answers with
    the documented default window before any row exists.

    `notes_mode_available` rides along because the four-way control has to
    know whether `internal`/`both` can be chosen at all: the #572 kill
    switch is deployment config the browser cannot see, and offering a stop
    that is guaranteed to 400 is a dead affordance dressed as a live one
    (the same reasoning `ContractTypeDial`'s "coming soon" stops already
    encode).
    """
    caller_sub = _caller_sub(caller_user_row)
    table = _preferences_table(dynamodb_resource)
    resp = table.get_item(Key={"cognito_sub": caller_sub})
    return {
        "preferences": _apply_defaults(resp.get("Item")),
        "notes_mode_available": config.notes_mode_enabled(),
    }


def save_preferences(
    body: dict[str, Any],
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """PUT /api/me/preferences — a partial update of the CALLER'S own row.

    Body: `{"preferences": {"<known key>": <value>, ...}}`. Only the keys
    present are written; anything else the row already holds is untouched
    (`update_item`, not `put_item`) so a second consumer's preference is
    never clobbered by a caller that only knows about the first.

    Returns the same shape `get_preferences` returns, so a client never has
    to guess what actually landed.

    Raises 400 for an unknown key or a value its spec rejects, and 403 for a
    body naming another user's `cognito_sub`.
    """
    caller_sub = _caller_sub(caller_user_row)
    _require_self(caller_sub, body.get("cognito_sub"))

    raw = body.get("preferences")
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must carry a `preferences` object.",
        )
    _require_self(caller_sub, raw.get("cognito_sub"))

    unknown = sorted(set(raw) - set(PREFERENCE_SPECS))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown preference(s): {', '.join(unknown)}. "
                # Read live rather than from the import-time
                # KNOWN_PREFERENCE_KEYS snapshot, so this message stays true
                # the moment a new spec is registered.
                f"Known preferences: {', '.join(sorted(PREFERENCE_SPECS))}."
            ),
        )
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No preferences supplied.",
        )

    validated: dict[str, Any] = {}
    for key, value in raw.items():
        try:
            validated[key] = PREFERENCE_SPECS[key].validate(value)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    table = _preferences_table(dynamodb_resource)
    names = {f"#k{i}": key for i, key in enumerate(validated)}
    values = {f":v{i}": validated[key] for i, key in enumerate(validated)}
    names["#updated_at"] = "updated_at"
    values[":updated_at"] = int(time.time())
    assignments = ", ".join(f"#k{i} = :v{i}" for i in range(len(validated)))
    table.update_item(
        Key={"cognito_sub": caller_sub},
        UpdateExpression=f"SET {assignments}, #updated_at = :updated_at",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )

    return get_preferences(caller_user_row, dynamodb_resource)
