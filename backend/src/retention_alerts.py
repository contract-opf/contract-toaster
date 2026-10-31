"""
The General Counsel alarm on a retroactive retention reduction (QA
remediation W6.8).

docs/data-handling.md purge invariant 5 promises that a parked reduction
"fires a GC alarm immediately". Nothing fired. This module is the one place
that raises it, called by `retention.request_retention_change` right after a
reduction is parked and audited. Two signals, one per deployment target:

AWS  ONE structured log line, `RETENTION_REDUCTION_REQUESTED {json}`, at
     WARNING. App Runner ships application stdout/stderr to CloudWatch Logs;
     a metric filter on that log group (infra/lib/nested/observability-stack.ts)
     counts the marker and the `contract-toaster-retention-reduction-{env}`
     alarm notifies the alarms SNS topic on any one.

DTS  When `RETENTION_ALERT_URL` is set -- an Uptime Kuma *Push* monitor URL,
     the same mechanism `AUTOPULL_HEARTBEAT_URL` uses for the autopull job
     (deploy/dts/README.md) -- one GET with `status=down&msg=...`, which
     turns that monitor red and notifies whoever it notifies. Optional:
     unset, nothing is sent (the log line is still written).

Neither signal is a gate. A failed push is logged and swallowed; the
reduction is already parked, audited, and cannot apply for 72 hours, so the
alarm failing must not fail the request. Neither carries the admin's
free-text reason (that lives in the audit row): ids, window values, the
requester's id and the apply-at time only.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# The CloudWatch metric filter matches this literal. Changing it silently
# disarms the AWS alarm -- tests/test_retention_alert_w6.py and the infra
# AC4 check both pin it.
LOG_MARKER = "RETENTION_REDUCTION_REQUESTED "

ALERT_URL_ENV = "RETENTION_ALERT_URL"
_PUSH_TIMEOUT_SECONDS = 5


def _open_url(url: str) -> None:
    """One GET, short timeout. Separate so tests can observe it."""
    with urllib.request.urlopen(url, timeout=_PUSH_TIMEOUT_SECONDS) as response:  # noqa: S310 - scheme checked by caller
        response.read(0)


def _push_url(base: str, message: str) -> str | None:
    """`base` with `status=down&msg=<message>` merged into its query, or None
    for anything that is not an http(s) URL with a host."""
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in ("status", "msg")]
    query += [("status", "down"), ("msg", message)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def announce_reduction_request(pending: dict[str, Any], before_window: Any) -> None:
    """Raise the GC alarm for one parked reduction. Never raises."""
    payload = {
        "pending_id": pending.get("pending_id", ""),
        "requested_by": pending.get("requested_by", ""),
        "before_retention_window_days": before_window,
        "after_retention_window_days": pending.get("new_window_days"),
        "apply_after": pending.get("apply_after"),
    }
    logger.warning("%s%s", LOG_MARKER, json.dumps(payload, sort_keys=True, default=str))

    base = os.environ.get(ALERT_URL_ENV, "").strip()
    if not base:
        return
    apply_at = time.strftime(
        "%Y-%m-%d %H:%M UTC", time.gmtime(float(pending.get("apply_after") or 0))
    )
    message = (
        f"Retention reduction requested: {before_window} -> "
        f"{pending.get('new_window_days')} days, applies {apply_at} unless "
        f"cancelled (pending {str(pending.get('pending_id', ''))[:8]})"
    )
    url = _push_url(base, message)
    if url is None:
        logger.warning("RETENTION_ALERT: %s is not an http(s) URL; no push sent", ALERT_URL_ENV)
        return
    try:
        _open_url(url)
    except Exception:  # noqa: BLE001 -- the alarm is best-effort; the reduction is parked either way
        # Not the exception text: a provider error can echo the URL, and the
        # push URL carries the monitor's token.
        logger.warning("RETENTION_ALERT: the push to %s failed", ALERT_URL_ENV)
