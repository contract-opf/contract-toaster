#!/usr/bin/env python3
"""
Executable tests for issue #258: shared failing-stage + status taxonomy
(target-agnostic core, splits out of #244).

Today `failing_stage` is hardcoded to `'pipeline'` in the AWS Step Functions
error-transition Lambda (infra/lib/nested/pipeline-stack.ts) and the Docker Compose
in-process runner (backend/src/pipeline_runner.py::_fail_review) has its own
separate, hardcoded ("inprocess_pipeline") stage-failure write. This ticket
adds ONE target-agnostic `reviews.record_stage_failure(review_id, stage_name,
reason, dynamodb_resource)` that either caller can invoke, plus a small,
explicit reason -> terminal-status taxonomy. Since issue #133 every reason
resolves to `ERROR` (the two "manual review" statuses are retired); the
reason token itself is what distinguishes a structured-output retry
exhausted from a `document_too_large` cap exceeded.

Wiring the AWS errorTransition Lambda and the Docker Compose runner's per-stage `except`
blocks onto this function is explicitly OUT OF SCOPE for this ticket (folds
into #244 and the Docker Compose wiring ticket respectively) -- these tests exercise
`record_stage_failure` and `get_review_detail` directly, against an
in-memory fake DynamoDB table (no live AWS, no moto needed for this
mechanism itself).

Covers the issue's acceptance criteria:
  1. A forced failure (any caller, any stage) records the REAL stage name to
     the reviews row -- never the hardcoded 'pipeline' string.
  2. Every reason, documented or not, resolves to ERROR with its own
     token (issue #133).
  3. `failing_stage` + `status` + `reason` are all surfaced by
     `get_review_detail` (GET /api/reviews/{id}'s implementation).

This test MUST FAIL on the pre-fix tree (reviews.record_stage_failure does
not exist; get_review_detail does not surface failing_stage) and PASS after
the fix. Run standalone: `python3 tests/test_stage_failure_taxonomy.py`.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")

import reviews  # noqa: E402

REVIEW_ID = "00000000-0000-4000-a000-000000000258"


# ---------------------------------------------------------------------------
# Minimal in-memory fake DynamoDB Table -- generic-enough SET interpreter to
# apply whatever UpdateExpression record_stage_failure emits (unlike the
# purpose-built fakes in other test files, tailored to their own module's
# expressions).
# ---------------------------------------------------------------------------

class FakeTable:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item):
        self.items[Item["review_id"]] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(Key["review_id"])
        return {"Item": item} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                     ExpressionAttributeNames=None, ConditionExpression=None):
        review_id = Key["review_id"]
        item = self.items.setdefault(review_id, {"review_id": review_id})
        vals = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}

        assert UpdateExpression.startswith("SET "), UpdateExpression
        clauses = UpdateExpression[len("SET "):].split(",")
        for clause in clauses:
            field, _, value_token = clause.strip().partition("=")
            field = field.strip()
            value_token = value_token.strip()
            field = names.get(field, field)
            item[field] = vals[value_token]


class FakeDynamoDBResource:
    def __init__(self):
        self._table = FakeTable()

    def Table(self, name):
        assert name == os.environ["REVIEWS_TABLE"]
        return self._table


class TestRecordStageFailure(unittest.TestCase):
    def setUp(self) -> None:
        self.ddb = FakeDynamoDBResource()
        self.ddb.Table(os.environ["REVIEWS_TABLE"]).put_item(
            Item={
                "review_id": REVIEW_ID,
                "owner_sub": "user-1",
                "playbook_id": "eiaa",
                "status": "RUNNING",
            }
        )

    def test_records_the_real_stage_name_not_pipeline(self) -> None:
        reviews.record_stage_failure(
            REVIEW_ID, "primary_review_pass", "model_timeout", self.ddb
        )
        item = self.ddb.Table(os.environ["REVIEWS_TABLE"]).items[REVIEW_ID]
        self.assertEqual(item["failing_stage"], "primary_review_pass")
        self.assertNotEqual(item["failing_stage"], "pipeline")

    def test_structured_output_retry_exhausted_reaches_error(self) -> None:
        """Issue #133: once ERROR_MANUAL_REVIEW_REQUIRED; now ERROR with the
        SAME reason token, so the reader's copy is unchanged."""
        status_written = reviews.record_stage_failure(
            REVIEW_ID, "primary_review_pass", "structured_output_retry_exhausted", self.ddb
        )
        item = self.ddb.Table(os.environ["REVIEWS_TABLE"]).items[REVIEW_ID]
        self.assertEqual(status_written, "ERROR")
        self.assertEqual(item["status"], "ERROR")
        self.assertEqual(item["reason"], "structured_output_retry_exhausted")

    def test_document_too_large_reaches_error(self) -> None:
        """Issue #133: once MANUAL_REVIEW_REQUIRED; now ERROR with the SAME
        reason token."""
        status_written = reviews.record_stage_failure(
            REVIEW_ID, "extract_normalize", "document_too_large", self.ddb
        )
        item = self.ddb.Table(os.environ["REVIEWS_TABLE"]).items[REVIEW_ID]
        self.assertEqual(status_written, "ERROR")
        self.assertEqual(item["status"], "ERROR")
        self.assertEqual(item["reason"], "document_too_large")

    def test_unrecognized_reason_falls_back_to_generic_error(self) -> None:
        status_written = reviews.record_stage_failure(
            REVIEW_ID, "leakage_scan", "unmapped_reason_xyz", self.ddb
        )
        item = self.ddb.Table(os.environ["REVIEWS_TABLE"]).items[REVIEW_ID]
        self.assertEqual(status_written, "ERROR")
        self.assertEqual(item["status"], "ERROR")
        self.assertEqual(item["failing_stage"], "leakage_scan")

    def test_every_reason_resolves_to_error(self) -> None:
        """The #258 invariant, restated after issue #133 (owner decision
        2026-09-16): a review never concludes as "manual review required".

        Every token in the table -- including the four that used to land on
        the retired MANUAL_REVIEW_REQUIRED / ERROR_MANUAL_REVIEW_REQUIRED
        statuses -- resolves to ERROR, and keeps its own token so the
        reader-facing copy keyed on it is unchanged.
        """
        for reason in (
            "structured_output_retry_exhausted",
            "document_too_large",
            "model_context_length_exceeded",
            "redline_not_persisted",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(reviews.STAGE_FAILURE_REASON_STATUS[reason], "ERROR")
        self.assertEqual(set(reviews.STAGE_FAILURE_REASON_STATUS.values()), {"ERROR"})
        for reason, status_value in reviews.STAGE_FAILURE_REASON_STATUS.items():
            with self.subTest(reason=reason):
                self.assertIn(status_value, reviews.REVIEW_STATUSES_TERMINAL)


class TestGetReviewDetailSurfacesStageFailure(unittest.TestCase):
    def test_failing_stage_status_and_reason_are_all_surfaced(self) -> None:
        ddb = FakeDynamoDBResource()
        ddb.Table(os.environ["REVIEWS_TABLE"]).put_item(
            Item={
                "review_id": REVIEW_ID,
                "owner_sub": "user-1",
                "playbook_id": "eiaa",
                "status": "RUNNING",
            }
        )
        reviews.record_stage_failure(
            REVIEW_ID, "critic_review_pass", "document_too_large", ddb
        )
        detail = reviews.get_review_detail(
            REVIEW_ID, {"cognito_sub": "user-1", "is_admin": False}, ddb
        )
        self.assertEqual(detail["failing_stage"], "critic_review_pass")
        self.assertEqual(detail["status"], "ERROR")
        self.assertEqual(detail["reason"], "document_too_large")


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestRecordStageFailure))
    suite.addTests(loader.loadTestsFromTestCase(TestGetReviewDetailSurfacesStageFailure))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
