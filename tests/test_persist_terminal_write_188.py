#!/usr/bin/env python3
"""
Unit tests for the persist stage's terminal-state write — issue #188.

infra/lambda/persist/handler.py now flips the reviews row RUNNING -> terminal
success state and lands the pipeline result onto it, in addition to settling
the spend reservation (issue #189, covered by
tests/test_spend_reservation_settlement.py). These tests drive the handler
with an in-memory fake DynamoDB (no boto3/live AWS).

Covered:
  1. REQUEST_CHANGE + output_object_written -> status DONE, decision, summary,
     and output_s3_key recorded.
  2. REQUEST_CHANGE WITHOUT output_object_written -> DONE + decision, but
     output_s3_key is NOT recorded (coupling: no key for an unmaterialized
     object, so the UI never shows a download that 404s).
  3. MANUAL_REVIEW_REQUIRED -> that terminal status + reason, no output.
  4. A row already ERROR is not clobbered (conditional write no-op).
  5. Terminal write is independent of spend settlement (still happens when
     there is no reservation to settle), and the handler still returns the
     event unchanged (pass-through contract).

Run: python3 tests/test_persist_terminal_write_188.py
Exit 0 = pass, 1 = fail.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PERSIST_HANDLER_PATH = REPO_ROOT / "infra" / "lambda" / "persist" / "handler.py"
TESTS_DIR = REPO_ROOT / "tests"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

REVIEWS_TABLE = "contract-toaster-reviews-test"
REVIEW_SUBMISSIONS_TABLE = "contract-toaster-review-submissions-test"
DAILY_SPEND_TABLE = "contract-toaster-daily-spend-test"


import os  # noqa: E402

os.environ["REVIEWS_TABLE"] = REVIEWS_TABLE
os.environ["REVIEW_SUBMISSIONS_TABLE"] = REVIEW_SUBMISSIONS_TABLE
os.environ["DAILY_SPEND_TABLE"] = DAILY_SPEND_TABLE
# moto needs a region and (fake) credentials for boto3.resource("dynamodb").
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

# Issue #67: the handler's submission lookup is now a `review_id-index` query
# (`from boto3.dynamodb.conditions import Key`) with no scan fallback left,
# and that import cannot resolve against a bare `types.ModuleType("boto3")`
# stub. Real boto3/botocore, with moto intercepting them.
import boto3  # noqa: E402
from botocore.exceptions import ClientError as ClientErrorRef  # noqa: E402
from moto import mock_aws  # noqa: E402

from ddb_fixtures import create_submissions_table  # noqa: E402


def _load_handler(module_name: str = "_persist_terminal_under_test"):
    spec = importlib.util.spec_from_file_location(module_name, PERSIST_HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_persist = _load_handler()

REVIEW_ID = "00000000-0000-4000-a000-000000000001"


class FakeReviewsTable:
    def __init__(self, initial: dict | None = None):
        self.items: dict[str, dict] = {}
        if initial:
            self.items[initial["review_id"]] = dict(initial)

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                     ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        key = Key["review_id"]
        vals = ExpressionAttributeValues or {}
        current = self.items.get(key, {}).get("status")
        # ConditionExpression: attribute_not_exists(#status) OR #status <> :error
        if ConditionExpression and ":error" in vals:
            if current is not None and current == vals[":error"]:
                raise ClientErrorRef(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
        item = self.items.setdefault(key, {"review_id": key})
        item["status"] = vals[":status"]
        item["updated_at"] = vals[":now"]
        if ":decision" in vals:
            item["decision"] = vals[":decision"]
        if ":summary" in vals:
            item["summary"] = vals[":summary"]
        if ":reason" in vals:
            item["reason"] = vals[":reason"]
        if ":okey" in vals:
            item["output_s3_key"] = vals[":okey"]
        if ":has_ar" in vals:
            item["has_analysis_report"] = vals[":has_ar"]
        if ":arr" in vals:
            item["analysis_report_reason"] = vals[":arr"]


class FakeDDB:
    """The REAL (moto) review_submissions table plus the in-memory reviews
    stand-in this file uses to observe the terminal write's exact
    UpdateExpression."""

    def __init__(self, reviews: FakeReviewsTable, submissions):
        self._reviews = reviews
        self._subs = submissions

    def Table(self, name):
        if name == REVIEWS_TABLE:
            return self._reviews
        return self._subs


class TestPersistTerminalWrite(unittest.TestCase):
    """Issue #67: the review_submissions table is a real (moto) one carrying
    `review_id-index`. It used to be a scan-only stand-in whose whole job was
    to make the handler take a duck-typed scan fallback a real boto3 Table can
    never reach. Left EMPTY here on purpose: an empty `review_id-index` query
    is the "no reservation to settle" state, which is what isolates these
    tests to the terminal write."""

    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.addCleanup(self._mock_aws.stop)
        self.boto_ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.submissions = create_submissions_table(self.boto_ddb)

    def _run(self, reviews: FakeReviewsTable, event: dict) -> dict:
        _persist._ddb = lambda: FakeDDB(reviews, self.submissions)  # type: ignore[assignment]
        return _persist.handler(dict(event))

    def test_request_change_with_object_written_records_done_and_key(self) -> None:
        reviews = FakeReviewsTable({"review_id": REVIEW_ID, "status": "RUNNING"})
        event = {
            "review_id": REVIEW_ID,
            "decision": "REQUEST_CHANGE",
            "reason": None,
            "summary": "Mock review: canned REQUEST_CHANGE result.",
            "output_s3_key": f"outputs/{REVIEW_ID}/out.docx",
            "output_object_written": True,
        }
        result = self._run(reviews, event)
        row = reviews.items[REVIEW_ID]
        self.assertEqual(row["status"], "DONE")
        self.assertEqual(row["decision"], "REQUEST_CHANGE")
        self.assertEqual(row["summary"], event["summary"])
        self.assertEqual(row["output_s3_key"], f"outputs/{REVIEW_ID}/out.docx")
        self.assertEqual(result, event, "persist must pass the event through unchanged")

    def test_request_change_without_object_written_omits_key(self) -> None:
        reviews = FakeReviewsTable({"review_id": REVIEW_ID, "status": "RUNNING"})
        event = {
            "review_id": REVIEW_ID,
            "decision": "REQUEST_CHANGE",
            "summary": "x",
            "output_s3_key": f"outputs/{REVIEW_ID}/out.docx",
            # no output_object_written -> the object was never materialized
        }
        self._run(reviews, event)
        row = reviews.items[REVIEW_ID]
        self.assertEqual(row["status"], "DONE")
        self.assertNotIn(
            "output_s3_key",
            row,
            "output_s3_key must NOT be recorded when the object was not written",
        )

    def test_manual_review_required_sets_that_status_and_reason(self) -> None:
        reviews = FakeReviewsTable({"review_id": REVIEW_ID, "status": "RUNNING"})
        event = {
            "review_id": REVIEW_ID,
            "decision": "MANUAL_REVIEW_REQUIRED",
            "reason": "playbook_coming_soon",
            "summary": "playbook coming soon - separate playbook later.",
            "output_s3_key": None,
        }
        self._run(reviews, event)
        row = reviews.items[REVIEW_ID]
        self.assertEqual(row["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(row["reason"], "playbook_coming_soon")
        self.assertNotIn("output_s3_key", row)

    def test_error_row_is_not_clobbered(self) -> None:
        reviews = FakeReviewsTable({"review_id": REVIEW_ID, "status": "ERROR",
                                    "failing_stage": "redline"})
        event = {
            "review_id": REVIEW_ID,
            "decision": "REQUEST_CHANGE",
            "output_s3_key": f"outputs/{REVIEW_ID}/out.docx",
            "output_object_written": True,
        }
        # Must not raise; ERROR must survive.
        self._run(reviews, event)
        self.assertEqual(reviews.items[REVIEW_ID]["status"], "ERROR")
        self.assertEqual(reviews.items[REVIEW_ID]["failing_stage"], "redline")

    def test_terminal_write_happens_without_a_reservation_to_settle(self) -> None:
        # FakeScanTable returns no submission, so settlement is a no-op; the
        # terminal write must still land (it runs before the settlement guard).
        reviews = FakeReviewsTable({"review_id": REVIEW_ID, "status": "RUNNING"})
        event = {"review_id": REVIEW_ID, "decision": "ACCEPT", "summary": "s"}
        self._run(reviews, event)
        self.assertEqual(reviews.items[REVIEW_ID]["status"], "DONE")
        self.assertEqual(reviews.items[REVIEW_ID]["decision"], "ACCEPT")


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestPersistTerminalWrite)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
