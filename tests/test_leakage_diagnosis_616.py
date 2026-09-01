#!/usr/bin/env python3
"""
Issue #616, first stage: a leakage-blocked review must SAY WHICH DETECTOR
FIRED -- and must still never say what it matched.

## The failure this proves fixed

A real affiliation agreement reviewed against the real EDUCATIONAL-AFFILIATION
playbook was blocked in production by the leakage gate. The row recorded
`reason="leakage_detected"` and nothing else, and `reason` is written
identically for every one of `scripts/leakage_scan.py`'s five detection
categories. So nobody could tell whether the model had echoed the system
prompt, quoted the playbook, or named a precedent counterparty -- which is
exactly the difference between "the gate is correct, fix the prompt upstream"
and "the detector is too broad". The diagnosis EXISTED at every step and was
thrown away twice:

  * `scripts/redline_generate.py` builds it (`field_name`/`category`/
    `rule_id`, straight off `leakage_scan.LeakageDetectedError`) --
    `scripts/review_spine.py::run_review` then assembled a result dict
    without it;
  * `backend/src/pipeline_runner.py::_ANALYSIS_FIELDS` / `_write_real_
    terminal` persist an explicit whitelist, which listed `reason` but none
    of the three.

## The two tests that matter, and why the second is the important one

1. `TestLeakageDiagnosisIsPersisted` -- the category, the rule id and the
   scanned field survive all the way onto the reviews row, into
   `outputs/{review_id}/analysis.json`, and out through the admin
   Diagnostics route's field allowlist.

2. `TestMatchedConfidentialTextNeverLeaves` -- THE CONSTRAINT. The scanner's
   own header promises it reports "detection category, and rule id -- never
   the matched confidential text". This surfaces the first two, so the
   guarantee that the third never rides along with them stops being a
   property of a comment and becomes a property with a test. The playbook
   text the model quoted (a `hard_rejections[].description` -- confidential
   internal reasoning, per `leakage_scan.ConfidentialCorpus.from_playbook`)
   is asserted absent from the persisted row, from the analysis artifact,
   and from the API response.

Both were MUTATION-CHECKED against the diff that makes them pass:
  * breaking the plumbing (dropping `leakage_category` from
    `pipeline_runner._ANALYSIS_FIELDS`, and again from `review_spine`'s
    result assembly) turns (1) red;
  * putting the matched gram into `leakage_rule_id` -- the realistic bug,
    since that is where a "helpful" detector improvement would put it --
    turns (2) red.

## Deliberately NOT asserted here

That the EIAA playbook stops tripping the gate. This slice changes NO
detection behaviour: the same reviews are blocked, they merely now say why.
The behavioural question (#616's own root cause) is answered by the evidence
this change produces, not by loosening anything.

Run standalone: `python3 tests/test_leakage_diagnosis_616.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import leakage_scan  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import reviews  # noqa: E402

# Cross-test-file import (established convention -- see
# tests/test_spend_settlement_actual_usage.py and
# tests/test_model_invocation_ledger.py): reuse #259's real-pipeline docx
# fixture builder, canned responses and DynamoDB/S3 fakes rather than
# duplicating them, so this file exercises the SAME production writers.
import test_dts_pipeline_runner_real_review as dts  # noqa: E402

REVIEW_ID = dts.REVIEW_ID


# ---------------------------------------------------------------------------
# The confidential text the model is made to quote.
#
# Read out of the SAME playbook fixture the pipeline loads, and out of the
# SAME field `ConfidentialCorpus.from_playbook` treats as confidential
# (`hard_rejections[].description` -- "confidential internal-strategy
# reasoning (why a position is a hard line) ... never externally-facing").
# Read, never hardcoded: a fixture edit must move this sentinel with it,
# otherwise the leak assertion below would silently start checking for a
# string nothing could ever have leaked.
# ---------------------------------------------------------------------------


def _confidential_rule_description() -> tuple[str, str]:
    """`(rule_id, description)` of the playbook hard-rejection rule this file
    makes the model quote verbatim."""
    bundle = dts._load_bundle()
    for rule in bundle["hard_rejections"]:
        if rule["id"] == "preserve-liability-cap":
            return rule["id"], rule["description"]
    raise AssertionError("fixture no longer carries the preserve-liability-cap rule")


LEAKED_RULE_ID, LEAKED_DESCRIPTION = _confidential_rule_description()


def _primary_leaky_response() -> str:
    """A REQUEST_CHANGE result identical to #259's, except that the
    counterparty-facing footnote rationale quotes the playbook's confidential
    hard-rejection description verbatim -- the shape the gate exists to catch.

    `verdict_summary` is deliberately CLEAN so the block is attributed to
    `external_rationale_for_footnote`: `scan_model_output` returns the first
    detection in scan order (verdict_summary, then issues), and a field
    attribution that came from the summary would prove nothing about
    per-issue field attribution.
    """
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [
                {
                    "issue_key": "I1",
                    "section_ref": "sec-8",
                    "section_title": "Limitation on Liability",
                    "counterparty_change_summary": (
                        "Counterparty removed the liability cap from Section 8."
                    ),
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": (
                        f"This change is not acceptable to us. {LEAKED_DESCRIPTION}"
                    ),
                    "playbook_topic_id": "limitation-of-liability",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                }
            ],
            # Issue #627: the same edit as a block transcript, addressed to
            # the canonical planted draft every consumer of this fixture
            # family builds (`dts._canonical_planted_draft`). The leak stays
            # where it was -- in `external_rationale_for_footnote` -- so the
            # gate still has to attribute the block to that field, which is
            # what this file is about.
            "block_patches": [
                {
                    "block_id": dts._block_id_for_text(
                        dts._canonical_planted_draft(), dts._SEC8_DRAFT_TEXT
                    ),
                    "segments": [
                        {
                            "op": "delete",
                            "text": dts._SEC8_DRAFT_TEXT,
                            "issue_key": "I1",
                        },
                        {
                            "op": "insert",
                            "text": dts._SEC8_STANDARD_TEXT,
                            "issue_key": "I1",
                        },
                    ],
                }
            ],
            "block_ops": [],
            "critic_delta": None,
            "verdict_summary": (
                "One issue identified in Section 8 requiring attention "
                "before your organization can accept this draft."
            ),
        }
    )


class _Run:
    """One completed `run_real_pipeline` call, with everything a test needs to
    look at: the reviews row the production writer actually shaped, the
    analysis artifact it actually PUT, and the admin Diagnostics projection of
    that same row."""

    def __init__(self, primary_response: str):
        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        client = dts._fake_client(primary_response, dts._critic_no_delta_response())
        self.reviews_table = dts.FakeReviewsTable()
        self.s3 = dts.FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": docx_bytes})
        with patch.object(pr, "_settle_reservation"):
            pr.run_real_pipeline(
                REVIEW_ID,
                dts._payload(),
                dynamodb_resource=dts.FakeDDB(self.reviews_table),
                s3_client=self.s3,
                model_client=client,
            )

    @property
    def row(self) -> dict[str, Any]:
        return self.reviews_table.item

    @property
    def analysis(self) -> dict[str, Any]:
        for put in self.s3.puts:
            if put["Key"] == f"outputs/{REVIEW_ID}/analysis.json":
                return json.loads(put["Body"].decode("utf-8"))
        raise AssertionError("no analysis.json was written")

    def diagnostics_row(self) -> dict[str, Any]:
        """The row as the admin Diagnostics route actually serves it -- driven
        through the REAL `reviews.list_recent_failures`, so its
        `_RECENT_FAILURE_FIELDS` allowlist is what decides what is visible,
        not this test's idea of it."""
        rows = reviews.list_recent_failures(
            {"cognito_sub": "sub-admin", "is_admin": True},
            _DiagnosticsDDB(self.row),
        )
        assert len(rows) == 1, f"expected exactly one failure row, got {rows!r}"
        return rows[0]


class _DetailGetTable:
    def __init__(self, row: dict[str, Any]):
        self._row = row

    def get_item(self, Key):  # noqa: N803 - boto3 kwarg name
        return {"Item": dict(self._row, owner_sub="user-1")}


class _DetailDDB:
    def __init__(self, row: dict[str, Any]):
        self._table = _DetailGetTable(row)

    def Table(self, _name):  # noqa: N802 - boto3 resource API name
        return self._table


class _DiagnosticsScanTable:
    def __init__(self, row: dict[str, Any]):
        self._row = row

    def scan(self, **_kwargs):
        return {"Items": [dict(self._row)]}


class _DiagnosticsDDB:
    def __init__(self, row: dict[str, Any]):
        self._table = _DiagnosticsScanTable(row)

    def Table(self, _name):  # noqa: N802 - boto3 resource API name
        return self._table


# ---------------------------------------------------------------------------
# 1. The diagnosis reaches an operator.
# ---------------------------------------------------------------------------


class TestLeakageDiagnosisIsPersisted(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pipeline = _Run(_primary_leaky_response())

    def test_the_review_really_was_blocked_by_the_leakage_gate(self) -> None:
        """Guards the rest of this class: if the fixture stopped tripping the
        gate, every assertion below would be vacuous."""
        self.assertEqual(self.pipeline.row["status"], "ERROR_MANUAL_REVIEW_REQUIRED")
        self.assertEqual(self.pipeline.row["reason"], "leakage_detected")
        self.assertIsNone(self.pipeline.row.get("output_s3_key"))

    def test_the_row_names_the_detector_that_fired(self) -> None:
        self.assertEqual(self.pipeline.row.get("leakage_category"), leakage_scan.CATEGORY_PLAYBOOK)
        self.assertEqual(self.pipeline.row.get("leakage_rule_id"), "playbook-ngram")
        self.assertEqual(
            self.pipeline.row.get("leakage_field_name"), "external_rationale_for_footnote"
        )

    def test_the_analysis_artifact_names_the_detector_that_fired(self) -> None:
        analysis = self.pipeline.analysis
        self.assertEqual(analysis["leakage_category"], leakage_scan.CATEGORY_PLAYBOOK)
        self.assertEqual(analysis["leakage_rule_id"], "playbook-ngram")
        self.assertEqual(analysis["leakage_field_name"], "external_rationale_for_footnote")

    def test_the_admin_diagnostics_route_serves_the_detector(self) -> None:
        row = self.pipeline.diagnostics_row()
        self.assertEqual(row["reason"], "leakage_detected")
        self.assertEqual(row["leakage_category"], leakage_scan.CATEGORY_PLAYBOOK)
        self.assertEqual(row["leakage_rule_id"], "playbook-ngram")
        self.assertEqual(row["leakage_field_name"], "external_rationale_for_footnote")

    def test_the_category_is_one_of_the_scanners_own_constants(self) -> None:
        """Not free text: an operator branching on this value, and the
        Diagnostics line rendering it, both depend on the stable vocabulary
        `leakage_scan.py` declares."""
        self.assertIn(
            self.pipeline.row["leakage_category"],
            {
                leakage_scan.CATEGORY_SYSTEM_PROMPT,
                leakage_scan.CATEGORY_PLAYBOOK,
                leakage_scan.CATEGORY_CITATION,
                leakage_scan.CATEGORY_PRECEDENT_QUOTATION,
                leakage_scan.CATEGORY_CONFIDENTIAL_RATIONALE,
            },
        )

    def test_the_failure_is_stage_attributed(self) -> None:
        """Issue #616 second finding: the Diagnostics STAGE column rendered an
        em dash for these. `run_review`'s fail-closed conditions come back as
        a status DICT rather than a raised exception, so
        `reviews.record_stage_failure` -- the only writer that stamped
        `failing_stage` -- never ran for any of them."""
        self.assertEqual(self.pipeline.row.get("failing_stage"), "run_review")
        self.assertEqual(self.pipeline.diagnostics_row()["failing_stage"], "run_review")


# ---------------------------------------------------------------------------
# 2. THE CONSTRAINT: what was matched never leaves the scanner.
# ---------------------------------------------------------------------------


class TestMatchedConfidentialTextNeverLeaves(unittest.TestCase):
    """`scripts/leakage_scan.py`: reports "detection category, and rule id --
    never the matched confidential text". Now that the first two are
    persisted, served and rendered, that promise needs a test rather than a
    comment."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pipeline = _Run(_primary_leaky_response())

    def _assert_no_confidential_text(self, label: str, haystack: str) -> None:
        self.assertNotIn(LEAKED_DESCRIPTION, haystack, f"{label} carries the matched text")
        # Not merely the whole sentence: no distinctive fragment of it either,
        # so a truncated or partially-quoted leak is caught too.
        for fragment in ("removes or alters", "aggregate liability cap"):
            self.assertNotIn(
                fragment, haystack, f"{label} carries a fragment of the matched text"
            )

    def test_the_fixture_really_does_quote_confidential_playbook_text(self) -> None:
        """Guards this whole class. If the model output stopped carrying the
        confidential description, 'it is absent from the record' would be true
        for the wrong reason."""
        self.assertIn(LEAKED_DESCRIPTION, _primary_leaky_response())
        self.assertGreater(len(LEAKED_DESCRIPTION), 40)
        self.assertEqual(self.pipeline.row["reason"], "leakage_detected")

    def test_the_persisted_reviews_row_carries_no_matched_text(self) -> None:
        self._assert_no_confidential_text(
            "the reviews row", json.dumps(self.pipeline.row, default=str)
        )

    def test_the_analysis_artifact_carries_no_matched_text(self) -> None:
        self._assert_no_confidential_text(
            "analysis.json", json.dumps(self.pipeline.analysis, default=str)
        )

    def test_the_admin_diagnostics_response_carries_no_matched_text(self) -> None:
        self._assert_no_confidential_text(
            "the diagnostics response", json.dumps(self.pipeline.diagnostics_row(), default=str)
        )

    def test_no_leaked_model_prose_survives_at_all(self) -> None:
        """The wider promise the gate makes: a leakage block produces NO
        human-surfaced model output, not a redacted one. So the leaking issue
        itself is gone from `findings`, not merely scrubbed of the matched
        span."""
        self.assertEqual(self.pipeline.analysis["findings"], [])
        self.assertIsNone(self.pipeline.analysis["summary"])
        self.assertNotIn("issues", self.pipeline.row)

    def test_the_end_user_review_detail_is_told_nothing_technical(self) -> None:
        """Issue #616 scope line: the detector is ADMIN detail. The reader's
        own result screen keeps the non-technical `leakage_detected` prose
        (`frontend/src/ReviewSubmission.tsx`) and is handed no detector
        internals to render -- `get_review_detail` is an explicit projection,
        and these three fields are deliberately not in it, so a row attribute
        added for the admin surface cannot arrive on the reader's."""
        detail = reviews.get_review_detail(
            REVIEW_ID,
            {"cognito_sub": "user-1", "is_admin": False},
            _DetailDDB(self.pipeline.row),
        )
        self.assertEqual(detail["reason"], "leakage_detected")
        for field in ("leakage_category", "leakage_rule_id", "leakage_field_name"):
            with self.subTest(field=field):
                self.assertNotIn(field, detail)
        self._assert_no_confidential_text(
            "the review-detail response", json.dumps(detail, default=str)
        )

    def test_the_error_object_has_nowhere_to_put_a_matched_span(self) -> None:
        """Structural, not incidental: the exception the pipeline catches and
        reads these fields off carries no matched text to begin with, which is
        why surfacing the fields it DOES carry is safe."""
        exc = leakage_scan.LeakageDetectedError(
            field_name="verdict_summary",
            category=leakage_scan.CATEGORY_PLAYBOOK,
            rule_id="playbook-ngram",
        )
        self.assertEqual(
            sorted(vars(exc)),
            sorted(["field_name", "category", "rule_id", "confidence_state"]),
        )


# ---------------------------------------------------------------------------
# 3. Absent, never a null placeholder, on a review that was not blocked.
# ---------------------------------------------------------------------------


class TestCleanReviewCarriesNoLeakageFields(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pipeline = _Run(dts._primary_request_change_response())

    def test_a_clean_review_still_completes(self) -> None:
        self.assertEqual(self.pipeline.row["status"], "DONE")

    def test_the_row_grows_no_leakage_attributes(self) -> None:
        for field in ("leakage_category", "leakage_rule_id", "leakage_field_name"):
            with self.subTest(field=field):
                self.assertNotIn(field, self.pipeline.row)

    def test_a_clean_review_is_not_stage_attributed_as_a_failure(self) -> None:
        self.assertNotIn("failing_stage", self.pipeline.row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
