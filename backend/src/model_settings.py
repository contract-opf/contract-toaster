"""
Admin-managed model-provider settings — the instance-wide OpenRouter API key,
and (issue #445) the instance-wide choice of which primary/critic models to
review with.

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

  - **Write-only.** `get_model_key_settings` NEVER returns the stored key --
    not whole, not masked, not partially. It returns `key_fingerprint`: the
    first eight hex characters of a salted SHA-256 OVER the key (issue
    #651). That is enough for an admin to tell two keys apart and to see
    that a rotation took effect, and no reversible step derives it from the
    key. A lost key is regenerated at OpenRouter, not recovered here.

    This REPLACED a last-four `key_hint` (issue #651): four characters of a
    live credential are still four characters of a live credential leaving
    the server on every panel load -- into screenshots, page reads and
    automation transcripts. "Masked but real" is precisely the failure mode
    that ticket exists to remove, so do not reintroduce a hint, a preview or
    a reveal affordance here.
  - **Never logged.** No log line, exception message, or audit row in this
    module interpolates the key. The audit rows record the fingerprint only.

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

MODEL SELECTION (issue #445) lives in the SAME table but on its OWN row,
`setting_id="models"`, deliberately not alongside the key on the "global" row:
`clear_model_key` deletes its row outright, so a shared row would silently
throw away an admin's model choice the moment they rotated the key back to the
environment. The two settings have independent lifecycles and now have
independent rows.

The choice is constrained to `model-policy/openrouter.json`'s `selectable`
allowlist — the same allowlist `model_client.enforce_openrouter_policy_model_id`
accepts at invocation time — so the store cannot persist a model the runtime
would then refuse. Precedence mirrors the key's exactly (admin row wins, env
var is the fallback/break-glass):

    admin selection  >  OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID  >  policy pin

Endpoints this module backs (wired in src/main.py):
  GET    /api/admin/model-key        get_model_key_settings
  POST   /api/admin/model-key        set_model_key
  DELETE /api/admin/model-key        clear_model_key
  GET    /api/admin/model-selection  get_model_selection_settings
  POST   /api/admin/model-selection  set_model_selection
  POST   /api/admin/spend-cap        set_daily_spend_cap_cents

The selection endpoints are SIBLINGS of the key endpoints rather than extra
fields on them, so the key's write-only response shape is left byte-identical
and there is no new way for a key to escape through a route that now also
serves model metadata.

Environment variables consumed:
  MODEL_SETTINGS_TABLE   DynamoDB model-settings table name (PK: setting_id).
                         Unset -> no admin-managed key store (the AWS target).
  AUDIT_TABLE            DynamoDB audit table name (append-only).
  OPENROUTER_API_KEY     Fallback key when no admin has set one.
  OPENROUTER_PRIMARY_MODEL_ID / OPENROUTER_CRITIC_MODEL_ID
                         Break-glass model-id overrides, read by
                         src/model_client.py; reported here as the `source`
                         of an effective id so the admin panel can say where
                         the running choice actually came from.
  DAILY_SPEND_CAP_USD_CENTS
                         Fallback daily ceiling in cents when no admin cap is
                         stored (default 2000 = $20/day), issue #653.

THE DAILY SPEND CAP (issue #653) is the THIRD row, `setting_id="spend"`, on
the same table and separate for the same reason. Same precedence again -- admin
row wins, env var is the fallback:

    admin cap  >  DAILY_SPEND_CAP_USD_CENTS  >  2000c ($20/day)

It has no GET of its own: the admin screen reads it off `GET /api/admin/spend`
(src/admin_dashboard.py), where it sits next to the spend it bounds, so the cap
and the spend it is compared against can never be read from two answers taken
at different moments. See the section comment above `_spend_row` for why the
admin row is allowed to RAISE the deployment's cap rather than only lower it,
and for the direction its read-failure fallback moves.
"""

import hashlib
import logging
import os
import time
import uuid
from typing import Any

from fastapi import HTTPException, status

try:  # production runs `src.main`; tests put backend/src on sys.path
    from src import config, model_client
    from src.authz import require_admin
except ImportError:  # pragma: no cover
    import config  # type: ignore[no-redef]
    import model_client  # type: ignore[no-redef]
    from authz import require_admin  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

MODEL_KEY_SETTING_ID = "global"

# Separate row from the key's (see the module docstring): clear_model_key
# deletes the key's row, which must never take the model selection with it.
MODEL_SELECTION_SETTING_ID = "models"

# Roles an admin may choose a model for. There is deliberately no "use one
# model for both" option: the adversarial critic is a design invariant
# (ARCHITECTURE.md; scripts/review_spine.py never reports a silent
# single-pass result), so the two roles are always chosen independently.
MODEL_SELECTION_ROLES = ("primary", "critic")

# Shortest value accepted by set_model_key. Not a security property — a real
# OpenRouter key is far longer — just enough that a fat-fingered paste is
# rejected here rather than surfacing later as an opaque HTTP 401 from
# OpenRouter mid-review.
MIN_API_KEY_LENGTH = 8

# Issue #651: validate the provider's key shape on write, so a mistyped or
# wrong-service paste fails at the form instead of at the next paid review.
#
# `sk-or-`, NOT `sk-or-v1-`. The version segment is the part that moves: the
# earlier decision here was to validate NOTHING, on the grounds that pinning
# a provider's key format rejects a perfectly good key the day the provider
# changes it. That objection is real, and this prefix answers it — a
# hypothetical `sk-or-v2-…` key still passes, while `sk-ant-…`,
# `sk-proj-…` and a half-pasted key do not.
OPENROUTER_KEY_PREFIX = "sk-or-"

# Domain-separation salt for `secret_fingerprint`. Constant and public by
# design, NOT a secret: it exists so a fingerprint here can never be matched
# against a bare SHA-256 of the same string computed anywhere else, not to
# make the digest itself hard to compute. The non-reversibility rests on
# SHA-256 and on the key's own entropy (an OpenRouter key is a long random
# string), which is why the salt being in the source costs nothing.
#
# It is constant rather than per-deployment ON PURPOSE: a fingerprint has to
# stay stable across restarts and across the audit rows written months apart,
# or "the fingerprint changed" stops meaning "the key changed".
SECRET_FINGERPRINT_SALT = b"contract-toaster/secret-fingerprint/v1|"

# How much of the digest an operator sees. Eight hex characters is enough to
# tell two keys apart at a glance and to read out over a call; the rest adds
# nothing an operator can use.
SECRET_FINGERPRINT_LENGTH = 8


def secret_fingerprint(secret: str) -> str:
    """A stable, non-reversible identifier for a secret: the first
    `SECRET_FINGERPRINT_LENGTH` hex characters of a salted SHA-256 (issue
    #651). `""` for an absent secret.

    This is the ONLY thing this module is allowed to say about a stored
    secret's value. It is not a mask, a hint or a preview: no character of
    the input survives into the output, so publishing it on a screen, in a
    log line or in an audit row leaks nothing about the credential itself.

    Deliberately a plain digest and not a password hash: the input is a
    high-entropy machine-generated credential, not a human-chosen password,
    so there is no dictionary for an attacker to run and nothing for a work
    factor to buy — while a per-call KDF would have to run on every admin
    panel load.
    """
    if not secret:
        return ""
    digest = hashlib.sha256(SECRET_FINGERPRINT_SALT + secret.encode("utf-8")).hexdigest()
    return digest[:SECRET_FINGERPRINT_LENGTH]


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


def _write_audit_entry(
    dynamodb_resource: Any,
    actor: str,
    action: str,
    target: str,
    target_type: str,
    detail: dict[str, Any],
) -> None:
    """Append an immutable audit row -- identifiers and key FINGERPRINTS only,
    never the key itself, and never any part of it (same posture as
    src/demo_auth.py's own _write_audit_entry, which never records a plaintext
    password)."""
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

    Reports whether a key is loaded, which source it came from, a
    non-reversible `key_fingerprint` telling an admin WHICH key it is, and
    when/by whom the stored one was last set — NEVER the key itself, and
    never any substring of it (issue #651). Raises HTTPException(403) if the
    caller is not an admin.

    The fingerprint is recomputed from the live value on every read rather
    than read back from the row, so it describes the key that is actually in
    force — including an env-var key this store never wrote, and a row an
    older build stamped before fingerprints existed.

    `key_store_available` False means this deployment has no admin-managed
    store (the AWS target); the panel renders an explanation rather than a
    form, and `source` can then only ever be "env" or None.
    """
    require_admin(caller_user_row, "Admin privilege required to view the model key setting.")

    store_available = _model_settings_table_name() is not None
    row = _stored_row(dynamodb_resource) if store_available else None
    env_key = _env_api_key()

    if row:
        source: str | None = "admin"
        fingerprint = secret_fingerprint(str(row["api_key"]))
    elif env_key:
        source = "env"
        fingerprint = secret_fingerprint(env_key)
    else:
        source = None
        fingerprint = ""

    return {
        "setting_id": MODEL_KEY_SETTING_ID,
        "key_store_available": store_available,
        "model_provider": config.model_provider(),
        "key_set": source is not None,
        "key_source": source,
        "key_fingerprint": fingerprint,
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

    Raises HTTPException(403) for a non-admin caller, 400 for an empty,
    implausibly short or wrong-shaped key, or 400 when this deployment has no
    key store.
    """
    require_admin(caller_user_row, "Admin privilege required to change the model key setting.")

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
    if not candidate.startswith(OPENROUTER_KEY_PREFIX):
        # Issue #651: the cheap, safe half of validation. The detail names the
        # expected prefix and nothing about what was actually submitted -- an
        # error message is a log sink like any other, and echoing the rejected
        # value back would leak a key that was merely pasted into the wrong
        # field.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"api_key does not look like an OpenRouter key (it must start "
                f"with {OPENROUTER_KEY_PREFIX!r}). Copy the key from "
                "openrouter.ai/keys."
            ),
        )

    before = get_model_key_settings(caller_user_row, dynamodb_resource)
    fingerprint = secret_fingerprint(candidate)
    actor = caller_user_row.get("cognito_sub", "")
    now = str(int(time.time()))

    table = _model_settings_table(dynamodb_resource)
    # `key_fingerprint` is stored as well as returned, so an operator reading
    # the table directly can match a row against an audit row without the
    # key; reads recompute it from `api_key` regardless, so a row written by
    # an older build still reports one.
    #
    # `REMOVE key_hint` retires the pre-#651 attribute -- the last four
    # characters of whatever key that row held -- rather than leaving real key
    # material sitting in the table on every deployment that ever set one.
    table.update_item(
        Key={"setting_id": MODEL_KEY_SETTING_ID},
        UpdateExpression=(
            "SET api_key = :k, key_fingerprint = :f, updated_at = :t, "
            "updated_by = :a REMOVE key_hint"
        ),
        ExpressionAttributeValues={
            ":k": candidate,
            ":f": fingerprint,
            ":t": now,
            ":a": actor,
        },
    )

    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="model_key_change",
        target=MODEL_KEY_SETTING_ID,
        target_type="model_settings",
        detail={
            "before_key_source": before["key_source"] or "none",
            "before_key_fingerprint": before["key_fingerprint"],
            "after_key_source": "admin",
            "after_key_fingerprint": fingerprint,
        },
    )

    # Fingerprint only -- no part of the key ever reaches a log sink.
    logger.info("MODEL_KEY_CHANGE: actor=%s after_key_fingerprint=%s", actor, fingerprint)

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
    require_admin(caller_user_row, "Admin privilege required to change the model key setting.")

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
            "before_key_fingerprint": before["key_fingerprint"],
            "after_key_source": after["key_source"] or "none",
            "after_key_fingerprint": after["key_fingerprint"],
        },
    )

    logger.info(
        "MODEL_KEY_CLEAR: actor=%s reverted_to=%s", actor, after["key_source"] or "none"
    )

    return after


# ---------------------------------------------------------------------------
# Admin model selection (issue #445) — which primary/critic models reviews run
# on. Its own row (MODEL_SELECTION_SETTING_ID) in the same table; see the
# module docstring for why it is not stored beside the key.
# ---------------------------------------------------------------------------


def _selection_row(dynamodb_resource: Any) -> dict[str, Any]:
    """The raw model-selection row, or {} when nothing is stored (or this
    deployment has no settings store at all)."""
    if _model_settings_table_name() is None:
        return {}
    table = _model_settings_table(dynamodb_resource)
    resp = table.get_item(Key={"setting_id": MODEL_SELECTION_SETTING_ID})
    return resp.get("Item") or {}


def _selection_row_or_default(dynamodb_resource: Any) -> dict[str, Any]:
    """The raw model-selection row, degrading to `{}` on ANY read failure.

    This is the only read of that row callers should use. A failure is
    swallowed for the same reason `resolve_openrouter_api_key` swallows its
    own: a transient DynamoDB blip should degrade to the configured default,
    never fail every review -- and, on the admin panel, never turn a page
    load into a 500 and an error banner where the defaults would have done
    (the loading/error-state family of issue #439).
    """
    try:
        return _selection_row(dynamodb_resource)
    except Exception:  # noqa: BLE001 - degrade to the default, never wedge a review
        logger.warning(
            "Could not read the admin-set model selection; falling back to the "
            "env override / policy pin.",
            exc_info=True,
        )
        return {}


def _selection_from_row(row: dict[str, Any]) -> dict[str, str]:
    """`{"primary": <id or "">, "critic": <id or "">}` out of a stored row."""
    return {
        role: str(row.get(f"{role}_model_id") or "").strip()
        for role in MODEL_SELECTION_ROLES
    }


def _stored_selection(dynamodb_resource: Any) -> dict[str, str]:
    """`{"primary": <id or "">, "critic": <id or "">}` as stored by an admin,
    or all-empty when nothing is stored or the row could not be read."""
    return _selection_from_row(_selection_row_or_default(dynamodb_resource))


def resolve_openrouter_model_ids(dynamodb_resource: Any = None) -> dict[str, str]:
    """The EFFECTIVE primary/critic model ids for the next review:
    `{"primary": str, "critic": str}`.

    Threads the admin selection (if any) through
    `model_client.openrouter_{primary,critic}_model_id`, which applies the
    documented precedence — admin selection, then
    OPENROUTER_{PRIMARY,CRITIC}_MODEL_ID, then the policy pin — and quietly
    drops a stored id that has since fallen off the `selectable` allowlist.

    `dynamodb_resource` is optional so a caller with no DynamoDB handle (or a
    deployment with no settings store) still gets exactly the pre-#445
    env-var/policy behavior.
    """
    selection = (
        _stored_selection(dynamodb_resource)
        if dynamodb_resource is not None
        else {role: "" for role in MODEL_SELECTION_ROLES}
    )
    return {
        "primary": model_client.openrouter_primary_model_id(
            admin_model_id=selection["primary"] or None
        ),
        "critic": model_client.openrouter_critic_model_id(
            admin_model_id=selection["critic"] or None
        ),
    }


def _default_entry(policy: dict[str, Any], role: str) -> dict[str, Any]:
    """The policy-pinned default for a role, in the same shape as a
    `selectable` entry, so the panel can price the "keep the default" option
    from the artifact's own rates instead of a hardcoded string."""
    block = policy["models"][role]
    return {
        "model_id": block["model_id"],
        "cost_per_million_input_usd": block["cost_per_million_input_usd"],
        "cost_per_million_output_usd": block["cost_per_million_output_usd"],
    }


def _pricing_basis(policy: dict[str, Any], role: str) -> dict[str, int]:
    """The per-review token counts this repo's own cost model uses for a role
    (`approx_tokens_per_review_{input,output}`). Sent to the client so the
    displayed per-review cost is computed from the artifact, never hardcoded
    in the component -- the rates and the basis both move over time."""
    block = policy["models"][role]
    return {
        "input_tokens": int(block["approx_tokens_per_review_input"]),
        "output_tokens": int(block["approx_tokens_per_review_output"]),
    }


def _selection_source(role: str, stored_id: str, effective_id: str) -> str:
    """Where the effective id for a role actually came from: "admin" (an
    in-force stored selection), "env" (a break-glass
    OPENROUTER_*_MODEL_ID override), or "default" (the policy pin).

    Derived from the RESOLVED id rather than from "is something stored", so a
    stored id that `model_client` dropped as no-longer-selectable reports
    honestly as env/default instead of claiming an admin choice is in force.
    """
    if stored_id and stored_id == effective_id:
        return "admin"
    env_override = os.environ.get(f"OPENROUTER_{role.upper()}_MODEL_ID", "").strip()
    if env_override and env_override == effective_id:
        return "env"
    return "default"


def get_model_selection_settings(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """GET /api/admin/model-selection.

    Reports the selectable catalogue (ids, tier labels, notes and per-million
    rates straight out of model-policy/openrouter.json), the policy defaults,
    what an admin has stored, and which id each role will actually run on next
    — plus the token basis the panel needs to turn those rates into a
    per-review cost. Raises HTTPException(403) if the caller is not an admin.

    Carries NO API-key material of any kind: it reads a different row and
    never touches `api_key`.
    """
    require_admin(
        caller_user_row, "Admin privilege required to view the model selection setting."
    )

    store_available = _model_settings_table_name() is not None
    policy = model_client.load_openrouter_policy()
    # ONE guarded read serves both the stored ids and the "who changed it
    # when" stamps. Reading the row a second time (unguarded) would undo the
    # degradation `_selection_row_or_default` exists for: the blip it just
    # swallowed would escape here instead, as an HTTP 500.
    row = _selection_row_or_default(dynamodb_resource) if store_available else {}
    stored = _selection_from_row(row)
    effective = resolve_openrouter_model_ids(dynamodb_resource if store_available else None)

    result: dict[str, Any] = {
        "setting_id": MODEL_SELECTION_SETTING_ID,
        "selection_store_available": store_available,
        "model_provider": config.model_provider(),
        "selectable": model_client.openrouter_selectable_models(policy),
        "updated_at": str(row.get("models_updated_at", "")),
        "updated_by": str(row.get("models_updated_by", "")),
    }
    for role in MODEL_SELECTION_ROLES:
        result[f"default_{role}"] = _default_entry(policy, role)
        result[f"pricing_basis_{role}"] = _pricing_basis(policy, role)
        result[f"selected_{role}_model_id"] = stored[role]
        result[f"effective_{role}_model_id"] = effective[role]
        result[f"{role}_source"] = _selection_source(role, stored[role], effective[role])
    return result


def _validated_selection(role: str, raw: Any, selectable_ids: set[str]) -> str:
    """One role's requested model id, normalised to "" (meaning "revert to the
    default") or an id proven to be on the `selectable` allowlist.

    Rejecting an unlisted id HERE is what keeps the store and the runtime in
    agreement: `model_client.enforce_openrouter_policy_model_id` would refuse
    it at invocation time, which would surface as every review failing rather
    than as a 400 on the save that caused it.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{role}_model_id must be a string (or null to use the default).",
        )
    candidate = raw.strip()
    if not candidate:
        return ""
    if candidate not in selectable_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"{candidate!r} is not a selectable model. Choose one of: "
                f"{', '.join(sorted(selectable_ids))}."
            ),
        )
    return candidate


def set_model_selection(
    primary_model_id: Any,
    critic_model_id: Any,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """POST /api/admin/model-selection.

    Stores the instance-wide primary and critic model choice, taking effect on
    the next review with no redeploy (`pipeline_runner` resolves it per review,
    not at import). Either role may be passed "" or null to revert THAT role to
    the policy default; the two are always set together, and there is no option
    to use one model for both passes.

    Raises HTTPException(403) for a non-admin caller, 400 for a non-string or
    non-selectable id, or 400 when this deployment has no settings store.
    """
    require_admin(
        caller_user_row, "Admin privilege required to change the model selection setting."
    )

    if _model_settings_table_name() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This deployment has no admin-managed model settings store "
                "(MODEL_SETTINGS_TABLE is unset). Set OPENROUTER_PRIMARY_MODEL_ID "
                "/ OPENROUTER_CRITIC_MODEL_ID in the environment instead."
            ),
        )

    selectable_ids = model_client.openrouter_selectable_model_ids()
    requested = {
        "primary": _validated_selection("primary", primary_model_id, selectable_ids),
        "critic": _validated_selection("critic", critic_model_id, selectable_ids),
    }

    before = get_model_selection_settings(caller_user_row, dynamodb_resource)
    actor = caller_user_row.get("cognito_sub", "")
    now = str(int(time.time()))

    table = _model_settings_table(dynamodb_resource)
    table.update_item(
        Key={"setting_id": MODEL_SELECTION_SETTING_ID},
        UpdateExpression=(
            "SET primary_model_id = :p, critic_model_id = :c, "
            "models_updated_at = :t, models_updated_by = :a"
        ),
        ExpressionAttributeValues={
            ":p": requested["primary"],
            ":c": requested["critic"],
            ":t": now,
            ":a": actor,
        },
    )

    after = get_model_selection_settings(caller_user_row, dynamodb_resource)

    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="model_selection_change",
        target=MODEL_SELECTION_SETTING_ID,
        target_type="model_settings",
        detail={
            "before_primary_model_id": before["effective_primary_model_id"],
            "before_critic_model_id": before["effective_critic_model_id"],
            "after_primary_model_id": after["effective_primary_model_id"],
            "after_critic_model_id": after["effective_critic_model_id"],
        },
    )

    # Model ids are configuration, not substance -- safe to log in full.
    logger.info(
        "MODEL_SELECTION_CHANGE: actor=%s primary=%s critic=%s",
        actor,
        after["effective_primary_model_id"],
        after["effective_critic_model_id"],
    )

    return after


# ---------------------------------------------------------------------------
# The daily spend cap (issue #653, epic #649) — a THIRD row, `setting_id="spend"`.
#
# The ceiling used to be readable ONLY as `DAILY_SPEND_CAP_USD_CENTS` in the
# deployment's compose file — a file that, on this deployment, also holds
# plaintext secrets, is not in git, and has no revert (epic #649's evidence
# section). Adjusting a number that decides whether the next review is allowed
# to run should not require an unreviewable edit to a secret-bearing file.
#
# ITS OWN ROW, for exactly the reason the model selection has its own:
# `clear_model_key` deletes the key's row outright, and rotating the key back
# to the environment must not silently take the instance's spend ceiling with
# it.
#
# NO NEW TABLE, deliberately. The cap is priced against the model selection
# stored one row over and is read by the same admin screen; a second DynamoDB
# table for one integer would cost a compose and bootstrap change on both
# deployment targets and buy nothing.
#
# PRECEDENCE, mirroring the API key's and the model selection's exactly:
#
#     admin-stored cap  >  DAILY_SPEND_CAP_USD_CENTS  >  the built-in default
#
# The admin row WINS over the environment rather than being clamped by it.
# That is a deliberate departure from the "the env may only force a switch
# OFF" shape issue #650 once proposed for feature switches, and it is safe for
# the same reason `set_model_selection` already is: an admin can ALREADY move
# real worst-case exposure by a large factor by choosing the dearest selectable
# pair, and can already point the instance at a different provider key. A cap
# they could only lower would not remove that power — it would only make the
# one number that bounds it the single value they still have to leave the app
# to change, which is the whole complaint epic #649 was filed on.
# `MAX_DAILY_SPEND_CAP_USD_CENTS` is the real bound, every change is audited,
# and the write is admin-gated.
#
# Both readers of the cap — `reviews.reserve_spend`'s conditional write and
# `reviews.cover_note_daily_cap_reached`'s read-then-compare — resolve it
# through `resolve_daily_spend_cap_cents` on EVERY call, so a lowered cap takes
# effect on the next reservation rather than at the next UTC midnight.
# ---------------------------------------------------------------------------

SPEND_SETTING_ID = "spend"

# The ceiling that applies when nothing else says otherwise. Lives HERE, not in
# src/reviews.py, because the cap is a stored setting now and this module owns
# settings; `reviews.DAILY_SPEND_CAP_USD_CENTS_DEFAULT` is kept as an alias of
# this name for the callers and tests that already reference it, so there is
# one number rather than two that can drift.
DAILY_SPEND_CAP_USD_CENTS_DEFAULT = 2000  # $20.00/day default ceiling

# Bounds on an admin-set cap. The floor is 1 cent rather than 0 so that
# "adjust the cap" can never be spelled the same way as "stop the instance" —
# a zero cap refuses every review including the one that would show what went
# wrong, and there are deliberate ways to stop an instance that do not look
# like a typo in a spend field. The ceiling is a blast radius, not a policy: it
# is the largest number this form can turn into real provider spend, and it is
# what stands in for the deployment's veto now that the admin row outranks the
# environment.
MIN_DAILY_SPEND_CAP_USD_CENTS = 1
MAX_DAILY_SPEND_CAP_USD_CENTS = 1_000_000  # $10,000.00/day


def _spend_row(dynamodb_resource: Any) -> dict[str, Any]:
    """The raw spend-settings row, or {} when nothing is stored (or this
    deployment has no settings store at all)."""
    if _model_settings_table_name() is None:
        return {}
    table = _model_settings_table(dynamodb_resource)
    resp = table.get_item(Key={"setting_id": SPEND_SETTING_ID})
    return resp.get("Item") or {}


def _spend_row_or_default(dynamodb_resource: Any) -> dict[str, Any]:
    """The raw spend-settings row, degrading to `{}` on ANY read failure.

    Same discipline as `_selection_row_or_default`, and it matters more here:
    this read sits on the reservation path of every review submission, so a
    transient DynamoDB blip must fall back to the configured environment cap
    rather than fail the submission. Degrading to the ENV cap (not to "no cap")
    bounds the damage — a blip can never widen the ceiling beyond what the
    deployment itself configured.

    It DOES fail open relative to a LOWER admin-set cap, and that is the honest
    reading of it: `{}` means "no admin cap in force", so while the table is
    unreadable an admin cap of 100c under a `DAILY_SPEND_CAP_USD_CENTS` of 500
    leaves 500 in force. The alternative — refusing every review while DynamoDB
    is unhappy — fails an entire deployment closed over a transient, on the one
    path where the stored cap is exactly what cannot be read. The exposure is
    bounded on both ends and lasts only as long as the blip. Pinned by
    `tests/test_spend_cap_653.py::TestTheCapResolution::
    test_a_blip_over_a_lower_admin_cap_fails_OPEN_to_the_deployment_cap`, which
    seeds the lower admin cap and asserts the rise, so this stays a known
    degradation rather than a surprise found in an incident.
    """
    try:
        return _spend_row(dynamodb_resource)
    except Exception:  # noqa: BLE001 - degrade to the env cap, never wedge a review
        logger.warning(
            "Could not read the admin-set daily spend cap; falling back to "
            "DAILY_SPEND_CAP_USD_CENTS / the built-in default.",
            exc_info=True,
        )
        return {}


def _cap_from_row(row: dict[str, Any]) -> int | None:
    """The stored cap in cents, or None when the row carries no usable one.

    A row whose value is absent, non-numeric, or outside the accepted bounds
    reports None — i.e. "no admin cap is in force" — rather than being clamped
    into range. Clamping would invent a ceiling nobody chose; falling through
    to the environment value is the honest answer, and is the same thing
    `_selection_source` does with a selection that has gone stale.

    boto3's resource API hands stored numbers back as `decimal.Decimal`, which
    `int()` accepts.
    """
    raw = row.get("daily_cap_usd_cents")
    if raw is None:
        return None
    try:
        cents = int(raw)
    except (TypeError, ValueError):
        return None
    if not MIN_DAILY_SPEND_CAP_USD_CENTS <= cents <= MAX_DAILY_SPEND_CAP_USD_CENTS:
        return None
    return cents


def stored_daily_spend_cap_cents(dynamodb_resource: Any = None) -> int | None:
    """The admin-set daily cap in cents, or None when none is in force."""
    if dynamodb_resource is None:
        return None
    return _cap_from_row(_spend_row_or_default(dynamodb_resource))


def env_daily_spend_cap_cents() -> int | None:
    """`DAILY_SPEND_CAP_USD_CENTS` as an int, or None when it is unset or
    unparseable. Unparseable reads as unset on purpose: a typo'd env var must
    fall through to the documented default rather than raise on the reservation
    path of every submission."""
    raw = os.environ.get("DAILY_SPEND_CAP_USD_CENTS")
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "DAILY_SPEND_CAP_USD_CENTS is not an integer; using the built-in default."
        )
        return None


def resolve_daily_spend_cap_cents(dynamodb_resource: Any = None) -> int:
    """The daily ceiling the reservation path enforces RIGHT NOW, in cents.

    admin-stored > `DAILY_SPEND_CAP_USD_CENTS` > `DAILY_SPEND_CAP_USD_CENTS_
    DEFAULT`. `dynamodb_resource` is optional so a caller with no handle (or a
    deployment with no settings store — the AWS target, where
    `MODEL_SETTINGS_TABLE` is unset) keeps exactly the pre-#653 env-var
    behaviour, preserving config.py's "unset = AWS behavior, byte-identical"
    invariant.

    Every enforcement site reads THIS function, so the number the Settings tab
    renders and the number a submission is checked against cannot disagree.
    """
    stored = stored_daily_spend_cap_cents(dynamodb_resource)
    if stored is not None:
        return stored
    env_cap = env_daily_spend_cap_cents()
    if env_cap is not None:
        return env_cap
    return DAILY_SPEND_CAP_USD_CENTS_DEFAULT


def _cap_source(stored: int | None) -> str:
    """Where the effective cap came from: "admin", "env" or "default".

    Derived from the value that actually WON, so a stored cap that fell outside
    the accepted bounds (and was therefore ignored by `_cap_from_row`) reports
    honestly as env/default instead of claiming an admin choice is in force —
    the same rule `_selection_source` applies to a stale model id.
    """
    if stored is not None:
        return "admin"
    return "env" if env_daily_spend_cap_cents() is not None else "default"


def get_spend_cap_settings(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """The daily-cap setting as the admin UI needs it: what is stored, what the
    deployment configured, what is actually in force, where that came from, and
    the bounds a new value has to satisfy.

    Read by `admin_dashboard.get_spend_ledger` rather than by a route of its
    own, so the cap and the spend it bounds are always one answer taken at one
    moment. Raises HTTPException(403) if the caller is not an admin.

    Carries NO key material: it reads a different row and never touches
    `api_key`.
    """
    require_admin(
        caller_user_row, "Admin privilege required to view the daily spend cap."
    )
    store_available = _model_settings_table_name() is not None
    # ONE guarded read serves the stored cap AND the "who changed it when"
    # stamps, for the reason `get_model_selection_settings` gives: reading the
    # row a second time unguarded would let the blip this read just swallowed
    # escape as an HTTP 500.
    row = _spend_row_or_default(dynamodb_resource) if store_available else {}
    stored = _cap_from_row(row) if store_available else None
    env_cap = env_daily_spend_cap_cents()
    effective = (
        stored
        if stored is not None
        else (env_cap if env_cap is not None else DAILY_SPEND_CAP_USD_CENTS_DEFAULT)
    )
    return {
        "setting_id": SPEND_SETTING_ID,
        "cap_store_available": store_available,
        "stored_daily_cap_usd_cents": stored,
        "env_daily_cap_usd_cents": env_cap,
        "default_daily_cap_usd_cents": DAILY_SPEND_CAP_USD_CENTS_DEFAULT,
        "daily_cap_usd_cents": effective,
        "daily_cap_source": _cap_source(stored),
        "min_daily_cap_usd_cents": MIN_DAILY_SPEND_CAP_USD_CENTS,
        "max_daily_cap_usd_cents": MAX_DAILY_SPEND_CAP_USD_CENTS,
        "updated_at": str(row.get("spend_updated_at", "")),
        "updated_by": str(row.get("spend_updated_by", "")),
    }


def _validated_cap_cents(raw: Any) -> int | None:
    """A requested cap normalised to None ("revert to the deployment value") or
    an int proven to be inside the accepted bounds.

    `bool` is rejected explicitly: `isinstance(True, int)` is True in Python, so
    a JSON `true` would otherwise be stored as a one-cent ceiling.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="daily_cap_usd_cents must be a whole number of cents (or null).",
        )
    try:
        cents = int(str(raw).strip())
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="daily_cap_usd_cents must be a whole number of cents (or null).",
        ) from None
    if not MIN_DAILY_SPEND_CAP_USD_CENTS <= cents <= MAX_DAILY_SPEND_CAP_USD_CENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "daily_cap_usd_cents must be between "
                f"{MIN_DAILY_SPEND_CAP_USD_CENTS} and "
                f"{MAX_DAILY_SPEND_CAP_USD_CENTS} cents."
            ),
        )
    return cents


def set_daily_spend_cap_cents(
    daily_cap_usd_cents: Any,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """POST /api/admin/spend-cap.

    Stores the instance-wide daily ceiling, taking effect on the NEXT
    reservation (`reviews.reserve_spend` re-resolves it per call) with no
    redeploy. Pass null/"" to drop the stored cap and fall back to the
    deployment's `DAILY_SPEND_CAP_USD_CENTS`.

    Lowering the cap below what today has already committed is ALLOWED and is
    the point: the next submission is then refused with the existing 429 rather
    than silently admitted. Nothing already reserved is reversed — this changes
    the ceiling, it does not claw back spend.

    Raises HTTPException(403) for a non-admin caller, 400 for a value that is
    not a whole number of cents inside the accepted bounds, and 400 on a
    deployment with no settings store.
    """
    require_admin(
        caller_user_row, "Admin privilege required to change the daily spend cap."
    )
    if _model_settings_table_name() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This deployment has no admin-managed settings store "
                "(MODEL_SETTINGS_TABLE is unset). Set DAILY_SPEND_CAP_USD_CENTS "
                "in the environment instead."
            ),
        )

    requested = _validated_cap_cents(daily_cap_usd_cents)
    before = get_spend_cap_settings(caller_user_row, dynamodb_resource)
    actor = caller_user_row.get("cognito_sub", "")
    now = str(int(time.time()))

    table = _model_settings_table(dynamodb_resource)
    if requested is None:
        # REMOVE, not "store the deployment's current value": an absent
        # attribute is what `_cap_from_row` reads as "no admin cap in force",
        # and writing today's env value as a sentinel would stop the cleared
        # setting from TRACKING a later change to the deployment's own cap.
        table.update_item(
            Key={"setting_id": SPEND_SETTING_ID},
            UpdateExpression=(
                "REMOVE daily_cap_usd_cents "
                "SET spend_updated_at = :t, spend_updated_by = :a"
            ),
            ExpressionAttributeValues={":t": now, ":a": actor},
        )
    else:
        table.update_item(
            Key={"setting_id": SPEND_SETTING_ID},
            UpdateExpression=(
                "SET daily_cap_usd_cents = :c, spend_updated_at = :t, "
                "spend_updated_by = :a"
            ),
            ExpressionAttributeValues={":c": requested, ":t": now, ":a": actor},
        )

    after = get_spend_cap_settings(caller_user_row, dynamodb_resource)

    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="spend_cap_change",
        target=SPEND_SETTING_ID,
        target_type="model_settings",
        detail={
            "before_daily_cap_usd_cents": before["daily_cap_usd_cents"],
            "before_daily_cap_source": before["daily_cap_source"],
            "after_daily_cap_usd_cents": after["daily_cap_usd_cents"],
            "after_daily_cap_source": after["daily_cap_source"],
        },
    )

    # A spend ceiling is configuration, not substance -- safe to log in full.
    logger.info(
        "SPEND_CAP_CHANGE: actor=%s cap_cents=%s source=%s",
        actor,
        after["daily_cap_usd_cents"],
        after["daily_cap_source"],
    )

    return after
