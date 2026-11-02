"""
The one audit-table writer in `backend/src`.

WHY THIS MODULE EXISTS:
    Nine modules (`model_settings`, `playbook_instructions`, `demo_auth`,
    `retention`, `users`, `sample_playbooks`, `runner_recovery`,
    `entity_roster`, `playbook_versions`) and `review_routes` each carried
    their own copy of "build an audit row and `put_item` it". Every copy
    wrote an UNCONDITIONAL `put_item`, so the "append-only" claim rested
    only on the AWS table resource policy denying `UpdateItem`/`DeleteItem`
    -- a `PutItem` on an existing key silently replaces the row, and the
    DTS target (DynamoDB-Local) has no resource policy at all.

    Every audit row now goes through `write_audit_entry` below, which makes
    the write itself append-only: `PutItem` with
    `attribute_not_exists(partition) AND attribute_not_exists(timestamp)`.
    A write that would land on an existing key raises
    `ConditionalCheckFailedException` instead of overwriting history.
    `tests/test_audit_log_single_writer.py` AST-parses `backend/src` and
    fails if any other module grows its own audit `put_item` back.

KEY SHAPE (unchanged -- docs/audit-queries.md):
    PK `partition` = "%Y-%m" (UTC) of the event time.
    SK `timestamp` = "{epoch_seconds}#{event_id}", where `event_id` is a
    random uuid4 hex. The random suffix is what makes the sort key
    collision-safe: two events in the same second get different keys, and
    the condition above turns the astronomically unlikely collision into a
    loud failure rather than a silent overwrite. Every existing query
    (month partition + SK range, `actor-index`, `review_id-index`) reads
    this shape unchanged.

THE LAMBDA COPY:
    `infra/lambda/purge_worker/handler.py` ships independently of
    `backend/src` and cannot import this module. It carries an equivalent
    `_write_audit_entry` (same key shape, same condition) and
    `tests/test_audit_log_single_writer.py` holds the two in parity.

POSTURE:
    Identifiers, setting values, counts and hashes only -- never document
    substance, never a filename, never a secret (ARCHITECTURE.md ->
    "Audit posture"). This module cannot enforce that for the `detail` a
    caller hands it; each caller's own tests do.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def append_only_condition() -> Any:
    """The PutItem condition every audit write carries. A function rather
    than a module constant so the Lambda parity test can render it.

    `boto3.dynamodb.conditions` is imported here, not at module scope: many
    modules import this one transitively, and several offline test suites
    stand in a bare `boto3` module that has no `dynamodb` subpackage."""
    from boto3.dynamodb.conditions import Attr  # type: ignore[import-untyped]

    return Attr("partition").not_exists() & Attr("timestamp").not_exists()


def build_audit_item(
    *,
    actor: str,
    action: str,
    target: str | None = None,
    target_type: str | None = None,
    detail: dict[str, Any] | None = None,
    now: float | None = None,
    event_id: str | None = None,
    review_id: str | None = None,
    outcome: str = "success",
) -> dict[str, Any]:
    """The audit row, without writing it.

    `target` / `target_type` are omitted from the row when None (the
    standing-instructions save has never carried a `target`).

    `review_id` is stamped for the audit table's `review_id-index` (issue
    #253): explicitly when passed, otherwise from `target` when
    `target_type == "review"` -- the stamp `retention`, `review_routes` and
    `runner_recovery` each applied by hand before.
    """
    when = time.time() if now is None else now
    eid = event_id or uuid.uuid4().hex
    keys = {
        "partition": time.strftime("%Y-%m", time.gmtime(when)),
        "timestamp": f"{int(when)}#{eid}",
        "event_id": eid,
    }
    item: dict[str, Any] = {**keys, "actor": actor, "action": action}
    if target is not None:
        item["target"] = target
    if target_type is not None:
        item["target_type"] = target_type
    item["outcome"] = outcome
    if review_id:
        item["review_id"] = review_id
    elif target_type == "review" and target:
        item["review_id"] = target
    if detail:
        item.update(detail)
    # Re-assert the writer-owned fields last: nothing in `detail` moves the key.
    item.update(keys)
    return item


def write_audit_entry(
    dynamodb_resource: Any,
    *,
    actor: str,
    action: str,
    target: str | None = None,
    target_type: str | None = None,
    detail: dict[str, Any] | None = None,
    now: float | None = None,
    event_id: str | None = None,
    review_id: str | None = None,
    outcome: str = "success",
    best_effort: bool = False,
) -> dict[str, Any] | None:
    """Append one immutable audit row and return it.

    `best_effort=False` (the default, and every admin mutation's choice):
    a missing `AUDIT_TABLE` raises KeyError and a failed write raises, so
    the caller's action fails rather than going unaudited.

    `best_effort=True` (request-path and boot-path rows -- upload
    rejections, downloads, runner recovery): a missing `AUDIT_TABLE` is a
    no-op and a failed write is logged and swallowed, so an audit outage
    never turns a user's request (or a boot) into a 500. The log line names
    the action and target type only.
    """
    item = build_audit_item(
        actor=actor,
        action=action,
        target=target,
        target_type=target_type,
        detail=detail,
        now=now,
        event_id=event_id,
        review_id=review_id,
        outcome=outcome,
    )
    if best_effort:
        table_name = os.environ.get("AUDIT_TABLE")
        if not table_name:
            return None
        try:
            dynamodb_resource.Table(table_name).put_item(
                Item=item, ConditionExpression=append_only_condition()
            )
        except Exception:  # noqa: BLE001 -- best-effort by the caller's choice
            logger.warning(
                "AUDIT: failed to write audit row action=%s target_type=%s",
                action,
                target_type,
            )
            return None
        return item

    table = dynamodb_resource.Table(os.environ["AUDIT_TABLE"])
    table.put_item(Item=item, ConditionExpression=append_only_condition())
    return item
