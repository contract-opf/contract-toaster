#!/usr/bin/env python3
"""
Slice test for issue #79 (v1 scope, confirmed by the maintainer 2026-07-10):
release-bundle activation and rollback, layered on the #9 upload audit trail.

Exercises `backend/src/playbook_versions.py::activate_playbook_version` /
`rollback_playbook_version` against `moto`-mocked DynamoDB (no live AWS, no
network) with stub actor identities. Per the issue's "Required verification":

  1. Activating a version sets it active.
  2. Rollback restores the prior active version.
  3. Both actions write audit records (actor + timestamp).
  4. No "Exos"/"EXOS" in rendered output.

This test MUST FAIL on the pre-fix tree (no activate/rollback functions
exist) and PASS after the fix.

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
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.playbook_versions as pv  # noqa: E402

PLAYBOOK_ID = "eiaa"


class BundleActivateRollbackTestBase(unittest.TestCase):
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
        self.versions_table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])
        self.audit_table = self.ddb.Table(os.environ["AUDIT_TABLE"])

    def tearDown(self):
        self._mock_aws.stop()

    def _upload(self, version, uploader="admin-1@teamexos.com", ts=1_700_000_000):
        return pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version=version,
            uploader_identity=uploader,
            dynamodb_resource=self.ddb,
            content_hash=f"sha256:{version}",
            now_epoch_value=ts,
        )

    def _all_audit_rows(self):
        resp = self.audit_table.scan()
        return resp.get("Items", [])


# -- (1) activating a version sets it active --------------------------------

class TestActivateSetsActive(BundleActivateRollbackTestBase):
    def test_activate_sets_status_active(self):
        self._upload("1.0.0")

        result = pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity="admin-1@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_001_000,
        )
        self.assertEqual(result["status"], "active")

        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        self.assertEqual(row["status"], "active")

    def test_activating_new_version_retires_previous_active(self):
        self._upload("1.0.0", ts=1_700_000_000)
        self._upload("1.1.0", ts=1_700_000_100)

        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity="admin-1@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_001_000,
        )
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.1.0",
            actor_identity="admin-2@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_002_000,
        )

        old = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        new = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.1.0"}
        )["Item"]
        self.assertEqual(old["status"], "retired")
        self.assertEqual(new["status"], "active")

    def test_activate_unknown_version_raises_not_found(self):
        with self.assertRaises(pv.PlaybookVersionNotFoundError):
            pv.activate_playbook_version(
                playbook_id=PLAYBOOK_ID,
                version="9.9.9",
                actor_identity="admin-1@teamexos.com",
                dynamodb_resource=self.ddb,
                now_epoch_value=1_700_001_000,
            )


# -- (2) rollback restores the prior active version --------------------------

class TestRollbackRestoresPriorActive(BundleActivateRollbackTestBase):
    def test_rollback_restores_prior_active_version(self):
        self._upload("1.0.0", ts=1_700_000_000)
        self._upload("1.1.0", ts=1_700_000_100)

        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity="admin-1@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_001_000,
        )
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.1.0",
            actor_identity="admin-2@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_002_000,
        )
        # 1.1.0 is now active, 1.0.0 was demoted to retired -- the prior
        # active version.
        result = pv.rollback_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity="admin-3@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_003_000,
        )
        self.assertEqual(result["status"], "active")

        restored = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        demoted = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.1.0"}
        )["Item"]
        self.assertEqual(restored["status"], "active")
        self.assertEqual(demoted["status"], "retired")

    def test_rollback_to_never_active_version_raises(self):
        self._upload("1.0.0", ts=1_700_000_000)
        self._upload("1.1.0", ts=1_700_000_100)

        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity="admin-1@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_001_000,
        )
        # 1.1.0 was uploaded but never activated -- not a valid rollback
        # target (draft, not retired).
        with self.assertRaises(pv.PlaybookVersionRollbackError):
            pv.rollback_playbook_version(
                playbook_id=PLAYBOOK_ID,
                version="1.1.0",
                actor_identity="admin-2@teamexos.com",
                dynamodb_resource=self.ddb,
                now_epoch_value=1_700_002_000,
            )

    def test_rollback_unknown_version_raises_not_found(self):
        with self.assertRaises(pv.PlaybookVersionNotFoundError):
            pv.rollback_playbook_version(
                playbook_id=PLAYBOOK_ID,
                version="9.9.9",
                actor_identity="admin-1@teamexos.com",
                dynamodb_resource=self.ddb,
                now_epoch_value=1_700_001_000,
            )


# -- (3) both actions write audit records (actor + timestamp) ---------------

class TestAuditRecordsWritten(BundleActivateRollbackTestBase):
    def test_activate_writes_audit_record_with_actor_and_timestamp(self):
        self._upload("1.0.0")
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity="admin-1@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_001_000,
        )

        rows = self._all_audit_rows()
        activate_rows = [r for r in rows if r["action"] == "release_bundle_activate"]
        self.assertEqual(len(activate_rows), 1)
        row = activate_rows[0]
        self.assertEqual(row["actor"], "admin-1@teamexos.com")
        self.assertEqual(row["target"], f"{PLAYBOOK_ID}#1.0.0")
        self.assertIn("1700001000", row["timestamp"])
        self.assertEqual(row["after_status"], "active")

    def test_rollback_writes_audit_record_with_actor_and_timestamp(self):
        self._upload("1.0.0", ts=1_700_000_000)
        self._upload("1.1.0", ts=1_700_000_100)
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity="admin-1@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_001_000,
        )
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.1.0",
            actor_identity="admin-2@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_002_000,
        )
        pv.rollback_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity="admin-3@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_003_000,
        )

        rows = self._all_audit_rows()
        rollback_rows = [r for r in rows if r["action"] == "release_bundle_rollback"]
        self.assertEqual(len(rollback_rows), 1)
        row = rollback_rows[0]
        self.assertEqual(row["actor"], "admin-3@teamexos.com")
        self.assertEqual(row["target"], f"{PLAYBOOK_ID}#1.0.0")
        self.assertIn("1700003000", row["timestamp"])
        self.assertEqual(row["before_status"], "retired")
        self.assertEqual(row["after_status"], "active")
        self.assertEqual(row["prior_active_version"], "1.1.0")

        # Full sequence recorded: two activations + one rollback.
        all_relevant = [
            r
            for r in rows
            if r["action"] in ("release_bundle_activate", "release_bundle_rollback")
        ]
        self.assertEqual(len(all_relevant), 3)


# -- (4) no "Exos"/"EXOS" in rendered output ---------------------------------

class TestNoExosBranding(BundleActivateRollbackTestBase):
    def test_activate_and_rollback_audit_rows_contain_no_exos_branding(self):
        self._upload("1.0.0", ts=1_700_000_000)
        self._upload("1.1.0", ts=1_700_000_100)
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity="admin-1@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_001_000,
        )
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.1.0",
            actor_identity="admin-2@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_002_000,
        )
        pv.rollback_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity="admin-3@teamexos.com",
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_003_000,
        )

        rows = self._all_audit_rows()
        serialized = json.dumps(rows, default=str)
        self.assertNotIn("Exos", serialized)
        self.assertNotIn("EXOS", serialized)

        trail = pv.list_playbook_version_trail(PLAYBOOK_ID, self.ddb)
        serialized_trail = json.dumps(trail)
        self.assertNotIn("Exos", serialized_trail)
        self.assertNotIn("EXOS", serialized_trail)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestActivateSetsActive,
        TestRollbackRestoresPriorActive,
        TestAuditRecordsWritten,
        TestNoExosBranding,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
