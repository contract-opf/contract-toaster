#!/usr/bin/env python3
"""
Tests for the `completed_at` terminal stamp -- issue #71.

`reviews.review_duration_estimate` measures `completed_at - created_at`. That
subtraction only means anything if the moment a review reached DONE is
actually recorded, and before this issue it was not: the reviews row carried
`created_at`, `updated_at` and (issue #472) `failed_at`, and nothing else
about time.

`updated_at` cannot stand in for it. Every later administrative touch moves
that stamp -- a quarantine or supersede overlay, a recorded disposition, a
retention hold -- so a duration measured from it grows for reasons that have
nothing to do with the pipeline. `completed_at` is written once, on the DONE
transition, by both writers that can make that transition:

  * `backend/src/pipeline_runner.py::_write_real_terminal` -- the Docker
    Compose / in-process runner.
  * `infra/lambda/persist/handler.py::_write_terminal_reviews_state` -- the
    AWS Step Functions persist stage.

Both are driven here as themselves, against a REAL (moto) `reviews` table
built by `tests/ddb_fixtures.create_reviews_table` -- declared exactly as
`infra/lib/nested/data-stack.ts` declares it. The 2026-09 predecessor of this
file used a hand-rolled fake table that copied a named list of placeholders
into a dict, which cannot fail when a new placeholder is added and therefore
cannot prove a new field is written at all.

Covered, for BOTH writers:
  1. A DONE row carries `completed_at`, equal to its own `updated_at` and
     at or after `created_at`.
  2. The stamp is exactly the mirror of `failed_at`: a DONE row has one and
     not the other; a non-success terminal row has the other and not this.
  3. The stamp does not appear on a row the conditional write refuses (an
     already-ERROR row on the in-process path, an already-CANCELLED row on
     the AWS path) -- a refused write must add nothing at all.

moto-mocked DynamoDB only -- no live AWS, no network (standing rule 4).

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
TESTS_DIR = REPO_ROOT / "tests"
PERSIST_HANDLER_PATH = REPO_ROOT / "infra" / "lambda" / "persist" / "handler.py"

for _path in (str(BACKEND_ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ["REVIEWS_TABLE"] = "contract-toaster-reviews-test"
os.environ["REVIEW_SUBMISSIONS_TABLE"] = "contract-toaster-review-submissions-test"
os.environ["DAILY_SPEND_TABLE"] = "contract-toaster-daily-spend-test"

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.reviews as reviews  # noqa: E402

from ddb_fixtures import create_reviews_table, create_submissions_table  # noqa: E402


def _load_persist_handler():
    spec = importlib.util.spec_from_file_location(
        "_persist_completed_at_under_test", PERSIST_HANDLER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


persist = _load_persist_handler()

REVIEW_ID = "00000000-0000-4000-a000-000000000001"
OWNER = "reviewer-1"
PLAYBOOK = "synthetic-nda-sample"
BUNDLE_HASH = "a" * 64


class TerminalWriteTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = mock_aws()
        self.mock.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.table = create_reviews_table(self.ddb)
        create_submissions_table(self.ddb)
        self.started = int(time.time())

    def tearDown(self) -> None:
        self.mock.stop()

    def _pending_row(self, review_id: str = REVIEW_ID) -> None:
        """The real submission-time writer, so `created_at` is the field the
        duration is actually measured from rather than one invented here."""
        reviews._create_review_row(
            review_id, OWNER, PLAYBOOK, BUNDLE_HASH, self.ddb, word_count=1_200
        )

    def _row(self, review_id: str = REVIEW_ID) -> dict:
        return self.table.get_item(Key={"review_id": review_id})["Item"]

    def _assert_completed_stamp(self, row: dict) -> None:
        self.assertIn("completed_at", row)
        self.assertEqual(row["completed_at"], row["updated_at"])
        self.assertGreaterEqual(int(row["completed_at"]), int(row["created_at"]))
        self.assertGreaterEqual(int(row["completed_at"]), self.started)
        # The mirror half: a review that completed did not fail.
        self.assertNotIn("failed_at", row)


class TestTheInProcessRunner(TerminalWriteTestBase):
    """`pipeline_runner._write_real_terminal` -- the Compose/DTS path."""

    def _write(self, status_value: str, review_id: str = REVIEW_ID) -> None:
        pipeline_runner._write_real_terminal(
            review_id,
            {"status": status_value, "decision": "REQUEST_CHANGE", "summary": "s"},
            None,
            self.ddb,
        )

    def test_a_done_row_is_stamped_completed_at(self):
        self._pending_row()
        self._write("OK")
        row = self._row()
        self.assertEqual(row["status"], reviews.REVIEW_STATUS_SUCCESS_TERMINAL)
        self._assert_completed_stamp(row)

    def test_a_failed_terminal_row_is_stamped_failed_at_and_not_completed_at(self):
        self._pending_row()
        self._write("MANUAL_REVIEW_REQUIRED")
        row = self._row()
        self.assertEqual(row["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertIn("failed_at", row)
        self.assertNotIn("completed_at", row)

    def test_a_refused_write_over_an_error_row_adds_no_stamp(self):
        """The conditional write never clobbers ERROR. It must not leave a
        `completed_at` behind either -- a row that says it completed at a
        time it was already recorded as failed is worse than no stamp."""
        self._pending_row()
        self.table.update_item(
            Key={"review_id": REVIEW_ID},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "ERROR"},
        )
        self._write("OK")
        row = self._row()
        self.assertEqual(row["status"], "ERROR")
        self.assertNotIn("completed_at", row)


class TestThePersistStage(TerminalWriteTestBase):
    """`infra/lambda/persist/handler._write_terminal_reviews_state` -- AWS."""

    def _write(self, decision: str | None, review_id: str = REVIEW_ID) -> None:
        persist._write_terminal_reviews_state(
            {"review_id": review_id, "decision": decision, "summary": "s"}, self.ddb
        )

    def test_a_done_row_is_stamped_completed_at(self):
        self._pending_row()
        self._write("REQUEST_CHANGE")
        row = self._row()
        self.assertEqual(row["status"], "DONE")
        self._assert_completed_stamp(row)

    def test_an_accept_decision_is_stamped_too(self):
        """Both members of the handler's `_DONE_DECISIONS`, so the stamp
        cannot be wired to one decision string."""
        self._pending_row()
        self._write("ACCEPT")
        self._assert_completed_stamp(self._row())

    def test_a_manual_review_row_is_not_stamped(self):
        """MANUAL_REVIEW_REQUIRED is terminal for the pipeline but not a
        completed review -- a person still has to finish it, so there is no
        duration to measure and no stamp to write."""
        self._pending_row()
        self._write(None)
        row = self._row()
        self.assertEqual(row["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertNotIn("completed_at", row)

    def test_a_refused_write_over_a_cancelled_row_adds_no_stamp(self):
        """The owner stopped this review while the stage was in flight. The
        handler's condition refuses the whole write; nothing is added."""
        self._pending_row()
        self.table.update_item(
            Key={"review_id": REVIEW_ID},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "CANCELLED"},
        )
        self._write("REQUEST_CHANGE")
        row = self._row()
        self.assertEqual(row["status"], "CANCELLED")
        self.assertNotIn("completed_at", row)


class TestTheTwoWritersAgree(TerminalWriteTestBase):
    """The estimate reads one field, whichever runner produced the row."""

    def test_both_paths_produce_a_row_the_estimate_can_measure(self):
        in_process = "00000000-0000-4000-a000-0000000000a1"
        aws_path = "00000000-0000-4000-a000-0000000000a2"
        self._pending_row(in_process)
        self._pending_row(aws_path)
        pipeline_runner._write_real_terminal(
            in_process, {"status": "OK", "decision": "ACCEPT"}, None, self.ddb
        )
        persist._write_terminal_reviews_state(
            {"review_id": aws_path, "decision": "ACCEPT"}, self.ddb
        )
        for review_id in (in_process, aws_path):
            row = self._row(review_id)
            self.assertEqual(row["status"], "DONE")
            self.assertIn("completed_at", row)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestTheInProcessRunner,
        TestThePersistStage,
        TestTheTwoWritersAgree,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
