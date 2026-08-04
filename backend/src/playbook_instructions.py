"""
Per-playbook Standing instructions store — issue #482 (epic #481, sub-issue
A: backend store + API + review lineage).

## Why this exists

Epic #481 replaces the old "Pen rules & posture" tab (a validator for a
document that was never stored and never consumed) with a per-playbook,
plain-English "Standing instructions" text box that IS actually stored,
actually versioned, and actually governs reviews:

  > What the toaster follows for a given review = the active playbook
  > version + the current standing instructions for that playbook (+
  > anything typed for that one review).

This module owns the storage half of that promise: append-only, monotonic
versions of a playbook's standing-instructions text, with author +
timestamp on every version, and compare-and-set saves so a stale admin page
gets a clean conflict instead of silently clobbering a newer save. Reading
which version actually GOVERNED a given review is `src/reviews.py`'s job
(`_resolve_instructions_lineage`, extending #471's playbook-version
lineage) — this module never touches the `reviews` table.

Prompt composition (deciding HOW the resolved text is folded into a
review's prompt, precedence over the playbook's own positions, and
attribution) is issue #483's job, and the admin UI is #484's — both
explicitly out of scope here.

## Storage shape

Reuses the `playbook_versions`/`playbook-versions` naming convention
(PK: `playbook_id`, SK: `version` — but here `version` is a plain
monotonically-increasing NUMBER starting at 1, not an admin-supplied
string): one row per saved version, e.g.::

    {
        "playbook_id": "eiaa",
        "version": 2,
        "text": "Always flag auto-renewal clauses longer than 12 months.",
        "saved_by": "local:admin",       # same shape as playbook_versions'
                                          # `uploaded_by` -- see
                                          # src/demo_auth.py's
                                          # `local:<username>` convention.
        "saved_at": 1785000000,          # epoch seconds
        "text_hash": "sha256:...",       # sha256 of `text`, computed once
                                          # at save time so every reader
                                          # (including reviews.py's lineage
                                          # resolver) reads the SAME value
                                          # rather than recomputing it.
        "supersedes": 1,                 # the version this replaces, or
                                          # None for the first save (v1).
    }

Versions are append-only and immutable once written -- there is no update
or delete path in this module, only new versions. `text` may be the empty
string (an admin explicitly clearing standing instructions is itself a new,
recorded version -- not a delete).

## Monotonicity without a read-modify-write race

Two admins saving concurrently must never both land as the same version
number. Rather than a separate atomic counter item, this module reads the
current max version, then attempts to `put_item` the NEXT version number
with `ConditionExpression="attribute_not_exists(playbook_id) AND
attribute_not_exists(version)"` (the same append-only idiom
`playbook_versions.record_playbook_version_upload` already uses). Exactly
one of two concurrent callers computing the same next-version number can
ever win that conditional write; the loser gets
`ConditionalCheckFailedException`, which this module converts into
`PlaybookInstructionsConflictError` carrying the now-current version --
never a silently renumbered save, and never two rows claiming the same
version.

## Compare-and-set (`expected_current_version`)

A caller (the admin page) may additionally pass the version it believes is
current. If that no longer matches what this module reads as current
BEFORE attempting the write, the save is refused with the same
`PlaybookInstructionsConflictError` up front -- "Someone saved v4 while you
were editing" -- rather than proceeding to write a version that would
silently supersede work the caller never saw. `expected_current_version =
None` means the caller has no compare-and-set belief (e.g. saving for the
first time) and skips this check; the append-only conditional write above
still protects monotonicity either way.

## Size cap and injection posture

Text is capped at `MAX_INSTRUCTIONS_TEXT_CHARS` (10,000 -- "a page of
prose, not a document"); `save_instructions` raises
`PlaybookInstructionsTooLargeError` over that, mapped to HTTP 400 by the
route. The text is trusted first-party admin input, the SAME trust class as
`toaster_guidance` (`backend/src/review_routes.py`'s per-review free-text
box): this codebase has no dedicated prompt-injection screen for
free-text guidance today (verified: neither `scripts/leakage_scan.py` nor
any other module in this tree scans `toaster_guidance` at write time), so
there is none to route this text through either -- it is stored and later
interpolated into a prompt as DATA, never executed, exactly like
`toaster_guidance` already is. If a guidance-injection screen is ever added
upstream, this text should run through it too (see issue #483's prompt
composition, which is where the text is actually assembled into a
review's prompt).

Per this issue's Notes, the full text is never logged at INFO -- only
identifiers, version, and length (`backend/src/main.py`'s save route logs
`playbook_id`/`version`/`len(text)`, never `text` itself), and the audit
row this module appends on every save carries the same identifiers-only
shape every other admin-write audit row in this codebase does (see
`playbook_versions._write_audit_entry`) -- never document substance.

Environment variables:
  PLAYBOOK_INSTRUCTIONS_TABLE  — this module's table (PK: playbook_id,
                                 SK: version [Number])
  AUDIT_TABLE                   — append-only audit table (same table/shape
                                 as src/users.py / src/retention.py /
                                 src/playbook_versions.py)
"""

import os
import time
import uuid
from hashlib import sha256
from typing import Any

from boto3.dynamodb.conditions import Key

# "A page of prose, not a document" (issue #482).
MAX_INSTRUCTIONS_TEXT_CHARS = 10_000

# History is capped, newest-first (issue #482's GET route: "history:
# [...] (history newest-first, capped ~50)").
INSTRUCTIONS_HISTORY_MAX = 50


class PlaybookInstructionsConflictError(Exception):
    """Raised when a save cannot land as the version the caller intended --
    either `expected_current_version` no longer matches what is actually
    current (a stale admin page), or a concurrent save won the append-only
    conditional write first (see module docstring, "Monotonicity without a
    read-modify-write race"). `current_version` is the freshly-read actual
    current version (0 if none has ever been saved), for the caller/route
    to surface in the 409 body."""

    def __init__(self, current_version: int, message: str | None = None):
        self.current_version = current_version
        super().__init__(
            message
            or (
                "Standing instructions were saved by someone else in the "
                f"meantime; the current version is {current_version}."
            )
        )


class PlaybookInstructionsTooLargeError(Exception):
    """Raised when `save_instructions` is given text over
    `MAX_INSTRUCTIONS_TEXT_CHARS`."""


def _instructions_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["PLAYBOOK_INSTRUCTIONS_TABLE"])


def _audit_table(dynamodb_resource: Any):
    return dynamodb_resource.Table(os.environ["AUDIT_TABLE"])


def now_epoch() -> float:
    return time.time()


def hash_instructions_text(text: str) -> str:
    """`"sha256:" + hexdigest`, the same content-hash shape
    `backend/src/main.py`'s playbook-version upload route computes over
    uploaded bytes -- kept as a public function so `src/reviews.py`'s
    lineage resolver (and tests) can verify a resolved `text_hash` against
    the text that produced it without reaching into this module's
    internals."""
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _write_audit_entry(
    dynamodb_resource: Any,
    actor: str,
    detail: dict[str, Any],
    now_epoch_value: float | None = None,
) -> None:
    """Append an immutable audit row for a standing-instructions save.
    Identifiers, version, and length only -- never the instructions text
    itself. Same shape as `playbook_versions._write_audit_entry`."""
    table = _audit_table(dynamodb_resource)
    now = now_epoch_value if now_epoch_value is not None else now_epoch()
    event_id = uuid.uuid4().hex
    partition = time.strftime("%Y-%m", time.gmtime(now))
    timestamp = f"{int(now)}#{event_id}"

    item: dict[str, Any] = {
        "partition": partition,
        "timestamp": timestamp,
        "event_id": event_id,
        "actor": actor,
        "action": "playbook_instructions_save",
        "target_type": "playbook_instructions",
        "outcome": "success",
    }
    item.update(detail)
    table.put_item(Item=item)


def get_current_instructions(
    playbook_id: str,
    dynamodb_resource: Any,
) -> dict[str, Any] | None:
    """The highest-`version` row for `playbook_id`, or None if nothing has
    ever been saved. Never fabricates a row."""
    table = _instructions_table(dynamodb_resource)
    resp = table.query(
        KeyConditionExpression=Key("playbook_id").eq(playbook_id),
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    return dict(items[0]) if items else None


def list_instructions_history(
    playbook_id: str,
    dynamodb_resource: Any,
    limit: int = INSTRUCTIONS_HISTORY_MAX,
) -> list[dict[str, Any]]:
    """Every saved version for `playbook_id`, newest-first, capped at
    `INSTRUCTIONS_HISTORY_MAX` (issue #482's GET route: "history:
    [...] (history newest-first, capped ~50)"). Empty list if nothing has
    ever been saved."""
    table = _instructions_table(dynamodb_resource)
    resp = table.query(
        KeyConditionExpression=Key("playbook_id").eq(playbook_id),
        ScanIndexForward=False,
        Limit=min(limit, INSTRUCTIONS_HISTORY_MAX),
    )
    return [dict(item) for item in resp.get("Items", [])]


def save_instructions(
    playbook_id: str,
    text: str,
    saved_by: str,
    dynamodb_resource: Any,
    expected_current_version: int | None = None,
    now_epoch_value: float | None = None,
) -> dict[str, Any]:
    """Append a new standing-instructions version for `playbook_id` --
    version N+1, where N is the current max version (0 if none saved yet).

    Raises:
      `PlaybookInstructionsTooLargeError` -- `text` exceeds
        `MAX_INSTRUCTIONS_TEXT_CHARS`. Checked before any read/write.
      `PlaybookInstructionsConflictError` -- either
        `expected_current_version` (when supplied) does not match the
        current version at read time, or a concurrent save won the
        append-only conditional write first (see module docstring). In
        both cases the error's `current_version` is the freshly-resolved
        actual current version, for the caller to surface (or retry
        against).

    Returns the written item (`playbook_id`, `version`, `text`,
    `saved_by`, `saved_at`, `text_hash`, `supersedes`).
    """
    if len(text) > MAX_INSTRUCTIONS_TEXT_CHARS:
        raise PlaybookInstructionsTooLargeError(
            f"Standing instructions text is {len(text)} characters, over the "
            f"{MAX_INSTRUCTIONS_TEXT_CHARS}-character cap."
        )

    current = get_current_instructions(playbook_id, dynamodb_resource)
    current_version = int(current["version"]) if current else 0

    if (
        expected_current_version is not None
        and int(expected_current_version) != current_version
    ):
        raise PlaybookInstructionsConflictError(current_version=current_version)

    now = now_epoch_value if now_epoch_value is not None else now_epoch()
    next_version = current_version + 1

    item: dict[str, Any] = {
        "playbook_id": playbook_id,
        "version": next_version,
        "text": text,
        "saved_by": saved_by,
        "saved_at": int(now),
        "text_hash": hash_instructions_text(text),
        "supersedes": current_version if current_version else None,
    }

    table = _instructions_table(dynamodb_resource)
    try:
        table.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(playbook_id) AND attribute_not_exists(version)"
            ),
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException as exc:
        # A concurrent save claimed `next_version` first (see module
        # docstring, "Monotonicity without a read-modify-write race"). Never
        # retried/renumbered here -- the caller gets a clean conflict
        # against the now-actual current version, same as the
        # expected_current_version mismatch above.
        latest = get_current_instructions(playbook_id, dynamodb_resource)
        raise PlaybookInstructionsConflictError(
            current_version=int(latest["version"]) if latest else current_version
        ) from exc

    _write_audit_entry(
        dynamodb_resource=dynamodb_resource,
        actor=saved_by,
        detail={
            "target": f"{playbook_id}#{next_version}",
            "playbook_id": playbook_id,
            "version": next_version,
            "text_length": len(text),
        },
        now_epoch_value=now,
    )

    return item
