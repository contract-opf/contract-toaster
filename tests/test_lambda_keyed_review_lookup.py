#!/usr/bin/env python3
"""
Regression test for the issue #262 follow-up: the same unpaginated
Scan-by-review_id lookup that #262 fixed in backend/src/pipeline_runner.py
also existed in two Lambda handlers on the same review_submissions table:

  - infra/lambda/persist/handler.py::_find_submission_for_review
  - infra/lambda/orphan_reconciler/handler.py::_find_submission_for_review

Both used `Table.scan(FilterExpression="review_id = :rid")` + `Items[0]`.
Scan returns only the first (<=1MB) page: once the table's scanned data
outgrows a single page, a target row that isn't on page 1 is silently never
found. For persist that means a completed review's reservation never
settles; for the orphan reconciler it means a dead execution's reservation
is never released. Either way a phantom reservation accumulates against the
daily cap forever.

This test seeds review_submissions (real moto DynamoDB, matching the CDK
schema in infra/lib/nested/data-stack.ts -- PK idempotency_key, GSI
review_id-index on review_id) with enough large rows that an unpaginated
Scan's first page provably does NOT contain the target, late-inserted
review's row, then asserts each handler's lookup still finds it.

Pre-fix (Scan + Items[0]): the target row is absent from Scan's first page,
so each lookup returns None. This test FAILS on the unmodified tree.

Post-fix (Query via the review_id-index GSI, same query-with-scan-fallback
convention as pipeline_runner.py::_find_submission_by_review_id): the
lookup is keyed, so it finds the row regardless of table size or Scan-page
ordering. This test PASSES.

Companion to tests/test_settle_reservation_keyed_lookup.py (which covers
the backend copy fixed in #262).

Run: python3 tests/test_lambda_keyed_review_lookup.py
Exit 0 = pass, 1 = fail.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PERSIST_HANDLER_PATH = REPO_ROOT / "infra" / "lambda" / "persist" / "handler.py"
RECONCILER_HANDLER_PATH = (
    REPO_ROOT / "infra" / "lambda" / "orphan_reconciler" / "handler.py"
)

REVIEW_SUBMISSIONS_TABLE = "contract-toaster-review-submissions-test"

# Table names are read from the environment at module import time, so they
# must be set before _load_module() below runs.
os.environ["REVIEW_SUBMISSIONS_TABLE"] = REVIEW_SUBMISSIONS_TABLE
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("SEMAPHORE_TABLE", "contract-toaster-semaphore-test")
os.environ.setdefault("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:0:stateMachine:t")
# moto needs a region and (fake) credentials for boto3.resource("dynamodb").
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402


def _load_module(path: Path, module_name: str):
    """Both handlers are files named handler.py in sibling Lambda packages,
    so load each under its own module name rather than via sys.path."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_persist = _load_module(PERSIST_HANDLER_PATH, "_persist_keyed_lookup_under_test")
_reconciler = _load_module(
    RECONCILER_HANDLER_PATH, "_reconciler_keyed_lookup_under_test"
)

TARGET_REVIEW_ID = "00000000-0000-4000-a000-0000000000ff"
TARGET_IDEMPOTENCY_KEY = "idem-target-late-inserted"

# Large enough (well under DynamoDB's 400KB per-item cap) that a handful of
# rows exceeds a Scan's ~1MB single-page limit, forcing real pagination.
_FILLER_BYTES = 350_000
_FILLER_COUNT = 5


class TestLambdaKeyedReviewLookup(unittest.TestCase):
    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.addCleanup(self._mock_aws.stop)

        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")

        self.ddb.create_table(
            TableName=REVIEW_SUBMISSIONS_TABLE,
            KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "idempotency_key", "AttributeType": "S"},
                {"AttributeName": "review_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "review_id-index",
                    "KeySchema": [{"AttributeName": "review_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.subs_table = self.ddb.Table(REVIEW_SUBMISSIONS_TABLE)

        # Seed many large, earlier submissions (unrelated review_ids) so an
        # unpaginated Scan's first page fills up before it reaches the
        # target row, which is written LAST.
        filler = "x" * _FILLER_BYTES
        for i in range(_FILLER_COUNT):
            self.subs_table.put_item(
                Item={
                    "idempotency_key": f"idem-filler-{i}",
                    "review_id": f"review-filler-{i}",
                    "filler": filler,
                }
            )

        self.subs_table.put_item(
            Item={
                "idempotency_key": TARGET_IDEMPOTENCY_KEY,
                "review_id": TARGET_REVIEW_ID,
                "spend_reservation_id": "res-target",
            }
        )

        # Sanity precondition for the test itself: prove the target row is
        # genuinely NOT on an unpaginated Scan's first page (otherwise this
        # test wouldn't actually exercise the bug).
        scan_resp = self.subs_table.scan(
            FilterExpression="review_id = :rid",
            ExpressionAttributeValues={":rid": TARGET_REVIEW_ID},
        )
        assert "LastEvaluatedKey" in scan_resp, (
            "test setup did not produce a multi-page Scan -- increase "
            "_FILLER_COUNT / _FILLER_BYTES"
        )
        assert scan_resp.get("Items", []) == [], (
            "test setup's target row unexpectedly landed on Scan's first "
            "page -- this test would not exercise the bug"
        )

    def test_persist_lookup_finds_row_beyond_scan_page_1(self) -> None:
        submission = _persist._find_submission_for_review(TARGET_REVIEW_ID, self.ddb)
        self.assertIsNotNone(
            submission,
            "persist handler's lookup missed a row beyond Scan page 1 "
            "(unpaginated Scan bug) -- its reservation would never settle",
        )
        self.assertEqual(submission["idempotency_key"], TARGET_IDEMPOTENCY_KEY)

    def test_reconciler_lookup_finds_row_beyond_scan_page_1(self) -> None:
        # The reconciler's lookup builds its own boto3 resource via _ddb(),
        # which moto's active mock intercepts.
        submission = _reconciler._find_submission_for_review(TARGET_REVIEW_ID)
        self.assertIsNotNone(
            submission,
            "orphan reconciler's lookup missed a row beyond Scan page 1 "
            "(unpaginated Scan bug) -- the dead execution's reservation "
            "would never be released",
        )
        self.assertEqual(submission["idempotency_key"], TARGET_IDEMPOTENCY_KEY)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestLambdaKeyedReviewLookup)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
