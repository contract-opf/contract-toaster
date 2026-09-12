#!/usr/bin/env python3
"""
Tests for `GET /api/review-duration-estimate` -- issue #71, step 11 of the
2026-09-05 stability/maintainability series (finding G12, action B12).

The progress bar reported STAGE and never time. This route is the measured
answer: p50/p90 of how long finished reviews of a playbook have ACTUALLY
taken here, plus the median word count of that same sample so the caller can
scale the figures to the document in front of it.

## What every fixture in this file is, and why it can be trusted

Nothing here hand-writes a `reviews` row. Every sampled row is produced by
the two functions that produce it in production, in order:

  * `src.reviews._create_review_row` -- the submission-time write. It is
    what stamps `created_at`, `playbook_id` and (issue #71) `word_count`.
    `src.review_routes.post_review` reaches it through
    `reviews.submit_review`, passing the count computed by
    `_submitted_word_count` off the uploaded bytes.
  * `src.pipeline_runner._write_real_terminal` -- the real in-process
    pipeline's terminal write, which is what stamps `completed_at` on the
    DONE transition.

The clock is moved with `patch("time.time", ...)` around each call, so the
two stamps differ by a controlled number of seconds exactly as they would
across a real review. A row assembled by hand would have proved only that
this module can read a dict it also wrote.

The DynamoDB table is a REAL (moto) `reviews` table built by
`tests/ddb_fixtures.create_reviews_table`, i.e. declared exactly as
`infra/lib/nested/data-stack.ts` declares it and pinned to the CDK by
`tests/test_ddb_fixtures_cdk_parity_67.py`. That matters here more than
usual: the sampling query reads the `status-index` GSI, and the KEYS_ONLY
`playbook_hash-index` beside it could not answer this question at all. A
fake that projected everything would have hidden that.

Covered:
  1. The percentiles and the median word count, over a sample built by the
     real writers -- and ONLY the newest 50 rows, with five much older,
     much slower reviews seeded behind them to prove the bound bites.
  2. A sample below `REVIEW_DURATION_MIN_SAMPLE` answers with nulls in every
     figure while still reporting the true `sample_size`.
  3. Another playbook's finished reviews never enter the sample.
  4. A row with no `completed_at` -- what the MOCK pipeline's terminal write
     leaves behind, deliberately (see `pipeline_runner._write_terminal`) --
     is not a sample.
  5. A backwards clock step (completed before created) is dropped, not
     folded in as a zero that would drag the median down.
  6. Rows carrying no `word_count` give `median_words: null` while the
     duration percentiles still answer.
  7. The response is an EXACT key set, and an unauthenticated caller is
     refused.
  8. `review_routes._submitted_word_count` -- the production function that
     puts `word_count` on the row at all -- counts a real .docx and fails
     soft on bytes it cannot read.

moto-mocked DynamoDB only -- no live AWS, no network (standing rule 4).

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
TESTS_DIR = REPO_ROOT / "tests"

for _path in (str(BACKEND_ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")
os.environ.setdefault("AUTH_SETTINGS_TABLE", "contract-toaster-auth-settings-test")
os.environ.setdefault("MODEL_SETTINGS_TABLE", "contract-toaster-model-settings-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("ENV_NAME", "dev")
# Left UNSET on purpose: `_create_review_row` snapshots the retention window
# and falls back to the documented default when no settings table is
# configured, which keeps this file to the one table it is actually about.
os.environ.pop("RETENTION_SETTINGS_TABLE", None)

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.reviews as reviews  # noqa: E402

from ddb_fixtures import create_reviews_table  # noqa: E402

REVIEWER = {"cognito_sub": "reviewer-1", "email": "reviewer-1@example.com", "is_admin": False}

PLAYBOOK = "synthetic-nda-sample"
OTHER_PLAYBOOK = "synthetic-msa-sample"
BUNDLE_HASH = "a" * 64

# Ten digits wide, so the `created_at` STRING sort key on `status-index`
# orders identically to the epoch seconds it encodes.
BASE_EPOCH = 1_700_000_000


class DurationEstimateTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = mock_aws()
        self.mock.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.table = create_reviews_table(self.ddb)
        self.next_id = 0

    def tearDown(self) -> None:
        self.mock.stop()

    # -- the real producers, driven ----------------------------------------

    def _submit(self, playbook_id: str, created: int, words: int | None) -> str:
        """One submission, through the row writer production submits with."""
        self.next_id += 1
        review_id = f"00000000-0000-4000-a000-{self.next_id:012d}"
        with patch("time.time", return_value=float(created)):
            reviews._create_review_row(
                review_id,
                REVIEWER["cognito_sub"],
                playbook_id,
                BUNDLE_HASH,
                self.ddb,
                word_count=words,
            )
        return review_id

    def _finish(self, review_id: str, completed: int) -> None:
        """The real pipeline's terminal write, at a controlled clock."""
        with patch("time.time", return_value=float(completed)):
            pipeline_runner._write_real_terminal(
                review_id,
                {"status": "OK", "decision": "REQUEST_CHANGE", "summary": "s"},
                None,
                self.ddb,
            )

    def _seed(
        self,
        playbook_id: str,
        created: int,
        duration: int,
        words: int | None = 1000,
    ) -> str:
        review_id = self._submit(playbook_id, created, words)
        self._finish(review_id, created + duration)
        return review_id

    def _estimate(self, playbook_id: str = PLAYBOOK) -> dict:
        return reviews.review_duration_estimate(playbook_id, self.ddb)


class TestTheSample(DurationEstimateTestBase):
    """The figures, and the bound on what produces them."""

    def _seed_fifty_five(self) -> None:
        """55 finished reviews. The newest 50 took 1..50 seconds and carried
        1010..1500 words; the five behind them each took nearly three hours.

        The old ones are the whole point: if the sample were not bounded to
        `REVIEW_DURATION_SAMPLE_SIZE`, a p90 of 45 would be impossible.
        """
        for index in range(55):
            created = BASE_EPOCH + index * 1_000
            if index < 5:
                self._seed(PLAYBOOK, created, duration=9_999, words=1_000_000)
            else:
                rank = index - 4  # 1..50
                self._seed(PLAYBOOK, created, duration=rank, words=1_000 + rank * 10)

    def test_the_percentiles_and_median_words_come_from_the_newest_fifty(self):
        self._seed_fifty_five()
        body = self._estimate()
        self.assertEqual(body["sample_size"], 50)
        # Nearest rank over 1..50: ceil(0.5*50) = 25 -> 25s; ceil(0.9*50) = 45 -> 45s.
        self.assertEqual(body["p50_seconds"], 25)
        self.assertEqual(body["p90_seconds"], 45)
        # Same rank over 1010..1500 step 10: the 25th smallest.
        self.assertEqual(body["median_words"], 1_250)

    def test_another_playbooks_reviews_never_enter_the_sample(self):
        self._seed_fifty_five()
        for index in range(30):
            self._seed(
                OTHER_PLAYBOOK,
                BASE_EPOCH + 900_000 + index * 1_000,
                duration=7_200,
                words=50_000,
            )
        body = self._estimate()
        self.assertEqual(body["sample_size"], 50)
        self.assertEqual(body["p50_seconds"], 25)
        self.assertEqual(body["p90_seconds"], 45)
        self.assertEqual(body["median_words"], 1_250)
        # And the other playbook answers from its OWN rows, not from these.
        other = self._estimate(OTHER_PLAYBOOK)
        self.assertEqual(other["sample_size"], 30)
        self.assertEqual(other["p50_seconds"], 7_200)

    def test_a_thin_sample_answers_with_nulls_and_a_truthful_count(self):
        for index in range(reviews.REVIEW_DURATION_MIN_SAMPLE - 1):
            self._seed(PLAYBOOK, BASE_EPOCH + index * 1_000, duration=120)
        body = self._estimate()
        self.assertEqual(body["sample_size"], reviews.REVIEW_DURATION_MIN_SAMPLE - 1)
        self.assertIsNone(body["p50_seconds"])
        self.assertIsNone(body["p90_seconds"])
        self.assertIsNone(body["median_words"])

    def test_exactly_the_minimum_sample_does_answer(self):
        """The boundary in the other direction -- otherwise "nulls below
        five" could be satisfied by a route that never answers at all."""
        for index in range(reviews.REVIEW_DURATION_MIN_SAMPLE):
            self._seed(PLAYBOOK, BASE_EPOCH + index * 1_000, duration=120)
        body = self._estimate()
        self.assertEqual(body["sample_size"], reviews.REVIEW_DURATION_MIN_SAMPLE)
        self.assertEqual(body["p50_seconds"], 120)
        self.assertEqual(body["p90_seconds"], 120)

    def test_a_playbook_with_no_finished_reviews_answers_empty(self):
        body = self._estimate("never-used")
        self.assertEqual(body["sample_size"], 0)
        self.assertIsNone(body["p50_seconds"])


class TestWhatIsNotASample(DurationEstimateTestBase):
    """Rows production really does write that must not be measured."""

    def test_a_mock_pipeline_row_carries_no_completed_stamp_and_is_not_counted(self):
        """`pipeline_runner._write_terminal` is the MOCK pipeline's terminal
        write. It flips the row to DONE and deliberately stamps no
        `completed_at`, because a canned fixture returned in milliseconds is
        not a measurement of how long a review takes. Driven here through
        that real function, not by omitting a field from a hand-built row."""
        for index in range(8):
            review_id = self._submit(PLAYBOOK, BASE_EPOCH + index * 1_000, words=1_200)
            with patch("time.time", return_value=float(BASE_EPOCH + index * 1_000 + 1)):
                pipeline_runner._write_terminal(
                    review_id,
                    {"decision": "REQUEST_CHANGE", "summary": "s"},
                    False,
                    self.ddb,
                )
        rows = self.table.scan()["Items"]
        self.assertTrue(all(row["status"] == "DONE" for row in rows))
        self.assertTrue(all("completed_at" not in row for row in rows))

        body = self._estimate()
        self.assertEqual(body["sample_size"], 0)
        self.assertIsNone(body["p50_seconds"])

    def test_a_backwards_clock_step_is_dropped_rather_than_counted_as_zero(self):
        """A host whose clock steps backwards between submission and the
        terminal write produces `completed_at < created_at`. Clamping it to
        zero would drag the median toward "instant"; the row is dropped."""
        for index in range(5):
            self._seed(PLAYBOOK, BASE_EPOCH + index * 1_000, duration=300)
        skewed = self._submit(PLAYBOOK, BASE_EPOCH + 9_000, words=1_000)
        self._finish(skewed, BASE_EPOCH + 9_000 - 600)

        body = self._estimate()
        self.assertEqual(body["sample_size"], 5)
        self.assertEqual(body["p50_seconds"], 300)

    def test_a_still_running_review_is_not_a_sample(self):
        for index in range(5):
            self._seed(PLAYBOOK, BASE_EPOCH + index * 1_000, duration=240)
        self._submit(PLAYBOOK, BASE_EPOCH + 9_000, words=1_000)  # never finished
        self.assertEqual(self._estimate()["sample_size"], 5)

    def test_rows_with_no_word_count_answer_durations_but_no_median_words(self):
        """`_submitted_word_count` is fail-soft: an unreadable document
        submits fine and the row carries no count. The durations must still
        answer -- only the scaling denominator goes missing."""
        for index in range(6):
            self._seed(PLAYBOOK, BASE_EPOCH + index * 1_000, duration=180, words=None)
        body = self._estimate()
        self.assertEqual(body["sample_size"], 6)
        self.assertEqual(body["p50_seconds"], 180)
        self.assertIsNone(body["median_words"])


class TestTheWordCountProducer(unittest.TestCase):
    """`review_routes._submitted_word_count` -- the NON-TEST code that puts a
    `word_count` on the row the sample above reads.

    Without this class the `median_words` assertions would be asserting over
    a field only the fixtures write. `POST /api/reviews` calls this function
    on the post-gauntlet bytes and threads the result through
    `reviews.submit_review` -> `_create_review_row`, so the shape is
    genuinely reachable; these two tests pin both of its outcomes.

    The .docx builders are `tests/test_preflight_491.py`'s, imported
    cross-file exactly as `tests/test_preflight_party_signal_55.py` imports
    them -- the same real OOXML bytes the preflight route is tested against,
    not a second hand-rolled document shape.
    """

    def test_it_counts_the_words_of_a_real_docx(self):
        import src.review_routes as review_routes  # noqa: PLC0415
        import test_preflight_491 as pf491  # noqa: PLC0415

        # Nine words, then six. Counted by hand so this asserts a number
        # rather than re-running the implementation and comparing it to
        # itself.
        docx = pf491._build_docx(
            [
                pf491._paragraph("Each party shall hold the other party's information confidential"),
                pf491._paragraph("This Agreement runs for two years"),
            ]
        )
        self.assertEqual(review_routes._submitted_word_count(docx), 15)

    def test_it_fails_soft_on_bytes_it_cannot_read(self):
        """#491's advisory posture: a document whose stats cannot be computed
        still submits. The row carries no count, the estimate loses one
        sample, and nothing is refused."""
        import src.review_routes as review_routes  # noqa: PLC0415

        self.assertIsNone(review_routes._submitted_word_count(b"not a docx at all"))
        self.assertIsNone(review_routes._submitted_word_count(b""))


class TestTheHttpSurface(DurationEstimateTestBase):
    """The route as it is actually mounted on the shipped app."""

    def setUp(self) -> None:
        super().setUp()
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        self.client = TestClient(backend_main.app)

    def tearDown(self) -> None:
        backend_main.app.dependency_overrides.clear()
        super().tearDown()

    def _as(self, user_row: dict) -> None:
        backend_main.app.dependency_overrides[backend_main.get_active_user_row] = (
            lambda: user_row
        )

    def test_it_answers_an_active_reviewer(self):
        self._as(REVIEWER)
        for index in range(6):
            self._seed(PLAYBOOK, BASE_EPOCH + index * 1_000, duration=150, words=2_000)
        response = self.client.get(
            "/api/review-duration-estimate", params={"playbook_id": PLAYBOOK, "words": 4000}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["p50_seconds"], 150)
        self.assertEqual(body["median_words"], 2_000)
        self.assertEqual(body["sample_size"], 6)

    def test_it_carries_these_four_aggregates_and_nothing_else(self):
        """An EXACT key set, not "no review id is present": this is an
        any-active-user read over other reviewers' rows, so a field appended
        to the sampled shape tomorrow has to fail here rather than quietly
        reach a stranger."""
        self._as(REVIEWER)
        for index in range(6):
            self._seed(PLAYBOOK, BASE_EPOCH + index * 1_000, duration=150)
        body = self.client.get(
            "/api/review-duration-estimate", params={"playbook_id": PLAYBOOK}
        ).json()
        self.assertEqual(
            sorted(body.keys()), sorted(reviews.REVIEW_DURATION_ESTIMATE_FIELDS)
        )

    def test_an_unauthenticated_caller_is_refused(self):
        """No `get_active_user_row` override and no credential: the real
        dependency chain runs and refuses."""
        backend_main.app.dependency_overrides.pop(backend_main.get_active_user_row, None)
        response = self.client.get(
            "/api/review-duration-estimate", params={"playbook_id": PLAYBOOK}
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_a_missing_playbook_id_is_a_malformed_request(self):
        self._as(REVIEWER)
        self.assertEqual(
            self.client.get("/api/review-duration-estimate").status_code, 422
        )

    def test_a_negative_word_count_is_a_malformed_request(self):
        self._as(REVIEWER)
        response = self.client.get(
            "/api/review-duration-estimate", params={"playbook_id": PLAYBOOK, "words": -1}
        )
        self.assertEqual(response.status_code, 422, response.text)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestTheSample,
        TestWhatIsNotASample,
        TestTheWordCountProducer,
        TestTheHttpSurface,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
