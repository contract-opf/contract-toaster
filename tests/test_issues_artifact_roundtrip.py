#!/usr/bin/env python3
"""
Executable tests for the `issues` / `critic_delta` phantom-attribute bug.

## The bug

`scripts/review_spine.py` renames TWO of the pipeline's output keys when it
assembles the review result:

    verdict_summary -> summary     (lands on the reviews row; fixed in 561ac0d)
    issues          -> findings    (lands ONLY in the S3 analysis artifact)

The first rename was found and fixed. The second was not. `findings` and
`critic_delta` are written by `pipeline_runner._write_real_analysis` to
`outputs/{review_id}/analysis.json` (issue #416's `_ANALYSIS_FIELDS`) and by
NOTHING to the `reviews` row -- not `_write_terminal`, not
`_write_real_terminal`, not `infra/lambda/persist/handler.py`.

Every reader, however, read them off the row:

  * `reviews.get_review_detail`            -> item.get("issues") / ("critic_delta")
  * `review_routes.post_review_cover_note` -> item.get("issues") or []

so on EVERY real review:

  * `GET /api/reviews/{id}` returned `issues: null, critic_delta: null`;
  * the receipt's changes-requested / clauses-touched / critic counts rendered
    empty (frontend/src/toaster/receipt.ts);
  * the critic-delta panel rendered nothing (frontend/src/ReviewSubmission.tsx);
  * #499's "Butter it" 409'd with "This review has no requested changes to
    describe" -- the feature was non-functional in production.

## The fix under test

`reviews.load_analysis_artifact` reads the artifact off the row's OWN
`analysis_s3_key` and both readers go through it. The fields are deliberately
NOT added to the DynamoDB row: the artifact is already destroyed by the purge's
`outputs/{review_id}/` prefix scan, whereas the row carries a 35-day PITR
retention floor (docs/data-handling.md).

## Why no existing test caught it

Every fixture hand-seeded `item["issues"]` on the row -- the READER's key --
so fixture and reader agreed and the suite stayed green while production
returned null. A fake that accepts what the real dependency would reject is
not a test.

So the round-trip tests below drive the REAL writer
(`pipeline_runner._write_real_analysis`) into a real (moto) S3 bucket and read
back through the REAL reader (`reviews.get_review_detail`). Nothing here
hand-seeds the field under test.

This file MUST FAIL on the pre-fix tree: every round-trip assertion comes back
None because the reader looked at the row.

Run standalone: `python3 tests/test_issues_artifact_roundtrip.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-artifact-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-artifact-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-artifact-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-artifact-test")
os.environ.setdefault(
    "RETENTION_SETTINGS_TABLE", "contract-toaster-retention-artifact-test"
)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.retention as retention_module  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

DAY = 86400

OWNER = {"cognito_sub": "owner-artifact", "email": "owner@example.com"}

# A findings list shaped like the real thing: the prose fields the cover-note
# drafter reads, plus the locator/metadata fields the UI renders.
FINDINGS = [
    {
        "section_ref": "8",
        "section_title": "Limitation on Liability",
        "decision": "REQUEST_CHANGE",
        "counterparty_change_summary": "The liability cap was struck entirely.",
        "external_rationale_for_footnote": "We require a mutual cap at fees paid.",
        "proposed_replacement_text": "...capped at the fees paid in the prior 12 months.",
        "playbook_topic_id": "liability-cap",
        "provenance": "model",
    },
    {
        "section_ref": "12",
        "section_title": "Indemnification",
        "decision": "REQUEST_CHANGE",
        "counterparty_change_summary": "Indemnity was made one-way.",
        "external_rationale_for_footnote": "Indemnities are mutual in our form.",
        "proposed_replacement_text": "Each party shall indemnify the other...",
        "playbook_topic_id": "indemnity-mutual",
        "provenance": "critic-added",
    },
]

CRITIC_DELTA = {
    "added_issues": [{"section_ref": "12", "playbook_topic_id": "indemnity-mutual"}],
    "contested_replacements": [
        {
            "section_ref": "8",
            "critic_objection": "Primary's cap drifts from the playbook position.",
            "critic_suggested_replacement": "...capped at fees paid.",
        }
    ],
    "rationale_objections": [],
}


class AnalysisArtifactTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()

        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = boto3.client("s3", region_name="us-east-1")

        self.ddb.create_table(
            TableName=os.environ["REVIEWS_TABLE"],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        ).wait_until_exists()
        self.s3.create_bucket(Bucket=os.environ["UPLOADS_BUCKET"])
        self.s3.create_bucket(Bucket=os.environ["OUTPUTS_BUCKET"])

        self.table = self.ddb.Table(os.environ["REVIEWS_TABLE"])

    def tearDown(self) -> None:
        self._mock_aws.stop()

    def _row(self, review_id: str) -> dict[str, Any]:
        return self.table.get_item(Key={"review_id": review_id}).get("Item") or {}

    def _seed_bare_row(
        self, review_id: str, *, age_days: int = 1, window_days: int = 90, **extra: Any
    ) -> None:
        """Identity/ownership only. Deliberately carries NO `issues`,
        `critic_delta` or `analysis_s3_key` -- everything under test must
        arrive via a real writer."""
        import time as _time

        item: dict[str, Any] = {
            "review_id": review_id,
            "owner_sub": OWNER["cognito_sub"],
            "status": "DONE",
            "created_at": str(int(_time.time()) - age_days * DAY),
            "retention_window_at_creation": window_days,
            "legal_hold": False,
        }
        item.update(extra)
        self.table.put_item(Item=item)

    def _run_real_pipeline_writers(self, review_id: str) -> str:
        """Drive the REAL artifact writer, then record its key on the row the
        way `run_real_pipeline` does -- artifact first, then the row that
        points at it (pipeline_runner's documented ordering)."""
        result = {
            "status": "OK",
            "decision": "REQUEST_CHANGE",
            "summary": "Cap restored; indemnity made mutual.",
            "findings": FINDINGS,
            "critic_delta": CRITIC_DELTA,
        }
        key = pipeline_runner._write_real_analysis(review_id, result, self.s3)
        pipeline_runner._write_real_terminal(review_id, result, None, self.ddb, key)
        return key


# ---------------------------------------------------------------------------
# 1. The real writer's output reaches the real reader.
# ---------------------------------------------------------------------------


class TestArtifactReachesGetReviewDetail(AnalysisArtifactTestBase):
    def test_findings_reach_get_review_detail_as_issues(self):
        """Fails pre-fix: the reader looked for `issues` on the row, which no
        writer has ever put there, so this came back None on every real
        review -- and with it the receipt's counts and #499's 409 gate."""
        self._seed_bare_row("rid-issues")
        self._run_real_pipeline_writers("rid-issues")

        detail = reviews_module.get_review_detail(
            "rid-issues", OWNER, self.ddb, self.s3
        )

        self.assertIsNotNone(
            detail["issues"],
            "get_review_detail must surface the findings the REAL pipeline "
            "writer just persisted. Pre-fix this was None on every real "
            "review because the reader read the row and the writer wrote S3.",
        )
        self.assertEqual(len(detail["issues"]), 2)
        self.assertEqual(
            detail["issues"][0]["counterparty_change_summary"],
            "The liability cap was struck entirely.",
        )

    def test_critic_delta_reaches_get_review_detail(self):
        self._seed_bare_row("rid-critic")
        self._run_real_pipeline_writers("rid-critic")

        detail = reviews_module.get_review_detail(
            "rid-critic", OWNER, self.ddb, self.s3
        )

        self.assertIsNotNone(detail["critic_delta"])
        self.assertEqual(len(detail["critic_delta"]["contested_replacements"]), 1)

    def test_no_writer_puts_issues_or_critic_delta_on_the_row(self):
        """The anti-regression guard. After a REAL write the row carries
        `analysis_s3_key` and NEITHER `issues` NOR `critic_delta`.

        If this ever fails, a writer started persisting document substance to
        DynamoDB -- which inherits the 35-day PITR floor and must be added to
        BOTH purge lists in lockstep before it is allowed to ship."""
        self._seed_bare_row("rid-names")
        self._run_real_pipeline_writers("rid-names")

        row = self._row("rid-names")
        self.assertIn("analysis_s3_key", row)
        self.assertNotIn("issues", row)
        self.assertNotIn("critic_delta", row)


# ---------------------------------------------------------------------------
# 2. It degrades to None instead of raising. Every failure mode, separately.
# ---------------------------------------------------------------------------


class TestArtifactReadDegradesQuietly(AnalysisArtifactTestBase):
    def _detail(self, review_id: str, s3_client: Any) -> dict[str, Any]:
        return reviews_module.get_review_detail(review_id, OWNER, self.ddb, s3_client)

    def test_no_s3_client_skips_the_read(self):
        """`request_cancel`'s internal calls pass no client and only need
        `status` -- they must not pay for, or fail on, an S3 round trip."""
        self._seed_bare_row("rid-noclient")
        self._run_real_pipeline_writers("rid-noclient")

        detail = self._detail("rid-noclient", None)

        self.assertIsNone(detail["issues"])
        self.assertEqual(detail["status"], "DONE")

    def test_row_without_analysis_key_reads_back_none(self):
        """A review that predates the artifact, or one persisted by the Step
        Functions target (whose Lambda writes no artifact, pending #80-#83)."""
        self._seed_bare_row("rid-nokey")

        self.assertIsNone(self._detail("rid-nokey", self.s3)["issues"])

    def test_missing_s3_object_reads_back_none(self):
        self._seed_bare_row("rid-gone")
        key = self._run_real_pipeline_writers("rid-gone")
        self.s3.delete_object(Bucket=os.environ["OUTPUTS_BUCKET"], Key=key)

        self.assertIsNone(self._detail("rid-gone", self.s3)["issues"])

    def test_malformed_json_body_reads_back_none(self):
        self._seed_bare_row("rid-badjson")
        key = self._run_real_pipeline_writers("rid-badjson")
        self.s3.put_object(
            Bucket=os.environ["OUTPUTS_BUCKET"], Key=key, Body=b"{not json at all"
        )

        self.assertIsNone(self._detail("rid-badjson", self.s3)["issues"])

    def test_valid_json_that_is_not_an_object_reads_back_none(self):
        """Valid JSON is not a usable artifact. A body that parses to a list
        would clear the decode guard and then raise AttributeError on
        `.get(...)` -- a 500 on the detail route, which this read promises
        never to cause."""
        self._seed_bare_row("rid-jsonlist")
        key = self._run_real_pipeline_writers("rid-jsonlist")
        self.s3.put_object(
            Bucket=os.environ["OUTPUTS_BUCKET"], Key=key, Body=b"[1, 2, 3]"
        )

        self.assertIsNone(self._detail("rid-jsonlist", self.s3)["issues"])


# ---------------------------------------------------------------------------
# 3. The cover-note gate #499 shipped with now passes on a real review.
# ---------------------------------------------------------------------------


class TestCoverNoteGateSeesRealFindings(AnalysisArtifactTestBase):
    def test_the_gate_input_is_non_empty_for_a_review_with_real_findings(self):
        """#499's route does `issues = <read> or []` then `if not issues: 409`.
        Reading the row made that gate fire on EVERY real review. Exercises
        the SAME shared helper the route calls -- not a copy of it."""
        self._seed_bare_row("rid-butter")
        self._run_real_pipeline_writers("rid-butter")

        analysis = reviews_module.load_analysis_artifact(
            self._row("rid-butter"), self.s3
        )
        issues = (analysis.get("findings") if analysis is not None else None) or []

        self.assertTrue(
            issues,
            "'Butter it' 409'd on every real review because this list was "
            "always empty -- the feature never ran in production.",
        )
        self.assertEqual(len(issues), 2)

    def test_the_drafter_and_the_gate_read_the_same_list(self):
        """The 409 gate and the prompt builder must not diverge; a second
        independent reader is how this class of drift starts."""
        self._seed_bare_row("rid-same")
        self._run_real_pipeline_writers("rid-same")
        row = self._row("rid-same")

        gate_issues = reviews_module.load_analysis_artifact(row, self.s3)["findings"]
        detail_issues = reviews_module.get_review_detail(
            "rid-same", OWNER, self.ddb, self.s3
        )["issues"]

        self.assertEqual(gate_issues, detail_issues)


# ---------------------------------------------------------------------------
# 4. A purge really does take the findings with it.
# ---------------------------------------------------------------------------


class TestPurgeDestroysTheArtifact(AnalysisArtifactTestBase):
    def test_after_a_purge_issues_read_back_none_through_the_real_reader(self):
        """The property that actually matters: serving substance from S3 is
        only safe if the purge reaches it. It does -- via the
        `outputs/{review_id}/` prefix scan, not a field list."""
        self._seed_bare_row("rid-purge", age_days=400, window_days=90)
        key = self._run_real_pipeline_writers("rid-purge")

        summary = retention_module.run_purge_sweep_now(self.s3, self.ddb)
        self.assertIn("rid-purge", summary["deleted_reviews"])

        listed = self.s3.list_objects_v2(
            Bucket=os.environ["OUTPUTS_BUCKET"], Prefix=f"outputs/rid-purge/"
        )
        self.assertEqual(
            listed.get("KeyCount", 0),
            0,
            f"the purge left {key} behind -- the findings would outlive it",
        )

        detail = reviews_module.get_review_detail("rid-purge", OWNER, self.ddb, self.s3)
        self.assertIsNone(detail["issues"])
        self.assertIsNone(detail["critic_delta"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
