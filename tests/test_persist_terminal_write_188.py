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
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PERSIST_HANDLER_PATH = REPO_ROOT / "infra" / "lambda" / "persist" / "handler.py"

REVIEWS_TABLE = "contract-toaster-reviews-test"
REVIEW_SUBMISSIONS_TABLE = "contract-toaster-review-submissions-test"
DAILY_SPEND_TABLE = "contract-toaster-daily-spend-test"


class ClientError(Exception):
    def __init__(self, error_response=None, operation_name=""):
        self.response = error_response or {}
        super().__init__(str(error_response))


def _stub_third_party() -> None:
    if "botocore" not in sys.modules:
        botocore_mod = types.ModuleType("botocore")
        exceptions_mod = types.ModuleType("botocore.exceptions")
        exceptions_mod.ClientError = ClientError
        botocore_mod.exceptions = exceptions_mod
        sys.modules["botocore"] = botocore_mod
        sys.modules["botocore.exceptions"] = exceptions_mod
    if "boto3" not in sys.modules:
        boto3_mod = types.ModuleType("boto3")

        def _unset(*_a, **_kw):
            raise AssertionError("boto3.resource called without test patching module._ddb")

        boto3_mod.resource = _unset
        sys.modules["boto3"] = boto3_mod


_stub_third_party()

import os  # noqa: E402

os.environ["REVIEWS_TABLE"] = REVIEWS_TABLE
os.environ["REVIEW_SUBMISSIONS_TABLE"] = REVIEW_SUBMISSIONS_TABLE
os.environ["DAILY_SPEND_TABLE"] = DAILY_SPEND_TABLE

ClientErrorRef = sys.modules["botocore.exceptions"].ClientError


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


class FakeScanTable:
    """review_submissions stand-in: scan-by-review_id returns nothing, so the
    spend-settlement branch is a no-op and these tests isolate the terminal
    write."""

    def scan(self, FilterExpression=None, ExpressionAttributeValues=None):
        return {"Items": []}


class FakeDDB:
    def __init__(self, reviews: FakeReviewsTable):
        self._reviews = reviews
        self._subs = FakeScanTable()

    def Table(self, name):
        if name == REVIEWS_TABLE:
            return self._reviews
        return self._subs


def _run(reviews: FakeReviewsTable, event: dict) -> dict:
    _persist._ddb = lambda: FakeDDB(reviews)  # type: ignore[assignment]
    return _persist.handler(dict(event))


class TestPersistTerminalWrite(unittest.TestCase):
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
        result = _run(reviews, event)
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
        _run(reviews, event)
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
        _run(reviews, event)
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
        _run(reviews, event)
        self.assertEqual(reviews.items[REVIEW_ID]["status"], "ERROR")
        self.assertEqual(reviews.items[REVIEW_ID]["failing_stage"], "redline")

    def test_terminal_write_happens_without_a_reservation_to_settle(self) -> None:
        # FakeScanTable returns no submission, so settlement is a no-op; the
        # terminal write must still land (it runs before the settlement guard).
        reviews = FakeReviewsTable({"review_id": REVIEW_ID, "status": "RUNNING"})
        event = {"review_id": REVIEW_ID, "decision": "ACCEPT", "summary": "s"}
        _run(reviews, event)
        self.assertEqual(reviews.items[REVIEW_ID]["status"], "DONE")
        self.assertEqual(reviews.items[REVIEW_ID]["decision"], "ACCEPT")


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestPersistTerminalWrite)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
