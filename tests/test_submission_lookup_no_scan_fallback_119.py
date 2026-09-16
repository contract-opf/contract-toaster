#!/usr/bin/env python3
"""
Regression test for issue #119: `backend/src/reviews.py::
_find_submission_row_for_review` turned any `review_id-index` query error
(throttle, timeout, or a stand-in table without `.query()`) into a silent
fallback to an unpaginated `Table.scan(FilterExpression="review_id = :rid")`.

That fallback was already removed from the sibling copy of this lookup --
`pipeline_runner.py::_find_submission_by_review_id` -- by issue #67, because a
real boto3 Table always has `.query`, so the branch was dead in production.
Here it was worse than dead: a GSI throttle or timeout, exactly the moment
you least want extra load, was answered by a full-table Scan, which also
only ever sees its first (<=1MB) page -- so `settle_reservation_for_cancel`
could treat a cancelled review's reservation as settled-or-absent while it
sat unresolved past that first page.

Pre-fix: a `.query()` error is swallowed and `.scan()` is called instead --
this test's fake table raises `AssertionError` from `.scan()` to catch that,
and the moto-backed "missing index" case silently returns `None` (a scan
over an empty table) instead of raising. Both FAIL on the unmodified tree.

Post-fix: a `.query()` error propagates to the caller untouched, and `.scan`
is never invoked. This test PASSES.

Run: python3 tests/test_submission_lookup_no_scan_fallback_119.py
Exit 0 = pass, 1 = fail.
"""

import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import os  # noqa: E402

REVIEW_SUBMISSIONS_TABLE = "contract-toaster-review-submissions-test"
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", REVIEW_SUBMISSIONS_TABLE)
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")

import boto3  # noqa: E402
import reviews  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

REVIEW_ID = "00000000-0000-4000-a000-000000000119"


class _QueryRaisesScanAsserts:
    """A stand-in whose `.query()` raises like a throttled/timed-out GSI and
    whose `.scan()` fails the test outright if the lookup ever reaches it --
    exactly the ticket's own reproduction shape."""

    def query(self, **kwargs: Any) -> Any:
        raise RuntimeError("ProvisionedThroughputExceededException: throttled")

    def scan(self, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError(
            "_find_submission_row_for_review fell back to a table scan"
        )


class TestNoScanFallbackOnQueryError(unittest.TestCase):
    def test_a_query_error_propagates_instead_of_falling_back_to_scan(self) -> None:
        with self.assertRaises(RuntimeError):
            reviews._find_submission_row_for_review(
                _QueryRaisesScanAsserts(), REVIEW_ID
            )


class TestNoScanFallbackOnMissingGsi(unittest.TestCase):
    """The real-world trigger: a `review_submissions` table that exists but
    predates `review_id-index` (matches the live-deployment shape reproduced
    in tests/test_review_result_not_clobbered_446.py)."""

    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.addCleanup(self._mock_aws.stop)
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=REVIEW_SUBMISSIONS_TABLE,
            KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "idempotency_key", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.table = self.ddb.Table(REVIEW_SUBMISSIONS_TABLE)
        # Seed a row that a scan-fallback WOULD have found, so a regression
        # back to scan+filter would make this test pass for the wrong
        # reason (silently finding it) rather than the right one (raising).
        self.table.put_item(
            Item={
                "idempotency_key": "idem-1",
                "review_id": REVIEW_ID,
                "spend_reservation_id": "res-1",
            }
        )

    def test_missing_index_raises_rather_than_silently_scanning(self) -> None:
        with self.assertRaises(ClientError):
            reviews._find_submission_row_for_review(self.table, REVIEW_ID)


class TestKeyedLookupStillWorks(unittest.TestCase):
    """The other branch: with a real index and a matching row, the query
    path itself is untouched by this fix."""

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
        self.table = self.ddb.Table(REVIEW_SUBMISSIONS_TABLE)
        self.table.put_item(
            Item={
                "idempotency_key": "idem-1",
                "review_id": REVIEW_ID,
                "spend_reservation_id": "res-1",
            }
        )

    def test_finds_the_row_via_the_gsi(self) -> None:
        found = reviews._find_submission_row_for_review(self.table, REVIEW_ID)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["spend_reservation_id"], "res-1")

    def test_no_match_returns_none(self) -> None:
        found = reviews._find_submission_row_for_review(self.table, "no-such-review")
        self.assertIsNone(found)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        [
            loader.loadTestsFromTestCase(TestNoScanFallbackOnQueryError),
            loader.loadTestsFromTestCase(TestNoScanFallbackOnMissingGsi),
            loader.loadTestsFromTestCase(TestKeyedLookupStillWorks),
        ]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
