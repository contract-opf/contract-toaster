"""
Per-attempt model diagnostics persistence (issue #669).

`scripts/primary_review_pass.py::run_primary_pass` and
`scripts/critic_review_pass.py::run_critic_pass` already BUILD a structured
diagnostic for every non-successful model attempt (issue #643) -- the
attempt's full `last_error`, and for a `schema_invalid` rejection the exact
path/validator/offending value `validate_model_response` rejected -- and
`scripts/review_spine.py::run_review` threads the `attempt_diagnostic_write`
sink through to both passes. But `run_review` DEFAULTS that sink to `None`,
and `backend/src/pipeline_runner.py::run_real_pipeline` -- the one call site
that runs every real review -- never supplied one. Both passes gate on
`attempt_diagnostic_write is not None`, so in production the diagnostic was
never even constructed: the one artifact that can say WHY a pass exhausted
its retries existed only inside the offline eval harness
(`scripts/live_smoke_eval.py`'s `--dump-dir` path).

This is the identical omission issue #414 found for the ledger, in the same
`run_review` call, one argument away -- see `invocation_ledger.py`'s module
docstring, which is this module's direct model.

WHERE IT GOES, AND WHY NOT DYNAMODB
-----------------------------------
One S3 object per review, `outputs/{review_id}/attempt-diagnostics.json` in
`OUTPUTS_BUCKET` -- the SAME prefix the redline and the #416 analysis
artifact already use. Three consequences, all deliberate:

  * Retention needs no new machinery. Both purge implementations resolve
    their S3 targets by LISTING `outputs/{review_id}/` (see
    `backend/src/retention.py::_list_keys` at the `outputs/{review_id}/`
    call site) rather than by a hardcoded key list, so this object is
    destroyed with the document like every other object under that prefix.
    A DynamoDB row would instead have inherited the 35-day PITR tail
    docs/data-handling.md calls out as an accepted limitation -- for the
    most substance-bearing debug payload in the system.

  * It is NOT in the model-invocation ledger. `invocation_ledger.py`'s
    METADATA-ONLY invariant is the whole reason issue #573 reduced each
    attempt's error to a closed-vocabulary `error_token` there, and
    `admin_dashboard._pass_usage_by_review` SCANS that table for the
    operator dashboard. Putting raw provider error text and echoed model
    output into rows that a dashboard scan reads would break both.

  * NO ROUTE SURFACES IT. The Diagnostics tab (#443) reads the `reviews`
    row and the ledger; `reviews.load_analysis_artifact` reads exactly the
    one key the row's own `analysis_s3_key` names. Nothing writes an
    `attempt_diagnostics_s3_key` attribute -- deliberately, and that is
    this module's answer to #443's disclosure line: the raw text is
    persisted where an operator with deployment-level object-store access
    can read it at a well-known key, and is reachable through no admin
    projection at all. The CLASSIFIED token an operator reads in the UI is
    issue #665's `reason`, not this. Do not add a route that serves this
    object without re-opening that decision.

WHAT IS STORED
--------------
A fixed allowlist (`_DIAGNOSTIC_FIELDS` / `_SCHEMA_ERROR_FIELDS`), not
whatever the passes happen to hand over -- the same posture as
`pipeline_runner._ANALYSIS_FIELDS`, so a future key added to the pass-side
diagnostic cannot start reaching the data plane by accident.

BOUNDED, in three independent places, because "attempts are already
bounded" is a property of today's retry budget rather than of this writer:
at most `MAX_DIAGNOSTICS_PER_REVIEW` records are kept (further ones are
counted in `dropped_count`, never silently lost), every stored string is
clipped to `MAX_FIELD_CHARS` with an explicit truncation marker, and the
object is REPLACED on every call rather than appended to, so it cannot grow
across the retries of a single review beyond those two caps.

NEVER FAILS A REVIEW. Exactly the contract `_write_progress_stage` and
`invocation_ledger.make_ledger_write` hold: every exception -- resolving the
bucket name, serialising, the `put_object` itself -- is caught here and
logged as a substance-free warning. A diagnostic is a side channel; a review
that fails BECAUSE its debug sink failed would be strictly worse than one
that cannot be diagnosed.

Environment variables consumed:
  OUTPUTS_BUCKET   the reviews output bucket (shared with the redline and
                   the #416 analysis artifact)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

# The pass-side diagnostic's own keys (scripts/primary_review_pass.py
# ~L3947, scripts/critic_review_pass.py ~L534). An allowlist rather than a
# passthrough: see "WHAT IS STORED" above.
_DIAGNOSTIC_FIELDS = (
    "review_id",
    "pass_name",
    "attempt_number",
    "outcome",
    "error_token",
    "error_message",
)

# `validate_model_response`'s `schema_error_sink` shape
# (scripts/primary_review_pass.py ~L3180).
_SCHEMA_ERROR_FIELDS = ("message", "path", "validator", "offending_value")

# Records kept per review. Today's budget is at most three attempts per pass
# (`MAX_RETRIES_PER_PASS` + `MAX_TRUNCATION_RETRIES_PER_PASS`, both 1) across
# two passes, i.e. at most six non-successful attempts -- so this cap is
# ~3x headroom and is never reached by the current pipeline. It exists so
# that raising a retry budget, or adding a third pass, cannot turn this
# writer into an unbounded one without someone editing this line.
MAX_DIAGNOSTICS_PER_REVIEW = 20

# Per-string clip. `offending_value` is already bounded upstream by
# `primary_review_pass._debug_safe_value` (2000 chars); `error_message` is
# the pass's raw `last_error`, which carries whatever the provider returned
# and has NO upstream bound at all -- an HTML error page or a multi-megabyte
# provider payload reaches this sink verbatim. This is the bound for it.
MAX_FIELD_CHARS = 8000

_TRUNCATION_SUFFIX = "... (truncated, {total} chars total)"


def _clipped(value: Any) -> Any:
    """Strings longer than `MAX_FIELD_CHARS` are clipped with a marker that
    reports the TRUE length, so a reader can tell "the model sent 40 chars
    of nonsense" from "the provider sent 4MB of HTML" -- the same
    truncation convention `primary_review_pass._debug_safe_value` uses.
    Non-strings (`attempt_number`) pass through untouched."""
    if not isinstance(value, str) or len(value) <= MAX_FIELD_CHARS:
        return value
    return value[:MAX_FIELD_CHARS] + _TRUNCATION_SUFFIX.format(total=len(value))


def _projected(diagnostic: dict[str, Any]) -> dict[str, Any]:
    """One pass-side diagnostic -> the allowlisted, length-bounded record
    stored in the artifact. Absent keys are simply absent (never a null
    placeholder), matching the pass side, which omits `schema_error`
    entirely on a non-schema failure."""
    record = {
        field: _clipped(diagnostic[field])
        for field in _DIAGNOSTIC_FIELDS
        if field in diagnostic
    }
    schema_error = diagnostic.get("schema_error")
    if isinstance(schema_error, dict):
        record["schema_error"] = {
            field: _clipped(schema_error[field])
            for field in _SCHEMA_ERROR_FIELDS
            if field in schema_error
        }
    return record


def attempt_diagnostics_key(review_id: str) -> str:
    """The one well-known key this artifact ever lands at, under the same
    `outputs/{review_id}/` prefix the redline and the analysis artifact use
    (so the retention purge's prefix scan destroys it with them)."""
    return f"outputs/{review_id}/attempt-diagnostics.json"


def make_attempt_diagnostic_write(
    review_id: str, s3_client: Any
) -> Callable[[dict[str, Any]], None]:
    """Build the `attempt_diagnostic_write` callable `run_real_pipeline`
    passes into `review_spine.run_review` for ONE review.

    `review_id` is accepted (rather than trusted from `diagnostic
    ["review_id"]`) for the same reason `invocation_ledger.make_ledger_write`
    accepts it: every object this callable writes is provably scoped to the
    review it was built for, and a mismatch is a loud log line instead of a
    silent write under another review's prefix.

    WRITE-THROUGH, not batched: the whole accumulated document is re-PUT on
    every call. That costs at most `MAX_DIAGNOSTICS_PER_REVIEW` small PUTs
    per review (in practice at most six, and zero for a review whose every
    attempt succeeds -- the passes only call this sink on a NON-successful
    attempt), and it buys durability on the path that matters: an exception
    raised later in `run_review` -- or a worker killed outright -- still
    leaves every diagnostic produced up to that point on disk. A flush at
    the end of the review would lose exactly the failures hardest to
    reproduce.

    Returns a callable that NEVER raises -- see this module's docstring.
    """
    records: list[dict[str, Any]] = []
    dropped = 0

    def _attempt_diagnostic_write(diagnostic: dict[str, Any]) -> None:
        nonlocal dropped
        try:
            if not isinstance(diagnostic, dict):
                return
            if diagnostic.get("review_id") != review_id:
                logger.warning(
                    "attempt diagnostic skipped: diagnostic review_id did not match "
                    "the sink's own review_id"
                )
                return
            if len(records) >= MAX_DIAGNOSTICS_PER_REVIEW:
                # Counted rather than dropped in silence: an artifact that
                # says "and 4 more" is diagnosable; one that quietly stops
                # at the cap reads as if the review stopped there too.
                dropped += 1
            else:
                records.append(_projected(diagnostic))
            document = {
                "review_id": review_id,
                "diagnostics": records,
                "dropped_count": dropped,
            }
            s3_client.put_object(
                Bucket=os.environ["OUTPUTS_BUCKET"],
                Key=attempt_diagnostics_key(review_id),
                Body=json.dumps(
                    document, sort_keys=True, indent=2, default=str
                ).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:  # noqa: BLE001 - never let a diagnostic write fail the review
            # Substance-free by construction: the diagnostic itself is the
            # thing that carries model output, so nothing from it -- not the
            # message, not the offending value -- is interpolated here.
            logger.warning(
                "attempt diagnostic write failed for review_id=%s -- review "
                "outcome unaffected",
                review_id,
            )

    return _attempt_diagnostic_write
