#!/usr/bin/env python3
"""
TDD slice test for issue #262: "Spend settle uses an unpaginated DynamoDB
Scan -- reservations can silently never settle."

backend/src/pipeline_runner.py::_settle_reservation locates the submission
row that owns a review_id with an unpaginated `Table.scan(FilterExpression=
"review_id = :rid")` and takes `Items[0]`. Scan returns only the first
(<=1MB) page: once the review_submissions table's scanned data outgrows a
single page, a target row that isn't on page 1 is silently never found, so
its reservation is never settled -- a phantom reservation accumulates
against the daily cap forever, and the review's spend is never credited.

This test seeds review_submissions (real moto DynamoDB, matching the CDK
schema in infra/lib/nested/data-stack.ts -- PK idempotency_key, GSI
review_id-index on review_id) with enough large rows that an unpaginated
Scan's first page provably does NOT contain the target, late-inserted
review's row. It then calls the real `_settle_reservation` end-to-end
(including the real `reviews.settle_spend`, against a real moto daily_spend
table) and asserts the reservation was actually settled.

Pre-fix (Scan + Items[0]): the target row is absent from Scan's first page,
so `_settle_reservation` treats it as "no submission" and silently no-ops --
`reservation_released` is never set and `daily_spend.reserved_usd_cents` is
never decremented. This test FAILS on the unmodified tree.

Post-fix (Query via the review_id-index GSI): the lookup is keyed, so it
finds the row regardless of table size or Scan-page ordering. This test
PASSES.

Run: python3 tests/test_settle_reservation_keyed_lookup.py
Exit 0 = pass, 1 = fail.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import os  # noqa: E402

REVIEWS_TABLE = "contract-toaster-reviews-test"
REVIEW_SUBMISSIONS_TABLE = "contract-toaster-review-submissions-test"
DAILY_SPEND_TABLE = "contract-toaster-daily-spend-test"

os.environ.setdefault("REVIEWS_TABLE", REVIEWS_TABLE)
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", REVIEW_SUBMISSIONS_TABLE)
os.environ.setdefault("DAILY_SPEND_TABLE", DAILY_SPEND_TABLE)

import boto3  # noqa: E402
import time  # noqa: E402
from moto import mock_aws  # noqa: E402

import pipeline_runner as pr  # noqa: E402
import reviews  # noqa: E402

TARGET_REVIEW_ID = "00000000-0000-4000-a000-0000000000ff"
TARGET_IDEMPOTENCY_KEY = "idem-target-late-inserted"
TARGET_RESERVATION_ID = "res-target"

# Large enough (well under DynamoDB's 400KB per-item cap) that a handful of
# rows exceeds a Scan's ~1MB single-page limit, forcing real pagination.
_FILLER_BYTES = 350_000
_FILLER_COUNT = 5


class TestSettleReservationKeyedLookup(unittest.TestCase):
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
        self.ddb.create_table(
            TableName=DAILY_SPEND_TABLE,
            KeySchema=[{"AttributeName": "spend_date", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "spend_date", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        self.subs_table = self.ddb.Table(REVIEW_SUBMISSIONS_TABLE)
        self.spend_table = self.ddb.Table(DAILY_SPEND_TABLE)

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
                    "spend_reservation_id": f"res-filler-{i}",
                }
            )

        # The target row: late-inserted, owns TARGET_REVIEW_ID, has an
        # unsettled reservation.
        self.subs_table.put_item(
            Item={
                "idempotency_key": TARGET_IDEMPOTENCY_KEY,
                "review_id": TARGET_REVIEW_ID,
                "spend_reservation_id": TARGET_RESERVATION_ID,
            }
        )

        # Pre-existing reservation against today's daily_spend row, matching
        # what reserve_spend() would have written at submission time.
        reservation_amount_cents = reviews.compute_worst_case_reservation_usd_cents()
        spend_date = time.strftime("%Y-%m-%d", time.gmtime())
        self.spend_date = spend_date
        self.reservation_amount_cents = reservation_amount_cents
        self.spend_table.put_item(
            Item={
                "spend_date": spend_date,
                "reserved_usd_cents": reservation_amount_cents,
                "daily_cap_usd_cents": 2000,
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

    def test_settle_finds_and_settles_a_late_inserted_row_beyond_scan_page_1(self) -> None:
        pr._settle_reservation(TARGET_REVIEW_ID, self.ddb)

        submission = self.subs_table.get_item(
            Key={"idempotency_key": TARGET_IDEMPOTENCY_KEY}
        )["Item"]
        self.assertTrue(
            submission.get("reservation_released"),
            "target submission's reservation was never released -- settle "
            "silently missed the row (unpaginated Scan bug)",
        )

        spend_row = self.spend_table.get_item(Key={"spend_date": self.spend_date})["Item"]
        self.assertEqual(
            spend_row["reserved_usd_cents"],
            0,
            "daily_spend reserved_usd_cents was not decremented -- settle_spend "
            "was never called for the target review",
        )
        self.assertEqual(spend_row.get("settled_usd_cents"), 0)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestSettleReservationKeyedLookup)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
