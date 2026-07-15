#!/usr/bin/env python3
"""
Executable tests for issue #242: a real playbook activation path with Gate
7 enforcement, wired to the resolver.

Before this fix, `backend/src/playbook_versions.py::activate_playbook_version`
(issue #79's v1 slice) flipped `playbook_versions.status` but never enforced
Gate 7 (ARCHITECTURE.md / docs/playbook-governance.md: `content_hash ==
legal_approval.content_hash`) and never wrote
`playbooks.active_release_bundle_hash` -- so activating a bundle had no
effect on what `backend/src/reviews.py::resolve_active_release_bundle_hash`
(and therefore the review pipeline) actually served, and there was no admin
HTTP endpoint at all.

Exercises the real `POST /api/admin/playbooks/{playbook_id}/versions/{version}/
activate` route wired into `backend/src/main.py` (issue #242 fix), using a
real FastAPI `TestClient` against the real `fastapi`/`boto3` stack, with AWS
(`users`, `playbook_versions`, `playbooks`, `audit` DynamoDB tables) mocked
with `moto` -- no live AWS, no network.

Per issue #242's acceptance criteria:
  1. Activating a version whose `content_hash` matches the approval sets
     the active bundle AND the resolver serves that hash.
  2. A mismatch (Gate 7) is rejected.
  3. No active bundle -> 503 (resolve_active_release_bundle_hash's existing,
     unmodified refusal).

This test MUST FAIL on the pre-fix tree (no activation endpoint, no
`activate_release_bundle` function) and PASS after the fix.

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

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-gate7-test")
os.environ.setdefault("PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-gate7-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-gate7-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-gate7-test")

import boto3  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.playbook_versions as pv  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

ADMIN_SUB = "admin-1"
NON_ADMIN_SUB = "reviewer-1"
PLAYBOOK_ID = "eiaa"
ACTIVATE_PATH = f"/api/admin/playbooks/{PLAYBOOK_ID}/versions/1.0.0/activate"


def _put_user(table, sub: str, is_admin: bool, status_: str = "active") -> None:
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@teamexos.com",
            "status": status_,
            "is_admin": is_admin,
        }
    )


class ActivationGate7TestBase(unittest.TestCase):
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
        self.playbooks_table = self.ddb.Table(os.environ["PLAYBOOKS_TABLE"])
        _put_user(self.users_table, ADMIN_SUB, is_admin=True)
        _put_user(self.users_table, NON_ADMIN_SUB, is_admin=False)

        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _authenticate_as(self, sub: str) -> None:
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": sub, "email": f"{sub}@teamexos.com", "token_use": "access"}
        )

    def _upload(self, version, content_hash, legal_approval=None, uploader="uploader@teamexos.com"):
        return pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version=version,
            uploader_identity=uploader,
            dynamodb_resource=self.ddb,
            content_hash=content_hash,
        )

    def _set_legal_approval(self, version, legal_approval):
        self.versions_table.update_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": version},
            UpdateExpression="SET legal_approval = :la",
            ExpressionAttributeValues={":la": legal_approval},
        )


# -- (0) route is mounted -----------------------------------------------

class TestActivationRouteMounted(unittest.TestCase):
    def test_route_is_registered(self):
        registered = any(
            getattr(route, "path", None)
            == "/api/admin/playbooks/{playbook_id}/versions/{version}/activate"
            and "POST" in getattr(route, "methods", set())
            for route in backend_main.app.routes
        )
        self.assertTrue(
            registered,
            "POST /api/admin/playbooks/{playbook_id}/versions/{version}/activate "
            "is not registered as a route in backend/src/main.py (issue #242).",
        )


# -- (1) admin gate -------------------------------------------------------

class TestActivationAdminGate(ActivationGate7TestBase):
    def test_non_admin_gets_403(self):
        self._upload("1.0.0", "sha256:" + "a" * 64)
        self._set_legal_approval("1.0.0", {"content_hash": "sha256:" + "a" * 64})
        self._authenticate_as(NON_ADMIN_SUB)
        resp = self.client.post(ACTIVATE_PATH)
        self.assertEqual(resp.status_code, 403)


# -- (2) matching hash activates AND the resolver serves it ---------------

class TestActivationMatchingHashServesResolver(ActivationGate7TestBase):
    def test_matching_hash_activates_and_resolver_serves_it(self):
        content_hash = "sha256:" + "a" * 64
        self._upload("1.0.0", content_hash)
        self._set_legal_approval("1.0.0", {"content_hash": content_hash})

        # Before activation: no active bundle -> resolver refuses (this is
        # the EXISTING, unmodified reviews.py behavior -- AC "no active
        # bundle -> 503").
        with self.assertRaises(HTTPException) as ctx:
            reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, reviews_module.NO_ACTIVE_PLAYBOOK_DETAIL)

        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(ACTIVATE_PATH)
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        self.assertEqual(result["status"], "active")

        # playbook_versions row is active.
        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        self.assertEqual(row["status"], "active")

        # playbooks.active_release_bundle_hash was written to the SAME
        # content_hash the version carries.
        playbook_row = self.playbooks_table.get_item(Key={"playbook_id": PLAYBOOK_ID})["Item"]
        self.assertEqual(playbook_row["active_release_bundle_hash"], content_hash)

        # The resolver -- reviews.py's SINGLE resolution point, entirely
        # unmodified by this fix -- now actually serves the newly activated
        # hash. This is the AC: "activating a bundle actually makes the
        # review pipeline serve it."
        resolved = reviews_module.resolve_active_release_bundle_hash(PLAYBOOK_ID, self.ddb)
        self.assertEqual(resolved, content_hash)


# -- (3) mismatch is rejected (Gate 7) -------------------------------------

class TestActivationGate7Mismatch(ActivationGate7TestBase):
    def test_mismatched_hash_is_rejected(self):
        self._upload("1.0.0", "sha256:" + "a" * 64)
        self._set_legal_approval("1.0.0", {"content_hash": "sha256:" + "b" * 64})

        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(ACTIVATE_PATH)
        self.assertEqual(resp.status_code, 409)

        # The version was NOT activated.
        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        self.assertEqual(row["status"], "draft")

        # playbooks table was never written.
        self.assertIsNone(
            self.playbooks_table.get_item(
                Key={"playbook_id": PLAYBOOK_ID}, ConsistentRead=True
            ).get("Item")
        )

    def test_missing_legal_approval_is_rejected(self):
        # Uploaded, but never approved -- no legal_approval field at all.
        self._upload("1.0.0", "sha256:" + "a" * 64)

        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(ACTIVATE_PATH)
        self.assertEqual(resp.status_code, 409)

        row = self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )["Item"]
        self.assertEqual(row["status"], "draft")

    def test_unknown_version_is_404(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(ACTIVATE_PATH)
        self.assertEqual(resp.status_code, 404)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestActivationRouteMounted,
        TestActivationAdminGate,
        TestActivationMatchingHashServesResolver,
        TestActivationGate7Mismatch,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
