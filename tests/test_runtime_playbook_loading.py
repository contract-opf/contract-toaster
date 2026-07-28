#!/usr/bin/env python3
"""
Required-verification suite for issue #401 (A1 -- Runtime playbook loading):
consume the active playbook at runtime, ship none.

## Root problem this proves fixed

Before this fix, "is a playbook active" and "what does the review actually
run against" were two disconnected questions:

  - `backend/src/reviews.py::resolve_active_release_bundle_hash` (issue
    #194) already refused `POST /api/reviews` with 503 "no active playbook"
    when the `playbooks` table carried no active bundle for a `playbook_id`
    -- SUBMISSION was already gated.
  - But `backend/src/pipeline_runner.py::_load_playbook_bundle` -- the
    function that actually reads playbook CONTENT for
    `scripts/review_spine.py::run_review` to review against -- took only a
    bare `playbook_id` and read `playbooks/<id>.json` off disk
    unconditionally. It never consulted the `playbooks` table at all.
    `reviews.verify_submission_time_bundle` (the existing, already-tested
    ARCHITECTURE.md step-10 "Retired-bundle-before-start" check) existed
    but was wired into nothing but the AWS Step Functions error-handler
    contract's DOCUMENTATION -- the Docker Compose in-process runner
    (`pipeline_runner.run_real_pipeline`, the actually-deployed target,
    see memory dts-docker-deployment) never called it.

So a bundle deactivated (or never activated at all) between submission and
execution start had zero effect on what actually got reviewed: the engine
would silently keep reading whatever `playbooks/<id>.json` happened to be
checked into the image -- exactly the "silent default" this issue's
acceptance criteria prohibit, and exactly what "never from a baked-in repo
file" (this issue's Goal) means concretely.

This file proves, against real moto-mocked DynamoDB + S3 (no live AWS, no
network):

  1. Submission time (pre-existing, re-asserted here as this issue's own
     regression anchor): an empty playbook store refuses `POST /api/reviews`
     with 503 and the documented "no active playbook" message -- no crash,
     no submission/review row created, no faked hash.
  2. Submission time: once a playbook is genuinely activated (a REAL
     `scripts/canonicalize.py` content hash, never a placeholder), the
     resolver returns it.
  3. Execution time (the actual gap this issue closes):
     `pipeline_runner.run_real_pipeline`, invoked directly (bypassing the
     submission route entirely, simulating both "nothing was ever active"
     and "the bundle that WAS active at submission has since been
     retired"), refuses BEFORE ever reading playbook content off disk --
     proven by patching `_load_playbook_bundle` (the one function that
     actually opens `playbooks/<id>.json`) and asserting it is never
     called. The review lands QUARANTINED, never wedged in
     PENDING/RUNNING, never silently DONE.
  4. Execution time, positive case: a genuinely activated, matching bundle
     passes the gate and the pipeline proceeds into real playbook loading
     -- "with an activated playbook, review runs against it."

This test MUST FAIL (at least on checks 3/4) against the pre-#401 tree,
where `_load_playbook_bundle` is reached unconditionally, and PASS after
the fix.

Run standalone: `python3 tests/test_runtime_playbook_loading.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_ROOT, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)

import boto3  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from moto import mock_aws  # noqa: E402

import seed_active_bundle  # noqa: E402
import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

# Pinned to "synthetic-generic" explicitly (not
# playbook_registry.DEFAULT_PLAYBOOK_ID -- issue #412 repointed the registry
# default to the bundled "synthetic-nda-sample" sample playbook), matching
# tests/test_active_bundle_resolver_194.py's own pin: this file's real-hash
# seeding needs a specific, known-valid, known-registered playbook_id.
PLAYBOOK_ID = "synthetic-generic"
REVIEW_ID = "review-401-runtime-loading"


class RuntimePlaybookLoadingTestBase(unittest.TestCase):
    """Real moto DynamoDB + S3, no live AWS, no network. Extends
    tests/test_active_bundle_resolver_194.py's ActiveBundleResolverTestBase
    pattern with S3, since this file also drives
    pipeline_runner.run_real_pipeline directly (not just the resolver)."""

    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = boto3.client("s3", region_name="us-east-1")

        self.ddb.create_table(
            TableName=os.environ["PLAYBOOKS_TABLE"],
            KeySchema=[{"AttributeName": "playbook_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "playbook_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["REVIEW_SUBMISSIONS_TABLE"],
            KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["REVIEWS_TABLE"],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.s3.create_bucket(Bucket=os.environ["UPLOADS_BUCKET"])
        self.s3.create_bucket(Bucket=os.environ["OUTPUTS_BUCKET"])

        self.playbooks_table = self.ddb.Table(os.environ["PLAYBOOKS_TABLE"])
        self.reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])

    def tearDown(self) -> None:
        self._mock_aws.stop()

    def _seed_eiaa_active_bundle(self) -> str:
        return seed_active_bundle.seed_active_bundle(PLAYBOOK_ID, self.ddb)

    def _payload(self, release_bundle_hash: str, playbook_id: str = PLAYBOOK_ID) -> dict[str, Any]:
        return {
            "review_id": REVIEW_ID,
            "owner_sub": "owner-401",
            "playbook_id": playbook_id,
            "upload_s3_key": f"uploads/owner-401/{REVIEW_ID}/in.docx",
            "release_bundle_hash": release_bundle_hash,
        }

    def _put_review_row(self, status: str = "RUNNING") -> None:
        self.reviews_table.put_item(Item={"review_id": REVIEW_ID, "status": status})


# ---------------------------------------------------------------------------
# 1. Submission time: empty store -> 503, no crash, no silent default.
# (Pre-existing behavior, issue #194 -- re-asserted here as issue #401's own
# regression anchor for the acceptance criterion it names verbatim.)
# ---------------------------------------------------------------------------


class TestSubmissionRefusesOnEmptyStore(RuntimePlaybookLoadingTestBase):
    def test_empty_store_refuses_with_503_no_active_playbook(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb)

        self.assertEqual(ctx.exception.status_code, 503)
        # The exact, documented user-visible message (ARCHITECTURE.md /
        # RUNBOOK.md / docs/playbook-governance.md all cite it verbatim) --
        # read off the shared constant rather than duplicated as a literal
        # here, so this test can never silently drift from the source of
        # truth. This is the "reason: no active playbook" this issue's
        # Scope names.
        self.assertEqual(ctx.exception.detail, reviews_module.NO_ACTIVE_PLAYBOOK_DETAIL)

    def test_empty_store_creates_no_submission_or_review_row_and_never_starts_execution(
        self,
    ) -> None:
        class _AssertNeverStartedSfn:
            class exceptions:
                class ExecutionAlreadyExists(Exception):
                    pass

            def start_execution(self, **_kwargs: Any) -> None:
                raise AssertionError("must never start an execution with no active bundle")

        with self.assertRaises(HTTPException):
            reviews_module.resolve_and_submit_review(
                owner_sub="owner-401",
                playbook_id=PLAYBOOK_ID,
                file_sha256="filehash-401",
                upload_pointer="uploads/owner-401/in.docx",
                dynamodb_resource=self.ddb,
                sfn_client=_AssertNeverStartedSfn(),
            )

        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        self.assertEqual(submissions_table.scan()["Items"], [])
        self.assertEqual(self.reviews_table.scan()["Items"], [])


# ---------------------------------------------------------------------------
# 2. Submission time: activated playbook -> resolver returns the REAL hash.
# ---------------------------------------------------------------------------


class TestSubmissionSucceedsOnceActivated(RuntimePlaybookLoadingTestBase):
    def test_activated_playbook_resolves_the_real_content_hash(self) -> None:
        seeded_hash = self._seed_eiaa_active_bundle()

        resolved = reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb)

        self.assertEqual(resolved, seeded_hash)
        self.assertTrue(resolved.startswith("sha256:"))


# ---------------------------------------------------------------------------
# 3/4. Execution time (backend/src/pipeline_runner.py::run_real_pipeline) --
# the actual "review path" this issue is about. Neither test group above
# proves this layer: resolve_active_release_bundle_hash only gates the
# FIRST 202 response. Before this issue, nothing stopped run_real_pipeline
# itself from reading playbooks/<id>.json unconditionally once execution
# started. These tests call run_real_pipeline directly -- bypassing the
# submission route entirely -- so the execution path's OWN gate is what is
# under test, independent of whatever the submission-time gate already did.
# ---------------------------------------------------------------------------


class TestExecutionPathGatesOnRuntimeActivation(RuntimePlaybookLoadingTestBase):
    def test_empty_store_quarantines_without_ever_reading_playbook_off_disk(self) -> None:
        """The core of this issue: 'No code path reads a hard-coded
        playbooks/*.json for the active ruleset.' Patch the one function
        that actually opens playbooks/<id>.json (_load_playbook_bundle) and
        prove it is never called when nothing is active -- the empty-shell
        default (no playbook ever activated)."""
        self._put_review_row(status="RUNNING")
        payload = self._payload(release_bundle_hash="")  # nothing was ever active to record

        with patch.object(pipeline_runner, "_load_playbook_bundle") as load_bundle, patch.object(
            pipeline_runner, "_settle_reservation"
        ) as settle:
            pipeline_runner.run_real_pipeline(
                REVIEW_ID,
                payload,
                dynamodb_resource=self.ddb,
                s3_client=self.s3,
                model_client=object(),
            )

        load_bundle.assert_not_called()
        settle.assert_called_once()
        review_row = self.reviews_table.get_item(Key={"review_id": REVIEW_ID})["Item"]
        self.assertEqual(review_row["status"], "QUARANTINED")
        self.assertEqual(review_row["quarantine_reason"], "submission_time_bundle_retired")

    def test_bundle_retired_between_submission_and_execution_quarantines(self) -> None:
        """Retired-bundle-before-start (ARCHITECTURE.md step 10): the bundle
        WAS active when this review was submitted (submission_time_bundle_hash
        is a real, once-valid hash) but has since been deactivated --
        run_real_pipeline must not silently review against whatever is
        CURRENTLY on disk for this playbook_id just because it once matched."""
        self._put_review_row(status="RUNNING")
        stale_hash = self._seed_eiaa_active_bundle()
        # Simulate deactivation landing between submission (202) and this
        # execution actually starting.
        self.playbooks_table.update_item(
            Key={"playbook_id": PLAYBOOK_ID},
            UpdateExpression="REMOVE active_release_bundle_hash",
        )
        payload = self._payload(release_bundle_hash=stale_hash)

        with patch.object(pipeline_runner, "_load_playbook_bundle") as load_bundle, patch.object(
            pipeline_runner, "_settle_reservation"
        ):
            pipeline_runner.run_real_pipeline(
                REVIEW_ID,
                payload,
                dynamodb_resource=self.ddb,
                s3_client=self.s3,
                model_client=object(),
            )

        load_bundle.assert_not_called()
        review_row = self.reviews_table.get_item(Key={"review_id": REVIEW_ID})["Item"]
        self.assertEqual(review_row["status"], "QUARANTINED")

    def test_activated_playbook_review_runs_against_it(self) -> None:
        """'With an activated playbook, review runs against it.' Proves the
        gate is not merely fail-closed but correctly passes the matching
        case through to the real playbook-loading stage rather than
        refusing everything unconditionally. No object is uploaded to S3
        for this review, so once past the gate it fails closed at the next
        real stage (fetch_upload) -- proof it got there, without needing
        the full docx/model fixture machinery
        tests/test_dts_pipeline_runner_real_review.py already covers."""
        self._put_review_row(status="RUNNING")
        seeded_hash = self._seed_eiaa_active_bundle()
        payload = self._payload(release_bundle_hash=seeded_hash)

        with patch.object(pipeline_runner, "_settle_reservation"):
            pipeline_runner.run_real_pipeline(
                REVIEW_ID,
                payload,
                dynamodb_resource=self.ddb,
                s3_client=self.s3,
                model_client=object(),
            )

        review_row = self.reviews_table.get_item(Key={"review_id": REVIEW_ID})["Item"]
        self.assertNotEqual(review_row["status"], "QUARANTINED")
        self.assertEqual(review_row.get("failing_stage"), "fetch_upload")


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestSubmissionRefusesOnEmptyStore,
        TestSubmissionSucceedsOnceActivated,
        TestExecutionPathGatesOnRuntimeActivation,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
