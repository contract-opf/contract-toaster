"""
Admin dashboard READ-API — issue #252 (the backend slice #91's UI binds to).

#91 (the admin dashboard UI) had nothing to bind to: there were no read
endpoints for the spend ledger, pipeline health, the manual-review queue, or
release activity, so every tile in that ticket was testable only against
hand-written fixtures. This module is those four reads, and nothing else.

  GET /api/admin/spend          -> `get_spend_ledger`
  GET /api/admin/health         -> `get_pipeline_health`
  GET /api/admin/manual-review  -> `list_manual_review_queue`
  GET /api/admin/releases       -> `get_release_activity`

READ-ONLY, BY SCOPE. Issue #252's Scope section is explicit ("All read-only"),
so nothing here writes, and the abandoned-reservation *reconcile action*
#91 also asks for is deliberately absent — that is a mutation with an audit
row, and it belongs to its own ticket rather than being smuggled into a read
slice. `get_spend_ledger` surfaces the reconcile *view* (what is reserved but
never settled) so an operator can see the condition; acting on it stays a
RUNBOOK procedure for now (RUNBOOK.md -> "Cost ceiling and reconcile").

ADMIN-ONLY, AND 403 RATHER THAN A FILTERED 200. Every one of these four is an
INSTANCE-WIDE operational view — the whole deployment's spend, the whole
deployment's queue — so unlike `reviews.get_review_detail` there is no
"your own row" subset a non-admin could legitimately be served. A filtered
200 would therefore be an empty 200 that merely looks like a capability, so
each function raises HTTPException(403) instead. That matches every other
`/api/admin/*` route in this codebase and `reviews.list_recent_failures`'s
own reasoning ("you are an admin or you get nothing").

Admin privilege is read from the caller's DynamoDB `users` row, never from a
JWT claim (ARCHITECTURE.md -> "Group-naming misnomer"; the row is fetched by
`main.get_active_user_row` -> `users.require_active_user` and passed in).

PROJECTED, NEVER SPREAD. Same security boundary as
`reviews._RECENT_FAILURE_FIELDS` and `retention._HOLD_LIST_FIELDS`: a reviews
row carries Confidential document substance (`summary`, `issues`, the
submitter's own `toaster_guidance`) and deployment internals (S3 keys,
execution ARNs), and none of it may reach an operator's browser through a
dashboard. Every row-shaped value returned by this module is built field by
field from a module-level allowlist tuple, so a field added to a stored row
tomorrow cannot appear here by accident. Adding a field to one of those
tuples is a deliberate disclosure decision.

NO SECRETS, EVER. No model API key, no session material and no password hash
is reachable from any of these reads. The ONE settings-table read that exists
is `get_spend_ledger`'s (issue #653): `model_settings.get_spend_cap_settings`
reads the `spend` row — a ceiling in cents and who last changed it — and never
the `global` row the API key lives on. Nothing about a credential can reach
this module through it.

Environment variables consumed:
  REVIEWS_TABLE             reviews table name (PK: review_id)
  DAILY_SPEND_TABLE         daily-spend counter table name (PK: spend_date)
  AUDIT_TABLE               append-only audit table (PK: partition,
                            SK: timestamp#event_id)
  MODEL_SETTINGS_TABLE      model-settings table (PK: setting_id), OPTIONAL —
                            read only for the admin-set daily cap (#653);
                            unset means the env cap below is the ceiling
  DAILY_SPEND_CAP_USD_CENTS the daily ceiling when no admin cap is stored, and
                            the fallback when a day's row carries no cap
  PIPELINE_MAX_CONCURRENCY  in-process pipeline concurrency cap, read for the
                            occupancy denominator (see `_concurrency_limit`)
  MODEL_INVOCATIONS_TABLE   model-invocation ledger (#414), OPTIONAL — see
                            `_cost_outliers`; absent on a mock-pipeline
                            deployment, which degrades to "not available"
                            rather than failing the request
"""

from __future__ import annotations

import os
import statistics
import time
from typing import Any

from boto3.dynamodb.conditions import Key
from fastapi import HTTPException, status

try:  # production runs `src.main`; tests put backend/src on sys.path
    from src import model_settings as model_settings_module
    from src import reviews as reviews_module
    from src.authz import require_admin
    from src.disposition import TRIAGE_STATUS_PENDING, TRIAGE_STATUS_TRIAGED
    from src.users import json_safe
except ImportError:  # pragma: no cover
    import model_settings as model_settings_module  # type: ignore[no-redef]
    import reviews as reviews_module  # type: ignore[no-redef]
    from authz import require_admin  # type: ignore[no-redef]
    from disposition import TRIAGE_STATUS_PENDING, TRIAGE_STATUS_TRIAGED  # type: ignore[no-redef]
    from users import json_safe  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Admin gate
# ---------------------------------------------------------------------------
#
# `src.authz.require_admin` is the single source of the admin predicate
# (issue #66); this module holds no local copy. Each call site below passes
# its own literal 403 detail naming WHICH panel refused the caller -- the
# per-route wording the rest of the codebase uses, and never anything
# caller-supplied.


def _clamp(value: Any, default: int, lowest: int, highest: int) -> int:
    """Coerce a query-string integer into [lowest, highest], defaulting a
    missing/unparseable value.

    Every caller-settable bound in this module goes through this — the spend
    window, the stale-in-flight threshold, the queue and release-feed
    limits, and the cost-outlier ledger sample — so no request parameter can
    widen a read past its documented ceiling.

    It is NOT a claim that every read here is bounded. `get_pipeline_health`
    and `list_manual_review_queue` deliberately read whole `status-index`
    partitions (through `reviews._query_by_status`, issue #52 -- never a
    table scan), because their `status_counts` / `counts` tiles have to be
    EXACT totals rather than a count of a sample. What this does guarantee
    for those two is that the row LIST each returns is a bounded slice of
    that read, never the read itself.
    """
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = default
    return max(lowest, min(requested, highest))


def _as_int(value: Any) -> int | None:
    """A stored numeric attribute as a plain `int`, or None if it is absent
    or unparseable.

    Every number this module reads arrives in one of three shapes and none
    of them may raise: `created_at`/`updated_at` are epoch-second STRINGS
    (`reviews._create_review_row` writes them that way), boto3's resource
    API hands stored numbers back as `decimal.Decimal`, and a field that was
    never measured is `None` (the model-invocation ledger's
    `actual_input_tokens`, deliberately None rather than 0).
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# GET /api/admin/spend — spend ledger + reconcile view
# ---------------------------------------------------------------------------

SPEND_LEDGER_DEFAULT_DAYS = 30
SPEND_LEDGER_MAX_DAYS = 90

# ALLOWLIST for one day's ledger row. The daily-spend row is a counter row
# and carries no substance today, but it is projected on the same rule as
# every other row in this module so a field added to it tomorrow (a per-user
# breakdown, a reservation id list) cannot reach a browser by accident.
_SPEND_DAY_FIELDS = (
    "spend_date",
    "reserved_usd_cents",
    "settled_usd_cents",
    "daily_cap_usd_cents",
)


def _spend_day_view(row: dict[str, Any], fallback_cap_cents: int) -> dict[str, Any]:
    """Project one daily-spend row and derive the reconcile figure.

    `outstanding_reservation_usd_cents = reserved - settled` is THE reconcile
    signal (#59/#61): `reserve_spend` adds the worst-case amount to
    `reserved_usd_cents`, and `settle_spend` then moves that counter by
    `actual - reservation` while adding `actual` to `settled_usd_cents` — so
    a fully settled day converges on reserved == settled, and a positive
    remainder is spend still held by reviews that have not settled (in
    flight, or abandoned; RUNBOOK.md -> "Cost ceiling and reconcile").

    Floored at zero deliberately: preflight (#491) and cover-note (#499)
    spend is ledgered straight into `settled_usd_cents` with no reservation
    behind it, so on a day dominated by those the raw difference goes
    NEGATIVE, which would read as "negative reservations outstanding". The
    two raw counters are returned unmodified alongside it, so nothing is
    hidden by the floor.

    `daily_cap_usd_cents` ON A DAY ROW is the cap as it WAS when a reservation
    on that day last wrote it — history, not the ceiling in force. Since issue
    #653 an admin can change the cap mid-day, so the ENFORCED figure is
    reported once at the top level of `get_spend_ledger` and that is the one
    the UI compares today's spend against. `fallback_cap_cents` is that same
    resolved figure, threaded in so a request resolves the store-backed cap
    ONCE rather than once per row in the window.
    """
    view = {field: row.get(field) for field in _SPEND_DAY_FIELDS}
    view["reserved_usd_cents"] = int(view.get("reserved_usd_cents") or 0)
    view["settled_usd_cents"] = int(view.get("settled_usd_cents") or 0)
    if view.get("daily_cap_usd_cents") is None:
        view["daily_cap_usd_cents"] = fallback_cap_cents
    else:
        view["daily_cap_usd_cents"] = int(view["daily_cap_usd_cents"])
    view["outstanding_reservation_usd_cents"] = max(
        0, view["reserved_usd_cents"] - view["settled_usd_cents"]
    )
    return view


def _empty_spend_day(spend_date: str, fallback_cap_cents: int) -> dict[str, Any]:
    """A well-formed zero row for a day with no spend at all, so the tile
    always renders rather than 404ing on a quiet morning."""
    return _spend_day_view({"spend_date": spend_date}, fallback_cap_cents)


def get_spend_ledger(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    days: Any = SPEND_LEDGER_DEFAULT_DAYS,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """GET /api/admin/spend — today's spend vs the ceiling, plus a bounded
    trailing window and the reserved-vs-settled reconcile view.

    Idempotent: a pure read over the `daily_spend` counter rows #61/#59
    already maintain. Nothing here reserves, settles, or reverses anything.

    `days` is clamped into [1, SPEND_LEDGER_MAX_DAYS] and is a CALENDAR
    window, not a row count: `days` are the rows whose `spend_date` falls in
    the last N UTC days ending today, so `window_days: 30` always means the
    same 30 calendar days a "last 30 days" tile would sum. A row exists only
    for a day that actually had spend (`reviews.reserve_spend` /
    `settle_spend` write it), so a quiet stretch simply contributes no rows
    — taking the N most recent ROWS instead would answer a 30-day question
    with a quarter's worth of spend on a deployment that sat idle. The table
    holds at most one row per UTC day, so a full scan of it is bounded by
    the deployment's age rather than by review volume; rows are sorted
    newest-first on `spend_date` (a fixed-width `YYYY-MM-DD` string, so a
    plain reverse string sort is a true date sort).

    `today` is always present and always well-formed, even on a day with no
    row yet. `worst_case_reservation_usd_cents` is what ONE review reserves
    right now under the models currently selected (#445), so the UI can
    render "outstanding ≈ N reservations" without duplicating the pricing
    formula.

    ISSUE #653 — the numbers a person deciding whether to press the button
    actually needs, answered here rather than computed by the client:

      `daily_cap_usd_cents`      the ceiling the reservation path enforces
                                 RIGHT NOW (admin row > env > default). NOT the
                                 same thing as `today.daily_cap_usd_cents`,
                                 which is whatever the cap was when a
                                 reservation last wrote that row and is left as
                                 the historical record it is.
      `spent_today_usd_cents`    `today.reserved_usd_cents` — the ONE counter
                                 `reserve_spend`'s condition is evaluated
                                 against. After a review settles this counter
                                 holds that review's ACTUAL cost, so it reads
                                 as "spent today, with in-flight reviews still
                                 held at their worst case". It is deliberately
                                 NOT reserved+settled: settlement adds the
                                 actual to `settled_usd_cents` while moving
                                 `reserved_usd_cents` to the same figure, so
                                 summing them double-counts every settled
                                 review. `settled_usd_cents` stays reported on
                                 its own, and is the only place preflight
                                 (#491) and cover-note (#499) spend appears —
                                 neither reserves, so neither is inside the
                                 ceiling this compares against.
      `remaining_usd_cents`      cap − spent, floored at zero.
      `estimated_review_usd_cents`  what the next review is EXPECTED to cost,
                                 beside the worst case it reserves.
      `next_review_admissible`   whether reserving `worst_case_reservation_
                                 usd_cents` right now would pass
                                 `reserve_spend`. This is the whole question
                                 the screen exists to answer, so it MIRRORS
                                 that gate — including the explicit
                                 whole-reservation check #653 added there, so
                                 it can neither promise a submission the server
                                 would refuse nor predict a refusal it would
                                 not perform.

    `cap_setting` is `model_settings.get_spend_cap_settings` — where the cap is
    set, the bounds a new one must satisfy, and whether this deployment even
    has a store to set it in. Read here rather than from a route of its own so
    the cap and the spend it bounds are always one answer taken at one moment.

    Raises HTTPException(403) for a non-admin caller.
    """
    require_admin(caller_user_row, "Admin privilege required to view the spend ledger.")
    # Admin-gated in its own right, and gated again above — the cap setting
    # never rides along on a read a non-admin could reach.
    cap_setting = model_settings_module.get_spend_cap_settings(
        caller_user_row, dynamodb_resource
    )
    enforced_cap_cents = int(cap_setting["daily_cap_usd_cents"])
    window = _clamp(days, SPEND_LEDGER_DEFAULT_DAYS, 1, SPEND_LEDGER_MAX_DAYS)
    now = time.time() if now_epoch is None else now_epoch
    today_key = time.strftime("%Y-%m-%d", time.gmtime(now))
    # The first day IN the window: N days ending today, so N-1 days back.
    # Plain epoch arithmetic is exact here because these are UTC days and
    # UTC has no DST shifts.
    window_start_key = time.strftime("%Y-%m-%d", time.gmtime(now - (window - 1) * 86400))

    table = dynamodb_resource.Table(os.environ["DAILY_SPEND_TABLE"])
    rows: list[dict[str, Any]] = []
    resp = table.scan()
    rows.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        rows.extend(resp.get("Items", []))

    projected = [_spend_day_view(row, enforced_cap_cents) for row in rows]
    projected.sort(key=lambda d: str(d.get("spend_date") or ""), reverse=True)

    today = next(
        (d for d in projected if d.get("spend_date") == today_key),
        _empty_spend_day(today_key, enforced_cap_cents),
    )
    # Bounded on BOTH ends: a row dated after today is not part of "the last
    # N days" either, whatever wrote it.
    in_window = [
        d
        for d in projected
        if window_start_key <= str(d.get("spend_date") or "") <= today_key
    ]

    next_review_cents = reviews_module.compute_worst_case_reservation_usd_cents(
        dynamodb_resource
    )
    spent_today_cents = int(today["reserved_usd_cents"])

    return json_safe(
        {
            "today": today,
            "days": in_window,
            "window_days": window,
            "worst_case_reservation_usd_cents": next_review_cents,
            # Issue #653: the EXPECTED cost of the next review beside the worst
            # case it reserves. Both, because the two differed by 4x on the
            # measurement this was filed on — an operator reading only the
            # reservation would conclude the cap buys a quarter of the reviews
            # it actually buys. None on a provider with no per-review basis;
            # see `reviews.estimate_review_usd_cents`.
            "estimated_review_usd_cents": (
                reviews_module.estimate_review_usd_cents(dynamodb_resource)
            ),
            "daily_cap_usd_cents": enforced_cap_cents,
            "daily_cap_source": cap_setting["daily_cap_source"],
            "cap_setting": cap_setting,
            "spent_today_usd_cents": spent_today_cents,
            "remaining_usd_cents": max(0, enforced_cap_cents - spent_today_cents),
            "next_review_admissible": (
                spent_today_cents + next_review_cents <= enforced_cap_cents
            ),
        }
    )


# ---------------------------------------------------------------------------
# GET /api/admin/health — pipeline health
# ---------------------------------------------------------------------------

# How old an in-flight (PENDING/RUNNING) review has to be before this view
# calls it stale. The deployment's real backstop is the stale-PENDING /
# stale-RUNNING alarm and the orphan reconciler (RUNBOOK.md -> "Stuck or
# failed review"); this constant is only the DASHBOARD's display threshold,
# which is why it is overridable per request rather than pinned to an alarm
# value it does not own.
STALE_IN_FLIGHT_SECONDS_DEFAULT = 3600
STALE_IN_FLIGHT_SECONDS_MIN = 60
STALE_IN_FLIGHT_SECONDS_MAX = 7 * 24 * 3600

# Bound on how many stale rows are enumerated. The COUNTS are always exact;
# only the enumerated sample is capped, so a deployment with 5,000 stuck
# reviews reports 5,000 and lists the oldest few.
STALE_SAMPLE_LIMIT = 25

# ALLOWLIST for one stale in-flight review. Identifiers, status, and
# timestamps only — the same rule as `reviews._RECENT_FAILURE_FIELDS`, and
# for the same reason: this is an operability panel, not a review viewer.
_STALE_REVIEW_FIELDS = ("review_id", "status", "created_at", "updated_at", "playbook_id")

_DEFAULT_PIPELINE_MAX_CONCURRENCY = 5


def _concurrency_limit() -> int:
    """The in-process pipeline concurrency cap — the semaphore-equivalent
    denominator for occupancy.

    Read from `PIPELINE_MAX_CONCURRENCY` exactly as
    `pipeline_runner._MAX_CONCURRENCY` reads it, and NOT by importing that
    module: `pipeline_runner` pulls in the whole review spine (model client,
    canonicalizer, injection scanner) at import time, which is a great deal
    of machinery to load into the API process just to learn one integer, and
    it resolves the env var at ITS import time — so an operator who changed
    the cap would see the stale value here. A malformed value falls back to
    the same default that module's `int(os.environ.get(..., "5"))` would
    otherwise raise on.
    """
    try:
        return max(1, int(os.environ.get("PIPELINE_MAX_CONCURRENCY", _DEFAULT_PIPELINE_MAX_CONCURRENCY)))
    except (TypeError, ValueError):
        return _DEFAULT_PIPELINE_MAX_CONCURRENCY


def get_pipeline_health(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    stale_after_seconds: Any = STALE_IN_FLIGHT_SECONDS_DEFAULT,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """GET /api/admin/health — pipeline health summary.

    Three things #91's health tile asks for, all derived from the `reviews`
    table plus the configured concurrency cap:

      * `status_counts` — every terminal and non-terminal review status with
        its exact count, including a zero for statuses nothing is currently
        in, so the tile's rows never appear and disappear between polls. An
        UNKNOWN status found on a row (a value written by a future version)
        is counted under its own key rather than dropped.
      * `stale` — in-flight (PENDING/RUNNING) reviews older than the
        threshold: exact counts plus a bounded oldest-first sample.
      * `concurrency` — RUNNING reviews against `PIPELINE_MAX_CONCURRENCY`,
        the occupancy figure RUNBOOK.md's "semaphore saturation" tile wants.
        `occupancy` can legitimately exceed 1.0 on the AWS target, where
        Step Functions rather than the in-process pool runs the work; it is
        reported rather than clamped, because a value over 1.0 is itself the
        interesting signal.

    NOT a liveness probe — the public `GET /health` route is unrelated and
    unchanged; this is the admin-only operational summary.

    Raises HTTPException(403) for a non-admin caller.
    """
    require_admin(caller_user_row, "Admin privilege required to view pipeline health.")
    threshold = _clamp(
        stale_after_seconds,
        STALE_IN_FLIGHT_SECONDS_DEFAULT,
        STALE_IN_FLIGHT_SECONDS_MIN,
        STALE_IN_FLIGHT_SECONDS_MAX,
    )
    now = int(time.time() if now_epoch is None else now_epoch)

    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    # Issue #52: one `status-index` query per status instead of a scan of the
    # whole table. `reviews._query_by_status` owns the LastEvaluatedKey loop,
    # so a count here is the whole partition, never its first page. The
    # counts read a `review_id`-only projection -- the tile wants a number,
    # not the rows -- and the vocabulary is closed: a status outside it has
    # no partition this read visits, which is why a new terminal status is
    # added to `reviews.REVIEW_STATUSES_*` rather than written ad hoc.
    status_counts: dict[str, int] = {
        name: 0
        for name in sorted(
            reviews_module.REVIEW_STATUSES_NON_TERMINAL | reviews_module.REVIEW_STATUSES_TERMINAL
        )
    }
    for name in status_counts:
        status_counts[name] = len(
            reviews_module._query_by_status(table, name, projection="review_id")
        )

    stale_rows: list[dict[str, Any]] = []
    in_flight = 0
    running = 0

    # The stale sample reads only the in-flight partitions (PENDING, RUNNING)
    # -- bounded by construction: everything that has finished is in a
    # terminal partition this loop never touches. Oldest-first from the
    # index, so the rows most likely to be stuck arrive first; the counts
    # stay exact because the whole (small) partition is read.
    for name in sorted(reviews_module.REVIEW_STATUSES_NON_TERMINAL):
        for item in reviews_module._query_by_status(table, name, newest_first=False):
            in_flight += 1
            if name == "RUNNING":
                running += 1
            # Age from the last time anything moved this review, falling
            # back to submission time: a row whose `updated_at` keeps
            # advancing is making progress and is not stuck, whatever its
            # total age.
            moved_at = _as_int(item.get("updated_at")) or _as_int(item.get("created_at"))
            if moved_at is None or (now - moved_at) < threshold:
                continue
            row = {field: item.get(field) for field in _STALE_REVIEW_FIELDS}
            row["age_seconds"] = now - moved_at
            stale_rows.append(row)

    stale_rows.sort(key=lambda r: r["age_seconds"], reverse=True)

    return json_safe(
        {
            "generated_at": now,
            "status_counts": status_counts,
            "in_flight": {
                "total": in_flight,
                "pending": status_counts.get("PENDING", 0),
                "running": running,
            },
            "stale": {
                "threshold_seconds": threshold,
                "total": len(stale_rows),
                "pending": sum(1 for r in stale_rows if r.get("status") == "PENDING"),
                "running": sum(1 for r in stale_rows if r.get("status") == "RUNNING"),
                "reviews": stale_rows[:STALE_SAMPLE_LIMIT],
            },
            "concurrency": {
                "running": running,
                "limit": _concurrency_limit(),
                "occupancy": round(running / _concurrency_limit(), 4),
            },
        }
    )


# ---------------------------------------------------------------------------
# GET /api/admin/manual-review — the #37 manual-review queue
# ---------------------------------------------------------------------------

# The two DOCUMENTED manual-review terminal statuses (issue #37; RUNBOOK.md
# -> "Manual-review filter: owner and SLA"). System statuses, never a third
# legal category (docs/output-contract.md).
MANUAL_REVIEW_STATUSES = ("MANUAL_REVIEW_REQUIRED", "ERROR_MANUAL_REVIEW_REQUIRED")

# RUNBOOK.md -> "Manual-review filter: owner and SLA": the
# `contract-toaster-manual-review-stale` alarm fires when an entry has sat
# unacknowledged for more than 24 hours. The queue flags the same entries so
# the legal admin's daily check and the alarm agree on what is overdue.
MANUAL_REVIEW_SLA_SECONDS = 24 * 3600

MANUAL_REVIEW_DEFAULT_LIMIT = 50
MANUAL_REVIEW_MAX_LIMIT = 200

# Filter values accepted for `?triage=` (issue #37's owner workflow, wired to
# `disposition.legal_triage_status`).
TRIAGE_FILTERS = ("all", "pending", "triaged", "none")

# ALLOWLIST for one queue entry. Deliberately the SAME disclosure decision as
# `reviews._RECENT_FAILURE_FIELDS`: identifiers, the controlled #442 `reason`
# token, the failing stage, the terminal status, timestamps, and the
# disposition/triage state the owner workflow acts on. No `summary`, no
# `issues`, no `toaster_guidance`, no S3 key, no owner identity — the legal
# admin triaging this queue needs to know WHICH review and WHY, not what the
# contract says.
_MANUAL_REVIEW_FIELDS = (
    "review_id",
    "created_at",
    "updated_at",
    "failed_at",
    "failing_stage",
    "status",
    "playbook_id",
    "legal_triage_status",
    "attorney_disposition",
)


def _manual_review_entry(item: dict[str, Any], now: int) -> dict[str, Any]:
    """Project one manual-review row and derive its SLA state.

    `reason` is coalesced through `reviews._resolve_failure_reason` — the
    SAME three-field read the Diagnostics route and the reviewer's own detail
    view use. Re-deriving it here would reintroduce exactly the drift that
    function's docstring documents (a QUARANTINED row stores its cause under
    `quarantine_reason`, so a bare `.get("reason")` reports "no cause
    recorded" for every one of them).
    """
    entry = {field: item.get(field) for field in _MANUAL_REVIEW_FIELDS}
    entry["reason"] = reviews_module._resolve_failure_reason(item)
    entered_at = _as_int(item.get("failed_at")) or _as_int(item.get("updated_at")) or _as_int(
        item.get("created_at")
    )
    entry["waiting_seconds"] = None if entered_at is None else max(0, now - entered_at)
    entry["sla_breached"] = bool(
        entry["waiting_seconds"] is not None
        and entry["waiting_seconds"] > MANUAL_REVIEW_SLA_SECONDS
    )
    return entry


def list_manual_review_queue(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    status_filter: str | None = None,
    triage: str | None = None,
    limit: Any = MANUAL_REVIEW_DEFAULT_LIMIT,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """GET /api/admin/manual-review — the manual-review queue, filterable.

    The cross-user view RUNBOOK.md -> "Manual-review filter: owner and SLA"
    assigns to the legal admin as a daily check, and the half of #74's
    disposition work #486 explicitly deferred here: every review sitting in
    `MANUAL_REVIEW_REQUIRED` or `ERROR_MANUAL_REVIEW_REQUIRED`, newest first,
    each carrying how long it has waited and whether it has passed the
    24-hour SLA.

    Filters (both optional, both validated — an unrecognised value is a 400,
    never a silently ignored parameter that returns an unfiltered queue the
    caller believes is filtered):

      `status_filter`  one of MANUAL_REVIEW_STATUSES, or "all" / None.
      `triage`         "pending" (awaiting legal triage), "triaged" (already
                       triaged), "none" (no triage state — i.e. no
                       EDITED/REJECTED disposition was ever recorded), or
                       "all" / None.

    `counts` is computed over the UNFILTERED queue and is therefore the tile
    figure (#37's "dashboard tile counts reviews in manual-review states"),
    independent of whatever filter the operator has applied to the list
    below it.

    Raises HTTPException(403) for a non-admin caller, HTTPException(400) for
    an unrecognised filter value.
    """
    require_admin(caller_user_row, "Admin privilege required to view the manual-review queue.")

    wanted_status = (status_filter or "all").strip()
    if wanted_status not in ("all",) + MANUAL_REVIEW_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported status filter {wanted_status!r}; must be one of "
                f"{sorted(('all',) + MANUAL_REVIEW_STATUSES)}."
            ),
        )
    wanted_triage = (triage or "all").strip()
    if wanted_triage not in TRIAGE_FILTERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported triage filter {wanted_triage!r}; must be one of {sorted(TRIAGE_FILTERS)}.",
        )

    bounded = _clamp(limit, MANUAL_REVIEW_DEFAULT_LIMIT, 1, MANUAL_REVIEW_MAX_LIMIT)
    now = int(time.time() if now_epoch is None else now_epoch)

    table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
    # Issue #52: the two manual-review partitions of `status-index`, each
    # read in full (the `counts` tile is exact), never a scan of the table.
    queue: list[dict[str, Any]] = []
    for name in MANUAL_REVIEW_STATUSES:
        queue.extend(reviews_module._query_by_status(table, name))
    # `created_at` is a fixed-width epoch-second string, so a reverse string
    # sort is a true newest-first ordering (same key `list_recent_failures`
    # and `list_reviews` use).
    queue.sort(key=lambda i: str(i.get("created_at") or ""), reverse=True)
    entries = [_manual_review_entry(item, now) for item in queue]

    counts = {
        "total": len(entries),
        "sla_breached": sum(1 for e in entries if e["sla_breached"]),
        "pending_triage": sum(
            1 for e in entries if e.get("legal_triage_status") == TRIAGE_STATUS_PENDING
        ),
        "triaged": sum(
            1 for e in entries if e.get("legal_triage_status") == TRIAGE_STATUS_TRIAGED
        ),
    }
    for name in MANUAL_REVIEW_STATUSES:
        counts[name] = sum(1 for e in entries if e.get("status") == name)

    if wanted_status != "all":
        entries = [e for e in entries if e.get("status") == wanted_status]
    if wanted_triage == "pending":
        entries = [e for e in entries if e.get("legal_triage_status") == TRIAGE_STATUS_PENDING]
    elif wanted_triage == "triaged":
        entries = [e for e in entries if e.get("legal_triage_status") == TRIAGE_STATUS_TRIAGED]
    elif wanted_triage == "none":
        entries = [e for e in entries if not e.get("legal_triage_status")]

    return json_safe(
        {
            "generated_at": now,
            "sla_seconds": MANUAL_REVIEW_SLA_SECONDS,
            "filters": {"status": wanted_status, "triage": wanted_triage},
            "counts": counts,
            "reviews": entries[:bounded],
        }
    )


# ---------------------------------------------------------------------------
# GET /api/admin/releases — release activity + cost-outlier flag
# ---------------------------------------------------------------------------

# The two audit actions `playbook_versions.py` writes for a release-bundle
# lifecycle change. Named here rather than matched by prefix so a future
# audit action starting with "release_" cannot silently join this feed.
RELEASE_ACTIVITY_ACTIONS = ("release_bundle_activate", "release_bundle_rollback")

RELEASE_ACTIVITY_DEFAULT_LIMIT = 25
RELEASE_ACTIVITY_MAX_LIMIT = 100

# How many `YYYY-MM` audit partitions back the walk goes before giving up.
# The audit table is partitioned by month (`playbook_versions._write_audit_
# entry`), so this bounds the read to at most 12 partition queries no matter
# how quiet the deployment has been.
RELEASE_ACTIVITY_MONTHS_BACK = 12

# ALLOWLIST for one release-activity row. `actor` is the admin identity that
# performed the act: that is the entire point of an activation audit trail,
# and this route is admin-only. Nothing document-derived is present — the
# audit row itself carries none (`playbook_versions._write_audit_entry`:
# "Identifiers, statuses, and hashes only — never document substance").
_RELEASE_ACTIVITY_FIELDS = (
    "event_id",
    "action",
    "actor",
    "playbook_id",
    "version",
    "before_status",
    "after_status",
    "prior_active_version",
    "gate7_reevaluated",
    "gate7_skip_reason",
    "content_hash",
    "outcome",
)

# docs/design-notes.md -> "Why we capture cost per review": "A 50x cost
# outlier is usually a signal that something is off (oversized document,
# runaway retry, prompt-injection attempt)." ARCHITECTURE.md -> "Security
# posture" records the same flag as a possible injection or runaway signal.
COST_OUTLIER_MULTIPLE = 50.0

# Below this many priced reviews there is no meaningful baseline to be a
# multiple OF — with two reviews the median is one of them, and any cheap
# first review would make the second look like an outlier. Under the
# minimum, the feed reports its baseline and flags nothing.
#
# This is a FLOOR for flagging, not a bound on work: it says nothing about
# how many ledger rows were read to get there. That bound is
# COST_OUTLIER_LEDGER_ROWS_* below.
COST_OUTLIER_MIN_SAMPLE = 3

# How many #414 ledger rows ONE cost-outlier computation may read.
#
# The ledger is append-only with one row per model attempt, so "the median
# over every review ever run" is a read whose cost grows for the life of the
# deployment: without a cap, a single `GET /api/admin/releases` eventually
# scans the whole table, and the dashboard gets slower and more expensive
# every day it is used.
#
# A trailing TIME window would not bound it. The ledger's only key is
# (`review_id`, `record_id`) and it carries no time-ordered index
# (`invocation_ledger.py`), so "the last N days" can only be a
# FilterExpression — which still reads every row and merely throws most of
# them away. A row cap is the one bound the table's own shape supports.
#
# The consequence is stated rather than hidden: the baseline is the median
# of a BOUNDED SAMPLE, and `cost_outliers.truncated` tells the operator when
# the sample was cut. At ~2-6 ledger rows per review the default is a
# several-hundred-review baseline, far above COST_OUTLIER_MIN_SAMPLE.
COST_OUTLIER_LEDGER_ROWS_DEFAULT = 2000
COST_OUTLIER_LEDGER_ROWS_MAX = 10_000

# Which #414 `pass_name` values are priced at the CRITIC model's rates.
#
# EVERY ledgered pass is priced — none is dropped. This codebase writes four
# distinct `pass_name` values: `primary`
# (scripts/primary_review_pass.py), `critic` (scripts/critic_review_pass.py),
# `floor` (scripts/floor_judge.py) and
# `cover_note` (backend/src/review_routes.py). A historical fifth, written by
# the address-repair pass issue #628 deleted, may still sit on old ledger
# rows and is priced by the same fall-through below. Only the critic pass is a
# critic-model invocation, so it alone is billed at the critic rate;
# everything else falls into the primary slot of
# `reviews.compute_actual_usd_cents_from_usage`, which is the only other
# rate role that function exposes. A pass whose name is new to this tuple
# is therefore still COUNTED (at primary rates) rather than silently priced
# at zero — the failure mode this set exists to prevent, because a review
# whose extra passes cost nothing is understated against the median and so
# escapes the runaway/injection flag.
_CRITIC_RATE_PASSES = ("critic",)


def _month_partitions(now: float, months_back: int) -> list[str]:
    """`YYYY-MM` audit partitions, newest first, walking back `months_back`
    months from `now` — the partition key format
    `playbook_versions._write_audit_entry` writes
    (`time.strftime("%Y-%m", time.gmtime(now))`)."""
    year, month = (int(p) for p in time.strftime("%Y-%m", time.gmtime(now)).split("-"))
    partitions: list[str] = []
    for _ in range(max(1, months_back)):
        partitions.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return partitions


def _release_audit_rows(dynamodb_resource: Any, limit: int, now: float) -> list[dict[str, Any]]:
    """Recent release-bundle activations/rollbacks, newest first.

    Queried partition by partition rather than scanned: the audit table holds
    every audited act in the deployment (user lifecycle, retention, playbook
    notes), and a scan would read all of it to find the handful of release
    rows. Within a month partition the sort key is
    `"{epoch}#{event_id}"`, so `ScanIndexForward=False` is a true
    newest-first ordering; across partitions, every row in a newer month is
    newer than every row in an older one, so concatenating the partitions in
    order preserves it.
    """
    table = dynamodb_resource.Table(os.environ["AUDIT_TABLE"])
    collected: list[dict[str, Any]] = []
    for partition in _month_partitions(now, RELEASE_ACTIVITY_MONTHS_BACK):
        resp = table.query(
            KeyConditionExpression=Key("partition").eq(partition),
            ScanIndexForward=False,
        )
        while True:
            for item in resp.get("Items", []):
                if item.get("action") in RELEASE_ACTIVITY_ACTIONS:
                    collected.append(item)
            if len(collected) >= limit or "LastEvaluatedKey" not in resp:
                break
            resp = table.query(
                KeyConditionExpression=Key("partition").eq(partition),
                ScanIndexForward=False,
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
        if len(collected) >= limit:
            break
    return collected[:limit]


def _release_activity_view(item: dict[str, Any]) -> dict[str, Any]:
    """Project one audit row into a release-activity entry.

    `recorded_at` is recovered from the `"{epoch}#{event_id}"` sort key,
    which is where the timestamp actually lives — the audit row carries no
    separate epoch attribute.
    """
    view = {field: item.get(field) for field in _RELEASE_ACTIVITY_FIELDS}
    sort_key = str(item.get("timestamp") or "")
    view["recorded_at"] = _as_int(sort_key.split("#", 1)[0]) if sort_key else None
    return view


def _pass_usage_by_review(
    dynamodb_resource: Any,
    max_rows: int,
) -> tuple[dict[str, dict[str, dict[str, int]]], bool, bool]:
    """Real per-review, per-pass token usage from the #414 model-invocation
    ledger, as `{review_id: {pass_name: {"input_tokens": n, "output_tokens": n}}}`,
    read from AT MOST `max_rows` ledger rows.

    Returns `(usage, available, truncated)`. `available` is False — and the
    mapping empty — whenever the ledger cannot be read at all:
    `MODEL_INVOCATIONS_TABLE` is unset (every mock-pipeline deployment,
    which never calls `run_review` and so provisions no ledger — see
    `invocation_ledger.py`'s module docstring) or the table read fails. The
    caller degrades to "cost outliers not available" rather than 500ing a
    dashboard because an optional ledger is absent, exactly as the ledger's
    own write path degrades to "no rows written".

    `truncated` is True when the read stopped AT the cap with more of the
    table still to scan, so the caller can say so in the payload instead of
    presenting a sample's median as the deployment's median. (DynamoDB
    reports a `LastEvaluatedKey` whenever a `Limit` cut a scan short, so a
    ledger holding exactly `max_rows` rows reports truncated conservatively
    — the direction that admits to a bounded sample rather than one that
    overstates it.) THE READ ITSELF IS BOUNDED: `Limit` is set on every
    page and the pagination loop stops as soon as the cap is reached, so
    this costs the same on a table of ten rows and a table of ten million
    (see COST_OUTLIER_LEDGER_ROWS_DEFAULT for why a row cap rather than a
    time window).

    Only attempts with REAL provider-reported usage are summed:
    `actual_input_tokens` / `actual_output_tokens` are `None` (never 0)
    whenever an attempt could not be measured, and treating that as zero
    would understate a review's cost rather than admit it is unknown.
    Failed and retried attempts ARE counted — they were billed.
    """
    table_name = os.environ.get("MODEL_INVOCATIONS_TABLE")
    if not table_name:
        return {}, False, False
    usage: dict[str, dict[str, dict[str, int]]] = {}
    try:
        table = dynamodb_resource.Table(table_name)
        resp = table.scan(Limit=max_rows)
        rows = list(resp.get("Items", []))
        while len(rows) < max_rows and "LastEvaluatedKey" in resp:
            resp = table.scan(
                ExclusiveStartKey=resp["LastEvaluatedKey"], Limit=max_rows - len(rows)
            )
            rows.extend(resp.get("Items", []))
    except Exception:  # noqa: BLE001 - an absent/unreadable optional ledger is not a 500
        return {}, False, False

    truncated = len(rows) >= max_rows and "LastEvaluatedKey" in resp
    rows = rows[:max_rows]
    if truncated and rows:
        # The cut can land in the MIDDLE of one review's rows, and pricing a
        # review from only some of its passes understates it — the exact
        # failure `_priced_review_usd_cents` exists to prevent, and an
        # understated review both escapes the flag and drags the median
        # down. A DynamoDB scan returns the rows sharing a partition key
        # (here `review_id`) together, so the cut falls inside at most one
        # review: that one is dropped rather than priced from a fragment.
        boundary_review_id = rows[-1].get("review_id")
        rows = [row for row in rows if row.get("review_id") != boundary_review_id]

    for row in rows:
        review_id = row.get("review_id")
        pass_name = row.get("pass_name")
        if not review_id or not pass_name:
            continue
        actual_in = _as_int(row.get("actual_input_tokens"))
        actual_out = _as_int(row.get("actual_output_tokens"))
        if actual_in is None and actual_out is None:
            continue
        bucket = usage.setdefault(str(review_id), {}).setdefault(
            str(pass_name), {"input_tokens": 0, "output_tokens": 0}
        )
        bucket["input_tokens"] += actual_in or 0
        bucket["output_tokens"] += actual_out or 0
    return usage, True, truncated


def _priced_review_usd_cents(
    passes: dict[str, dict[str, int]],
    rates: tuple[float, float, float, float],
) -> int:
    """One review's whole ledgered cost in cents — EVERY pass, not a
    hand-picked two.

    `passes` is one review's bucket from `_pass_usage_by_review`. Each
    pass's tokens land in the critic slot or the primary slot per
    `_CRITIC_RATE_PASSES`, and the two totals are priced by the same
    `reviews.compute_actual_usd_cents_from_usage` the settlement path uses,
    so a review that ran the bounded re-quote repair (#569), a Floor
    judgement, or a cover note is priced for those tokens instead of being
    credited with them for free.

    `rates` is the request's SINGLE already-resolved
    `reviews._active_provider_rates` tuple, passed in rather than resolved
    here: that function re-reads the model-policy JSON file and the admin
    model-selection DynamoDB row on every call, so resolving it per review
    made one dashboard poll an N+1 over the whole ledger (see
    `_cost_outliers`). No `dynamodb_resource` is needed here as a result —
    with `rates` supplied there is nothing left for the pricing function to
    look up.
    """
    primary_usage = {"input_tokens": 0, "output_tokens": 0}
    critic_usage = {"input_tokens": 0, "output_tokens": 0}
    for pass_name, tokens in passes.items():
        bucket = critic_usage if pass_name in _CRITIC_RATE_PASSES else primary_usage
        bucket["input_tokens"] += int(tokens.get("input_tokens", 0))
        bucket["output_tokens"] += int(tokens.get("output_tokens", 0))
    return reviews_module.compute_actual_usd_cents_from_usage(
        primary_usage, critic_usage, None, rates=rates
    )


def _cost_outliers(dynamodb_resource: Any, max_rows: int) -> dict[str, Any]:
    """Per-review cost outliers — the possible injection/runaway signal
    ARCHITECTURE.md -> "Security posture" says is flagged.

    Cost is priced from the #414 ledger's REAL provider-reported usage
    through `reviews.compute_actual_usd_cents_from_usage`, the same pricing
    function `pipeline_runner` settles a review's spend with — but this is a
    COMPARABLE figure, not the settled one. Settlement prices the model
    client's `cumulative_usage` grand total in a single slot
    (`pipeline_runner._actual_cents_from_client`), which bills the critic's
    tokens at primary rates; this reconstructs the same review pass by pass
    from the ledger and bills the critic pass at critic rates. The two can
    therefore differ by the primary/critic rate spread. What matters here is
    that every review in the sample is priced the SAME way as every other,
    so the multiple-of-median comparison is sound (see
    `_priced_review_usd_cents`: no pass is dropped).

    The baseline is the MEDIAN priced review, not the mean: one runaway
    review drags a mean up far enough to hide itself, which is the exact
    failure mode this flag exists to catch. Under
    `COST_OUTLIER_MIN_SAMPLE` priced reviews nothing is flagged at all (see
    that constant).

    BOUNDED, AND HONEST ABOUT IT. The median is taken over a bounded SAMPLE
    of the ledger — at most `max_rows` rows, clamped by the caller — not
    over every review ever run, because the ledger is append-only with one
    row per model attempt and an uncapped read would grow without limit for
    the life of the deployment (COST_OUTLIER_LEDGER_ROWS_DEFAULT explains
    why the cap is rows rather than a time window). The payload therefore
    reports `sample_row_cap` and `truncated` alongside `sample_size`, so an
    operator reading "median" knows exactly what it is the median of.

    ONE RATE RESOLUTION PER REQUEST. `reviews._active_provider_rates`
    re-reads `model-policy/openrouter.json` and the admin model-selection
    DynamoDB row on EVERY call, so pricing each review through
    `compute_actual_usd_cents_from_usage`'s own resolution meant one file
    load and one DynamoDB read PER REVIEW in the sample. It is resolved
    exactly once here and threaded into `_priced_review_usd_cents`; the
    figures are identical (same tuple, same pricing function), the read
    amplification is not.
    """
    usage_by_review, available, truncated = _pass_usage_by_review(dynamodb_resource, max_rows)
    if not available:
        return {
            "available": False,
            "multiple": COST_OUTLIER_MULTIPLE,
            "median_usd_cents": None,
            "sample_size": 0,
            "sample_row_cap": max_rows,
            "truncated": False,
            "reviews": [],
        }

    # Resolved once, AFTER the availability check — a deployment with no
    # ledger pays for neither the policy load nor the settings read.
    rates = reviews_module._active_provider_rates(dynamodb_resource)
    priced: dict[str, int] = {
        review_id: _priced_review_usd_cents(passes, rates)
        for review_id, passes in usage_by_review.items()
    }
    non_zero = [cents for cents in priced.values() if cents > 0]
    median_cents = int(statistics.median(non_zero)) if non_zero else 0

    outliers: list[dict[str, Any]] = []
    if len(non_zero) >= COST_OUTLIER_MIN_SAMPLE and median_cents > 0:
        threshold = median_cents * COST_OUTLIER_MULTIPLE
        for review_id, cents in priced.items():
            if cents >= threshold:
                outliers.append(
                    {
                        "review_id": review_id,
                        "actual_usd_cents": cents,
                        "multiple_of_median": round(cents / median_cents, 2),
                    }
                )
    outliers.sort(key=lambda o: o["actual_usd_cents"], reverse=True)

    return {
        "available": True,
        "multiple": COST_OUTLIER_MULTIPLE,
        "median_usd_cents": median_cents,
        "sample_size": len(non_zero),
        "sample_row_cap": max_rows,
        "truncated": truncated,
        "reviews": outliers,
    }


def get_release_activity(
    caller_user_row: dict[str, Any],
    dynamodb_resource: Any,
    limit: Any = RELEASE_ACTIVITY_DEFAULT_LIMIT,
    outlier_sample_rows: Any = COST_OUTLIER_LEDGER_ROWS_DEFAULT,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """GET /api/admin/releases — recent activations/rollbacks with bundle
    hashes, plus the per-review cost-outlier flag.

    Two things #91's release tile asks for on one read: WHAT was promoted or
    rolled back (from the append-only audit trail
    `playbook_versions.activate_release_bundle` / `rollback_release_bundle`
    write, so the feed reflects the acts themselves and not a mutable
    current-state row), and WHICH reviews cost anomalously more than the
    deployment's median — flagged, per ARCHITECTURE.md, as a possible
    injection or runaway signal.

    `limit` is clamped into [1, RELEASE_ACTIVITY_MAX_LIMIT] and bounds the
    release feed. The cost-outlier block is bounded separately, by
    `outlier_sample_rows` — clamped into [1, COST_OUTLIER_LEDGER_ROWS_MAX] —
    which caps how many #414 ledger rows one request may read. Its baseline
    is therefore the median of that bounded SAMPLE, never of every review
    ever run: `cost_outliers.sample_row_cap` and `cost_outliers.truncated`
    report the bound and whether it bit. (`COST_OUTLIER_MIN_SAMPLE` is a
    floor for FLAGGING, not a bound on work — the two are unrelated.)

    Raises HTTPException(403) for a non-admin caller.
    """
    require_admin(caller_user_row, "Admin privilege required to view release activity.")
    bounded = _clamp(limit, RELEASE_ACTIVITY_DEFAULT_LIMIT, 1, RELEASE_ACTIVITY_MAX_LIMIT)
    sample_rows = _clamp(
        outlier_sample_rows, COST_OUTLIER_LEDGER_ROWS_DEFAULT, 1, COST_OUTLIER_LEDGER_ROWS_MAX
    )
    now = time.time() if now_epoch is None else now_epoch

    rows = _release_audit_rows(dynamodb_resource, bounded, now)
    return json_safe(
        {
            "generated_at": int(now),
            "releases": [_release_activity_view(item) for item in rows],
            "cost_outliers": _cost_outliers(dynamodb_resource, sample_rows),
        }
    )
