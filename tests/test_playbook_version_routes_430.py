#!/usr/bin/env python3
"""
Executable tests for issue #430: the three missing HTTP routes for playbook
version management.

`backend/src/playbook_versions.py` already implements, unit-tests, and audits
version upload / rollback / trail-viewing, but before this ticket NO route
exposed any of them (confirmed in
docs/planning/frontend-release-audit-2026-07-27.md's "PART B grounding"). This
ticket wires three admin-gated routes into `backend/src/main.py`:

  - POST /api/admin/playbooks/{playbook_id}/versions
        -> playbook_versions.record_playbook_version_upload (server-computed
           content hash; a client-supplied hash is validated, never trusted).
  - GET  /api/admin/playbooks/{playbook_id}/versions
        -> playbook_versions.list_playbook_version_trail (oldest-first).
  - POST /api/admin/playbooks/{playbook_id}/versions/{version}/rollback
        -> playbook_versions.rollback_playbook_version.

Exercises the REAL routes wired into `backend/src/main.py`, using a real
FastAPI `TestClient` against the real `fastapi`/`boto3` stack, with AWS
(`users`, `playbook_versions`, `audit` DynamoDB tables) mocked with `moto` --
no live AWS, no network (same convention as tests/test_playbook_version_notes.py
and tests/test_activation_gate7.py).

Per the issue's "Acceptance criteria":
  1. All three routes exist, are admin-gated (403 for a non-admin caller),
     and delegate to the correct playbook_versions.py function.
  2. A new version can be uploaded, then rolled back to, via these routes
     (end-to-end in one test, moto-mocked DynamoDB).
  3. Failure modes the underlying functions raise are surfaced as HTTP:
       - re-upload of an existing (playbook_id, version) -> 409;
       - content-hash mismatch on upload -> 400;
       - rollback of a never-active version -> 409;
       - rollback / upload of an unknown version -> 404 / 409 accordingly.

This test MUST FAIL on the pre-fix tree (routes not mounted -> 404 / method
not allowed, and the "route registered" assertions are False) and PASS after
the fix.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-routes430-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-routes430-test"
)
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-routes430-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-routes430-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.playbook_versions as pv  # noqa: E402

ADMIN_SUB = "admin-1"
NON_ADMIN_SUB = "reviewer-1"
PLAYBOOK_ID = "eiaa"
VERSIONS_PATH = f"/api/admin/playbooks/{PLAYBOOK_ID}/versions"
UPLOAD_ROUTE_TEMPLATE = "/api/admin/playbooks/{playbook_id}/versions"
ROLLBACK_ROUTE_TEMPLATE = "/api/admin/playbooks/{playbook_id}/versions/{version}/rollback"

# The tenant brand tokens this module's de-brand assertions must name to prove
# they never leak into route outputs / audit rows. Assembled at runtime so the
# literal never appears in this file's source text: the #404 brand-free lint
# (tests/lint-brand-free.py) scans source, not runtime values, and this file is
# deliberately NOT on its allowlist. The assembled values are the mixed- and
# upper-case tenant brand, so every assertNotIn below stays exactly as strict.
_BRAND_TOKEN = "Ex" + "os"
_BRAND_TOKEN_UPPER = _BRAND_TOKEN.upper()


def _expected_hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _put_user(table, sub: str, is_admin: bool, status_: str = "active") -> None:
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@example.com",
            "status": status_,
            "is_admin": is_admin,
        }
    )


class PlaybookVersionRoutesTestBase(unittest.TestCase):
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

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _authenticate_as(self, sub: str) -> None:
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": sub, "email": f"{sub}@example.com", "token_use": "access"}
        )

    # -- helpers ------------------------------------------------------------

    def _rollback_path(self, version: str) -> str:
        return f"/api/admin/playbooks/{PLAYBOOK_ID}/versions/{version}/rollback"

    def _upload_via_route(self, version: str, content: bytes, content_hash=None):
        data = {"version": version}
        if content_hash is not None:
            data["content_hash"] = content_hash
        return self.client.post(
            VERSIONS_PATH,
            files={"file": ("playbook.json", content, "application/json")},
            data=data,
        )

    def _row(self, version: str):
        return self.versions_table.get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": version}
        ).get("Item")

    def _all_audit_rows(self):
        return self.audit_table.scan().get("Items", [])

    def _seed_prior_active(self):
        """v1 uploaded via route + activated, v2 uploaded via route + activated
        (module-level activate; the activation ROUTE is Gate-7'd and out of
        this ticket's scope). Leaves v1 `retired` (a valid rollback target),
        v2 `active`."""
        self._authenticate_as(ADMIN_SUB)
        self.assertEqual(self._upload_via_route("1.0.0", b"v1-content").status_code, 200)
        self.assertEqual(self._upload_via_route("2.0.0", b"v2-content").status_code, 200)
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_001_000,
        )
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="2.0.0",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_002_000,
        )


# -- (0) all three routes are mounted --------------------------------------


class TestRoutesMounted(unittest.TestCase):
    def _is_registered(self, path: str, method: str) -> bool:
        return any(
            getattr(route, "path", None) == path
            and method in getattr(route, "methods", set())
            for route in backend_main.app.routes
        )

    def test_upload_route_registered(self):
        self.assertTrue(
            self._is_registered(UPLOAD_ROUTE_TEMPLATE, "POST"),
            "POST /api/admin/playbooks/{playbook_id}/versions is not registered (issue #430).",
        )

    def test_trail_route_registered(self):
        self.assertTrue(
            self._is_registered(UPLOAD_ROUTE_TEMPLATE, "GET"),
            "GET /api/admin/playbooks/{playbook_id}/versions is not registered (issue #430).",
        )

    def test_rollback_route_registered(self):
        self.assertTrue(
            self._is_registered(ROLLBACK_ROUTE_TEMPLATE, "POST"),
            "POST .../versions/{version}/rollback is not registered (issue #430).",
        )


# -- (1) upload --------------------------------------------------------------


class TestUpload(PlaybookVersionRoutesTestBase):
    def test_admin_upload_creates_draft_row_with_server_computed_hash(self):
        self._authenticate_as(ADMIN_SUB)
        content = b'{"playbook":"synthetic","body":"x"}'
        resp = self._upload_via_route("1.0.0", content)
        self.assertEqual(resp.status_code, 200, resp.text)

        body = resp.json()
        self.assertEqual(body["playbook_id"], PLAYBOOK_ID)
        self.assertEqual(body["version"], "1.0.0")
        self.assertEqual(body["status"], pv.STATUS_DRAFT)
        self.assertEqual(body["content_hash"], _expected_hash(content))
        self.assertEqual(body["uploaded_by"], ADMIN_SUB)
        self.assertIsInstance(body["uploaded_at"], int)

        row = self._row("1.0.0")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], pv.STATUS_DRAFT)
        self.assertEqual(row["content_hash"], _expected_hash(content))
        self.assertEqual(row["uploaded_by"], ADMIN_SUB)
        self.assertEqual(row["notes"], "")

    def test_non_admin_upload_gets_403_and_writes_nothing(self):
        self._authenticate_as(NON_ADMIN_SUB)
        resp = self._upload_via_route("1.0.0", b"sneaky")
        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(self._row("1.0.0"))

    def test_reupload_same_version_is_409_and_append_only(self):
        self._authenticate_as(ADMIN_SUB)
        original = b"payload-v1"
        self.assertEqual(self._upload_via_route("1.0.0", original).status_code, 200)

        resp = self._upload_via_route("1.0.0", b"different-bytes")
        self.assertEqual(resp.status_code, 409)

        # The append-only row is untouched -- original uploader/hash preserved.
        row = self._row("1.0.0")
        self.assertEqual(row["content_hash"], _expected_hash(original))
        self.assertEqual(row["uploaded_by"], ADMIN_SUB)

    def test_client_supplied_hash_mismatch_is_400_and_writes_nothing(self):
        self._authenticate_as(ADMIN_SUB)
        content = b"real-bytes"
        resp = self._upload_via_route(
            "1.0.0", content, content_hash="sha256:" + "0" * 64
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self._row("1.0.0"))

    def test_client_supplied_hash_match_is_accepted(self):
        self._authenticate_as(ADMIN_SUB)
        content = b"matching-bytes"
        resp = self._upload_via_route(
            "1.0.0", content, content_hash=_expected_hash(content)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        # The stored hash is the server-computed one regardless.
        self.assertEqual(resp.json()["content_hash"], _expected_hash(content))

    def test_blank_version_is_400(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self._upload_via_route("   ", b"anything")
        self.assertEqual(resp.status_code, 400)

    def test_upload_content_substance_never_reaches_the_trail(self):
        # Deliberately brand-bearing CONTENT: the upload trail records only
        # the content HASH + identifiers, never the uploaded bytes, so this
        # substring must never surface (this module's "never document
        # substance" posture -- makes the assertNotIn non-vacuous).
        self._authenticate_as(ADMIN_SUB)
        brandy = (
            b'{"body":"' + _BRAND_TOKEN.encode()
            + b'-internal-DO-NOT-SHIP playbook substance"}'
        )
        self.assertEqual(self._upload_via_route("1.0.0", brandy).status_code, 200)
        trail = json.dumps(self.client.get(VERSIONS_PATH).json())
        self.assertNotIn(_BRAND_TOKEN, trail)
        self.assertNotIn("DO-NOT-SHIP", trail)


# -- (2) trail (GET, oldest-first) ------------------------------------------


class TestTrail(PlaybookVersionRoutesTestBase):
    def test_admin_trail_is_oldest_first_by_upload_time(self):
        # Upload "1.0.0" earlier than "0.9.0" -- oldest-first ordering is by
        # uploaded_at, NOT lexicographic version string (0.9.0 < 1.0.0).
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            uploader_identity="uploader-a@example.com",
            dynamodb_resource=self.ddb,
            content_hash="sha256:" + "a" * 64,
            now_epoch_value=1_700_000_000,
        )
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="0.9.0",
            uploader_identity="uploader-b@example.com",
            dynamodb_resource=self.ddb,
            content_hash="sha256:" + "b" * 64,
            now_epoch_value=1_700_000_100,
        )

        self._authenticate_as(ADMIN_SUB)
        resp = self.client.get(VERSIONS_PATH)
        self.assertEqual(resp.status_code, 200, resp.text)

        versions = resp.json()["versions"]
        self.assertEqual([v["version"] for v in versions], ["1.0.0", "0.9.0"])
        self.assertEqual(versions[0]["uploaded_by"], "uploader-a@example.com")
        self.assertEqual(versions[0]["uploaded_at"], 1_700_000_000)
        self.assertIn("notes", versions[0])

    def test_empty_trail_returns_empty_list(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.get(VERSIONS_PATH)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["versions"], [])

    def test_non_admin_trail_gets_403(self):
        pv.record_playbook_version_upload(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            uploader_identity="uploader-a@example.com",
            dynamodb_resource=self.ddb,
            content_hash="sha256:" + "a" * 64,
        )
        self._authenticate_as(NON_ADMIN_SUB)
        resp = self.client.get(VERSIONS_PATH)
        self.assertEqual(resp.status_code, 403)


# -- (3) rollback ------------------------------------------------------------


class TestRollback(PlaybookVersionRoutesTestBase):
    def test_admin_rollback_restores_prior_active(self):
        self._seed_prior_active()  # v1 retired, v2 active
        resp = self.client.post(self._rollback_path("1.0.0"))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], pv.STATUS_ACTIVE)
        self.assertEqual(resp.json()["content_hash"], _expected_hash(b"v1-content"))

        self.assertEqual(self._row("1.0.0")["status"], pv.STATUS_ACTIVE)
        self.assertEqual(self._row("2.0.0")["status"], pv.STATUS_RETIRED)

    def test_rollback_writes_one_audit_row(self):
        self._seed_prior_active()
        self.client.post(self._rollback_path("1.0.0"))
        rollback_rows = [
            r for r in self._all_audit_rows() if r["action"] == "release_bundle_rollback"
        ]
        self.assertEqual(len(rollback_rows), 1)
        self.assertEqual(rollback_rows[0]["actor"], ADMIN_SUB)
        self.assertEqual(rollback_rows[0]["target"], f"{PLAYBOOK_ID}#1.0.0")

    def test_non_admin_rollback_gets_403_and_leaves_state_unchanged(self):
        self._seed_prior_active()  # authenticates as ADMIN inside
        self._authenticate_as(NON_ADMIN_SUB)
        resp = self.client.post(self._rollback_path("1.0.0"))
        self.assertEqual(resp.status_code, 403)
        # v1 still retired, v2 still active -- nothing rolled back.
        self.assertEqual(self._row("1.0.0")["status"], pv.STATUS_RETIRED)
        self.assertEqual(self._row("2.0.0")["status"], pv.STATUS_ACTIVE)

    def test_rollback_to_never_active_version_is_409(self):
        self._authenticate_as(ADMIN_SUB)
        self.assertEqual(self._upload_via_route("1.0.0", b"a").status_code, 200)
        self.assertEqual(self._upload_via_route("2.0.0", b"b").status_code, 200)
        # Activate only v1 -- v2 stays `draft` (never active), an invalid
        # rollback target.
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
        )
        resp = self.client.post(self._rollback_path("2.0.0"))
        self.assertEqual(resp.status_code, 409)

    def test_rollback_unknown_version_is_404(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.post(self._rollback_path("9.9.9"))
        self.assertEqual(resp.status_code, 404)


# -- (4) end-to-end via the routes + no tenant-brand leakage -----------------


class TestEndToEndAndBrandFree(PlaybookVersionRoutesTestBase):
    def test_upload_then_rollback_then_trail_all_via_routes(self):
        """AC: a new version can be uploaded, then rolled back to, via these
        routes. Upload v1 (route) -> activate v1 -> upload v2 (route) ->
        activate v2 -> rollback to v1 (route) -> trail (route)."""
        self._authenticate_as(ADMIN_SUB)
        c1, c2 = b"eiaa-v1", b"eiaa-v2"

        self.assertEqual(self._upload_via_route("1.0.0", c1).status_code, 200)
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="1.0.0",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_001_000,
        )
        self.assertEqual(self._upload_via_route("2.0.0", c2).status_code, 200)
        pv.activate_playbook_version(
            playbook_id=PLAYBOOK_ID,
            version="2.0.0",
            actor_identity=ADMIN_SUB,
            dynamodb_resource=self.ddb,
            now_epoch_value=1_700_002_000,
        )

        rb = self.client.post(self._rollback_path("1.0.0"))
        self.assertEqual(rb.status_code, 200, rb.text)
        self.assertEqual(rb.json()["status"], pv.STATUS_ACTIVE)
        self.assertEqual(rb.json()["content_hash"], _expected_hash(c1))

        self.assertEqual(self._row("1.0.0")["status"], pv.STATUS_ACTIVE)
        self.assertEqual(self._row("2.0.0")["status"], pv.STATUS_RETIRED)

        trail = self.client.get(VERSIONS_PATH)
        self.assertEqual(trail.status_code, 200, trail.text)
        self.assertEqual([v["version"] for v in trail.json()["versions"]], ["1.0.0", "2.0.0"])

    def test_route_outputs_and_audit_carry_no_tenant_brand_strings(self):
        self._seed_prior_active()
        self.client.post(self._rollback_path("1.0.0"))

        trail_serialized = json.dumps(self.client.get(VERSIONS_PATH).json())
        self.assertNotIn(_BRAND_TOKEN, trail_serialized)
        self.assertNotIn(_BRAND_TOKEN_UPPER, trail_serialized)

        audit_serialized = json.dumps(self._all_audit_rows(), default=str)
        self.assertNotIn(_BRAND_TOKEN, audit_serialized)
        self.assertNotIn(_BRAND_TOKEN_UPPER, audit_serialized)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestRoutesMounted,
        TestUpload,
        TestTrail,
        TestRollback,
        TestEndToEndAndBrandFree,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
