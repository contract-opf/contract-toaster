#!/usr/bin/env python3
"""
Executable tests for issue #74 AC: Human-review-outcome capture.

  "A lightweight capture records, per review, whether the attorney
   accepted, edited, or rejected the tool output, plus structured reason
   codes/topic IDs where applicable and an optional free-text note."
  "The outcome is stored against the review ... Edited/rejected outcomes
   enter a legal triage queue before becoming candidate gold-set changes."
  "Capturing the outcome does not turn the tool into an approval
   workflow — it is a feedback signal, not a legal gate."
  Reconciliation: "Add the disposition nag state ('N reviews awaiting
   disposition') so the eval loop actually gets data (#47)."

These are unit tests against the real enforcement code in
backend/src/disposition.py, using in-memory fakes for DynamoDB — same
third-party-stubbing convention as tests/test_review_submission_e2e.py so
the suite runs in CI without extra installs.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"

if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


def _stub_third_party() -> None:
    """Inject minimal stubs for boto3, botocore, and fastapi if absent."""
    if "fastapi" not in sys.modules:
        fastapi_mod = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail: str = "") -> None:
                self.status_code = status_code
                self.detail = detail
                super().__init__(detail)

        class status:  # noqa: N801
            HTTP_200_OK = 200
            HTTP_400_BAD_REQUEST = 400
            HTTP_404_NOT_FOUND = 404
            HTTP_409_CONFLICT = 409

        fastapi_mod.HTTPException = HTTPException
        fastapi_mod.status = status
        sys.modules["fastapi"] = fastapi_mod

    if "botocore" not in sys.modules:
        botocore_mod = types.ModuleType("botocore")
        exceptions_mod = types.ModuleType("botocore.exceptions")

        class ClientError(Exception):
            def __init__(self, error_response=None, operation_name=""):
                self.response = error_response or {}
                super().__init__(str(error_response))

        exceptions_mod.ClientError = ClientError
        botocore_mod.exceptions = exceptions_mod
        sys.modules["botocore"] = botocore_mod
        sys.modules["botocore.exceptions"] = exceptions_mod

    if "boto3" not in sys.modules:
        boto3_mod = types.ModuleType("boto3")
        sys.modules["boto3"] = boto3_mod


_stub_third_party()

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")

import disposition as _disposition_module  # noqa: E402

HTTPException = sys.modules["fastapi"].HTTPException


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------

class FakeReviewsTable:
    """A tiny in-memory stand-in for the reviews DynamoDB Table resource."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        item = self.items.get(Key["review_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        self.items[Item["review_id"]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                     ConditionExpression=None, ExpressionAttributeNames=None):
        review_id = Key["review_id"]
        item = self.items.setdefault(review_id, dict(Key))
        vals = ExpressionAttributeValues or {}

        if "attorney_disposition = :disposition" in UpdateExpression:
            item["attorney_disposition"] = vals[":disposition"]
            item["attorney_disposition_reason_codes"] = vals[":reason_codes"]
            item["attorney_disposition_topic_ids"] = vals[":topic_ids"]
            item["attorney_disposition_note"] = vals[":note"]
            item["attorney_disposition_recorded_at"] = vals[":recorded_at"]
            item["legal_triage_status"] = vals[":triage_status"]
            item["updated_at"] = vals[":now"]
            return

        if "legal_triage_status = :triaged" in UpdateExpression:
            item["legal_triage_status"] = vals[":triaged"]
            item["updated_at"] = vals[":now"]
            return

    def scan(self):
        return {"Items": [dict(v) for v in self.items.values()]}


class FakeDynamoDBResource:
    def __init__(self, table: FakeReviewsTable):
        self._table = table

    def Table(self, name: str) -> FakeReviewsTable:
        return self._table


def _seed_review(table: FakeReviewsTable, review_id: str, owner_sub: str, status_: str) -> None:
    table.items[review_id] = {
        "review_id": review_id,
        "owner_sub": owner_sub,
        "status": status_,
        "decision": "REQUEST_CHANGE",
        "created_at": "1000",
        "updated_at": "1000",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecordDisposition(unittest.TestCase):
    def setUp(self):
        self.table = FakeReviewsTable()
        self.ddb = FakeDynamoDBResource(self.table)

    def test_accepted_outcome_is_recorded_against_the_review(self):
        _seed_review(self.table, "review-1", "owner-1", "DONE")
        result = _disposition_module.record_disposition(
            "review-1", "ACCEPTED", self.ddb
        )
        self.assertEqual(result["attorney_disposition"], "ACCEPTED")
        self.assertEqual(self.table.items["review-1"]["attorney_disposition"], "ACCEPTED")

    def test_edited_outcome_captures_reason_codes_topic_ids_and_note(self):
        _seed_review(self.table, "review-2", "owner-1", "DONE")
        result = _disposition_module.record_disposition(
            "review-2",
            "EDITED",
            self.ddb,
            reason_codes=["missed-nuance", "over-flag"],
            topic_ids=["indemnification"],
            note="Attorney narrowed the indemnification carve-out further.",
        )
        self.assertEqual(result["attorney_disposition"], "EDITED")
        self.assertEqual(result["attorney_disposition_reason_codes"], ["missed-nuance", "over-flag"])
        self.assertEqual(result["attorney_disposition_topic_ids"], ["indemnification"])
        self.assertEqual(
            result["attorney_disposition_note"],
            "Attorney narrowed the indemnification carve-out further.",
        )

    def test_rejected_outcome_accepts_optional_reason_codes(self):
        _seed_review(self.table, "review-3", "owner-1", "DONE")
        result = _disposition_module.record_disposition("review-3", "REJECTED", self.ddb)
        self.assertEqual(result["attorney_disposition"], "REJECTED")
        self.assertEqual(result["attorney_disposition_reason_codes"], [])

    def test_invalid_outcome_rejected(self):
        _seed_review(self.table, "review-4", "owner-1", "DONE")
        with self.assertRaises(HTTPException) as ctx:
            _disposition_module.record_disposition("review-4", "APPROVED", self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_unknown_review_404s(self):
        with self.assertRaises(HTTPException) as ctx:
            _disposition_module.record_disposition("no-such-review", "ACCEPTED", self.ddb)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_cannot_record_disposition_before_review_completes(self):
        _seed_review(self.table, "review-5", "owner-1", "RUNNING")
        with self.assertRaises(HTTPException) as ctx:
            _disposition_module.record_disposition("review-5", "ACCEPTED", self.ddb)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_disposition_never_touches_status_or_decision(self):
        """Issue AC: 'does not turn the tool into an approval workflow' —
        the pipeline's own status/decision must be untouched by a
        disposition capture."""
        _seed_review(self.table, "review-6", "owner-1", "DONE")
        _disposition_module.record_disposition("review-6", "REJECTED", self.ddb)
        stored = self.table.items["review-6"]
        self.assertEqual(stored["status"], "DONE")
        self.assertEqual(stored["decision"], "REQUEST_CHANGE")


class TestLegalTriageQueue(unittest.TestCase):
    def setUp(self):
        self.table = FakeReviewsTable()
        self.ddb = FakeDynamoDBResource(self.table)

    def test_accepted_never_enters_triage_queue(self):
        _seed_review(self.table, "review-a", "owner-1", "DONE")
        _disposition_module.record_disposition("review-a", "ACCEPTED", self.ddb)
        queue = _disposition_module.list_legal_triage_queue(self.ddb)
        self.assertNotIn("review-a", [r["review_id"] for r in queue])

    def test_edited_and_rejected_enter_triage_queue(self):
        _seed_review(self.table, "review-b", "owner-1", "DONE")
        _seed_review(self.table, "review-c", "owner-1", "DONE")
        _disposition_module.record_disposition("review-b", "EDITED", self.ddb)
        _disposition_module.record_disposition("review-c", "REJECTED", self.ddb)
        queue_ids = {r["review_id"] for r in _disposition_module.list_legal_triage_queue(self.ddb)}
        self.assertEqual(queue_ids, {"review-b", "review-c"})

    def test_mark_triaged_removes_review_from_pending_queue(self):
        _seed_review(self.table, "review-d", "owner-1", "DONE")
        _disposition_module.record_disposition("review-d", "REJECTED", self.ddb)
        _disposition_module.mark_triaged("review-d", self.ddb)
        queue_ids = {r["review_id"] for r in _disposition_module.list_legal_triage_queue(self.ddb)}
        self.assertNotIn("review-d", queue_ids)
        self.assertEqual(
            self.table.items["review-d"]["legal_triage_status"],
            _disposition_module.TRIAGE_STATUS_TRIAGED,
        )


class TestDispositionNag(unittest.TestCase):
    """ARCHITECTURE.md -> 'Disposition nag': the reviewer list view shows a
    nag count of completed reviews still missing a disposition."""

    def setUp(self):
        self.table = FakeReviewsTable()
        self.ddb = FakeDynamoDBResource(self.table)

    def test_completed_reviews_without_disposition_are_counted(self):
        _seed_review(self.table, "review-x", "owner-1", "DONE")
        _seed_review(self.table, "review-y", "owner-1", "MANUAL_REVIEW_REQUIRED")
        _seed_review(self.table, "review-z", "owner-1", "DONE")
        _disposition_module.record_disposition("review-z", "ACCEPTED", self.ddb)

        count = _disposition_module.count_reviews_awaiting_disposition("owner-1", self.ddb)
        self.assertEqual(count, 2, "review-x and review-y await disposition; review-z was captured.")

    def test_nag_is_scoped_to_owner(self):
        _seed_review(self.table, "review-m", "owner-1", "DONE")
        _seed_review(self.table, "review-n", "owner-2", "DONE")

        count_owner_1 = _disposition_module.count_reviews_awaiting_disposition("owner-1", self.ddb)
        self.assertEqual(count_owner_1, 1)

    def test_running_reviews_are_not_counted_in_the_nag(self):
        _seed_review(self.table, "review-p", "owner-1", "RUNNING")
        count = _disposition_module.count_reviews_awaiting_disposition("owner-1", self.ddb)
        self.assertEqual(count, 0, "A still-running review has no output to disposition yet.")


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestRecordDisposition))
    suite.addTests(loader.loadTestsFromTestCase(TestLegalTriageQueue))
    suite.addTests(loader.loadTestsFromTestCase(TestDispositionNag))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
