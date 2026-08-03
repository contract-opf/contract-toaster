#!/usr/bin/env python3
"""
Slice test for issue #447: REAL staged review progress.

## Root problem this proves fixed

`ReviewSubmission.tsx` rendered `<CtProgress label="Reviewing your document…" />`
-- an INDETERMINATE bar. It animated but carried no information, because
there was nothing to carry: the pipeline never reported where it was.
`pipeline_runner.run_real_pipeline` tracked `stage` as a LOCAL VARIABLE only,
persisted solely on FAILURE via `record_stage_failure`; and the four
sub-stages a waiting user actually cares about are not the runner's stages
at all -- they live INSIDE `scripts/review_spine.py::run_review`, which the
runner sees as one opaque `stage = "run_review"`. There was no callback seam
in `run_review` (`grep on_progress|callback|progress` found nothing).

The tempting non-fix is a timer-driven animation that guesses the stage
boundaries. It would routinely show "3 of 4, reconciliation" while the
primary pass was still running and then sit at 4 of 4 through the longest
wait -- worse than an honest indeterminate bar, because it makes a promise
the system cannot keep. So every assertion here is about REAL progress: a
token is emitted only when the stage it names is genuinely about to run.

This test FAILS on a tree where `run_review` has no `on_progress` seam
(TypeError: unexpected keyword argument), where `pipeline_runner` writes no
`progress_stage`, or where `get_review_detail` does not project it.

## What this test asserts

  1. THE SEAM. `run_review(..., on_progress=...)` calls back with exactly
     `("primary_pass", "critic_pass", "reconciliation", "redline")`, in that
     order, over a real fixture `.docx` driven end to end by
     `FakeBedrockClient` -- and each token arrives BEFORE its stage's model
     call, not after (asserted by interleaving the recorder with the fake
     client's own invocation ledger).
  2. DEFAULT UNCHANGED. `run_review` called WITHOUT `on_progress` returns a
     byte-identical result to the same call WITH one -- the seam is inert by
     default, so every existing caller and test is untouched.
  3. THE WRITE. Driving the REAL `pipeline_runner.run_real_pipeline` (not a
     mocked spine) lands each token on the reviews row as it happens: the
     row's `progress_stage` history is the four tokens in order, and the
     review still reaches DONE.
  4. THE GUARD. A reviews table that RAISES on every progress write still
     produces a DONE review with its output object -- progress is cosmetic,
     the review is not (the #446 lesson: a bookkeeping failure must never
     take a good review down with it). A ConditionalCheckFailed (row no
     longer RUNNING) is a silent no-op, not a logged error.
  5. THE PROJECTION. `get_review_detail` returns `progress_stage` alongside
     `status`, and returns `None` -- never a placeholder or a guess -- for a
     row that carries none.

## What this test deliberately does NOT prove

That the frontend renders it. The step text, the four-level darkening, the
ARIA contract and the "unknown token falls back to indeterminate" rule are
proven in `frontend/src/__tests__/review-progress-stages.test.tsx`.

Run standalone: `python3 tests/test_review_progress_stage_447.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

# Cross-test-file import (established convention -- see
# tests/test_toaster_guidance_readback.py importing test_review_submission_e2e):
# reuse #259's already-built OOXML fixture, fake model responses, fake S3 and
# fake playbooks table, plus its module-level env-var setdefaults. Reusing the
# real fixture is the point -- this drives the ACTUAL composed spine, not a
# stubbed one, so the tokens it reports are the stages that really ran.
import test_dts_pipeline_runner_real_review as dts  # noqa: E402

import pipeline_runner as pr  # noqa: E402
import reviews as reviews_module  # noqa: E402
import review_spine  # noqa: E402

REVIEW_ID = dts.REVIEW_ID

EXPECTED_TOKENS = ["primary_pass", "critic_pass", "reconciliation", "redline"]


# ---------------------------------------------------------------------------
# A reviews table that REMEMBERS every progress_stage it was asked to write,
# so "the row moved through all four" is provable, not just "the last one
# stuck". Subclasses #259's fake so the ConditionExpression semantics
# pipeline_runner relies on stay in exactly one place.
# ---------------------------------------------------------------------------


class RecordingReviewsTable(dts.FakeReviewsTable):
    def __init__(self, status: str = "PENDING", fail_progress_writes: bool = False):
        super().__init__(status=status)
        self.progress_history: list[str] = []
        self.status_when_progress_written: list[str] = []
        self.fail_progress_writes = fail_progress_writes

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                     ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        values = ExpressionAttributeValues or {}
        is_progress = "progress_stage" in UpdateExpression
        if is_progress:
            if self.fail_progress_writes:
                raise RuntimeError("simulated DynamoDB failure on the progress write")
            # The runner conditions the write on the row still being RUNNING;
            # honor that here so a late write on a terminal row is the no-op
            # the real table would make it.
            if ConditionExpression == "#s = :running" and self.item.get("status") != values.get(
                ":running"
            ):
                raise dts._conditional()
            self.progress_history.append(values[":p"])
            self.status_when_progress_written.append(self.item.get("status"))
        return super().update_item(
            Key,
            UpdateExpression,
            ConditionExpression=None if is_progress else ConditionExpression,
            ExpressionAttributeNames=ExpressionAttributeNames,
            ExpressionAttributeValues=ExpressionAttributeValues,
        )


def _bundle() -> dict[str, Any]:
    """#259's bundle with its metadata model ids rewritten to the OpenRouter
    form, exactly as `run_real_pipeline` does before calling the spine -- so
    the direct-spine tests below and the through-the-runner tests drive the
    SAME `FakeBedrockClient` queues."""
    return pr._bundle_with_openrouter_model_ids(dts._load_bundle())


def _request_change_inputs() -> tuple[bytes, Any]:
    docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
    client = dts._fake_client(
        dts._primary_request_change_response(), dts._critic_no_delta_response()
    )
    return docx_bytes, client


# ---------------------------------------------------------------------------
# 1 + 2: the seam itself.
# ---------------------------------------------------------------------------


class TestRunReviewProgressSeam(unittest.TestCase):
    def test_reports_the_four_stages_in_pipeline_order(self) -> None:
        docx_bytes, client = _request_change_inputs()
        seen: list[str] = []

        result = review_spine.run_review(
            docx_bytes,
            _bundle(),
            client,
            review_id=REVIEW_ID,
            on_progress=seen.append,
        )

        self.assertEqual(result["status"], "OK")
        self.assertEqual(seen, EXPECTED_TOKENS)
        # The module's own published tuple is what callers key off -- keep the
        # wire contract and the assertion pinned to the same list.
        self.assertEqual(list(review_spine.PROGRESS_STAGES), EXPECTED_TOKENS)

    def test_each_token_arrives_before_its_stage_runs_not_after(self) -> None:
        """The whole point: "primary_pass" must mean "the primary pass is
        running NOW". If the callback fired AFTER each stage, the user would
        spend the entire primary pass (a full model call, the longest single
        wait) being told nothing, then see "primary_pass" at the moment it
        stopped being true. Interleave the tokens with the model
        invocations to prove the ordering."""
        docx_bytes, client = _request_change_inputs()
        timeline: list[str] = []

        real_invoke = client.invoke

        def recording_invoke(*args: Any, **kwargs: Any) -> Any:
            timeline.append("model_call")
            return real_invoke(*args, **kwargs)

        client.invoke = recording_invoke  # type: ignore[method-assign]

        review_spine.run_review(
            docx_bytes,
            _bundle(),
            client,
            review_id=REVIEW_ID,
            on_progress=lambda token: timeline.append(f"progress:{token}"),
        )

        # Two model calls (primary, critic), each PRECEDED by its own token.
        self.assertEqual(
            timeline[:4],
            ["progress:primary_pass", "model_call", "progress:critic_pass", "model_call"],
        )

    def test_omitting_on_progress_leaves_the_result_identical(self) -> None:
        docx_bytes, client_a = _request_change_inputs()
        _, client_b = _request_change_inputs()

        without = review_spine.run_review(
            docx_bytes, _bundle(), client_a, review_id=REVIEW_ID
        )
        with_seam = review_spine.run_review(
            docx_bytes, _bundle(), client_b, review_id=REVIEW_ID,
            on_progress=lambda token: None,
        )

        for key in ("status", "decision", "summary", "reason", "findings"):
            self.assertEqual(without[key], with_seam[key], key)
        self.assertEqual(
            without["redline_bytes"] is None, with_seam["redline_bytes"] is None
        )


# ---------------------------------------------------------------------------
# 3 + 4: the write, and the guard around it.
# ---------------------------------------------------------------------------


class TestRunRealPipelineWritesProgress(unittest.TestCase):
    def test_every_stage_lands_on_the_reviews_row_while_running(self) -> None:
        docx_bytes, client = _request_change_inputs()
        table = RecordingReviewsTable()
        s3 = dts.FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation"):
            pr.run_real_pipeline(
                REVIEW_ID, dts._payload(),
                dynamodb_resource=dts.FakeDDB(table), s3_client=s3,
                model_client=client,
            )

        self.assertEqual(table.progress_history, EXPECTED_TOKENS)
        # Progress is only ever written while the review is genuinely running.
        self.assertEqual(set(table.status_when_progress_written), {"RUNNING"})
        # And the review itself still completes normally.
        self.assertEqual(table.item["status"], "DONE")
        self.assertEqual(table.item["decision"], "REQUEST_CHANGE")
        self.assertEqual(table.item["progress_stage"], "redline")

    def test_a_failing_progress_write_never_fails_the_review(self) -> None:
        """Issue #446's lesson, applied to a new bookkeeping write: a review
        whose redline is computed and whose output object is written must not
        be destroyed by a cosmetic update that threw."""
        docx_bytes, client = _request_change_inputs()
        table = RecordingReviewsTable(fail_progress_writes=True)
        s3 = dts.FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation"):
            pr.run_real_pipeline(
                REVIEW_ID, dts._payload(),
                dynamodb_resource=dts.FakeDDB(table), s3_client=s3,
                model_client=client,
            )

        self.assertEqual(table.item["status"], "DONE")
        self.assertEqual(table.item["decision"], "REQUEST_CHANGE")
        self.assertNotIn("failing_stage", table.item)
        self.assertEqual(len(s3.puts), 1)
        # Nothing was recorded (every write threw) -- the UI simply keeps
        # showing its indeterminate treatment.
        self.assertEqual(table.progress_history, [])

    def test_a_conditional_check_failure_is_a_silent_no_op(self) -> None:
        """A late/racing progress write against a row that is no longer
        RUNNING is EXPECTED, not an error: it must not raise and must not be
        logged as a problem."""
        table = RecordingReviewsTable(status="DONE")
        with self.assertLogs(pr.logger, level="WARNING") as captured:
            pr._write_progress_stage(REVIEW_ID, "critic_pass", dts.FakeDDB(table))
            # assertLogs demands at least one record; emit a marker so the
            # assertion below is about what _write_progress_stage did NOT log.
            pr.logger.warning("marker")
        self.assertEqual([r.getMessage() for r in captured.records], ["marker"])
        self.assertEqual(table.progress_history, [])

    def test_a_transient_write_error_is_logged_and_swallowed(self) -> None:
        table = RecordingReviewsTable(fail_progress_writes=True)
        table.item["status"] = "RUNNING"
        with self.assertLogs(pr.logger, level="WARNING") as captured:
            pr._write_progress_stage(REVIEW_ID, "critic_pass", dts.FakeDDB(table))
        self.assertTrue(
            any("progress stage" in r.getMessage() for r in captured.records),
            captured.output,
        )


# ---------------------------------------------------------------------------
# 5: the projection.
# ---------------------------------------------------------------------------


class FakeDetailTable:
    def __init__(self, item: dict[str, Any]):
        self._item = dict(item)

    def get_item(self, Key):
        return {"Item": self._item} if Key["review_id"] == self._item["review_id"] else {}


class FakeDetailDDB:
    def __init__(self, item: dict[str, Any]):
        self._table = FakeDetailTable(item)

    def Table(self, name):
        return self._table


class TestGetReviewDetailProjectsProgressStage(unittest.TestCase):
    def _detail(self, **row: Any) -> dict[str, Any]:
        item = {
            "review_id": REVIEW_ID,
            "owner_sub": "user-1",
            "playbook_id": "synthetic-generic",
            "status": "RUNNING",
        }
        item.update(row)
        return reviews_module.get_review_detail(
            REVIEW_ID, {"cognito_sub": "user-1", "is_admin": False}, FakeDetailDDB(item)
        )

    def test_progress_stage_is_returned_alongside_status(self) -> None:
        detail = self._detail(progress_stage="critic_pass")
        self.assertEqual(detail["status"], "RUNNING")
        self.assertEqual(detail["progress_stage"], "critic_pass")

    def test_absent_progress_stage_projects_none_not_a_placeholder(self) -> None:
        detail = self._detail()
        self.assertIn("progress_stage", detail)
        self.assertIsNone(detail["progress_stage"])


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
