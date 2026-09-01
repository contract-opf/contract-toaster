"""
Audit query READ-API — issue #253 (the backend the #93 audit explorer binds to).

`GET /api/audit` did not exist. `docs/audit-queries.md` is the standard
catalogue of audit/review queries operators run for investigations,
rollback/quarantine, and compliance, and until this module every one of them
was a DynamoDB console session. This is that catalogue, as named,
parameterised, admin-gated queries — and nothing else.

  GET /api/audit                          -> `run_audit_query` (catalogue index)
  GET /api/audit?query=<name>&...         -> one catalogue entry

READ-ONLY, AND STRUCTURALLY SO. The `audit` table is append-only (every app
role is DENIED UpdateItem/DeleteItem on it — infra/lib/nested/data-stack.ts
`_addAuditImmutabilityPolicy`), so there is no mutation this module could
perform even if it tried. The `reviews`-table entries here (rollback/
quarantine population, REQUEST_CHANGE-under-a-version) return the POPULATION
an operator would act on; applying the `QUARANTINED`/`SUPERSEDED` overlay is
a mutation with its own audit row and belongs to its own ticket, exactly as
`admin_dashboard` surfaces the reconcile view without performing it.

NO FULL-TABLE SCANS. THIS IS THE LOAD-BEARING INVARIANT.
Every read below is a `query` (partition key, or a GSI partition key) or a
`get_item`/`batch_get_item` by primary key. This module never calls `.scan()`
— not once, on any table — because the two tables it reads are precisely the
two that grow without bound: `audit` is append-only forever, and `reviews`
holds one row per review ever submitted. A scan-backed "audit explorer" is a
tool that works in a demo and times out in the investigation it was built
for. Where a catalogue entry CANNOT be answered by key/index access on the
tables as they exist today, this module refuses with 400 and says which
index is missing (see `ROLLBACK_INDEXED_COMPONENTS`) rather than quietly
scanning. tests/test_audit_query_api_93.py asserts the invariant with a
DynamoDB spy, and — because a green assertion that was never seen red proves
nothing — that test also runs the same assertion against a deliberately
scan-based stand-in and requires it to FAIL.

ADMIN-ONLY, AND 403 RATHER THAN A FILTERED 200. Same reasoning as
`admin_dashboard._require_admin`: every entry here is a deployment-wide
investigative view (everything an actor did, everything that ran under a bad
bundle), so there is no "your own row" subset a non-admin could legitimately
be served, and a filtered 200 would be an empty 200 masquerading as a
capability. Admin privilege is read from the caller's DynamoDB `users` row,
never from a JWT claim (ARCHITECTURE.md -> "Group-naming misnomer").

PROJECTED, NEVER SPREAD. Audit rows are non-substantive by construction
(ARCHITECTURE.md -> "Audit posture"), but "by construction" is a claim about
the writers, not a guarantee about the reader: `review_routes._write_audit_row`
takes a free-form `detail` dict, and one of its call sites already puts an S3
key in it. So every row this module returns is built field by field from a
module-level allowlist tuple. Adding a field to one of those tuples is a
deliberate disclosure decision.

THE RETENTION BOUNDARY IS PART OF THE ANSWER (issue #34). Every response
carries a `retention_boundary` block saying whether the entry is answerable
FOREVER or only inside a review's retention window, because an investigator
who does not know which they are holding runs into a dead end and concludes
the wrong thing. See docs/audit-queries.md -> "Substance retention boundary".

WHERE THE CATALOGUE OUTRUNS THE CURRENT PIPELINE. Three entries are honest
about having no live producer rather than being propped up with fixtures:

  * `review_clause_ids` — retrieval was retired by owner decision 2026-08-11
    (docs/rag-dormant.md); `scripts/review_spine.py` passes
    `retrieved_precedent=[]`, so no review run under the current pipeline
    records clause ids. Neither did any earlier one: no code in this repo's
    history has ever written a `review_complete` audit row or a
    `retrieved_clause_ids` attribute (#27 landed as a docs-only CI gate), so
    the query has no population under any pipeline, past or present. It
    stays as the standing filter should retrieval be revived, and reports
    `dormant: true` with `producers_wired: false`.
  * `document_access` — the "and who was denied" half. `review_routes`
    audits only a SUCCESSFUL download-URL issuance ("Audit only a
    SUCCESSFUL download-URL issuance"); no writer emits an `access_denied`
    row today, so that half returns nothing under the current pipeline.
  * `rollback_population` for `prompt_hash` / `standard_form_hash` /
    `model_policy_hash` / `corpus_snapshot_version` — these are RELEASE
    BUNDLE fields (scripts/bind_bundle.py), and no writer has ever put them
    on a `reviews` row; only `playbook_hash` is recorded there
    (`reviews._create_review_row`) and only it has a GSI. Refused with 400,
    naming the missing index, rather than scanned.

Environment variables consumed:
  AUDIT_TABLE              append-only audit table (PK: partition, SK:
                           "{epoch}#{event_id}"; GSIs: actor-index,
                           review_id-index)
  REVIEWS_TABLE            reviews table (PK: review_id; GSI:
                           playbook_hash-index)
  PLAYBOOK_VERSIONS_TABLE  playbook_versions table (PK: playbook_id, SK:
                           version), read to turn an admin-facing version
                           string into the content hash the reviews GSI is
                           keyed by. OPTIONAL — a deployment without it
                           cannot answer `playbook_request_changes`, and
                           says so rather than failing the request.
"""

from __future__ import annotations

import os
import time
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from fastapi import HTTPException, status

try:  # production runs `src.main`; tests put backend/src on sys.path
    from src.admin_dashboard import RELEASE_ACTIVITY_ACTIONS
    from src.users import json_safe
except ImportError:  # pragma: no cover
    from admin_dashboard import RELEASE_ACTIVITY_ACTIONS  # type: ignore[no-redef]
    from users import json_safe  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

AUDIT_QUERY_DEFAULT_LIMIT = 50
AUDIT_QUERY_MAX_LIMIT = 200

# How many `YYYY-MM` partitions an action-filter entry walks backwards before
# it stops. The three action-filter entries (release activity, break-glass,
# model recertification) have no index on `action` — the honest options are a
# bounded walk of the month partitions (a Query per month, key access) or a
# Scan. This is that bound, and the response reports it as
# `months_searched` so an operator reading an empty result knows they are
# looking at "nothing in the last year", not "nothing, ever".
# Matches admin_dashboard.RELEASE_ACTIVITY_MONTHS_BACK, which walks the same
# partitions for the same reason.
AUDIT_QUERY_MONTHS_BACK = 12


def _clamp(value: Any, default: int, lowest: int, highest: int) -> int:
    """Coerce a query-string integer into [lowest, highest], defaulting a
    missing/unparseable value. Every caller-settable bound in this module
    goes through this, so no request parameter can widen a read past its
    documented ceiling."""
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = default
    return max(lowest, min(requested, highest))


# ---------------------------------------------------------------------------
# Admin gate
# ---------------------------------------------------------------------------

def _require_admin(caller_user_row: dict[str, Any]) -> None:
    """Raise HTTPException(403) unless the caller's `users` row is an admin.

    The detail never echoes anything caller-supplied — not even the query
    name, which is attacker-controlled text on this route.
    """
    if not bool(caller_user_row.get("is_admin", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privilege required to run audit queries.",
        )


# ---------------------------------------------------------------------------
# Row projections (see PROJECTED, NEVER SPREAD in the module docstring)
# ---------------------------------------------------------------------------

# The fields EVERY audit writer in this codebase sets (users.py,
# retention.py, review_routes.py, playbook_versions.py, model_settings.py,
# demo_auth.py, playbook_instructions.py, sample_playbooks.py). `reason` is
# included because the break-glass entry is keyed on it.
_AUDIT_BASE_FIELDS: tuple[str, ...] = (
    "event_id",
    "action",
    "actor",
    "target",
    "target_type",
    "outcome",
    "reason",
    "review_id",
)

# Release activations/rollbacks carry the bundle identity on top of the base
# set. Deliberately the same disclosure decision already made in
# `admin_dashboard._RELEASE_ACTIVITY_FIELDS` for `GET /api/admin/releases`;
# kept in step with it by hand, since that tuple is module-private there.
_RELEASE_EXTRA_FIELDS: tuple[str, ...] = (
    "playbook_id",
    "version",
    "before_status",
    "after_status",
    "prior_active_version",
    "content_hash",
)

# The retrieved-clause record (#27). Opaque identifiers with polarity and
# channel — never clause text (docs/data-handling.md's canonical field
# dictionary classifies exactly this field as non-substantive).
_CLAUSE_EXTRA_FIELDS: tuple[str, ...] = ("retrieved_clause_ids",)

# A `reviews` row, as an investigation needs it: which review, when, what it
# decided, and which governing inputs it ran under. NO document substance
# (`summary`, `findings`, `toaster_guidance`), no S3 key, no execution ARN —
# the same boundary `admin_dashboard._MANUAL_REVIEW_FIELDS` draws.
_REVIEW_ROW_FIELDS: tuple[str, ...] = (
    "review_id",
    "created_at",
    "status",
    "admin_overlay",
    "decision",
    "playbook_id",
    "playbook_hash",
    "playbook_version",
    "attorney_disposition",
)


def _audit_view(item: dict[str, Any], extra_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    """Project one audit row onto the allowlist, newest-first callers only.

    `recorded_at` is recovered from the `"{epoch}#{event_id}"` sort key,
    which is where the timestamp actually lives — the audit row carries no
    separate epoch attribute (same recovery `admin_dashboard.
    _release_activity_view` performs).
    """
    view: dict[str, Any] = {}
    for field in _AUDIT_BASE_FIELDS + extra_fields:
        if field in item:
            view[field] = item[field]
    sort_key = str(item.get("timestamp") or "")
    view["recorded_at"] = _epoch_from_sort_key(sort_key)
    view["partition"] = item.get("partition")
    return json_safe(view)


def _review_view(item: dict[str, Any]) -> dict[str, Any]:
    """Project one `reviews` row onto `_REVIEW_ROW_FIELDS`."""
    return json_safe({f: item[f] for f in _REVIEW_ROW_FIELDS if f in item})


def _epoch_from_sort_key(sort_key: str) -> int | None:
    if not sort_key:
        return None
    head = sort_key.split("#", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Sort-key range helpers
#
# The audit sort key is `f"{int(now)}#{event_id}"` — an UNPADDED decimal
# epoch. DynamoDB compares strings by UTF-8 bytes, so a lexicographic range
# over these is a chronological range exactly while every value has the same
# digit count. Epoch seconds are 10 digits from 2001-09-09 to 2286-11-20, so
# that holds for every row this deployment can write; the bounds below are
# built the same unpadded way the writers build the keys, so bound and stored
# value agree by construction rather than by luck.
# ---------------------------------------------------------------------------

# Sorts after every hexadecimal event_id, so an upper bound built with it
# includes every event recorded during the bounding second.
_SORT_KEY_CEILING = "#\uffff"


def _timestamp_condition(since: Any, until: Any) -> Any | None:
    """A sort-key `KeyConditionExpression` fragment for [since, until], or
    None when the caller bounded neither end."""
    lower = _as_epoch(since)
    upper = _as_epoch(until)
    if lower is not None and upper is not None:
        return Key("timestamp").between(f"{lower}#", f"{upper}{_SORT_KEY_CEILING}")
    if lower is not None:
        return Key("timestamp").gte(f"{lower}#")
    if upper is not None:
        return Key("timestamp").lte(f"{upper}{_SORT_KEY_CEILING}")
    return None


def _as_epoch(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`since` and `until` must be epoch seconds.",
        ) from None


# ---------------------------------------------------------------------------
# Table handles
# ---------------------------------------------------------------------------

def _audit_table(dynamodb_resource: Any) -> Any:
    return dynamodb_resource.Table(os.environ["AUDIT_TABLE"])


def _reviews_table(dynamodb_resource: Any) -> Any:
    return dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])


# ---------------------------------------------------------------------------
# Bounded, paginating Query helpers. NEVER a Scan (module docstring).
# ---------------------------------------------------------------------------

def _query_pages(table: Any, limit: int, **kwargs: Any) -> list[dict[str, Any]]:
    """Run `table.query(**kwargs)`, following `LastEvaluatedKey` until
    `limit` rows are collected or the partition is exhausted.

    Pagination is required, not optional: a `FilterExpression` is applied
    AFTER DynamoDB reads a page, so a partition whose first page holds no
    matching row still returns `Items: []` with a `LastEvaluatedKey`. A
    single-shot query would report "no break-glass events" on a busy month
    simply because the first 1MB of it was ordinary traffic.
    """
    collected: list[dict[str, Any]] = []
    resp = table.query(**kwargs)
    while True:
        collected.extend(resp.get("Items", []))
        if len(collected) >= limit or "LastEvaluatedKey" not in resp:
            break
        resp = table.query(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
    return collected[:limit]


def _month_partitions(now: float, months_back: int) -> list[str]:
    """`YYYY-MM` audit partitions, newest first — the partition-key format
    every audit writer uses (`time.strftime("%Y-%m", time.gmtime(now))`)."""
    year, month = (int(p) for p in time.strftime("%Y-%m", time.gmtime(now)).split("-"))
    partitions: list[str] = []
    for _ in range(max(1, months_back)):
        partitions.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return partitions


def _walk_month_partitions(
    dynamodb_resource: Any,
    condition: Any,
    limit: int,
    now: float,
) -> list[dict[str, Any]]:
    """Rows matching `condition`, newest first, across the trailing
    `AUDIT_QUERY_MONTHS_BACK` month partitions.

    One Query per month partition — key access, never a Scan. Within a
    partition the sort key is `"{epoch}#{event_id}"`, so
    `ScanIndexForward=False` is a true newest-first ordering; across
    partitions, every row in a newer month is newer than every row in an
    older one, so concatenating the partitions in order preserves it.
    """
    table = _audit_table(dynamodb_resource)
    collected: list[dict[str, Any]] = []
    for partition in _month_partitions(now, AUDIT_QUERY_MONTHS_BACK):
        collected.extend(
            _query_pages(
                table,
                limit - len(collected),
                KeyConditionExpression=Key("partition").eq(partition),
                FilterExpression=condition,
                ScanIndexForward=False,
            )
        )
        if len(collected) >= limit:
            break
    return collected[:limit]


def _query_audit_by_review(
    dynamodb_resource: Any,
    review_id: str,
    limit: int,
    condition: Any | None = None,
) -> list[dict[str, Any]]:
    """Every audit row for one review, newest first, via the audit table's
    `review_id-index` GSI — the index docs/audit-queries.md names for
    "full history of one review/document".

    The rows this reads are the ones `review_routes._write_audit_row` and
    `retention._write_audit_entry` stamp with `review_id` for
    `target_type="review"`. Audit rows written BEFORE that stamp existed
    carry the review id in `target` only and are therefore not on this
    index; they remain readable through `month_activity` and through the
    object-locked `audit-archive` mirror.
    """
    kwargs: dict[str, Any] = {
        "IndexName": "review_id-index",
        "KeyConditionExpression": Key("review_id").eq(review_id),
        "ScanIndexForward": False,
    }
    if condition is not None:
        kwargs["FilterExpression"] = condition
    return _query_pages(_audit_table(dynamodb_resource), limit, **kwargs)


def _batch_get_reviews(
    dynamodb_resource: Any,
    review_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """`reviews` rows for `review_ids`, keyed by review_id — BatchGetItem by
    primary key, in DynamoDB's 100-key chunks.

    One batched round trip per 100 ids rather than one GetItem per id: the
    callers here already hold up to `AUDIT_QUERY_MAX_LIMIT` keys from a GSI
    query, and a per-row read would turn one request into 200.
    """
    table_name = os.environ["REVIEWS_TABLE"]
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(review_ids), 100):
        keys = [{"review_id": rid} for rid in review_ids[start : start + 100]]
        request = {table_name: {"Keys": keys}}
        while request:
            resp = dynamodb_resource.batch_get_item(RequestItems=request)
            for item in resp.get("Responses", {}).get(table_name, []):
                found[str(item.get("review_id"))] = item
            # UnprocessedKeys is DynamoDB's backpressure signal, not an
            # error: dropping it would silently under-report the quarantine
            # population, which is the one result set that must be complete.
            request = resp.get("UnprocessedKeys") or {}
    return found


# ---------------------------------------------------------------------------
# Retention-boundary annotations (issue #34)
# ---------------------------------------------------------------------------

_BOUNDARY_ALWAYS = {
    "answerable": "always",
    "note": (
        "Reads only non-substantive facts (actor, action, target, decision, "
        "hashes, identifiers), which are never purged."
    ),
}

_BOUNDARY_CLAUSE_IDS = {
    "answerable": "always",
    "note": (
        "The clause IDENTIFIERS survive purge. Resolving an identifier back to "
        "clause TEXT requires the corpus snapshot still holding that clause, "
        "which is a separate retention concern from this review's own window."
    ),
}

_BOUNDARY_SEE = "docs/audit-queries.md -> Substance retention boundary"


def _boundary(block: dict[str, str], extra: str | None = None) -> dict[str, str]:
    boundary = dict(block)
    boundary["see"] = _BOUNDARY_SEE
    if extra:
        boundary["note"] = f"{boundary['note']} {extra}"
    return boundary


# ---------------------------------------------------------------------------
# Action / field vocabularies
#
# Each maps a docs/audit-queries.md row onto the action values THIS codebase's
# writers actually emit. Where the catalogue names a category rather than a
# stored value, the mapping is spelled out — a filter built from the
# catalogue's prose would match nothing.
# ---------------------------------------------------------------------------

# "Who viewed/downloaded a document (and who was denied)". The catalogue's
# {view, download, presign} are all one act in this system — issuing a
# presigned URL — and `review_routes` writes it under two action names, one
# per direction. `access_denied` has NO writer today: the download routes
# audit only a successful issuance, so the "who was denied" half of this
# entry returns nothing under the current pipeline. It is listed anyway so
# that the query does not have to change when denial auditing lands.
DOCUMENT_ACCESS_ACTIONS: tuple[str, ...] = (
    "review_output_downloaded",
    "review_input_downloaded",
    "access_denied",
)

# "Which clauses informed review X" (#27).
REVIEW_COMPLETE_ACTION = "review_complete"

# "Break-glass / governance-bypass uses" — matched on EITHER the action or
# the reason, because the catalogue defines it as either.
BREAK_GLASS_ACTIONS: tuple[str, ...] = ("governance_bypass",)
BREAK_GLASS_REASON = "emergency-override"

# "Model recertification record" — recorded even when "no change; pin
# reaffirmed".
MODEL_RECERTIFICATION_ACTIONS: tuple[str, ...] = ("model_recertification",)

# "Rollback/quarantine population". The catalogue lists five component
# fields; only `playbook_hash` is written to a `reviews` row
# (`reviews._create_review_row`) and only it has a GSI
# (`playbook_hash-index`). `playbook_content_hash` is the same value under
# the name `_resolve_playbook_version_lineage` records it as, so it resolves
# to the same index rather than being a second, unindexed field.
ROLLBACK_INDEXED_COMPONENTS: dict[str, str] = {
    "playbook_hash": "playbook_hash-index",
    "playbook_content_hash": "playbook_hash-index",
}

# The remaining catalogue components. These live on the RELEASE BUNDLE
# (scripts/bind_bundle.py), not on a `reviews` row: no writer has ever set
# them there, and there is no GSI to query them by. Answering them would
# require a full-table Scan of `reviews`, which this module does not do — so
# they are refused with 400 naming the missing index. `standard_form_hash` is
# additionally historical-only by decision: #631 removes the standard-form
# diff subsystem while deliberately preserving the bundle schema fields, so
# no NEW record will carry a meaningful value.
ROLLBACK_UNINDEXED_COMPONENTS: tuple[str, ...] = (
    "prompt_hash",
    "standard_form_hash",
    "model_policy_hash",
    "corpus_snapshot_version",
)

REQUEST_CHANGE_DECISION = "REQUEST_CHANGE"


# ---------------------------------------------------------------------------
# The catalogue index — one entry per docs/audit-queries.md row.
#
# Served by `GET /api/audit` with no `query`, so the #93 explorer can build
# its query picker from the backend rather than from a second, drifting copy
# of the catalogue in the frontend.
# ---------------------------------------------------------------------------

AUDIT_QUERY_CATALOGUE: tuple[dict[str, Any], ...] = (
    {
        "query": "month_activity",
        "need": "What happened this month, in order",
        "params": ("month", "since", "until", "limit"),
        "answerable": True,
    },
    {
        "query": "review_history",
        "need": "Full history of one review/document",
        "params": ("review_id", "limit"),
        "answerable": True,
    },
    {
        "query": "actor_activity",
        "need": "Everything a given user did",
        "params": ("actor", "since", "until", "limit"),
        "answerable": True,
    },
    {
        "query": "document_access",
        "need": "Who viewed/downloaded a document (and who was denied)",
        "params": ("review_id", "limit"),
        "answerable": True,
    },
    {
        "query": "review_clause_ids",
        "need": "Which clauses informed review X",
        "params": ("review_id", "limit"),
        "answerable": True,
    },
    {
        "query": "rollback_population",
        "need": "Rollback/quarantine population — every review run under a bad bundle",
        "params": ("component", "value", "limit"),
        "answerable": True,
    },
    {
        "query": "playbook_request_changes",
        "need": "Every REQUEST_CHANGE under a given playbook version",
        "params": ("playbook_id", "playbook_version", "limit"),
        "answerable": True,
    },
    {
        "query": "release_activity",
        "need": "Release-bundle activations / rollbacks",
        "params": ("limit",),
        "answerable": True,
    },
    {
        "query": "break_glass",
        "need": "Break-glass / governance-bypass uses",
        "params": ("limit",),
        "answerable": True,
    },
    {
        "query": "denied_audit_mutations",
        "need": "Denied audit-table mutation attempts",
        "params": (),
        "answerable": False,
    },
    {
        "query": "model_recertification",
        "need": "Model recertification record",
        "params": ("limit",),
        "answerable": True,
    },
)

AUDIT_QUERY_NAMES: tuple[str, ...] = tuple(entry["query"] for entry in AUDIT_QUERY_CATALOGUE)


# ---------------------------------------------------------------------------
# Parameter helpers
# ---------------------------------------------------------------------------

def _required(params: dict[str, Any], name: str) -> str:
    value = params.get(name)
    if value is None or str(value).strip() == "":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Audit query parameter `{name}` is required.",
        )
    return str(value).strip()


def _limit(params: dict[str, Any]) -> int:
    return _clamp(params.get("limit"), AUDIT_QUERY_DEFAULT_LIMIT, 1, AUDIT_QUERY_MAX_LIMIT)


def _envelope(query: str, rows: list[dict[str, Any]], limit: int, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": query,
        "count": len(rows),
        "limit": limit,
        "results": rows,
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# The eleven catalogue entries
# ---------------------------------------------------------------------------

def _month_activity(dynamodb_resource: Any, params: dict[str, Any], now: float) -> dict[str, Any]:
    """`audit` by PK `YYYY-MM`, SK range on `timestamp`."""
    month = str(params.get("month") or time.strftime("%Y-%m", time.gmtime(now))).strip()
    limit = _limit(params)
    condition = Key("partition").eq(month)
    span = _timestamp_condition(params.get("since"), params.get("until"))
    if span is not None:
        condition = condition & span
    items = _query_pages(
        _audit_table(dynamodb_resource),
        limit,
        KeyConditionExpression=condition,
        ScanIndexForward=False,
    )
    return _envelope(
        "month_activity",
        [_audit_view(item) for item in items],
        limit,
        month=month,
        retention_boundary=_boundary(_BOUNDARY_ALWAYS),
    )


def _review_history(dynamodb_resource: Any, params: dict[str, Any], now: float) -> dict[str, Any]:
    """`audit` by the `review_id` GSI — the full history of one review."""
    review_id = _required(params, "review_id")
    limit = _limit(params)
    items = _query_audit_by_review(dynamodb_resource, review_id, limit)
    return _envelope(
        "review_history",
        [_audit_view(item) for item in items],
        limit,
        review_id=review_id,
        retention_boundary=_boundary(_BOUNDARY_ALWAYS),
    )


def _actor_activity(dynamodb_resource: Any, params: dict[str, Any], now: float) -> dict[str, Any]:
    """`audit` `actor` GSI, optionally with a `timestamp` range."""
    actor = _required(params, "actor")
    limit = _limit(params)
    condition = Key("actor").eq(actor)
    span = _timestamp_condition(params.get("since"), params.get("until"))
    if span is not None:
        condition = condition & span
    items = _query_pages(
        _audit_table(dynamodb_resource),
        limit,
        IndexName="actor-index",
        KeyConditionExpression=condition,
        ScanIndexForward=False,
    )
    return _envelope(
        "actor_activity",
        [_audit_view(item) for item in items],
        limit,
        actor=actor,
        retention_boundary=_boundary(_BOUNDARY_ALWAYS),
    )


def _document_access(dynamodb_resource: Any, params: dict[str, Any], now: float) -> dict[str, Any]:
    """`audit` by `review_id`, filtered to the document-access actions."""
    review_id = _required(params, "review_id")
    limit = _limit(params)
    items = _query_audit_by_review(
        dynamodb_resource,
        review_id,
        limit,
        condition=Attr("action").is_in(list(DOCUMENT_ACCESS_ACTIONS)),
    )
    return _envelope(
        "document_access",
        [_audit_view(item) for item in items],
        limit,
        review_id=review_id,
        actions=list(DOCUMENT_ACCESS_ACTIONS),
        denials_recorded=False,
        note=(
            "The download routes audit only a SUCCESSFUL presigned-URL issuance, "
            "so no `access_denied` row exists under the current pipeline; the "
            "'who was denied' half of this query returns nothing until denial "
            "auditing lands."
        ),
        retention_boundary=_boundary(_BOUNDARY_ALWAYS),
    )


def _review_clause_ids(
    dynamodb_resource: Any, params: dict[str, Any], now: float
) -> dict[str, Any]:
    """`audit` by `review_id`, `action = review_complete`, projecting
    `retrieved_clause_ids` (#27).

    DORMANT UNDER THE CURRENT PIPELINE, AND UNWIRED UNDER EVERY EARLIER ONE.
    Retrieval was retired by owner decision 2026-08-11 (docs/rag-dormant.md)
    and `scripts/review_spine.py` passes `retrieved_precedent=[]`, so no
    review run today records clause ids — and no writer ever did: nothing in
    this repo's history emits a `review_complete` audit row or a
    `retrieved_clause_ids` attribute (#27 landed as a docs-only CI gate), so
    there is no pre-retirement population to be answerable for either. The
    entry stays as the standing filter should retrieval be revived, and says
    so in the payload, so an empty result is not read as "this review
    retrieved nothing".
    """
    review_id = _required(params, "review_id")
    limit = _limit(params)
    items = _query_audit_by_review(
        dynamodb_resource,
        review_id,
        limit,
        condition=Attr("action").eq(REVIEW_COMPLETE_ACTION),
    )
    return _envelope(
        "review_clause_ids",
        [_audit_view(item, _CLAUSE_EXTRA_FIELDS) for item in items],
        limit,
        review_id=review_id,
        dormant=True,
        producers_wired=False,
        note=(
            "Retrieval is dormant by owner decision 2026-08-11 "
            "(docs/rag-dormant.md): no review run under the current pipeline "
            "records retrieved clause ids, and no writer in this codebase has "
            "ever recorded them, so this query has no population under any "
            "pipeline, past or present. It is the standing filter for when "
            "retrieval is revived."
        ),
        retention_boundary=_boundary(_BOUNDARY_CLAUSE_IDS),
    )


def _rollback_population(
    dynamodb_resource: Any, params: dict[str, Any], now: float
) -> dict[str, Any]:
    """`reviews` by the relevant component hash — the population a rollback
    or quarantine would act on."""
    component = _required(params, "component")
    value = _required(params, "value")
    limit = _limit(params)

    if component in ROLLBACK_UNINDEXED_COMPONENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"`{component}` is a release-bundle field, not a `reviews` row "
                "field: no writer records it on a review and there is no index "
                "to query it by, so answering it would require a full-table "
                "scan, which this endpoint does not perform. Use "
                "`component=playbook_hash`, or run the ad-hoc query against "
                "the audit-archive trail (RUNBOOK.md -> Observability)."
            ),
        )
    index_name = ROLLBACK_INDEXED_COMPONENTS.get(component)
    if index_name is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unrecognised `component`. Supported: "
                + ", ".join(sorted(ROLLBACK_INDEXED_COMPONENTS))
            ),
        )

    # KEYS_ONLY index (infra/lib/nested/data-stack.ts): this Query returns
    # review_id + playbook_hash + created_at and nothing else, so the row
    # bodies come from a BatchGetItem by primary key rather than from a
    # projection this index does not carry.
    keys = _query_pages(
        _reviews_table(dynamodb_resource),
        limit,
        IndexName=index_name,
        KeyConditionExpression=Key("playbook_hash").eq(value),
        ScanIndexForward=False,
    )
    review_ids = [str(key["review_id"]) for key in keys if key.get("review_id")]
    rows = _batch_get_reviews(dynamodb_resource, review_ids)
    results = [_review_view(rows[rid]) for rid in review_ids if rid in rows]
    return _envelope(
        "rollback_population",
        results,
        limit,
        component=component,
        index=index_name,
        retention_boundary=_boundary(_BOUNDARY_ALWAYS),
    )


def _playbook_request_changes(
    dynamodb_resource: Any, params: dict[str, Any], now: float
) -> dict[str, Any]:
    """`reviews` filtered on `playbook_version` + `decision = REQUEST_CHANGE`.

    `playbook_version` is the admin-facing string on a `playbook_versions`
    row and has no index on `reviews`; the review row records the version's
    `content_hash` as its `playbook_hash`
    (`reviews._resolve_playbook_version_lineage` resolves the version BEHIND
    that hash). So the version is resolved to its hash by primary-key
    GetItem, and the population comes off `playbook_hash-index` — index
    access end to end, no scan.

    `decision` is not on the index, so the REQUEST_CHANGE filter runs AFTER
    the population is read, and the population itself is capped at
    `AUDIT_QUERY_MAX_LIMIT` reviews, newest first. On a version with more
    reviews than that cap, REQUEST_CHANGEs older than the newest
    `AUDIT_QUERY_MAX_LIMIT` reviews fall outside the window -- and because
    the filter shrinks the answer, `count` can sit far below `limit` while
    the population was truncated, so a short answer would read as a complete
    one. The envelope therefore reports the PRE-filter population
    (`reviews_examined`) and `population_truncated`, the same way the
    month-walk entries report `months_searched`.
    """
    playbook_id = _required(params, "playbook_id")
    playbook_version = _required(params, "playbook_version")
    limit = _limit(params)

    versions_table_name = os.environ.get("PLAYBOOK_VERSIONS_TABLE")
    if not versions_table_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This deployment has no playbook_versions table, so an "
                "admin-facing playbook version cannot be resolved to the hash "
                "reviews are indexed by."
            ),
        )
    resp = dynamodb_resource.Table(versions_table_name).get_item(
        Key={"playbook_id": playbook_id, "version": playbook_version}
    )
    version_row = resp.get("Item")
    if not version_row or not version_row.get("content_hash"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No playbook version with that id and version, or it carries no content hash.",
        )
    content_hash = str(version_row["content_hash"])

    keys = _query_pages(
        _reviews_table(dynamodb_resource),
        AUDIT_QUERY_MAX_LIMIT,
        IndexName="playbook_hash-index",
        KeyConditionExpression=Key("playbook_hash").eq(content_hash),
        ScanIndexForward=False,
    )
    review_ids = [str(key["review_id"]) for key in keys if key.get("review_id")]
    rows = _batch_get_reviews(dynamodb_resource, review_ids)
    results = [
        _review_view(rows[rid])
        for rid in review_ids
        if rid in rows and rows[rid].get("decision") == REQUEST_CHANGE_DECISION
    ][:limit]
    truncated = len(review_ids) >= AUDIT_QUERY_MAX_LIMIT
    return _envelope(
        "playbook_request_changes",
        results,
        limit,
        playbook_id=playbook_id,
        playbook_version=playbook_version,
        playbook_hash=content_hash,
        decision=REQUEST_CHANGE_DECISION,
        reviews_examined=len(review_ids),
        population_examined_cap=AUDIT_QUERY_MAX_LIMIT,
        population_truncated=truncated,
        note=(
            "`decision` is not on `playbook_hash-index`, so this filters the "
            f"newest {AUDIT_QUERY_MAX_LIMIT} reviews under the version; "
            "`reviews_examined` is that pre-filter population."
            + (
                " It hit the cap, so REQUEST_CHANGEs on OLDER reviews under "
                "this version are not represented here -- this answer is a "
                "window, not the whole version."
                if truncated
                else ""
            )
        ),
        retention_boundary=_boundary(_BOUNDARY_ALWAYS),
    )


def _release_activity(dynamodb_resource: Any, params: dict[str, Any], now: float) -> dict[str, Any]:
    """`audit` filtered to the release activation/rollback actions."""
    limit = _limit(params)
    items = _walk_month_partitions(
        dynamodb_resource,
        Attr("action").is_in(list(RELEASE_ACTIVITY_ACTIONS)),
        limit,
        now,
    )
    return _envelope(
        "release_activity",
        [_audit_view(item, _RELEASE_EXTRA_FIELDS) for item in items],
        limit,
        actions=list(RELEASE_ACTIVITY_ACTIONS),
        months_searched=AUDIT_QUERY_MONTHS_BACK,
        retention_boundary=_boundary(_BOUNDARY_ALWAYS),
    )


def _break_glass(dynamodb_resource: Any, params: dict[str, Any], now: float) -> dict[str, Any]:
    """`audit` filtered on `reason = emergency-override` OR
    `action = governance_bypass` (also alarmed, per the catalogue)."""
    limit = _limit(params)
    condition = Attr("action").is_in(list(BREAK_GLASS_ACTIONS)) | Attr("reason").eq(
        BREAK_GLASS_REASON
    )
    items = _walk_month_partitions(dynamodb_resource, condition, limit, now)
    return _envelope(
        "break_glass",
        [_audit_view(item) for item in items],
        limit,
        actions=list(BREAK_GLASS_ACTIONS),
        reason=BREAK_GLASS_REASON,
        months_searched=AUDIT_QUERY_MONTHS_BACK,
        producers_wired=False,
        note=(
            "No writer in this codebase emits a governance-bypass or "
            "emergency-override row today; this query is the standing filter "
            "for when one does, and for records written outside the app."
        ),
        retention_boundary=_boundary(_BOUNDARY_ALWAYS),
    )


def _model_recertification(
    dynamodb_resource: Any, params: dict[str, Any], now: float
) -> dict[str, Any]:
    """`audit` filtered on `action = model_recertification`."""
    limit = _limit(params)
    items = _walk_month_partitions(
        dynamodb_resource,
        Attr("action").is_in(list(MODEL_RECERTIFICATION_ACTIONS)),
        limit,
        now,
    )
    return _envelope(
        "model_recertification",
        [_audit_view(item) for item in items],
        limit,
        actions=list(MODEL_RECERTIFICATION_ACTIONS),
        months_searched=AUDIT_QUERY_MONTHS_BACK,
        producers_wired=False,
        note=(
            "Recertification is a RUNBOOK procedure; no application writer "
            "emits this action today, so an empty result means the procedure "
            "has not been recorded through the app, not that no model is pinned."
        ),
        retention_boundary=_boundary(_BOUNDARY_ALWAYS),
    )


def _denied_audit_mutations(
    dynamodb_resource: Any, params: dict[str, Any], now: float
) -> dict[str, Any]:
    """The one catalogue entry that is NOT a DynamoDB query.

    A DENIED UpdateItem/DeleteItem never becomes an audit row — that is the
    whole point of the deny policy — so the record lives in the CloudWatch
    alarm and CloudTrail management events, cross-checked against the
    object-locked `audit-archive` copy. Serving a plausible-looking empty
    result set here would be worse than useless: an investigator would read
    "no denied mutation attempts" off a query that could never have found
    one. So this returns the pointer instead, and reads nothing.
    """
    return {
        "query": "denied_audit_mutations",
        "answerable_via_api": False,
        "results": [],
        "count": 0,
        "source": "CloudWatch alarm + CloudTrail management events",
        "cross_check": "the object-locked audit-archive S3 copy",
        "see": "RUNBOOK.md -> Observability",
        "note": (
            "A denied mutation is refused by IAM before it reaches the table, "
            "so it leaves no audit row to query. This endpoint will not "
            "manufacture an empty result set that reads as an all-clear."
        ),
        "retention_boundary": _boundary(_BOUNDARY_ALWAYS),
    }


_HANDLERS = {
    "month_activity": _month_activity,
    "review_history": _review_history,
    "actor_activity": _actor_activity,
    "document_access": _document_access,
    "review_clause_ids": _review_clause_ids,
    "rollback_population": _rollback_population,
    "playbook_request_changes": _playbook_request_changes,
    "release_activity": _release_activity,
    "break_glass": _break_glass,
    "denied_audit_mutations": _denied_audit_mutations,
    "model_recertification": _model_recertification,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_audit_query(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    *,
    query: str | None = None,
    params: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """GET /api/audit — the docs/audit-queries.md catalogue, admin-gated.

    With no `query`, returns the catalogue index (what can be asked, and with
    which parameters) so the explorer UI does not carry a second copy of the
    catalogue. With a `query`, runs that entry.

    Raises HTTPException(403) for a non-admin caller, 400 for an unknown
    query name or a missing/invalid parameter (never a silently-ignored one),
    404 when a named playbook version does not exist.
    """
    _require_admin(caller_user_row)
    resolved_params = params or {}
    resolved_now = time.time() if now is None else now

    if query is None or str(query).strip() == "":
        return {
            "catalogue": [
                {**entry, "params": list(entry["params"])} for entry in AUDIT_QUERY_CATALOGUE
            ],
            "source": "docs/audit-queries.md",
            "default_limit": AUDIT_QUERY_DEFAULT_LIMIT,
            "max_limit": AUDIT_QUERY_MAX_LIMIT,
            "months_searched": AUDIT_QUERY_MONTHS_BACK,
        }

    handler = _HANDLERS.get(str(query).strip())
    if handler is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unrecognised audit query. Supported: " + ", ".join(AUDIT_QUERY_NAMES),
        )
    return handler(dynamodb_resource, resolved_params, resolved_now)
