"""
Admin-managed model-provider API key — the instance-wide OpenRouter key.

The Docker Compose deployment target calls a direct model provider (OpenRouter) instead
of Bedrock, authenticated by a single API key shared by every user of the
instance. Before this module the key could only arrive as the
`OPENROUTER_API_KEY` environment variable, which means an operator had to
edit `deploy/dts/.env` and restart the stack to rotate it. This module adds
an admin-settable store so the key can be set and rotated from the running
app.

Conventions mirrored exactly from src/demo_auth.py's auth-mode setting (which
itself mirrors src/retention.py): one row, `setting_id="global"`, admin-gated
read/write, every mutation appended to the shared `audit` table.

Two properties matter here that the other settings modules don't have to
worry about, because this row holds a live spending credential:

  - **Write-only.** `get_model_key_settings` NEVER returns the stored key.
    It returns `key_hint` — the last four characters — so an admin can tell
    *which* key is loaded without the key itself travelling to a browser on
    every panel load. A lost key is regenerated at OpenRouter, not recovered
    here.
  - **Never logged.** No log line, exception message, or audit row in this
    module interpolates the key. The audit rows record the hint only.

Resolution order (`resolve_openrouter_api_key`, used by
src/pipeline_runner.py): the admin-set row wins; `OPENROUTER_API_KEY` is the
fallback. That order matters — an existing Docker Compose deploy with the key in its
`.env` keeps working untouched after this module lands, and an admin who
later sets a key in the UI sees it take effect rather than being silently
overridden by the env var.

AWS target: `MODEL_SETTINGS_TABLE` is unset (no CDK table is provisioned —
that target uses Bedrock, for which this key is meaningless), so
`key_store_available` is False, the admin panel renders an explanatory
message instead of a form, and writes are refused. Every read path degrades
to the pre-existing env-var behavior, keeping config.py's "unset = AWS
behavior, byte-identical" invariant intact.

Endpoints this module backs (wired in src/main.py):
  GET    /api/admin/model-key    get_model_key_settings
  POST   /api/admin/model-key    set_model_key
  DELETE /api/admin/model-key    clear_model_key

Environment variables consumed:
  MODEL_SETTINGS_TABLE   DynamoDB model-settings table name (PK: setting_id).
                         Unset -> no admin-managed key store (the AWS target).
  AUDIT_TABLE            DynamoDB audit table name (append-only).
  OPENROUTER_API_KEY     Fallback key when no admin has set one.
"""

import logging
import os
import time
import uuid
from typing import Any

from fastapi import HTTPException, status

try:  # production runs `src.main`; tests put backend/src on sys.path
    from src import config
except ImportError:  # pragma: no cover
    import config  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

MODEL_KEY_SETTING_ID = "global"

# Shortest value accepted by set_model_key. Not a security property — a real
# OpenRouter key is far longer — just enough that a fat-fingered paste is
# rejected here rather than surfacing later as an opaque HTTP 401 from
# OpenRouter mid-review, and enough that `_key_hint` has four characters to
# show without revealing most of the value.
MIN_API_KEY_LENGTH = 8

# Deliberately NOT validated: the `sk-or-v1-` prefix OpenRouter currently
# uses. Pinning a provider's key format here would reject a perfectly good
# key the day they change it, and the request itself is the real validator.


def _model_settings_table_name() -> str | None:
    """The model-settings table name, or None when this deployment has no
    admin-managed key store (the AWS target). Empty/whitespace-only counts as
    unset, matching config.endpoint_url's convention."""
    return os.environ.get("MODEL_SETTINGS_TABLE", "").strip() or None


def _model_settings_table(dynamodb_resource: Any):
    name = _model_settings_table_name()
    if name is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This deployment has no admin-managed model key store "
                "(MODEL_SETTINGS_TABLE is unset). Set OPENROUTER_API_KEY in "
                "the environment instead."
            ),
        )
    return dynamodb_resource.Table(name)


def _audit_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["AUDIT_TABLE"])


def _is_admin(caller_user_row: dict[str, Any]) -> bool:
    """`is_admin` is a DynamoDB `users`-row flag, never a JWT claim -- same
    convention as src/users.py::_is_admin, src/retention.py::_is_admin and
    src/demo_auth.py::_is_admin."""
    return bool(caller_user_row.get("is_admin", False))


def _require_admin(caller_user_row: dict[str, Any], detail: str) -> None:
    if not _is_admin(caller_user_row):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _key_hint(api_key: str) -> str:
    """A display hint for a stored key: the last four characters only.

    Enough for an admin to confirm *which* key is loaded (and that a rotation
    took effect); not enough to reconstruct it. The leading `sk-or-v1-` is
    deliberately not shown — it is constant across every OpenRouter key, so
    it would identify nothing while making the hint look more key-like than
    it is.
    """
    if len(api_key) < MIN_API_KEY_LENGTH:
        return "…"
    return f"…{api_key[-4:]}"


def _write_audit_entry(
    dynamodb_resource: Any,
    actor: str,
    action: str,
    target: str,
    target_type: str,
    detail: dict[str, Any],
) -> None:
    """Append an immutable audit row -- identifiers and key HINTS only, never
    the key itself (same posture as src/demo_auth.py's own _write_audit_entry,
    which never records a plaintext password)."""
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


def _stored_row(dynamodb_resource: Any) -> dict[str, Any] | None:
    """The raw model-settings row, or None when no admin has set a key (or
    this deployment has no key store at all). Callers outside this module
    must not touch the `api_key` attribute this returns."""
    if _model_settings_table_name() is None:
        return None
    table = _model_settings_table(dynamodb_resource)
    resp = table.get_item(Key={"setting_id": MODEL_KEY_SETTING_ID})
    item = resp.get("Item")
    if not item or not item.get("api_key"):
        return None
    return item


def _env_api_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def resolve_openrouter_api_key(dynamodb_resource: Any = None) -> str:
    """The API key the OpenRouter client should authenticate with: the
    admin-set row if one exists, else `OPENROUTER_API_KEY`, else "".

    `dynamodb_resource` is optional so a caller with no DynamoDB handle (or a
    deployment with no key store) still gets the env-var behavior that
    existed before this module. Any failure reading the row is swallowed
    deliberately and falls back to the env var: a transient DynamoDB blip
    should degrade to the operator-configured key, not fail every review.

    Returns "" when neither source has a key — the caller
    (OpenRouterModelClient) raises on an empty key with its own message.
    """
    if dynamodb_resource is not None:
        try:
            row = _stored_row(dynamodb_resource)
        except Exception:  # noqa: BLE001 - degrade to the env var, never wedge a review
            logger.warning(
                "Could not read the admin-set model key; falling back to "
                "OPENROUTER_API_KEY.",
                exc_info=True,
            )
        else:
            if row:
                return str(row["api_key"])
    return _env_api_key()


def get_model_key_settings(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """GET /api/admin/model-key.

    Reports whether a key is loaded, which source it came from, and a hint at
    which key it is — NEVER the key itself. Raises HTTPException(403) if the
    caller is not an admin.

    `key_store_available` False means this deployment has no admin-managed
    store (the AWS target); the panel renders an explanation rather than a
    form, and `source` can then only ever be "env" or None.
    """
    _require_admin(caller_user_row, "Admin privilege required to view the model key setting.")

    store_available = _model_settings_table_name() is not None
    row = _stored_row(dynamodb_resource) if store_available else None
    env_key = _env_api_key()

    if row:
        source: str | None = "admin"
        hint = str(row.get("key_hint") or _key_hint(str(row["api_key"])))
    elif env_key:
        source = "env"
        hint = _key_hint(env_key)
    else:
        source = None
        hint = ""

    return {
        "setting_id": MODEL_KEY_SETTING_ID,
        "key_store_available": store_available,
        "model_provider": config.model_provider(),
        "key_set": source is not None,
        "key_source": source,
        "key_hint": hint,
        "updated_at": str(row.get("updated_at", "")) if row else "",
        "updated_by": str(row.get("updated_by", "")) if row else "",
    }


def set_model_key(
    new_api_key: str,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """POST /api/admin/model-key.

    Stores the instance-wide OpenRouter key, overriding `OPENROUTER_API_KEY`
    for every subsequent review. Applies immediately and single-admin: unlike
    a retroactive retention reduction there is nothing irreversible to
    dual-control here, and the previous key is not recoverable from this
    store anyway (it is overwritten, and was never readable).

    Raises HTTPException(403) for a non-admin caller, 400 for an empty or
    implausibly short key, or 400 when this deployment has no key store.
    """
    _require_admin(caller_user_row, "Admin privilege required to change the model key setting.")

    candidate = (new_api_key or "").strip()
    if not candidate:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="api_key must be a non-empty string.",
        )
    if len(candidate) < MIN_API_KEY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"api_key must be at least {MIN_API_KEY_LENGTH} characters.",
        )
    if any(ch.isspace() for ch in candidate):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="api_key must not contain whitespace.",
        )

    before = get_model_key_settings(caller_user_row, dynamodb_resource)
    hint = _key_hint(candidate)
    actor = caller_user_row.get("cognito_sub", "")
    now = str(int(time.time()))

    table = _model_settings_table(dynamodb_resource)
    table.update_item(
        Key={"setting_id": MODEL_KEY_SETTING_ID},
        UpdateExpression=(
            "SET api_key = :k, key_hint = :h, updated_at = :t, updated_by = :a"
        ),
        ExpressionAttributeValues={":k": candidate, ":h": hint, ":t": now, ":a": actor},
    )

    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="model_key_change",
        target=MODEL_KEY_SETTING_ID,
        target_type="model_settings",
        detail={
            "before_key_source": before["key_source"] or "none",
            "before_key_hint": before["key_hint"],
            "after_key_source": "admin",
            "after_key_hint": hint,
        },
    )

    # Hint only -- the key itself never reaches a log sink.
    logger.info("MODEL_KEY_CHANGE: actor=%s after_key_hint=%s", actor, hint)

    return get_model_key_settings(caller_user_row, dynamodb_resource)


def clear_model_key(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """DELETE /api/admin/model-key.

    Removes the admin-set key, reverting the instance to whatever
    `OPENROUTER_API_KEY` provides (or to no key at all). Idempotent — clearing
    when nothing is stored is a success, not a 404.

    Raises HTTPException(403) for a non-admin caller, 400 when this
    deployment has no key store.
    """
    _require_admin(caller_user_row, "Admin privilege required to change the model key setting.")

    before = get_model_key_settings(caller_user_row, dynamodb_resource)

    table = _model_settings_table(dynamodb_resource)
    table.delete_item(Key={"setting_id": MODEL_KEY_SETTING_ID})

    actor = caller_user_row.get("cognito_sub", "")
    after = get_model_key_settings(caller_user_row, dynamodb_resource)

    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="model_key_clear",
        target=MODEL_KEY_SETTING_ID,
        target_type="model_settings",
        detail={
            "before_key_source": before["key_source"] or "none",
            "before_key_hint": before["key_hint"],
            "after_key_source": after["key_source"] or "none",
        },
    )

    logger.info(
        "MODEL_KEY_CLEAR: actor=%s reverted_to=%s", actor, after["key_source"] or "none"
    )

    return after
