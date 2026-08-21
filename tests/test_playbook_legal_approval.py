#!/usr/bin/env python3
"""
Executable tests for issue #485 blocker 2: the missing product path for Gate
7's step 2 -- `POST /api/admin/playbooks/{playbook_id}/versions/{version}/
legal-approval`.

## Root problem this proves fixed

`backend/src/playbook_versions.py::activate_release_bundle` has enforced
Gate 7 (`content_hash == legal_approval.content_hash`) since issue #242, but
NOTHING in the shipped product ever wrote `legal_approval` onto a
`playbook_versions` row -- only `tests/test_activation_gate7.py`'s own
`_set_legal_approval` helper, via a raw `update_item`, ever had. Every real
admin upload therefore landed a version Gate 7 could never let activate: the
shipped "Activate" button always 409'd.

This file exercises the real `POST .../legal-approval` route wired into
`backend/src/main.py`, then proves the missing half of the story: that a
version approved through THIS route -- not a test's raw `update_item` --
actually clears Gate 7 at the real `.../activate` route.

This test MUST FAIL on the pre-fix tree (no legal-approval endpoint, no
`record_legal_approval` function) and PASS after the fix.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-legalapproval-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-legalapproval-test"
)
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-legalapproval-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-legalapproval-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.playbook_versions as pv  # noqa: E402

ADMIN_SUB = "admin-1"
NON_ADMIN_SUB = "reviewer-1"
PLAYBOOK_ID = "synthetic-generic"
VERSION = "1.0.0"
APPROVAL_PATH = f"/api/admin/playbooks/{PLAYBOOK_ID}/versions/{VERSION}/legal-approval"
ACTIVATE_PATH = f"/api/admin/playbooks/{PLAYBOOK_ID}/versions/{VERSION}/activate"
CONTENT_HASH = "sha256:" + "cd" * 32


def _put_user(table, sub: str, is_admin: bool) -> None:
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@example.com",
            "status": "active",
            "is_admin": is_admin,
        }
    )


class LegalApprovalTestBase(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")

        self.ddb.create_table(
            TableName=os.environ["USERS_TABLE"],
            KeySchema=[{"AttributeName": "cognito_sub", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cognito_sub", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
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
            TableName=os.environ["PLAYBOOKS_TABLE"],
            KeySchema=[{"AttributeName": "playbook_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "playbook_id", "AttributeType": "S"}],
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

        self.users_table = self.ddb.Table(os.environ["USERS_TABLE"])
        self.versions_table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])
        self.audit_table = self.ddb.Table(os.environ["AUDIT_TABLE"])
        _put_user(self.users_table, ADMIN_SUB, is_admin=True)
        _put_user(self.users_table, NON_ADMIN_SUB, is_admin=False)

        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "token_use": "access"}
        )

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _authenticate_as(self, sub: str) -> None:
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": sub, "email": f"{sub}@example.com", "token_use": "access"}
        )

    def _upload(self, content_hash: str = CONTENT_HASH) -> None:
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version=VERSION,
            uploader_identity="uploader@example.com",
            dynamodb_resource=self.ddb,
            content_hash=content_hash,
        )

    def _approve(self, content_hash: str):
        return self.client.post(APPROVAL_PATH, json={"content_hash": content_hash})


class TestLegalApprovalRouteMounted(unittest.TestCase):
    def test_route_is_registered(self):
        registered = any(
            getattr(route, "path", None)
            == "/api/admin/playbooks/{playbook_id}/versions/{version}/legal-approval"
            for route in backend_main.app.routes
        )
        self.assertTrue(registered)


class TestLegalApprovalAuth(LegalApprovalTestBase):
    def test_non_admin_gets_403(self):
        self._upload()
        self._authenticate_as(NON_ADMIN_SUB)
        resp = self._approve(CONTENT_HASH)
        self.assertEqual(resp.status_code, 403)
        # Nothing should have been recorded.
        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": VERSION}
        )["Item"]
        self.assertNotIn("legal_approval", row)


class TestLegalApprovalValidation(LegalApprovalTestBase):
    def test_missing_content_hash_is_400(self):
        self._upload()
        resp = self.client.post(APPROVAL_PATH, json={})
        self.assertEqual(resp.status_code, 400)

    def test_empty_content_hash_is_400(self):
        self._upload()
        resp = self.client.post(APPROVAL_PATH, json={"content_hash": ""})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_version_is_404(self):
        # No upload at all -- there is no row to approve.
        resp = self._approve(CONTENT_HASH)
        self.assertEqual(resp.status_code, 404)

    def test_mismatched_content_hash_is_409_and_records_nothing(self):
        self._upload(content_hash=CONTENT_HASH)
        resp = self._approve("sha256:" + "ff" * 32)
        self.assertEqual(resp.status_code, 409, resp.text)
        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": VERSION}
        )["Item"]
        self.assertNotIn("legal_approval", row)


class TestLegalApprovalHappyPath(LegalApprovalTestBase):
    def test_matching_content_hash_records_approval(self):
        self._upload(content_hash=CONTENT_HASH)
        resp = self._approve(CONTENT_HASH)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["playbook_id"], PLAYBOOK_ID)
        self.assertEqual(body["version"], VERSION)
        self.assertEqual(body["content_hash"], CONTENT_HASH)
        self.assertEqual(body["approved_by"], ADMIN_SUB)
        self.assertIsNotNone(body["approved_at"])

        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": VERSION}
        )["Item"]
        self.assertEqual(row["legal_approval"]["content_hash"], CONTENT_HASH)
        self.assertEqual(row["legal_approval"]["approved_by"], ADMIN_SUB)

    def test_writes_an_audit_row_naming_actor_and_hash(self):
        self._upload(content_hash=CONTENT_HASH)
        self._approve(CONTENT_HASH)

        items = self.audit_table.scan()["Items"]
        approvals = [i for i in items if i.get("action") == "playbook_version_legal_approval"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["actor"], ADMIN_SUB)
        self.assertEqual(approvals[0]["content_hash"], CONTENT_HASH)

    def test_a_real_approval_through_this_route_actually_clears_gate_7(self):
        """The whole point: activation must succeed against an approval
        recorded through the real product path, not just a test's raw
        `update_item` (as tests/test_activation_gate7.py's own fixture
        does)."""
        self._upload(content_hash=CONTENT_HASH)

        approve_resp = self._approve(CONTENT_HASH)
        self.assertEqual(approve_resp.status_code, 200, approve_resp.text)

        activate_resp = self.client.post(ACTIVATE_PATH)
        self.assertEqual(activate_resp.status_code, 200, activate_resp.text)
        self.assertEqual(activate_resp.json()["status"], pv.STATUS_ACTIVE)

    def test_activation_still_fails_without_any_approval(self):
        """Sanity check that this test file's happy path isn't accidentally
        passing for some OTHER reason -- an upload with no approval call at
        all must still 409 at activate, exactly as issue #242 designed."""
        self._upload(content_hash=CONTENT_HASH)
        activate_resp = self.client.post(ACTIVATE_PATH)
        self.assertEqual(activate_resp.status_code, 409)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
