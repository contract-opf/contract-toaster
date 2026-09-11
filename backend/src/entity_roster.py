"""
The deployment's roster of OUR OWN legal entity names — issue #678.

## Why this is deployment-scoped and not a playbook field

We are ~25 legal entities. Any of them can be the contracting party, and on
third-party paper the counterparty drafts with whichever name it was given: a
subsidiary, a former name, a d/b/a. Recognising all of them is what makes
party binding work at all (#677).

`perspective.party` + `perspective.counterparty_type` are genuinely
playbook-scoped — they state whose viewpoint that playbook's content
encodes. The roster is ORGANISATION-scoped: identical for every playbook we
will ever build, and it changes over time (acquisitions, dissolutions,
renames).

Copying an org-level fact into every artifact produces one specific, nasty
bug rather than generic staleness: the affiliation playbook knows 25
entities, the NDA playbook was compiled months earlier and knows 22, and the
same counterparty on the same document is recognised under one agreement
type and not another. That presents as flakiness and its cause is invisible
from the symptom. A single roster cannot produce that bug at all. The
playbook-engine owner reached the same conclusion independently and noted
that `OPF-BUNDLE-BOUNDARY.md` enumerates exactly `party` and
`counterparty_type` under `perspective` — so bolting an org-scoped roster
onto that object would itself be boundary drift. There is no `party_aliases`
OPF field coming; nothing here should wait for one.

It is not an environment variable either: a 25-line list in a compose file is
miserable to maintain and needs a redeploy to fix, and the first missing
entity will be found in production.

## `perspective.party` is NOT canonical, and this store does not make it so

Under the flat-set model the playbook's single `party` value is arbitrary —
populated because the schema requires it. No code path may treat it as the
primary entity. Accordingly this module knows nothing about it: it stores a
plain list of names, and the union with `perspective.party` happens at
composition time in `scripts/opf_prompt.py::resolve_party_recognition_set`,
which flattens and deduplicates so that NOTHING in the rendered prompt says
which source a name came from. The intended configuration is that the roster
contains every entity INCLUDING the one in `perspective.party`, which makes
the union a no-op in practice and leaves no "one plus the rest" shape to
leak.

## Conventions

Mirrored from `src/model_settings.py`'s model-selection row (which itself
mirrors `src/retention.py` and `src/demo_auth.py`): one row,
`setting_id="global"`, admin-gated read/write, every mutation appended to the
shared `audit` table.

Unlike the model key this holds NO credential: entity names are ordinary
business configuration, so they are returned in full on read, logged by count
and rendered in the audit row as before/after counts. (Counts, not the names:
an audit row is a different retention class from a settings row, and the
names are already readable at any time from the settings route.)

Environment:
  ENTITY_ROSTER_TABLE   DynamoDB entity-roster table name (PK: setting_id).
                        UNSET means this deployment has no roster store, and
                        every read degrades to the empty roster — the review
                        then recognises exactly the playbook's own
                        `perspective.party`, which is the pre-#678 behaviour.
                        Read with `.get(...)`, never `os.environ[...]`, so an
                        unset value is a documented degradation and not a
                        boot refusal (`src/startup_checks.py`).
  AUDIT_TABLE           DynamoDB audit table name (append-only).

## Why the input is validated so tightly

An entity name typed here is interpolated VERBATIM into a model system
prompt — `opf_prompt._context_block`'s JSON and
`review_spine._floor_perspective_note`'s "Our client:" line. A name
containing a newline could forge a second line of that note (e.g. another
"WHO THIS REVIEW ACTS FOR." heading naming someone else). Admins are trusted
with the roster's CONTENT; they are not the reason to allow a name to carry
prompt STRUCTURE. So control characters — newlines included — are rejected
at the door rather than escaped downstream, where one renderer forgetting to
escape is a silent hole.
"""

import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from fastapi import HTTPException, status

# `scripts/entity_normalize.py` is the ONE place party-name folding lives
# (issue #55). Put `scripts/` on `sys.path` the same idempotent way
# `src/pipeline_runner.py` does, so this module imports it by bare name
# whichever of the two import styles loaded `src` itself.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import entity_normalize  # noqa: E402

logger = logging.getLogger(__name__)

#: The single row this module reads and writes.
ENTITY_ROSTER_SETTING_ID = "global"

#: Ceilings. Not arbitrary: the roster is ~25 entities today, and every name
#: is composed into every OPF review's system prompt, so an unbounded list is
#: an unbounded prompt. Generous enough to absorb a decade of acquisitions and
#: small enough that the block stays a block.
MAX_ENTITIES = 200
MAX_NAME_LENGTH = 200


DEFAULT_ENTITY_ROSTER_TABLE = "contract-toaster-entity-roster-dts"


def _entity_roster_table_name() -> str:
    """The entity-roster table name, defaulting to
    `contract-toaster-entity-roster-dts` when unset. Empty/whitespace-only
    counts as unset."""
    return os.environ.get("ENTITY_ROSTER_TABLE", "").strip() or DEFAULT_ENTITY_ROSTER_TABLE


def _ensure_table(dynamodb_resource: Any, table_name: str) -> Any:
    """Ensure the entity roster table exists, creating it on demand if missing."""
    try:
        table = dynamodb_resource.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        if hasattr(table, "meta") and hasattr(table.meta, "client") and hasattr(table.meta.client, "get_waiter"):
            try:
                table.meta.client.get_waiter("table_exists").wait(TableName=table_name)
            except Exception:
                pass
        return table
    except Exception as exc:
        err_code = ""
        if hasattr(exc, "response") and isinstance(exc.response, dict):
            err_code = exc.response.get("Error", {}).get("Code", "")
        if err_code == "ResourceInUseException":
            return dynamodb_resource.Table(table_name)
        logger.warning("Could not auto-create entity roster table %s: %s", table_name, exc)
        return dynamodb_resource.Table(table_name)


def _entity_roster_table(dynamodb_resource: Any):
    name = _entity_roster_table_name()
    return dynamodb_resource.Table(name)


def _audit_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["AUDIT_TABLE"])


def _is_admin(caller_user_row: dict[str, Any]) -> bool:
    """`is_admin` is a DynamoDB `users`-row flag, never a JWT claim -- same
    convention as src/users.py, src/retention.py, src/demo_auth.py and
    src/model_settings.py."""
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
    """Append an immutable audit row (same shape as src/model_settings.py's
    own `_write_audit_entry`)."""
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


def normalize_entities(raw: Any) -> list[str]:
    """The stored form of a submitted roster: stripped, blank entries
    dropped, duplicates removed on `entity_normalize.recognition_key`, and
    the admin's own ordering preserved for display.

    Raises HTTPException(400) for a non-list body, a non-string entry, a name
    that is too long, a name carrying a control character (see the module
    docstring), or a roster over `MAX_ENTITIES`.

    Issue #55: the identity is the recognition key, not the old
    case-and-whitespace fold — so `Synthetic Holdings GmbH` and `SYNTHETIC
    HOLDINGS G.m.b.H.` are one entity typed twice, not two roster lines.
    The SPELLING IS STORED AS TYPED: the key is lossy (it throws the
    legal-form suffix away) and exists only to answer "same entity?". The
    surviving spelling is the FIRST one typed, which keeps the admin's own
    ordering meaningful and makes a Save idempotent.

    The 400 rules are deliberately untouched by that change: a name is
    rejected for what it contains, never for what it collides with.

    The dedup here is a courtesy for the admin reading the list back; it is
    NOT what the prompt relies on. `opf_prompt.resolve_party_recognition_set`
    deduplicates again -- on the same key -- over the union with the
    playbook's own party, because that is the only place both sources are in
    scope.
    """
    if not isinstance(raw, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="entities must be a list of legal entity names.",
        )
    if len(raw) > MAX_ENTITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"An entity roster may hold at most {MAX_ENTITIES} names.",
        )

    seen: set[str] = set()
    entities: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Every entity roster entry must be a string.",
            )
        name = entry.strip()
        if not name:
            continue
        if len(name) > MAX_NAME_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"An entity name may be at most {MAX_NAME_LENGTH} characters; "
                    f"one entry is {len(name)}."
                ),
            )
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "An entity name may not contain control characters (a line "
                    "break included): these names are composed verbatim into the "
                    "review prompt, where a line break would forge prompt structure."
                ),
            )
        identity = entity_normalize.recognition_key(name)
        if identity in seen:
            continue
        seen.add(identity)
        entities.append(name)
    return entities


def _stored_row(dynamodb_resource: Any) -> dict[str, Any]:
    """The raw roster row, or `{}` when no admin has set one (or table is newly created)."""
    table_name = _entity_roster_table_name()
    table = _entity_roster_table(dynamodb_resource)
    try:
        return table.get_item(Key={"setting_id": ENTITY_ROSTER_SETTING_ID}).get("Item") or {}
    except Exception as exc:
        err_code = ""
        if hasattr(exc, "response") and isinstance(exc.response, dict):
            err_code = exc.response.get("Error", {}).get("Code", "")
        if err_code == "ResourceNotFoundException":
            _ensure_table(dynamodb_resource, table_name)
            return {}
        raise


def _entities_from_row(item: dict[str, Any]) -> list[str]:
    """One row's `entities`, cleaned. Non-string members of a hand-edited row
    are dropped rather than raised on: this is read on the review path, where
    a console typo must narrow the recognition set, not fail the review."""
    stored = item.get("entities")
    if not isinstance(stored, Iterable) or isinstance(stored, (str, bytes)):
        return []
    return [entry.strip() for entry in stored if isinstance(entry, str) and entry.strip()]


def _stored_entities(dynamodb_resource: Any) -> list[str]:
    """The stored roster, or `[]`."""
    return _entities_from_row(_stored_row(dynamodb_resource))


def resolve_entity_roster(dynamodb_resource: Any = None) -> tuple[str, ...]:
    """The roster the review pipeline composes into its prompts:
    `backend/src/pipeline_runner.py` -> `review_spine.run_review
    (entity_roster=...)`.

    NEVER raises and never returns None. A caller with no DynamoDB handle, a
    deployment with no roster store, an unset row and a transient DynamoDB
    blip all resolve to `()` — which composes exactly the playbook's own
    `perspective.party`, i.e. the pre-#678 behaviour. Degrading to a SMALLER
    recognition set can only make a binding fail closed (#677); it can never
    bind us to the wrong party, which is why swallowing the error here is
    safe in the way swallowing it on a spend or auth path would not be.
    """
    if dynamodb_resource is None:
        return ()
    try:
        return tuple(_stored_entities(dynamodb_resource))
    except Exception:  # noqa: BLE001 - degrade to the playbook's own party, never wedge a review
        logger.warning(
            "Could not read the admin-set entity roster; this review recognises "
            "only the playbook's own perspective.party.",
            exc_info=True,
        )
        return ()


MAX_HISTORY_ENTRIES = 25


def get_entity_roster(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """GET /api/admin/entity-roster.

    Returns the stored names in full, whether this deployment has a store at
    all, and when/by whom the roster was last set. Raises HTTPException(403)
    if the caller is not an admin.
    """
    _require_admin(caller_user_row, "Admin privilege required to view the entity roster.")

    store_available = True
    try:
        item = _stored_row(dynamodb_resource)
    except Exception:
        logger.warning("Failed to retrieve entity roster item", exc_info=True)
        item = {}

    raw_history = item.get("history") or []
    history: list[dict[str, Any]] = []
    if isinstance(raw_history, list):
        for h in raw_history:
            if isinstance(h, dict):
                history.append({
                    "timestamp": str(h.get("timestamp", "")),
                    "actor": str(h.get("actor", "")),
                    "count": int(h.get("count", 0)),
                    "added": list(h.get("added") or []),
                    "removed": list(h.get("removed") or []),
                })

    return {
        "setting_id": ENTITY_ROSTER_SETTING_ID,
        "roster_store_available": store_available,
        "entities": _entities_from_row(item),
        "max_entities": MAX_ENTITIES,
        "max_name_length": MAX_NAME_LENGTH,
        "updated_at": str(item.get("entities_updated_at", "")),
        "updated_by": str(item.get("entities_updated_by", "")),
        "history": history,
    }


def set_entity_roster(
    entities: Any,
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
) -> dict[str, Any]:
    """PUT /api/admin/entity-roster.

    Replaces the roster wholesale — the panel edits a list, so a whole-list
    write is what an admin's Save means, and there is no per-entry add/remove
    route to keep consistent with it. Takes effect on the next review with no
    redeploy (`pipeline_runner` resolves the roster per review, not at
    import).

    An empty list is a legitimate save (it clears the roster) and degrades to
    the playbook's own `perspective.party`, per `resolve_entity_roster`.

    Raises HTTPException(403) for a non-admin caller or 400 for an invalid body
    (`normalize_entities`).
    """
    _require_admin(caller_user_row, "Admin privilege required to change the entity roster.")

    normalized = normalize_entities(entities)
    before = _stored_entities(dynamodb_resource)
    actor = caller_user_row.get("cognito_sub", "")
    now = str(int(time.time()))

    added = [e for e in normalized if e not in before]
    removed = [e for e in before if e not in normalized]
    history_entry = {
        "timestamp": now,
        "actor": actor,
        "count": len(normalized),
        "added": added,
        "removed": removed,
    }

    current_row = _stored_row(dynamodb_resource)
    existing_history = current_row.get("history") or []
    if not isinstance(existing_history, list):
        existing_history = []
    updated_history = [history_entry] + existing_history
    updated_history = updated_history[:MAX_HISTORY_ENTRIES]

    table_name = _entity_roster_table_name()
    table = _entity_roster_table(dynamodb_resource)
    try:
        table.update_item(
            Key={"setting_id": ENTITY_ROSTER_SETTING_ID},
            UpdateExpression=(
                "SET entities = :e, entities_updated_at = :t, entities_updated_by = :a, history = :h"
            ),
            ExpressionAttributeValues={
                ":e": normalized,
                ":t": now,
                ":a": actor,
                ":h": updated_history,
            },
        )
    except Exception as exc:
        err_code = ""
        if hasattr(exc, "response") and isinstance(exc.response, dict):
            err_code = exc.response.get("Error", {}).get("Code", "")
        if err_code == "ResourceNotFoundException":
            _ensure_table(dynamodb_resource, table_name)
            table = _entity_roster_table(dynamodb_resource)
            table.update_item(
                Key={"setting_id": ENTITY_ROSTER_SETTING_ID},
                UpdateExpression=(
                    "SET entities = :e, entities_updated_at = :t, entities_updated_by = :a, history = :h"
                ),
                ExpressionAttributeValues={
                    ":e": normalized,
                    ":t": now,
                    ":a": actor,
                    ":h": updated_history,
                },
            )
        else:
            raise

    _write_audit_entry(
        dynamodb_resource,
        actor=actor,
        action="entity_roster_change",
        target=ENTITY_ROSTER_SETTING_ID,
        target_type="entity_roster",
        detail={
            "before_entity_count": len(before),
            "after_entity_count": len(normalized),
        },
    )

    logger.info(
        "ENTITY_ROSTER_CHANGE: actor=%s before=%d after=%d",
        actor,
        len(before),
        len(normalized),
    )

    return get_entity_roster(caller_user_row, dynamodb_resource)
