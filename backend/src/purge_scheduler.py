"""
Retention purge cadence for the Docker Compose (DTS) deployment — issue #509.

## Why this module exists

Issue #454 made the purge sweep itself correct: it deletes input documents and
ties reported success to the object actually being gone. But on DTS nothing
ever invoked it. `main.py` imported only `preview_purge_sweep`;
`retention.run_purge_sweep_now` had no caller anywhere in `backend/src`, and
the compose file shipped no scheduler. Only the AWS Lambda
(`infra/lambda/purge_worker/handler.py`) had a cadence.

So an operator who set a retention window watched the PREVIEW work and
reasonably concluded data was being purged on schedule. It was not: uploaded
contracts and outputs accumulated in MinIO forever. For a product whose whole
retention story is "we don't keep your counterparty's paper longer than N
days", a deployment that silently never purges is a real governance gap — and
an invisible one, because the preview looks healthy either way.

## What this module is NOT

It is not purge logic. It calls `retention.run_purge_sweep_now` — the exact
function #454 hardened, the one the admin API already drives — and does
nothing else. Every invariant that matters (a review's own snapshotted window,
legal hold, delete-then-verify) lives there and is not restated, re-checked,
or reimplemented here. A cadence that carried its own copy of "which reviews
are eligible" would be a second thing to keep in step with the Lambda, which
is precisely the shape of bug this fixes.

## Target gating

DTS only. The AWS target has the Lambda, and starting both would double-sweep
the same rows. The gate reads `config.deploy_target()`, the same value
everything else in this codebase branches on, plus a `PURGE_SWEEP_ENABLED=0`
escape hatch: a cadence an operator cannot stop without editing code is its
own operational problem.

## Failure posture

A sweep that raises costs ONE cycle. It is logged (ids and counts only, never
document substance, never the provider's message) and the loop continues — a
transient S3 blip that silently ended the cadence would restore exactly the
gap this issue is about, and would look completely healthy from outside.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable

from . import config
from . import retention

logger = logging.getLogger(__name__)

# One hour. Retention windows are measured in days, so the cadence only has to
# be fine enough that "purged on schedule" is true to within a rounding error a
# human would not notice; sweeping more often just re-scans the table.
DEFAULT_SWEEP_INTERVAL_SECONDS = 3600

# How long the loop waits between checks of the stop flag. The interval is an
# hour, so sleeping the whole interval in one call would make shutdown take up
# to an hour — this keeps the container's stop prompt without making the sweep
# itself any more frequent.
_STOP_CHECK_SECONDS = 1.0


@dataclass
class PurgeSchedulerHandle:
    """The running scheduler. `stop()` is idempotent and returns immediately;
    the thread is a daemon, so a process that forgets to call it still exits."""

    thread: threading.Thread
    _stop: threading.Event

    def stop(self) -> None:
        self._stop.set()


def sweep_interval_seconds() -> int:
    raw = os.environ.get("PURGE_SWEEP_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SWEEP_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "PURGE_SWEEP_INTERVAL_SECONDS is not an integer; using the default cadence"
        )
        return DEFAULT_SWEEP_INTERVAL_SECONDS
    # A zero or negative interval would spin the CPU scanning the reviews
    # table; refusing it is friendlier than honouring it.
    return value if value > 0 else DEFAULT_SWEEP_INTERVAL_SECONDS


def scheduler_enabled() -> bool:
    """DTS only, and only when not explicitly disabled."""
    if config.deploy_target() != "dts":
        return False
    return os.environ.get("PURGE_SWEEP_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _sweep_loop(
    *,
    s3_client: Any,
    dynamodb_resource: Any,
    interval_seconds: int,
    sweep: Callable[..., dict[str, Any]],
    sleep_fn: Callable[[float], None],
    should_run: Callable[[], bool],
) -> None:
    """The cadence itself, with every moving part injected so it is testable
    without real threads, real sleeping, or real AWS.

    `should_run` is checked BEFORE each sweep rather than after, so a stop
    requested during a sweep takes effect without paying for another one.
    """
    while should_run():
        try:
            result = sweep(s3_client, dynamodb_resource, dry_run=False)
            deleted = len(result.get("deleted_reviews") or [])
            failed = len(result.get("failed_reviews") or [])
            if deleted or failed:
                # Counts only. Which reviews, and what was in them, is the
                # audit trail's job (retention.py writes it) — not a line in
                # the application log.
                logger.info(
                    "PURGE_SWEEP: deleted=%d failed=%d",
                    deleted,
                    failed,
                )
        except Exception:  # noqa: BLE001 - one bad cycle must not end the cadence
            # Deliberately NOT `logger.exception` and deliberately not
            # interpolating the exception: a provider error message can quote
            # a key, and a key can name a document. The fact that a sweep
            # failed is what an operator needs; the next cycle will retry.
            logger.warning("PURGE_SWEEP: a sweep failed and was skipped; will retry")
        sleep_fn(interval_seconds)


def start_purge_scheduler(
    *,
    s3_client: Any,
    dynamodb_resource: Any,
    sweep: Callable[..., dict[str, Any]] | None = None,
) -> PurgeSchedulerHandle | None:
    """Start the DTS purge cadence, or return None where it does not belong.

    None is the ordinary outcome on the AWS target and is not an error — the
    Lambda owns the cadence there.
    """
    if not scheduler_enabled():
        return None

    stop_event = threading.Event()
    interval = sweep_interval_seconds()
    sweep_fn = sweep or retention.run_purge_sweep_now

    def sleep_in_slices(seconds: float) -> None:
        # `Event.wait` returns as soon as the flag is set, so shutdown does not
        # wait out the remaining interval.
        stop_event.wait(min(seconds, _STOP_CHECK_SECONDS))
        remaining = seconds - _STOP_CHECK_SECONDS
        while remaining > 0 and not stop_event.is_set():
            stop_event.wait(min(remaining, _STOP_CHECK_SECONDS))
            remaining -= _STOP_CHECK_SECONDS

    thread = threading.Thread(
        target=_sweep_loop,
        kwargs={
            "s3_client": s3_client,
            "dynamodb_resource": dynamodb_resource,
            "interval_seconds": interval,
            "sweep": sweep_fn,
            "sleep_fn": sleep_in_slices,
            "should_run": lambda: not stop_event.is_set(),
        },
        name="purge-sweep",
        daemon=True,
    )
    thread.start()
    logger.info("PURGE_SWEEP: cadence started (every %ds)", interval)
    return PurgeSchedulerHandle(thread=thread, _stop=stop_event)
