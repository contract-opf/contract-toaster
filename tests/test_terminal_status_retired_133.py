#!/usr/bin/env python3
"""
Issue #133 (owner decision 2026-09-16): a review never concludes as "manual
review required".

A run that completes is DONE (with its decision and its file); a run that does
not complete is ERROR, carrying the SAME `reason` token it carried before, so
the attorney-facing copy keyed on `reason` is unchanged. `MANUAL_REVIEW_REQUIRED`
and `ERROR_MANUAL_REVIEW_REQUIRED` are retired as terminal statuses -- no
writer produces them -- and a row stored with one before the change is still
read (as ERROR) with no migration.

This file drives the REAL producers, not hand-built result dicts:

  1. The spine (`scripts/review_spine.py::run_review`) through
     `unnormalizable_input` (a zero-width character in the body -- the
     control-character screen), `document_too_large` (a document over
     `primary_review_pass.MAX_INPUT_TOKENS`) and `leakage_detected` (the model
     quoting the playbook's confidential hard-rejection description), and
     asserts `status == "ERROR"` with the original `reason`.
  2. The in-process runner (`backend/src/pipeline_runner.py::
     run_real_pipeline`) through those same three, plus `redline_not_persisted`
     (a REQUEST_CHANGE whose every issue is flag-only, the #584 shape), and
     asserts the reviews row the production writer shaped: `status == "ERROR"`,
     the original `reason`, no `decision`.
  3. The other writers the ticket names: `reviews.STAGE_FAILURE_REASON_STATUS`
     / `record_stage_failure`, the mock pipeline, and the AWS persist Lambda --
     each run against a REAL (moto) `reviews` table built by
     `tests/ddb_fixtures.create_reviews_table`, on a seeded RUNNING row, and
     judged by the item DynamoDB stored. A recording fake that keeps only
     `ExpressionAttributeValues` would accept a SET clause naming a
     placeholder with no value (or a value no clause uses), which DynamoDB
     refuses with a ValidationException; moto refuses it too.
  4. The one behaviour the collapse could have silently changed: the spine
     used to withhold `findings` / `critic_delta` on exactly the redline-stage
     results that carried `ERROR_MANUAL_REVIEW_REQUIRED`. That set is now named
     by `redline_generate.output_withheld`, and both variants are pinned -- a
     leakage block still withholds, an unprovable transcript still does not.

Fixtures: every document and model response here is reused from the suites
that already prove these producers (`test_dts_pipeline_runner_real_review`,
`test_leakage_diagnosis_616`, `test_block_mode_e2e`), or built from the same
raw-OOXML builder they use. Nothing here hand-writes a status.

Run standalone: `python3 tests/test_terminal_status_retired_133.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402, I001
from moto import mock_aws  # noqa: E402

import model_client  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import redline_generate  # noqa: E402
import review_spine  # noqa: E402
import reviews  # noqa: E402

# Cross-file reuse of already-proven synthetic builders, canned responses and
# fakes -- the convention tests/test_leakage_diagnosis_616.py and
# tests/test_terminal_reason_completeness_670.py already follow.
import test_dts_pipeline_runner_real_review as dts  # noqa: E402
from ddb_fixtures import create_reviews_table  # noqa: E402
from test_block_mode_e2e import (  # noqa: E402
    SECTIONS,
    _issue,
    _make_docx,
    _reconciled,
    _run_block_mode,
)
from test_leakage_diagnosis_616 import LEAKED_DESCRIPTION, _primary_leaky_response  # noqa: E402

REVIEW_ID = dts.REVIEW_ID
RETIRED = ("MANUAL_REVIEW_REQUIRED", "ERROR_MANUAL_REVIEW_REQUIRED")

_FILLER_UNIT = "The parties agree to this synthetic clause. "


# ---------------------------------------------------------------------------
# Documents -- raw OOXML through the same builder the runner suite uses.
# ---------------------------------------------------------------------------


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _unnormalizable_docx() -> bytes:
    """A body paragraph carrying U+200B. `normalize_input`'s control-character
    screen (issue #632) refuses the whole document: the model and the attorney
    would no longer be reading the same text."""
    return dts._build_docx_bytes(
        dts._heading_p("Section 1") + _paragraph("The parties​ agree to these terms.")
    )


def _oversized_docx() -> bytes:
    """One paragraph whose text ALONE exceeds the assembled-input cap, in the
    units the cap is judged in (the calibrated input estimate)."""
    target = int(pp.MAX_INPUT_TOKENS * pp.INPUT_CHARS_PER_TOKEN_ESTIMATE) + 40_000
    text = (_FILLER_UNIT * (target // len(_FILLER_UNIT) + 2))[:target]
    assert pp.estimate_input_tokens(text) > pp.MAX_INPUT_TOKENS, "fixture is not oversized"
    return dts._build_docx_bytes(dts._heading_p("Section 1") + _paragraph(text))


def _flag_only_primary_response() -> str:
    """The #584 shape: a genuine REQUEST_CHANGE finding with no edits at all,
    so there is nothing to persist. Copied from
    test_dts_pipeline_runner_real_review's own #584 case."""
    payload = json.loads(dts._primary_request_change_response())
    payload["block_patches"] = []
    payload["block_ops"] = []
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# 1. The spine
# ---------------------------------------------------------------------------


def _spine(docx_bytes: bytes, primary_responses: list[str], review_id: str) -> dict[str, Any]:
    bundle = dts._load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    client = model_client.FakeBedrockClient(
        {primary_id: list(primary_responses), critic_id: [dts._critic_no_delta_response()]}
    )
    return review_spine.run_review(docx_bytes, bundle, client, review_id=review_id)


class TestSpineFailsAsError(unittest.TestCase):
    def assert_error(self, result: dict[str, Any], reason: str) -> None:
        self.assertEqual(result["status"], "ERROR", result.get("reason"))
        self.assertNotIn(result["status"], RETIRED)
        self.assertEqual(result["reason"], reason)
        # A failure never carries a decision or a document.
        self.assertIsNone(result["decision"])
        self.assertIsNone(result["redline_bytes"])

    def test_unnormalizable_input(self) -> None:
        result = _spine(_unnormalizable_docx(), [], "retired-133-unnormalizable")
        self.assert_error(result, "unnormalizable_input")

    def test_document_too_large(self) -> None:
        result = _spine(_oversized_docx(), [], "retired-133-too-large")
        self.assert_error(result, "document_too_large")

    def test_leakage_detected_still_withholds_every_model_derived_field(self) -> None:
        result = _spine(
            dts._canonical_planted_draft(), [_primary_leaky_response()], "retired-133-leak"
        )
        self.assert_error(result, "leakage_detected")
        # The withholding the retired ERROR_MANUAL_REVIEW_REQUIRED status used
        # to key: no findings, no critic delta, and the leaked text nowhere.
        self.assertEqual(result["findings"], [])
        self.assertIsNone(result["critic_delta"])
        self.assertNotIn(LEAKED_DESCRIPTION, json.dumps(result, default=str))

    def test_the_retired_status_constants_are_gone(self) -> None:
        self.assertEqual(review_spine.STATUS_ERROR, "ERROR")
        for name in ("STATUS_MANUAL_REVIEW_REQUIRED", "STATUS_ERROR_MANUAL_REVIEW_REQUIRED"):
            self.assertFalse(hasattr(review_spine, name), name)
        for name in ("MANUAL_REVIEW_REQUIRED", "ERROR_MANUAL_REVIEW_REQUIRED"):
            self.assertFalse(hasattr(redline_generate, name), name)


# ---------------------------------------------------------------------------
# 2. The in-process runner -- the row the production writer actually shapes.
# ---------------------------------------------------------------------------


def _run_pipeline(docx_bytes: bytes, primary_responses: list[str]) -> dict[str, Any]:
    primary_id = model_client.openrouter_primary_model_id()
    critic_id = model_client.openrouter_critic_model_id()
    client = model_client.FakeBedrockClient(
        {primary_id: list(primary_responses), critic_id: [dts._critic_no_delta_response()]}
    )
    reviews_table = dts.FakeReviewsTable()
    s3 = dts.FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": docx_bytes})
    with patch.object(pr, "_settle_reservation"):
        pr.run_real_pipeline(
            REVIEW_ID,
            dts._payload(),
            dynamodb_resource=dts.FakeDDB(reviews_table),
            s3_client=s3,
            model_client=client,
        )
    return reviews_table.item


class TestInProcessRunnerWritesError(unittest.TestCase):
    def assert_error_row(self, row: dict[str, Any], reason: str) -> None:
        self.assertEqual(row["status"], "ERROR", row)
        self.assertNotIn(row["status"], RETIRED)
        self.assertEqual(row.get("reason"), reason)
        self.assertIsNone(row.get("decision"))
        self.assertNotIn("output_s3_key", row)
        self.assertIn("failed_at", row)
        self.assertNotIn("completed_at", row)

    def test_unnormalizable_input(self) -> None:
        self.assert_error_row(_run_pipeline(_unnormalizable_docx(), []), "unnormalizable_input")

    def test_document_too_large(self) -> None:
        self.assert_error_row(_run_pipeline(_oversized_docx(), []), "document_too_large")

    def test_leakage_detected(self) -> None:
        row = _run_pipeline(dts._canonical_planted_draft(), [_primary_leaky_response()])
        self.assert_error_row(row, "leakage_detected")
        self.assertEqual(row.get("leakage_field_name"), "external_rationale_for_footnote")

    def test_redline_not_persisted(self) -> None:
        # Two identical responses: the empty edit is a pen-rules violation that
        # spends one retry before demoting to flag-only (see the #584 case).
        flag_only = _flag_only_primary_response()
        row = _run_pipeline(
            dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT}), [flag_only, flag_only]
        )
        self.assert_error_row(row, "redline_not_persisted")
        self.assertEqual(row.get("failing_stage"), "persist_result")


# ---------------------------------------------------------------------------
# 3. Every other writer the ticket names.
# ---------------------------------------------------------------------------


def _load_persist_handler() -> Any:
    spec = importlib.util.spec_from_file_location(
        "persist_handler_133", REPO_ROOT / "infra" / "lambda" / "persist" / "handler.py"
    )
    assert spec is not None and spec.loader is not None
    persist = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(persist)
    return persist


class TestEveryOtherWriter(unittest.TestCase):
    """Each writer runs against a REAL (moto) `reviews` table declared as the
    CDK declares it, on a RUNNING row, and is judged by the stored item -- the
    same arrangement `tests/test_completed_at_terminal_write_71.py` uses. An
    UpdateExpression whose SET clause and value map disagree raises here,
    exactly as it would against DynamoDB."""

    #: The reasons that used to land on one of the two retired statuses.
    FORMERLY_RETIRED = (
        "structured_output_retry_exhausted",
        "document_too_large",
        "model_context_length_exceeded",
        "redline_not_persisted",
    )

    def setUp(self) -> None:
        self._mock = mock_aws()
        self._mock.start()
        self.addCleanup(self._mock.stop)
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.table = create_reviews_table(self.ddb)

    def _seed_running_row(self) -> None:
        """A review the pipeline has picked up: no decision, no reason yet.
        `put_item` replaces the row, so each subtest starts from the same one."""
        now = "1790000000"
        self.table.put_item(
            Item={
                "review_id": REVIEW_ID,
                "owner_sub": "reviewer-1",
                "playbook_id": "synthetic-nda-sample",
                "playbook_hash": "a" * 64,
                "status": "RUNNING",
                "created_at": now,
                "updated_at": now,
            }
        )

    def _row(self) -> dict[str, Any]:
        return self.table.get_item(Key={"review_id": REVIEW_ID})["Item"]

    def assert_error_row(self, row: dict[str, Any], reason: str) -> None:
        self.assertEqual(row["status"], "ERROR", row)
        self.assertNotIn(row["status"], RETIRED)
        self.assertEqual(row.get("reason"), reason)
        # Not merely null: a row that did not complete carries no decision.
        self.assertNotIn("decision", row)
        self.assertNotIn("output_s3_key", row)
        self.assertNotIn("completed_at", row)

    def test_the_taxonomy_maps_every_reason_to_error(self) -> None:
        self.assertEqual(set(reviews.STAGE_FAILURE_REASON_STATUS.values()), {"ERROR"})
        for reason in self.FORMERLY_RETIRED:
            self.assertIn(reason, reviews.STAGE_FAILURE_REASON_STATUS)

    def test_record_stage_failure_writes_error_with_the_same_reason(self) -> None:
        for reason in self.FORMERLY_RETIRED:
            with self.subTest(reason=reason):
                self._seed_running_row()
                written = reviews.record_stage_failure(REVIEW_ID, "run_review", reason, self.ddb)
                self.assertEqual(written, "ERROR")
                row = self._row()
                self.assert_error_row(row, reason)
                self.assertEqual(row.get("failing_stage"), "run_review")
                self.assertIn("failed_at", row)

    def test_no_status_message_promises_a_manual_review(self) -> None:
        for status_value in RETIRED:
            self.assertNotIn(status_value, reviews.STATUS_USER_MESSAGES)

    def test_legacy_rows_are_still_terminal_on_read(self) -> None:
        for status_value in RETIRED:
            self.assertIn(status_value, reviews.REVIEW_STATUSES_TERMINAL)

    def test_the_mock_pipeline_decides_nothing_for_a_run_it_cannot_complete(self) -> None:
        for playbook_id, reason in (
            ("synthetic-nda-sample", "playbook_coming_soon"),
            ("no-such-playbook", "unknown_playbook"),
        ):
            with self.subTest(playbook_id=playbook_id):
                result = pr._mock_decision(REVIEW_ID, playbook_id)
                self.assertIsNone(result["decision"])
                self.assertEqual(result["reason"], reason)
                self._seed_running_row()
                pr._write_terminal(REVIEW_ID, result, False, self.ddb)
                self.assert_error_row(self._row(), reason)

    def test_the_aws_persist_lambda_writes_error_for_a_run_with_no_decision(self) -> None:
        persist = _load_persist_handler()
        self._seed_running_row()
        with patch.object(persist, "REVIEWS_TABLE", self.table.name):
            persist._write_terminal_reviews_state(
                {"review_id": REVIEW_ID, "decision": None, "reason": "playbook_coming_soon"},
                self.ddb,
            )
        self.assert_error_row(self._row(), "playbook_coming_soon")


# ---------------------------------------------------------------------------
# 4. Which ERROR results withhold output -- both variants.
# ---------------------------------------------------------------------------


class TestOutputWithheldIsNamedNotInferred(unittest.TestCase):
    def test_an_unprovable_transcript_fails_without_withholding(self) -> None:
        """`block_transcript_rejected` used to be plain MANUAL_REVIEW_REQUIRED,
        whose findings the spine surfaced. It is ERROR now, and must still not
        be mistaken for a leakage block."""
        response = {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "issues": [_issue("I1", "term-length", section="Section 1. Term and Fee")],
            "block_patches": [
                {
                    "block_id": "p9999",  # not in this document's block map
                    "segments": [
                        {"op": "keep", "text": "The Term shall be "},
                        {"op": "delete", "text": "sixty (60)", "issue_key": "I1"},
                        {"op": "insert", "text": "thirty (30)", "issue_key": "I1"},
                    ],
                }
            ],
        }
        result = _run_block_mode(_reconciled(response), _make_docx(SECTIONS))
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["reason"], redline_generate.REASON_BLOCK_TRANSCRIPT_REJECTED)
        self.assertFalse(redline_generate.output_withheld(result))

    def test_a_leakage_block_withholds(self) -> None:
        """The other variant, at the same stage boundary the spine reads."""
        captured: list[dict[str, Any]] = []
        real = redline_generate.generate_redline_from_blocks

        def spy(**kwargs: Any) -> dict[str, Any]:
            result = real(**kwargs)
            captured.append(result)
            return result

        with patch.object(redline_generate, "generate_redline_from_blocks", spy):
            _spine(
                dts._canonical_planted_draft(), [_primary_leaky_response()], "retired-133-spy"
            )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["status"], "ERROR")
        self.assertEqual(captured[0]["reason"], "leakage_detected")
        self.assertTrue(redline_generate.output_withheld(captured[0]))


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestSpineFailsAsError,
        TestInProcessRunnerWritesError,
        TestEveryOtherWriter,
        TestOutputWithheldIsNamedNotInferred,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
