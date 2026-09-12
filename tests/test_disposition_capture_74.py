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
backend/src/disposition.py.

Issue #67 — WHY THIS USES moto AND NOT A HAND-ROLLED TABLE. This file used
to stub boto3 out entirely and hand `disposition.py` an in-memory
`FakeReviewsTable` with `get_item`/`put_item`/`update_item`/`scan`. That fake
had no `.query`, so every read under test took a duck-typed scan fallback
(`hasattr(table, "query")`) that a real boto3 Table can never reach: the
tests covered a branch production never runs, and the branch production DOES
run — the `owner_sub-index` / `status-index` query — was covered nowhere.
#67 deleted those fallbacks, so the table here is a real (moto) one built by
`tests/ddb_fixtures.py::create_reviews_table`, which declares the same GSIs
`infra/lib/nested/data-stack.ts` does (parity asserted by
`tests/test_ddb_fixtures_cdk_parity_67.py`). moto also rejects what DynamoDB
rejects, so a malformed UpdateExpression or a missing index now fails here
instead of in production.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _path in (str(BACKEND_SRC), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
# moto needs a region and (fake) credentials for boto3.resource("dynamodb").
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from moto import mock_aws  # noqa: E402

from ddb_fixtures import create_reviews_table  # noqa: E402

import disposition as _disposition_module  # noqa: E402


class _MotoReviewsTestCase(unittest.TestCase):
    """A real DynamoDB `reviews` table, with the CDK's GSIs, per test."""

    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.addCleanup(self._mock_aws.stop)

        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.table = create_reviews_table(self.ddb)

    def _seed_review(self, review_id: str, owner_sub: str, status_: str) -> None:
        """The row shape `backend/src/reviews.py::create_review` writes: a
        review_id PK plus the owner_sub/status/created_at attributes the
        three GSIs key on."""
        self.table.put_item(
            Item={
                "review_id": review_id,
                "owner_sub": owner_sub,
                "status": status_,
                "decision": "REQUEST_CHANGE",
                "created_at": "1000",
                "updated_at": "1000",
            }
        )

    def _stored(self, review_id: str) -> dict:
        return self.table.get_item(Key={"review_id": review_id})["Item"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecordDisposition(_MotoReviewsTestCase):
    def test_accepted_outcome_is_recorded_against_the_review(self):
        self._seed_review("review-1", "owner-1", "DONE")
        result = _disposition_module.record_disposition(
            "review-1", "ACCEPTED", self.ddb
        )
        self.assertEqual(result["attorney_disposition"], "ACCEPTED")
        self.assertEqual(self._stored("review-1")["attorney_disposition"], "ACCEPTED")

    def test_edited_outcome_captures_reason_codes_topic_ids_and_note(self):
        self._seed_review("review-2", "owner-1", "DONE")
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
        self._seed_review("review-3", "owner-1", "DONE")
        result = _disposition_module.record_disposition("review-3", "REJECTED", self.ddb)
        self.assertEqual(result["attorney_disposition"], "REJECTED")
        self.assertEqual(result["attorney_disposition_reason_codes"], [])

    def test_invalid_outcome_rejected(self):
        self._seed_review("review-4", "owner-1", "DONE")
        with self.assertRaises(HTTPException) as ctx:
            _disposition_module.record_disposition("review-4", "APPROVED", self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_unknown_review_404s(self):
        with self.assertRaises(HTTPException) as ctx:
            _disposition_module.record_disposition("no-such-review", "ACCEPTED", self.ddb)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_cannot_record_disposition_before_review_completes(self):
        self._seed_review("review-5", "owner-1", "RUNNING")
        with self.assertRaises(HTTPException) as ctx:
            _disposition_module.record_disposition("review-5", "ACCEPTED", self.ddb)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_disposition_never_touches_status_or_decision(self):
        """Issue AC: 'does not turn the tool into an approval workflow' —
        the pipeline's own status/decision must be untouched by a
        disposition capture."""
        self._seed_review("review-6", "owner-1", "DONE")
        _disposition_module.record_disposition("review-6", "REJECTED", self.ddb)
        stored = self._stored("review-6")
        self.assertEqual(stored["status"], "DONE")
        self.assertEqual(stored["decision"], "REQUEST_CHANGE")


class TestLegalTriageQueue(_MotoReviewsTestCase):
    def test_accepted_never_enters_triage_queue(self):
        self._seed_review("review-a", "owner-1", "DONE")
        _disposition_module.record_disposition("review-a", "ACCEPTED", self.ddb)
        queue = _disposition_module.list_legal_triage_queue(self.ddb)
        self.assertNotIn("review-a", [r["review_id"] for r in queue])

    def test_edited_and_rejected_enter_triage_queue(self):
        self._seed_review("review-b", "owner-1", "DONE")
        self._seed_review("review-c", "owner-1", "DONE")
        _disposition_module.record_disposition("review-b", "EDITED", self.ddb)
        _disposition_module.record_disposition("review-c", "REJECTED", self.ddb)
        queue_ids = {r["review_id"] for r in _disposition_module.list_legal_triage_queue(self.ddb)}
        self.assertEqual(queue_ids, {"review-b", "review-c"})

    def test_triage_queue_covers_every_dispositionable_terminal_status(self):
        """The queue reads ONE `status-index` partition per terminal status
        (issue #52). Seeding only DONE would leave every other partition —
        and the loop that reads them — green forever, so seed each
        dispositionable status and require all of them back."""
        dispositionable = sorted(_disposition_module.DISPOSITIONABLE_REVIEW_STATUSES)
        self.assertGreater(
            len(dispositionable),
            1,
            "this test only means something while more than one status is "
            "dispositionable",
        )
        for index, status_ in enumerate(dispositionable):
            review_id = f"review-term-{index}"
            self._seed_review(review_id, "owner-1", status_)
            _disposition_module.record_disposition(review_id, "REJECTED", self.ddb)

        queue_ids = {r["review_id"] for r in _disposition_module.list_legal_triage_queue(self.ddb)}
        self.assertEqual(
            queue_ids,
            {f"review-term-{i}" for i in range(len(dispositionable))},
            f"a review in one of {dispositionable} never reached the triage queue",
        )

    def test_mark_triaged_removes_review_from_pending_queue(self):
        self._seed_review("review-d", "owner-1", "DONE")
        _disposition_module.record_disposition("review-d", "REJECTED", self.ddb)
        _disposition_module.mark_triaged("review-d", self.ddb)
        queue_ids = {r["review_id"] for r in _disposition_module.list_legal_triage_queue(self.ddb)}
        self.assertNotIn("review-d", queue_ids)
        self.assertEqual(
            self._stored("review-d")["legal_triage_status"],
            _disposition_module.TRIAGE_STATUS_TRIAGED,
        )


class TestDispositionAwaitingCount(_MotoReviewsTestCase):
    """`count_reviews_awaiting_disposition` counts completed reviews still
    missing a disposition, owner-scoped via the `owner_sub-index` GSI. It is
    deliberately uncalled in the product (ARCHITECTURE.md -> 'Disposition
    capture — optional, never a nag'); these tests keep it correct."""

    def test_completed_reviews_without_disposition_are_counted(self):
        self._seed_review("review-x", "owner-1", "DONE")
        self._seed_review("review-y", "owner-1", "MANUAL_REVIEW_REQUIRED")
        self._seed_review("review-z", "owner-1", "DONE")
        _disposition_module.record_disposition("review-z", "ACCEPTED", self.ddb)

        count = _disposition_module.count_reviews_awaiting_disposition("owner-1", self.ddb)
        self.assertEqual(count, 2, "review-x and review-y await disposition; review-z was captured.")

    def test_nag_is_scoped_to_owner(self):
        self._seed_review("review-m", "owner-1", "DONE")
        self._seed_review("review-n", "owner-2", "DONE")

        count_owner_1 = _disposition_module.count_reviews_awaiting_disposition("owner-1", self.ddb)
        self.assertEqual(count_owner_1, 1)

    def test_running_reviews_are_not_counted_in_the_nag(self):
        self._seed_review("review-p", "owner-1", "RUNNING")
        count = _disposition_module.count_reviews_awaiting_disposition("owner-1", self.ddb)
        self.assertEqual(count, 0, "A still-running review has no output to disposition yet.")


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestRecordDisposition))
    suite.addTests(loader.loadTestsFromTestCase(TestLegalTriageQueue))
    suite.addTests(loader.loadTestsFromTestCase(TestDispositionAwaitingCount))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
