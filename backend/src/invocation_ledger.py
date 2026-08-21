"""
Model-invocation ledger persistence (issue #414).

`scripts/review_spine.py::run_review` and the pass modules it composes
(`scripts/primary_review_pass.py::run_primary_pass`,
`scripts/critic_review_pass.py::run_critic_pass`) already ledger EVERY model
invocation attempt -- success, retry, or terminal failure alike -- as a
`model_client.ModelInvocationRecord` via an injected `ledger_write` callable
(see those modules' own docstrings). Before this issue, `run_review`
defaulted `ledger_write` to a no-op and `backend/src/pipeline_runner.py`
(`run_real_pipeline`) never supplied one, so on the real (OpenRouter)
pipeline every one of those records was silently dropped -- zero rows ever
persisted, even though the plumbing to write them was fully wired end to
end.

This module is that missing sink: `make_ledger_write` returns the callable
`run_real_pipeline` now passes into `run_review`. It owns exactly one
DynamoDB table, named by env `MODEL_INVOCATIONS_TABLE`:

  Partition key: review_id
  Sort key:      record_id = "{pass_name}#{attempt_number:02d}#{timestamp}"

METADATA-ONLY INVARIANT: only `ModelInvocationRecord`'s own dataclass fields
are ever stored (plus the derived `record_id` sort key) -- no prompt, no
response, no document text. `dataclasses.asdict` is used precisely because
it can only ever produce that field set; there is no code path here that
could accidentally widen it to carry substance.

Ledger write failures must NEVER fail a review (see `run_review`'s
`ledger_write` contract): every exception from resolving the table name,
building the item, or the `put_item` itself is caught here and logged as a
substance-free warning -- the review's own outcome is completely unaffected.
This is also how the mock pipeline (`MODEL_PROVIDER=mock`) gets to require
no `MODEL_INVOCATIONS_TABLE` at all: it never calls `run_review`, so this
module is never reached, and a real-pipeline deployment that somehow left
the env var unset degrades to "no rows written" rather than a failed review.

Environment variables consumed:
  MODEL_INVOCATIONS_TABLE   DynamoDB model-invocations ledger table name
                             (PK: review_id, SK: record_id)
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from decimal import Decimal
from typing import Any, Callable

try:  # production runs `src.main`; tests put backend/src on sys.path
    from src import model_client
except ImportError:  # pragma: no cover
    import model_client  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


def _record_to_item(record: "model_client.ModelInvocationRecord") -> dict[str, Any]:
    """`ModelInvocationRecord` -> a DynamoDB-`put_item`-ready dict.

    `dataclasses.asdict` is the metadata-only invariant enforced by
    construction -- its output is exactly the record's own field names, and
    nothing else can be added here without editing this function AND the
    dataclass. `timestamp` (the only float field) is converted to `Decimal`
    -- boto3's DynamoDB resource API rejects a native Python `float` outright
    ("Float types are not supported. Use Decimal types instead."), and
    `Decimal(str(x))` round-trips a float exactly rather than introducing
    binary-float imprecision the way `Decimal(x)` directly would.
    """
    item = asdict(record)
    item["timestamp"] = Decimal(str(item["timestamp"]))
    item["record_id"] = f"{record.pass_name}#{record.attempt_number:02d}#{record.timestamp}"
    return item


def make_ledger_write(
    review_id: str, dynamodb_resource: Any
) -> Callable[["model_client.ModelInvocationRecord"], None]:
    """Build the `ledger_write` callable `run_real_pipeline` passes into
    `review_spine.run_review` for ONE review.

    `review_id` is accepted (rather than reading `record.review_id` alone)
    so every row this callable ever writes is provably scoped to the review
    it was built for, and so a caller mismatch (a record from a different
    review accidentally handed to this closure) is loud in a log line
    rather than silently persisted under someone else's partition key.

    Returns a callable that NEVER raises -- see this module's docstring
    "Ledger write failures must NEVER fail a review".
    """

    def _ledger_write(record: "model_client.ModelInvocationRecord") -> None:
        if record.review_id != review_id:
            logger.warning(
                "model-invocation ledger write skipped: record review_id did not "
                "match the ledger's own review_id"
            )
            return
        try:
            table_name = os.environ["MODEL_INVOCATIONS_TABLE"]
            dynamodb_resource.Table(table_name).put_item(Item=_record_to_item(record))
        except Exception:  # noqa: BLE001 - never let a ledger write fail the review
            logger.warning(
                "model-invocation ledger write failed for review_id=%s (pass=%s, "
                "attempt=%s) -- review outcome unaffected",
                review_id,
                record.pass_name,
                record.attempt_number,
            )

    return _ledger_write
