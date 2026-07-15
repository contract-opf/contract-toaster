#!/usr/bin/env python3
"""
Unit tests for backend/src/pipeline_runner.py — the DTS in-process pipeline
transport + Phase 1 mock pipeline body. Fully offline (fake DDB/S3, synchronous
pool).

Covered:
  1. InProcessStepFunctionsClient.start_execution enqueues the review, returns
     an executionArn, and raises ExecutionAlreadyExists on a duplicate name —
     duck-typing the boto3 slice ensure_execution_started uses.
  2. run_mock_pipeline (eiaa): PENDING->RUNNING->DONE, output object copied,
     output_s3_key recorded, spend settled.
  3. run_mock_pipeline (registered-without-mock / unregistered):
     MANUAL_REVIEW_REQUIRED, no copy, no key (issue #289: registry-driven,
     not playbook_id-literal-driven -- see TestRunMockPipeline).
  4. A failure inside the body moves the review to ERROR (never wedged RUNNING)
     and still settles the reservation.

Run: python3 tests/test_pipeline_runner_inprocess.py
Exit 0 = pass, 1 = fail.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "daily-spend-test")

import pipeline_runner as pr  # noqa: E402

REVIEW_ID = "00000000-0000-4000-a000-000000000001"


class FakeReviewsTable:
    def __init__(self, status: str = "PENDING"):
        self.item = {"review_id": REVIEW_ID, "status": status}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                     ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        vals = ExpressionAttributeValues or {}
        cur = self.item.get("status")
        if ConditionExpression == "#s = :pending" and cur != vals.get(":pending"):
            raise _conditional()
        if ConditionExpression and ":error" in vals and cur == vals[":error"]:
            raise _conditional()
        if ":running" in vals:
            self.item["status"] = "RUNNING"
        elif ":e" in vals:
            self.item["status"] = "ERROR"
            self.item["failing_stage"] = vals[":stage"]
        else:  # terminal write
            self.item["status"] = vals[":s"]
            self.item["decision"] = vals.get(":d")
            if ":sum" in vals:
                self.item["summary"] = vals[":sum"]
            if ":r" in vals:
                self.item["reason"] = vals[":r"]
            if ":o" in vals:
                self.item["output_s3_key"] = vals[":o"]
        self.item["updated_at"] = vals.get(":now")


class FakeDDB:
    def __init__(self, reviews_table):
        self._reviews = reviews_table

    def Table(self, name):
        return self._reviews


class FakeS3:
    def __init__(self):
        self.copies = []

    def copy_object(self, Bucket, Key, CopySource):
        self.copies.append({"Bucket": Bucket, "Key": Key, "CopySource": CopySource})


def _conditional() -> Exception:
    exc = Exception("ConditionalCheckFailedException")
    exc.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
    return exc


class SyncPool:
    """ThreadPoolExecutor stand-in: submit runs synchronously."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))
        fn(*args)


class TestInProcessClient(unittest.TestCase):
    def test_start_execution_enqueues_and_returns_arn(self) -> None:
        seen = []
        client = pr.InProcessStepFunctionsClient(
            runner=lambda rid, payload: seen.append((rid, payload)), pool=SyncPool()
        )
        out = client.start_execution(
            stateMachineArn="sm", name="exec-1",
            input='{"review_id": "r1", "playbook_id": "eiaa"}',
        )
        self.assertEqual(out["executionArn"], "inprocess:exec-1")
        self.assertEqual(seen, [("r1", {"review_id": "r1", "playbook_id": "eiaa"})])

    def test_duplicate_name_raises_execution_already_exists(self) -> None:
        client = pr.InProcessStepFunctionsClient(runner=lambda r, p: None, pool=SyncPool())
        client.start_execution(stateMachineArn="sm", name="dup", input='{"review_id": "r"}')
        with self.assertRaises(pr.ExecutionAlreadyExists):
            client.start_execution(stateMachineArn="sm", name="dup", input='{"review_id": "r"}')

    def test_exceptions_attribute_matches_ensure_started_contract(self) -> None:
        client = pr.InProcessStepFunctionsClient(runner=lambda r, p: None, pool=SyncPool())
        self.assertIs(client.exceptions.ExecutionAlreadyExists, pr.ExecutionAlreadyExists)


class TestRunMockPipeline(unittest.TestCase):
    def _run(self, playbook_id: str, reviews_table: FakeReviewsTable, s3: FakeS3):
        with patch.object(pr, "_settle_reservation") as settle:
            pr.run_mock_pipeline(
                REVIEW_ID,
                {"review_id": REVIEW_ID, "playbook_id": playbook_id},
                dynamodb_resource=FakeDDB(reviews_table),
                s3_client=s3,
            )
        return settle

    def test_eiaa_reaches_done_with_output_and_settles(self) -> None:
        reviews_table = FakeReviewsTable()
        s3 = FakeS3()
        settle = self._run("eiaa", reviews_table, s3)
        self.assertEqual(reviews_table.item["status"], "DONE")
        self.assertEqual(reviews_table.item["decision"], "REQUEST_CHANGE")
        self.assertEqual(reviews_table.item["output_s3_key"], f"outputs/{REVIEW_ID}/out.docx")
        self.assertEqual(len(s3.copies), 1)
        self.assertEqual(s3.copies[0]["Key"], f"outputs/{REVIEW_ID}/out.docx")
        settle.assert_called_once()

    def test_synthetic_knowledge_is_manual_review_no_output(self) -> None:
        """Issue #289: _mock_decision is registry-driven, not literal-driven
        -- "nda" isn't (and was never) a playbooks/registry.json entry, so
        it now falls into the same unregistered/unknown_playbook bucket as
        any other unregistered id (see test_unknown_playbook_is_manual_review
        below). The "coming soon" MANUAL_REVIEW_REQUIRED branch is exercised
        by a playbook that IS registered but has no mock_output_key yet --
        the real "synthetic-knowledge" entry."""
        reviews_table = FakeReviewsTable()
        s3 = FakeS3()
        self._run("synthetic-knowledge", reviews_table, s3)
        self.assertEqual(reviews_table.item["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(reviews_table.item["reason"], "playbook_coming_soon")
        self.assertNotIn("output_s3_key", reviews_table.item)

    def test_nda_is_manual_review_unknown_playbook(self) -> None:
        """"nda" has no playbooks/registry.json entry, so it is
        indistinguishable from any other unregistered playbook_id (issue
        #289: no playbook_id literal comparisons left in _mock_decision)."""
        reviews_table = FakeReviewsTable()
        s3 = FakeS3()
        self._run("nda", reviews_table, s3)
        self.assertEqual(reviews_table.item["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(reviews_table.item["reason"], "unknown_playbook")
        self.assertNotIn("output_s3_key", reviews_table.item)
        self.assertEqual(s3.copies, [])

    def test_unknown_playbook_is_manual_review(self) -> None:
        reviews_table = FakeReviewsTable()
        self._run("mystery", reviews_table, FakeS3())
        self.assertEqual(reviews_table.item["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(reviews_table.item["reason"], "unknown_playbook")

    def test_failure_moves_review_to_error(self) -> None:
        reviews_table = FakeReviewsTable()

        # Make the S3 copy blow up -> body fails -> ERROR.
        class BoomS3:
            def copy_object(self, **_kw):
                raise RuntimeError("simulated copy failure")

        with patch.object(pr, "_settle_reservation"):
            pr.run_mock_pipeline(
                REVIEW_ID,
                {"review_id": REVIEW_ID, "playbook_id": "eiaa"},
                dynamodb_resource=FakeDDB(reviews_table),
                s3_client=BoomS3(),
            )
        self.assertEqual(reviews_table.item["status"], "ERROR")
        self.assertEqual(reviews_table.item["failing_stage"], "inprocess_pipeline")


class FakeSubmissionsTable:
    def __init__(self, submission: dict | None):
        self.submission = submission
        self.updates: list[dict] = []

    def scan(self, FilterExpression=None, ExpressionAttributeValues=None):
        return {"Items": [self.submission] if self.submission else []}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None):
        self.updates.append({"Key": Key, "vals": ExpressionAttributeValues})


class FakeDDBSubmissions:
    def __init__(self, submissions):
        self._subs = submissions

    def Table(self, name):
        return self._subs


class TestSettleReservation(unittest.TestCase):
    """Directly exercise _settle_reservation so the settle_spend call SIGNATURE
    is verified (the run_mock_pipeline tests mock this out; a live smoke test
    caught a wrong-arity call that this now guards)."""

    def test_calls_settle_spend_with_review_and_reservation_id(self) -> None:
        subs = FakeSubmissionsTable(
            {"idempotency_key": "idem-1", "review_id": REVIEW_ID, "spend_reservation_id": "res-1"}
        )
        calls = []
        with patch.object(pr.reviews, "settle_spend", side_effect=lambda *a: calls.append(a)):
            pr._settle_reservation(REVIEW_ID, FakeDDBSubmissions(subs))
        # (review_id, reservation_id, actual_usd_cents, dynamodb_resource)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], REVIEW_ID)
        self.assertEqual(calls[0][1], "res-1")
        self.assertEqual(calls[0][2], 0)
        self.assertTrue(subs.updates and subs.updates[0]["vals"][":t"] is True)

    def test_no_reservation_is_noop(self) -> None:
        subs = FakeSubmissionsTable({"idempotency_key": "idem-1", "review_id": REVIEW_ID})
        with patch.object(pr.reviews, "settle_spend", side_effect=AssertionError("must not call")):
            pr._settle_reservation(REVIEW_ID, FakeDDBSubmissions(subs))
        self.assertEqual(subs.updates, [])

    def test_already_released_is_noop(self) -> None:
        subs = FakeSubmissionsTable(
            {"idempotency_key": "i", "review_id": REVIEW_ID, "spend_reservation_id": "r",
             "reservation_released": True}
        )
        with patch.object(pr.reviews, "settle_spend", side_effect=AssertionError("must not call")):
            pr._settle_reservation(REVIEW_ID, FakeDDBSubmissions(subs))
        self.assertEqual(subs.updates, [])


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestInProcessClient))
    suite.addTests(loader.loadTestsFromTestCase(TestRunMockPipeline))
    suite.addTests(loader.loadTestsFromTestCase(TestSettleReservation))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
