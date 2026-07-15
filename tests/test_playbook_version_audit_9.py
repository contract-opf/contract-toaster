#!/usr/bin/env python3
"""
Slice test for issue #9 (v1 scope, confirmed by the maintainer 2026-07-10):
playbook-version upload audit trail.

Exercises `backend/src/playbook_versions.py` against `moto`-mocked DynamoDB
(no live AWS, no network) with a stub uploader identity. Per the issue's
"Required verification":

  1. Uploading a new version writes an append-only record with uploader
     identity + timestamp.
  2. The trail's read/serialized form returns records in order and
     contains no "Exos"/"EXOS".

This test MUST FAIL on the pre-fix tree (no
`backend/src/playbook_versions.py` module / no uploader-timestamp audit
path exists) and PASS after the fix.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-test")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.playbook_versions as pv  # noqa: E402

PLAYBOOK_ID = "eiaa"


class PlaybookVersionAuditTrailTestBase(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=os.environ["PLAYBOOK_VERSIONS_TABLE"],
            KeySchema=[
                {"AttributeName": "playbook_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "playbook_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])

    def tearDown(self):
        self._mock_aws.stop()


# -- (1) upload writes an append-only record with uploader identity + timestamp --

class TestUploadWritesAuditRecord(PlaybookVersionAuditTrailTestBase):
    def test_upload_records_uploader_and_timestamp(self):
        item = pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            uploader_identity="admin-1",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_000,
        )
        self.assertEqual(item["uploaded_by"], "admin-1")
        self.assertEqual(item["uploaded_at"], 1_700_000_000)

        # Directly verify the row landed in the real DynamoDB (moto) table --
        # not just the returned dict.
        resp = self.table.get_item(Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"})
        row = resp.get("Item")
        self.assertIsNotNone(row, "expected a playbook_versions row to exist after upload")
        self.assertEqual(row["uploaded_by"], "admin-1")
        self.assertEqual(int(row["uploaded_at"]), 1_700_000_000)

    def test_upload_is_append_only_rejects_reupload_of_same_version(self):
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            uploader_identity="admin-1",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_000,
        )
        # A second upload of the SAME (playbook_id, version) must be
        # rejected rather than silently overwriting the recorded
        # uploader/timestamp -- that is what makes the trail append-only.
        with self.assertRaises(pv.PlaybookVersionConflictError):
            pv.record_playbook_version_upload(
                playbook_id=PLAYBOOK_ID,
                version="1.0.0",
                uploader_identity="admin-2",
                dynamodb_resource=self.ddb,
                now_epoch_value=1_700_000_999,
            )

        # The original record must be unchanged.
        resp = self.table.get_item(Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"})
        row = resp["Item"]
        self.assertEqual(row["uploaded_by"], "admin-1")
        self.assertEqual(int(row["uploaded_at"]), 1_700_000_000)

    def test_different_versions_and_different_uploaders_both_recorded(self):
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            uploader_identity="admin-1",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_000,
        )
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.1",
            uploader_identity="admin-2",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_100,
        )

        resp = self.table.get_item(Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.1"})
        row = resp["Item"]
        self.assertEqual(row["uploaded_by"], "admin-2")
        self.assertEqual(int(row["uploaded_at"]), 1_700_000_100)


# -- (2) read path returns records in order, de-branded --------------------

class TestTrailReadPath(PlaybookVersionAuditTrailTestBase):
    def test_trail_returns_records_in_upload_order(self):
        # Deliberately upload out of lexicographic version order but in
        # chronological order, so an implementation that (incorrectly)
        # relies on string-sorting the version sort key rather than
        # sorting by upload time would be exposed.
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="9.0.0",
            uploader_identity="admin-1",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_000,
        )
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="10.0.0",
            uploader_identity="admin-2",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_100,
        )
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="11.0.0",
            uploader_identity="admin-3",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_200,
        )

        trail = pv.list_playbook_version_trail(PLAYBOOK_ID, self.ddb)

        self.assertEqual(len(trail), 3)
        self.assertEqual(
            [row["version"] for row in trail],
            ["9.0.0", "10.0.0", "11.0.0"],
        )
        self.assertEqual(
            [row["uploaded_by"] for row in trail],
            ["admin-1", "admin-2", "admin-3"],
        )
        # Strictly ascending timestamps.
        timestamps = [row["uploaded_at"] for row in trail]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_trail_scoped_to_playbook_id(self):
        pv.record_playbook_version_upload(
            playbook_id="eiaa",
            version="1.0.0",
            uploader_identity="admin-1",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_000,
        )
        pv.record_playbook_version_upload(
            playbook_id="nda",
            version="1.0.0",
            uploader_identity="admin-1",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_000,
        )

        trail = pv.list_playbook_version_trail("eiaa", self.ddb)
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["playbook_id"], "eiaa")

    def test_trail_serialized_form_contains_no_exos_branding(self):
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            uploader_identity="admin-1@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_000,
        )
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.1",
            uploader_identity="admin-2@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_000_100,
        )

        trail = pv.list_playbook_version_trail(PLAYBOOK_ID, self.ddb)
        serialized = json.dumps(trail)

        self.assertNotIn("Exos", serialized)
        self.assertNotIn("EXOS", serialized)

    def test_trail_only_carries_identifiers_and_timestamps(self):
        """The read path is a documented no-document-substance surface --
        only playbook_id/version/uploaded_by/uploaded_at, matching the
        `audit` table's identifiers-only posture (ARCHITECTURE.md ->
        'Audit posture')."""
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            uploader_identity="admin-1",
            dynamodb_resource=self.ddb,
            content_hash="sha256:deadbeef",
            now_epoch_value=1_700_000_000,
        )

        trail = pv.list_playbook_version_trail(PLAYBOOK_ID, self.ddb)
        self.assertEqual(
            set(trail[0].keys()),
            {"playbook_id", "version", "uploaded_by", "uploaded_at"},
        )

    def test_empty_trail_for_unknown_playbook(self):
        trail = pv.list_playbook_version_trail("no-such-playbook", self.ddb)
        self.assertEqual(trail, [])


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (TestUploadWritesAuditRecord, TestTrailReadPath):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
