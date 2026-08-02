#!/usr/bin/env python3
"""
Executable tests for issue #454: the retention purge sweep never deleted input
documents, and reported success anyway.

The API writes the input document to `uploads/{owner_sub}/{review_id}/in.docx`
(backend/src/review_routes.py) and records that exact key on the reviews row as
`upload_s3_key` (backend/src/reviews.py::_create_review_row, issue #449). Every
sweep, however, listed `uploads/{review_id}/` -- a prefix that omits the owner
segment and therefore matched nothing -- so `list_objects_v2` returned an empty
page, the object survived forever, and the review was STILL appended to
`deleted_reviews`. The same wrong prefix was in the legal-hold tagger, so a held
input document never received the `contract-toaster:legal-hold` tag the
bucket-policy DENY in data-stack.ts keys on.

Everything here drives the REAL code path against moto-mocked S3/DynamoDB (real
`put_object` at the real layout, real `run_purge_sweep_now`, real
`handler.run_purge_sweep`, real `head_object` afterwards) rather than an
in-memory fake -- deliberately, because this defect survived for exactly as long
as it did BECAUSE the existing fakes seeded objects at the sweep's own wrong
layout and so agreed with the bug. `moto==5.2.2` is in requirements-dev.txt;
tests/test_playbook_version_routes_430.py is the established pattern.

Covers, per the issue's acceptance criteria:
  1. A terminal, past-window review's input document at the REAL layout is
     deleted -- by backend/src/retention.py's mirror AND by
     infra/lambda/purge_worker/handler.py, the copy that actually runs on AWS.
  2. A row that predates the recorded `upload_s3_key` pointer is still purged,
     via the owner-scoped prefix-scan fallback.
  3. The legal-hold tagger tags the input object at the real layout (and
     release untags it), so the storage-layer DENY backstop covers uploads.
  4. A review whose targeted object SURVIVES the delete is not reported as
     deleted -- the deeper bug: the success record was never tied to the
     outcome. Its substance fields are kept for the next sweep to retry.
  5. The operator gate: a `dry_run` sweep itemises the backlog (review ids,
     object keys, counts) and deletes NOTHING.
  6. A row whose stored pointer names a DIFFERENT review cannot make the sweep
     delete that other review's object.

This test MUST FAIL on the pre-fix tree: (1)/(2) fail because the object
survives while the sweep reports success, (3) because the input object is never
tagged, (4)/(5) because neither `failed_reviews` nor `dry_run` exists.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
PURGE_WORKER_DIR = REPO_ROOT / "infra" / "lambda" / "purge_worker"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(PURGE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(PURGE_WORKER_DIR))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-454-test")
os.environ.setdefault(
    "RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-454-test"
)
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-454-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-454-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-454-test")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.retention as retention_module  # noqa: E402

# The production purge-worker Lambda -- a separate deployable that reads the
# same env vars but is imported independently of backend/src (see that file's
# module docstring).
import handler as purge_handler_module  # noqa: E402

DAY = 86400
ADMIN = {"cognito_sub": "admin-1", "email": "admin-1@example.com", "is_admin": True}

HOLD_TAG = "contract-toaster:legal-hold"


class _DeleteRefusingS3:
    """The real (moto) S3 client with `delete_object` neutered for one bucket.

    An injected fake TRANSPORT around the real client, not a mock of the
    function under test: every other call -- list_objects_v2, put_object,
    head_object -- still goes to moto, so the sweep runs its real logic and
    genuinely observes an object that is still there after it "deleted" it.
    This is what a bucket-policy DENY or a since-recreated object looks like
    from the sweep's side.
    """

    def __init__(self, real_client, refuse_bucket: str):
        self._real = real_client
        self._refuse_bucket = refuse_bucket
        self.refused_deletes: list[str] = []

    def delete_object(self, Bucket, Key):
        if Bucket == self._refuse_bucket:
            self.refused_deletes.append(Key)
            return {}
        return self._real.delete_object(Bucket=Bucket, Key=Key)

    def __getattr__(self, name):
        return getattr(self._real, name)


class PurgePrefixTestBase(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()

        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = boto3.client("s3", region_name="us-east-1")

        self.ddb.create_table(
            TableName=os.environ["REVIEWS_TABLE"],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["RETENTION_SETTINGS_TABLE"],
            KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # PK/SK match what retention.py::_write_audit_entry writes.
        self.ddb.create_table(
            TableName=os.environ["AUDIT_TABLE"],
            KeySchema=[
                {"AttributeName": "partition", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "partition", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.s3.create_bucket(Bucket=os.environ["UPLOADS_BUCKET"])
        self.s3.create_bucket(Bucket=os.environ["OUTPUTS_BUCKET"])

        self.reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])

    def tearDown(self):
        self._mock_aws.stop()

    # -- seeding ---------------------------------------------------------

    def _input_key(self, owner_sub: str, review_id: str) -> str:
        """The layout src/review_routes.py actually writes."""
        return f"uploads/{owner_sub}/{review_id}/in.docx"

    def _output_key(self, review_id: str) -> str:
        return f"outputs/{review_id}/out.docx"

    def _seed_review(
        self,
        review_id: str,
        owner_sub: str = "owner-probe",
        age_days: float = 400,
        window_days=90,
        status_: str = "DONE",
        legal_hold: bool = False,
        record_pointer: bool = True,
        upload_s3_key: str | None = None,
    ) -> tuple[str, str]:
        """Seed a review row plus its REAL S3 objects. Returns (input, output)
        keys."""
        now = retention_module.now_epoch()
        input_key = self._input_key(owner_sub, review_id)
        output_key = self._output_key(review_id)

        item = {
            "review_id": review_id,
            "owner_sub": owner_sub,
            "status": status_,
            "created_at": str(now - age_days * DAY),
            "retention_window_at_creation": window_days,
            "legal_hold": legal_hold,
            "verdict_summary": "some substantive summary",
            "issue_rationale_text": "some substantive rationale",
            "output_s3_key": output_key,
        }
        if record_pointer:
            # What _create_review_row records on the row (#449) -- the same
            # pointer GET /api/reviews/{id}/input presigns.
            item["upload_s3_key"] = upload_s3_key or input_key
        self.reviews_table.put_item(Item=item)

        self.s3.put_object(
            Bucket=os.environ["UPLOADS_BUCKET"], Key=input_key, Body=b"input-doc"
        )
        self.s3.put_object(
            Bucket=os.environ["OUTPUTS_BUCKET"], Key=output_key, Body=b"output-doc"
        )
        return input_key, output_key

    # -- assertions ------------------------------------------------------

    def _exists(self, bucket: str, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError:
            return False

    def assertObjectGone(self, bucket: str, key: str, msg: str = ""):
        self.assertFalse(self._exists(bucket, key), msg or f"{key} still exists")

    def assertObjectPresent(self, bucket: str, key: str, msg: str = ""):
        self.assertTrue(self._exists(bucket, key), msg or f"{key} was deleted")

    def _tags(self, bucket: str, key: str) -> dict:
        resp = self.s3.get_object_tagging(Bucket=bucket, Key=key)
        return {t["Key"]: t["Value"] for t in resp["TagSet"]}


# ---------------------------------------------------------------------------
# (1) The input document at the real layout is actually deleted.
# ---------------------------------------------------------------------------

class TestInputDocumentIsPurged(PurgePrefixTestBase):
    def test_backend_sweep_deletes_input_document_at_the_real_layout(self):
        input_key, output_key = self._seed_review("rid-probe")

        summary = retention_module.run_purge_sweep_now(self.s3, self.ddb)

        self.assertIn("rid-probe", summary["deleted_reviews"])
        self.assertObjectGone(
            os.environ["UPLOADS_BUCKET"],
            input_key,
            "The sweep reported the review purged while the INPUT document "
            "survived -- exactly the #454 defect.",
        )
        self.assertObjectGone(os.environ["OUTPUTS_BUCKET"], output_key)

    def test_production_worker_lambda_deletes_input_document_at_the_real_layout(self):
        """The same assertion against the copy that actually runs on AWS --
        fixing only backend/src/retention.py would leave the deployment
        broken."""
        input_key, output_key = self._seed_review("rid-lambda")

        summary = purge_handler_module.run_purge_sweep()

        self.assertIn("rid-lambda", summary["deleted_reviews"])
        self.assertObjectGone(os.environ["UPLOADS_BUCKET"], input_key)
        self.assertObjectGone(os.environ["OUTPUTS_BUCKET"], output_key)

    def test_row_predating_the_recorded_pointer_is_purged_via_prefix_scan(self):
        """Every review created before #449 has no `upload_s3_key` on its row.
        The owner-scoped prefix scan is the fallback that still finds its
        input document."""
        input_key, _ = self._seed_review("rid-legacy", record_pointer=False)
        row = self.reviews_table.get_item(Key={"review_id": "rid-legacy"})["Item"]
        self.assertNotIn("upload_s3_key", row)

        summary = retention_module.run_purge_sweep_now(self.s3, self.ddb)

        self.assertIn("rid-legacy", summary["deleted_reviews"])
        self.assertObjectGone(os.environ["UPLOADS_BUCKET"], input_key)

    def test_active_and_held_and_recent_reviews_keep_their_input_document(self):
        """The fix must not widen WHAT is purged -- invariants 1-3 still gate
        the (now correctly targeted) delete."""
        active_key, _ = self._seed_review("rid-active", status_="RUNNING")
        held_key, _ = self._seed_review("rid-held", legal_hold=True)
        recent_key, _ = self._seed_review("rid-recent", age_days=1)

        summary = retention_module.run_purge_sweep_now(self.s3, self.ddb)

        self.assertEqual(summary["deleted_reviews"], [])
        self.assertObjectPresent(os.environ["UPLOADS_BUCKET"], active_key)
        self.assertObjectPresent(os.environ["UPLOADS_BUCKET"], held_key)
        self.assertObjectPresent(os.environ["UPLOADS_BUCKET"], recent_key)

    def test_stored_pointer_naming_another_review_is_never_acted_on(self):
        """A corrupted / mis-migrated pointer must not let one review's purge
        delete a different review's object (the same key-binding rule
        download.py enforces before presigning)."""
        victim_key, _ = self._seed_review("rid-victim", age_days=1)
        self._seed_review(
            "rid-attacker",
            upload_s3_key=victim_key,  # points at ANOTHER review's object
        )

        summary = retention_module.run_purge_sweep_now(self.s3, self.ddb)

        self.assertIn("rid-attacker", summary["deleted_reviews"])
        self.assertObjectPresent(
            os.environ["UPLOADS_BUCKET"],
            victim_key,
            "A pointer naming another review's object must never be deleted.",
        )


# ---------------------------------------------------------------------------
# (2) Legal-hold tagging reaches the input object at the real layout.
# ---------------------------------------------------------------------------

class TestLegalHoldTagsInputObject(PurgePrefixTestBase):
    def test_set_hold_tags_the_input_document_at_the_real_layout(self):
        input_key, output_key = self._seed_review("rid-hold")

        retention_module.set_legal_hold(
            "rid-hold", "matter ref 123", ADMIN, self.ddb, self.s3
        )

        self.assertEqual(
            self._tags(os.environ["UPLOADS_BUCKET"], input_key).get(HOLD_TAG),
            "true",
            "A held INPUT document must carry the tag the bucket-policy DENY "
            "keys on -- otherwise the storage-layer backstop covers outputs "
            "only.",
        )
        self.assertEqual(
            self._tags(os.environ["OUTPUTS_BUCKET"], output_key).get(HOLD_TAG), "true"
        )

    def test_release_hold_removes_the_tag_from_the_input_document(self):
        input_key, _ = self._seed_review("rid-hold-release")
        retention_module.set_legal_hold(
            "rid-hold-release", "matter ref 123", ADMIN, self.ddb, self.s3
        )
        # Asserted before the release so this test cannot pass vacuously on a
        # tree where the input object was never tagged in the first place.
        self.assertEqual(
            self._tags(os.environ["UPLOADS_BUCKET"], input_key).get(HOLD_TAG), "true"
        )

        retention_module.release_legal_hold(
            "rid-hold-release", ADMIN, self.ddb, self.s3
        )

        self.assertNotEqual(
            self._tags(os.environ["UPLOADS_BUCKET"], input_key).get(HOLD_TAG), "true"
        )


# ---------------------------------------------------------------------------
# (3) A failed delete must never be recorded as success.
# ---------------------------------------------------------------------------

class TestSuccessIsTiedToOutcome(PurgePrefixTestBase):
    def test_review_whose_object_survives_is_not_reported_deleted(self):
        input_key, output_key = self._seed_review("rid-survivor")
        refusing_s3 = _DeleteRefusingS3(self.s3, os.environ["UPLOADS_BUCKET"])

        summary = retention_module.run_purge_sweep_now(refusing_s3, self.ddb)

        self.assertEqual(refusing_s3.refused_deletes, [input_key])
        self.assertNotIn(
            "rid-survivor",
            summary["deleted_reviews"],
            "A review whose targeted object survived must NOT be recorded as "
            "deleted -- the success record has to be tied to the outcome.",
        )
        self.assertIn("rid-survivor", summary["failed_reviews"])
        self.assertObjectPresent(os.environ["UPLOADS_BUCKET"], input_key)
        # Its substance fields survive too, so the next sweep can retry it
        # rather than leaving a document with no record of what it said.
        row = self.reviews_table.get_item(Key={"review_id": "rid-survivor"})["Item"]
        self.assertIn("verdict_summary", row)
        # The outputs half still went (a partial purge is retried, not undone).
        self.assertObjectGone(os.environ["OUTPUTS_BUCKET"], output_key)


# ---------------------------------------------------------------------------
# (4) Operator gate: dry run itemises the backlog and deletes nothing.
# ---------------------------------------------------------------------------

class TestDryRunOperatorGate(PurgePrefixTestBase):
    def test_dry_run_itemises_the_backlog_and_deletes_nothing(self):
        input_key, output_key = self._seed_review("rid-dry")

        summary = retention_module.run_purge_sweep_now(self.s3, self.ddb, dry_run=True)

        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["deleted_reviews"], [])
        self.assertIn("rid-dry", summary["eligible_reviews"])
        self.assertIn(input_key, summary["objects_by_review"]["rid-dry"])
        self.assertIn(output_key, summary["objects_by_review"]["rid-dry"])
        self.assertEqual(summary["object_count"], 2)

        self.assertObjectPresent(os.environ["UPLOADS_BUCKET"], input_key)
        self.assertObjectPresent(os.environ["OUTPUTS_BUCKET"], output_key)
        row = self.reviews_table.get_item(Key={"review_id": "rid-dry"})["Item"]
        self.assertIn("verdict_summary", row)

    def test_dry_run_reports_exactly_what_the_real_sweep_then_deletes(self):
        input_key, _ = self._seed_review("rid-parity")

        dry = retention_module.run_purge_sweep_now(self.s3, self.ddb, dry_run=True)
        real = retention_module.run_purge_sweep_now(self.s3, self.ddb)

        self.assertEqual(dry["eligible_reviews"], real["deleted_reviews"])
        self.assertEqual(dry["objects_by_review"], real["objects_by_review"])
        self.assertObjectGone(os.environ["UPLOADS_BUCKET"], input_key)

    def test_worker_handler_dry_run_event_deletes_nothing(self):
        input_key, _ = self._seed_review("rid-lambda-dry")

        summary = purge_handler_module.handler({"dry_run": True})

        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["deleted_reviews"], [])
        self.assertIn(input_key, summary["objects_by_review"]["rid-lambda-dry"])
        self.assertObjectPresent(os.environ["UPLOADS_BUCKET"], input_key)

    def test_worker_handler_scheduled_event_still_deletes(self):
        """The dry run is an explicit opt-in: the scheduled invocation carries
        no `dry_run` key and must behave exactly as before."""
        input_key, _ = self._seed_review("rid-lambda-scheduled")

        summary = purge_handler_module.handler({"trigger": "scheduled"})

        self.assertFalse(summary["dry_run"])
        self.assertIn("rid-lambda-scheduled", summary["deleted_reviews"])
        self.assertObjectGone(os.environ["UPLOADS_BUCKET"], input_key)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestInputDocumentIsPurged,
        TestLegalHoldTagsInputObject,
        TestSuccessIsTiedToOutcome,
        TestDryRunOperatorGate,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
