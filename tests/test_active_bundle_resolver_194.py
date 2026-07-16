#!/usr/bin/env python3
"""
Slice test for issue #194: the active-bundle resolver + pipeline verify
step.

Prior to this fix, `submit_review`'s `active_release_bundle_hash` parameter
was a bare parameter with no caller: nothing read
`playbooks.active_release_bundle_hash`, there was no no-active-bundle
refusal, and the pipeline's step-10 verify step was a pass-through stub
that never checked the stored hash. This test exercises the real fix
against `moto`-mocked DynamoDB (no live AWS, no network), per the issue's
"Required verification":

  1. The submission route (`resolve_and_submit_review`) resolves the
     active bundle by reading `playbooks.active_release_bundle_hash` and
     records it exactly as `submit_review` already expected: on the
     submission record AND as `playbook_hash` on the reviews row.
  2. When no active bundle exists, the route refuses with the documented
     no-active-bundle state: HTTPException(503, "no active playbook") --
     not a faked hash, and no submission/review records are created.
  3. The pipeline verify step (`verify_submission_time_bundle`, previously
     a pass-through stub) actually verifies the stored hash: it passes
     through on a match and QUARANTINEs (with reason
     `submission_time_bundle_retired`) on a mismatch, per ARCHITECTURE.md
     steps 3/10.
  4. The seeded eiaa v1.0.0 bundle hash (`scripts/seed_active_bundle.py`)
     matches the value computed by `scripts/canonicalize.py`.

This test MUST FAIL on the pre-fix tree (no resolver, no
`PLAYBOOKS_TABLE`-reading code, verify step is a stub) and PASS after the
fix.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)

import boto3  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from moto import mock_aws  # noqa: E402

import canonicalize  # noqa: E402
import playbook_registry  # noqa: E402
import seed_active_bundle  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

# Pinned to "eiaa" explicitly (not playbook_registry.DEFAULT_PLAYBOOK_ID --
# issue #343 repointed the registry default to the public "sample-agreement"
# sample playbook) because this file's golden-hash cross-check
# (real_eiaa_content_hash()) and seeded-bundle fixtures are all eiaa-specific.
PLAYBOOK_ID = "eiaa"


class ExecutionAlreadyExists(Exception):
    pass


class FakeSfnExceptions:
    ExecutionAlreadyExists = ExecutionAlreadyExists


class FakeSfnClient:
    """Minimal fake Step Functions client -- submit_review only needs
    start_execution + the ExecutionAlreadyExists exception type; a real
    moto stepfunctions mock is unnecessary machinery for this slice (the
    issue's "Required verification" only asks for DynamoDB to be
    moto-mocked)."""

    def __init__(self):
        self.exceptions = FakeSfnExceptions()
        self.started_names: set[str] = set()
        self.start_execution_call_count = 0

    def start_execution(self, stateMachineArn, name, input):
        self.start_execution_call_count += 1
        if name in self.started_names:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started_names.add(name)
        return {"executionArn": f"{stateMachineArn.replace(':stateMachine:', ':execution:')}:{name}"}


def real_eiaa_content_hash() -> str:
    """The real, current content_hash for the eiaa playbook -- computed the
    exact same way scripts/canonicalize.py's CLI does."""
    playbook_path = canonicalize.resolve_playbook_path(PLAYBOOK_ID)
    with open(playbook_path) as f:
        doc = json.load(f)
    return canonicalize.content_hash(doc)


# ---------------------------------------------------------------------------
# In-memory DynamoDB fake -- used ONLY for the one test that exercises the
# FULL resolve_and_submit_review -> submit_review -> reserve_spend path.
#
# moto 5.2.2's ConditionExpression parser cannot parse reserve_spend's
# existing (issue #189, pre-#194) atomic condition
# "attribute_not_exists(reserved_usd_cents) OR reserved_usd_cents + :amount
# <= if_not_exists(daily_cap_usd_cents, :cap)" -- arithmetic on the LHS of a
# comparison inside an OR is outside what its expression grammar supports
# (verified directly against moto: ValueError "Cannot parse condition
# starting at:+ :amount <= ..."). tests/test_review_submission_e2e.py hit
# this same wall and established the fix: a tiny in-memory
# FakeDynamoDBResource/FakeTable that interprets the specific
# UpdateExpressions reviews.py issues, rather than moto's general (but
# here, incomplete) expression engine. Reused verbatim here for the one
# test that needs the full submission path; every other test in this file
# uses real moto (see ActiveBundleResolverTestBase) because it only touches
# get_item/put_item/update_item shapes moto handles fine.
# ---------------------------------------------------------------------------

class FakeTable:
    """A tiny in-memory stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        key = Key[self.key_name]
        item = self.items.get(key)
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item[self.key_name]
        if ConditionExpression == "attribute_not_exists(idempotency_key)" and key in self.items:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)

    def scan(self):
        return {"Items": list(self.items.values())}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
    ):
        key = Key[self.key_name]
        item = self.items.setdefault(key, dict(Key))
        vals = ExpressionAttributeValues or {}

        if "reserved_usd_cents = if_not_exists" in UpdateExpression:
            current = item.get("reserved_usd_cents", 0)
            cap = item.get("daily_cap_usd_cents", vals.get(":cap"))
            amount = vals[":amount"]
            if ConditionExpression and current + amount > cap:
                from botocore.exceptions import ClientError

                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
            item["reserved_usd_cents"] = current + amount
            item.setdefault("daily_cap_usd_cents", vals.get(":cap"))
            return

        if "execution_arn = :arn" in UpdateExpression:
            item["execution_arn"] = vals[":arn"]
            if ":status" in vals:
                item["execution_status"] = vals[":status"]
            return

        if "spend_reservation_id = :rid" in UpdateExpression:
            item["spend_reservation_id"] = vals[":rid"]
            return

        # Generic fallback: no-op for anything else exercised indirectly.


class FakeDynamoDBResource:
    def __init__(self):
        self._tables: dict[str, FakeTable] = {}

    def Table(self, name: str) -> FakeTable:
        if name not in self._tables:
            key_name = {
                os.environ["REVIEW_SUBMISSIONS_TABLE"]: "idempotency_key",
                os.environ["REVIEWS_TABLE"]: "review_id",
                os.environ["DAILY_SPEND_TABLE"]: "spend_date",
                os.environ["PLAYBOOKS_TABLE"]: "playbook_id",
            }.get(name, "id")
            self._tables[name] = FakeTable(key_name)
        return self._tables[name]


class ActiveBundleResolverTestBase(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")

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
        self.ddb.create_table(
            TableName=os.environ["DAILY_SPEND_TABLE"],
            KeySchema=[{"AttributeName": "spend_date", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "spend_date", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        self.playbooks_table = self.ddb.Table(os.environ["PLAYBOOKS_TABLE"])
        self.submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        self.reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])

    def tearDown(self):
        self._mock_aws.stop()

    def _seed_eiaa_active_bundle(self) -> str:
        return seed_active_bundle.seed_active_bundle(PLAYBOOK_ID, self.ddb)


# -- (1) resolver resolves + records the hash exactly as submit_review expects --

class TestResolverReadsPlaybooksTable(ActiveBundleResolverTestBase):
    def test_resolver_reads_the_playbooks_table_attribute_directly(self):
        """The resolver must read playbooks.active_release_bundle_hash --
        not some other field, not a hard-coded value."""
        self.playbooks_table.put_item(
            Item={"playbook_id": PLAYBOOK_ID, "active_release_bundle_hash": "sha256:deadbeef"}
        )
        resolved = reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb)
        self.assertEqual(resolved, "sha256:deadbeef")


class TestResolverComposesWithSubmitReview(unittest.TestCase):
    """Exercises the FULL resolve_and_submit_review -> submit_review path
    (including reserve_spend's atomic condition, which real moto 5.2.2
    cannot parse -- see the FakeTable docstring above) against the
    in-memory FakeDynamoDBResource, matching the established pattern in
    tests/test_review_submission_e2e.py."""

    def setUp(self):
        self.ddb = FakeDynamoDBResource()

    def test_resolve_and_submit_review_stores_hash_on_submission_and_review(self):
        seeded_hash = seed_active_bundle.seed_active_bundle(PLAYBOOK_ID, self.ddb)
        sfn = FakeSfnClient()

        result = reviews_module.resolve_and_submit_review(
            owner_sub="owner-194",
            playbook_id=PLAYBOOK_ID,
            file_sha256="filehash-194",
            upload_pointer="uploads/owner-194/review-194/in.docx",
            dynamodb_resource=self.ddb,
            sfn_client=sfn,
        )

        self.assertEqual(result["status_code"], 202)
        review_id = result["review_id"]

        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])

        # Recorded on the submission record.
        submission_items = list(submissions_table.items.values())
        self.assertEqual(len(submission_items), 1)
        self.assertEqual(submission_items[0]["release_bundle_hash"], seeded_hash)
        self.assertEqual(submission_items[0]["review_id"], review_id)

        # Recorded as playbook_hash on the reviews row (reviews.py:569-584
        # per the issue's Evidence -- _create_review_row's
        # release_bundle_hash param lands in the playbook_hash field).
        review_row = reviews_table.get_item(Key={"review_id": review_id})["Item"]
        self.assertEqual(review_row["playbook_hash"], seeded_hash)
        self.assertEqual(review_row["status"], "PENDING")
        self.assertEqual(sfn.start_execution_call_count, 1)


# -- (2) no active bundle -> 503 "no active playbook", nothing faked --------

class TestNoActiveBundleRefusal(ActiveBundleResolverTestBase):
    def test_refuses_with_503_no_active_playbook_when_table_empty(self):
        sfn = FakeSfnClient()

        with self.assertRaises(HTTPException) as ctx:
            reviews_module.resolve_and_submit_review(
                owner_sub="owner-noactive",
                playbook_id=PLAYBOOK_ID,
                file_sha256="filehash-noactive",
                upload_pointer="uploads/owner-noactive/in.docx",
                dynamodb_resource=self.ddb,
                sfn_client=sfn,
            )

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "no active playbook")

        # No faked hash: nothing was created.
        self.assertEqual(self.submissions_table.scan()["Items"], [])
        self.assertEqual(self.reviews_table.scan()["Items"], [])
        self.assertEqual(sfn.start_execution_call_count, 0)

    def test_refuses_with_503_when_playbook_row_exists_but_bundle_deactivated(self):
        """Deactivation clears active_release_bundle_hash but the row can
        still exist (docs/playbook-governance.md -- the row is never
        deleted, just its active hash cleared)."""
        self.playbooks_table.put_item(Item={"playbook_id": PLAYBOOK_ID})

        with self.assertRaises(HTTPException) as ctx:
            reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "no active playbook")


# -- (3) pipeline verify step actually verifies ------------------------------

class TestPipelineVerifyStep(ActiveBundleResolverTestBase):
    def test_verify_passes_through_on_matching_hash(self):
        seeded_hash = self._seed_eiaa_active_bundle()
        self.reviews_table.put_item(
            Item={"review_id": "review-match", "status": "RUNNING", "playbook_hash": seeded_hash}
        )

        result = reviews_module.verify_submission_time_bundle(
            review_id="review-match",
            playbook_id=PLAYBOOK_ID,
            submission_time_bundle_hash=seeded_hash,
            dynamodb_resource=self.ddb,
        )

        self.assertTrue(result["verified"])
        review_row = self.reviews_table.get_item(Key={"review_id": "review-match"})["Item"]
        self.assertEqual(review_row["status"], "RUNNING", "A verified match must not be touched.")

    def test_verify_quarantines_on_mismatch(self):
        # Submission-time hash no longer matches: a different bundle is
        # now active (simulates a re-activation landing between
        # submission and execution start).
        self.playbooks_table.put_item(
            Item={"playbook_id": PLAYBOOK_ID, "active_release_bundle_hash": "sha256:new-bundle"}
        )
        self.reviews_table.put_item(
            Item={
                "review_id": "review-stale",
                "status": "RUNNING",
                "playbook_hash": "sha256:stale-bundle",
            }
        )

        result = reviews_module.verify_submission_time_bundle(
            review_id="review-stale",
            playbook_id=PLAYBOOK_ID,
            submission_time_bundle_hash="sha256:stale-bundle",
            dynamodb_resource=self.ddb,
        )

        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "QUARANTINED")
        self.assertEqual(result["reason"], "submission_time_bundle_retired")

        review_row = self.reviews_table.get_item(Key={"review_id": "review-stale"})["Item"]
        self.assertEqual(review_row["status"], "QUARANTINED")
        self.assertEqual(review_row["quarantine_reason"], "submission_time_bundle_retired")
        self.assertEqual(review_row["quarantine_bundle_hash"], "sha256:stale-bundle")

    def test_verify_quarantines_when_bundle_deactivated_entirely(self):
        """No active bundle at verify time (deactivated, not replaced) must
        also quarantine -- not silently pass through."""
        self.reviews_table.put_item(
            Item={
                "review_id": "review-deactivated",
                "status": "RUNNING",
                "playbook_hash": "sha256:stale-bundle",
            }
        )

        result = reviews_module.verify_submission_time_bundle(
            review_id="review-deactivated",
            playbook_id=PLAYBOOK_ID,
            submission_time_bundle_hash="sha256:stale-bundle",
            dynamodb_resource=self.ddb,
        )

        self.assertFalse(result["verified"])
        review_row = self.reviews_table.get_item(Key={"review_id": "review-deactivated"})["Item"]
        self.assertEqual(review_row["status"], "QUARANTINED")


# -- (4) seeded hash matches scripts/canonicalize.py -------------------------

class TestSeededHashMatchesCanonicalize(ActiveBundleResolverTestBase):
    def test_seed_active_bundle_hash_matches_canonicalize_content_hash(self):
        expected = real_eiaa_content_hash()
        seeded_hash = self._seed_eiaa_active_bundle()

        self.assertEqual(seeded_hash, expected)
        self.assertTrue(seeded_hash.startswith("sha256:"))

        row = self.playbooks_table.get_item(Key={"playbook_id": PLAYBOOK_ID})["Item"]
        self.assertEqual(row["active_release_bundle_hash"], expected)

    def test_seed_hash_matches_the_golden_ci_fixture(self):
        """Cross-check against tests/gold-fixtures/canonicalize-golden-hash.json
        so a change to the canonical form or playbook content that isn't
        also reflected there is caught (same guard
        tests/test_canonicalize.py already applies)."""
        golden_path = REPO_ROOT / "tests" / "gold-fixtures" / "canonicalize-golden-hash.json"
        with open(golden_path) as f:
            golden = json.load(f)

        seeded_hash = self._seed_eiaa_active_bundle()
        self.assertEqual(seeded_hash, golden["content_hash"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestResolverReadsPlaybooksTable,
        TestResolverComposesWithSubmitReview,
        TestNoActiveBundleRefusal,
        TestPipelineVerifyStep,
        TestSeededHashMatchesCanonicalize,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
