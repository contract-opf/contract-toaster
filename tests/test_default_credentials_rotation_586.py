#!/usr/bin/env python3
"""
Tests for issue #586: `default_credentials_warning` was, until this fix,
purely informational -- a banner the SPA rendered, nothing more. A caller
still signed in on a shipped default password (admin/admin, user/user) had
full, unrestricted access to every route indefinitely.

Chosen enforcement (ticket: "pick ONE"): refuse privileged operations while
the flag is set, wired into the SAME `get_active_user_row` dependency
(nearly) every route already goes through -- both the copy in
`backend/src/main.py` and the deliberately-separate copy in
`backend/src/review_routes.py` (see that module's "Dependency providers"
comment for why it is not imported from main.py). Two routes stay reachable
so a caller can see and clear the warning: `GET /api/me` and
`POST /api/me/password`.

This test MUST FAIL on the pre-fix tree (every route was reachable
regardless of `default_credentials_warning`) and PASS after the fix. Proves
BOTH directions per the ticket's acceptance criteria: the privileged action
SUCCEEDS on an already-rotated/SSO caller and on the two exempt routes, and
is REFUSED for a still-default caller -- then SUCCEEDS again for that same
caller once they rotate (positive control).

Also re-runs issue #469's login-throttle assertion (mode-gate priority: the
route level test below signs in via dependency override, not the throttled
POST /api/auth/login path, so #469's own suite is the source of truth for
"the throttle is unchanged" -- required verification runs it separately).

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

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-rotation586-test")
os.environ.setdefault(
    "RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-rotation586-test"
)
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-rotation586-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-rotation586-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-rotation586-test")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-rotation586-test")
os.environ.setdefault("AUTH_SETTINGS_TABLE", "contract-toaster-auth-settings-rotation586-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-rotation586-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-rotation586-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-rotation586-test"
)
os.environ.setdefault("DEMO_TOKEN_SECRET", "unit-test-demo-secret")

import boto3  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.demo_auth as demo_auth  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.review_routes as review_routes  # noqa: E402

ADMIN_SUB = "admin-1"
ADMIN = {"cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "is_admin": True}


# ---------------------------------------------------------------------------
# Route level, backend.src.main.app (admin routes: GET /api/users,
# GET /api/users/sync-status, GET/POST /api/me[/password]).
# ---------------------------------------------------------------------------


class MainAppRotationEnforcementTest(unittest.TestCase):
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
            TableName=os.environ["AUTH_SETTINGS_TABLE"],
            KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_id", "AttributeType": "S"}],
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
        self.users_table.put_item(Item={
            "cognito_sub": ADMIN_SUB, "email": ADMIN["email"],
            "status": "active", "is_admin": True,
        })

        demo_auth.seed_demo_users(self.ddb)
        demo_auth.set_auth_mode(demo_auth.AUTH_MODE_BOTH, ADMIN, self.ddb)

        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _sign_in_as(self, cognito_sub: str) -> None:
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": cognito_sub, "email": "", "token_use": "access"}
        )

    def test_admin_route_refused_while_seeded_admin_unrotated(self):
        """The privileged action (listing users) SUCCEEDS today only because
        it always did -- this asserts the pre-fix behavior would be a 200,
        by proving the post-fix behavior is specifically OUR new gate (its
        distinctive detail text), not the pre-existing "not an admin" 403
        this same route already raises for a different reason. If the
        enforcement were absent, this request would return 200 (the caller
        genuinely is an admin, so the route's own admin check passes)."""
        self._sign_in_as(demo_auth.local_user_sub("admin"))
        response = self.client.get("/api/users")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("shipped default password", response.json()["detail"])

    def test_admin_route_succeeds_after_rotation_positive_control(self):
        """Positive control (ticket AC): the SAME caller, the SAME route,
        after rotating away from the shipped default -- must succeed."""
        self._sign_in_as(demo_auth.local_user_sub("admin"))
        rotated = self.client.post(
            "/api/me/password",
            json={"current_password": "admin", "new_password": "brand-new-pw"},
        )
        self.assertEqual(rotated.status_code, 200, rotated.text)

        response = self.client.get("/api/users")
        self.assertEqual(response.status_code, 200, response.text)

    def test_non_admin_seeded_user_blocked_before_the_admin_check_even_runs(self):
        """A non-admin, still-default caller hitting an admin-only route
        gets OUR detail, not "Admin privilege required" -- proving the
        rotation gate runs BEFORE the route's own authorization logic, for
        every caller, not just admins."""
        self._sign_in_as(demo_auth.local_user_sub("user"))
        response = self.client.get("/api/users/sync-status")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("shipped default password", response.json()["detail"])
        self.assertNotIn("Admin privilege required", response.json()["detail"])

    def test_get_me_stays_reachable_while_unrotated(self):
        """GET /api/me must stay reachable while unrotated -- it is the
        route that SURFACES the warning; gating it would hide the very
        thing the caller needs to act on."""
        self._sign_in_as(demo_auth.local_user_sub("admin"))
        response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["default_credentials_warning"])

    def test_change_password_route_stays_reachable_while_unrotated(self):
        """POST /api/me/password must stay reachable while unrotated -- it
        is the ONLY way to clear the warning. A wrong current_password still
        401s (the real handler ran, it was not blocked by the rotation
        gate)."""
        self._sign_in_as(demo_auth.local_user_sub("admin"))
        response = self.client.post(
            "/api/me/password",
            json={"current_password": "not-the-password", "new_password": "brand-new-pw"},
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_sso_caller_never_blocked(self):
        """An SSO row never carries `default_credentials_warning` -- an
        SSO-admitted admin's privileged routes are entirely unaffected."""
        self._sign_in_as(ADMIN_SUB)
        response = self.client.get("/api/users")
        self.assertEqual(response.status_code, 200, response.text)


# ---------------------------------------------------------------------------
# Route level, backend.src.review_routes.router -- the SEPARATE
# get_active_user_row copy that module deliberately maintains (see its
# "Dependency providers" comment). Confirms the fix was applied there too,
# not just in main.py -- the review/upload pipeline is exactly the "real
# work" the ticket's Goal names.
# ---------------------------------------------------------------------------


def _password_user_row(username: str, is_admin: bool = False) -> dict:
    return {
        "cognito_sub": demo_auth.local_user_sub(username),
        "username": username,
        "user_type": "password",
        "status": "active",
        "is_admin": is_admin,
    }


class ReviewRoutesRotationEnforcementTest(unittest.TestCase):
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
        self.users_table = self.ddb.Table(os.environ["USERS_TABLE"])
        demo_auth.seed_demo_users(self.ddb)

        # GET /api/playbooks' handler (_load_playbook_catalog) scans this
        # table regardless of registry content -- needed even for the
        # not-blocked positive control below, or the request 500s before
        # ever reaching the rotation-gate assertion.
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

        self.app = FastAPI()
        self.app.include_router(review_routes.router)
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = lambda: self.ddb
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _sign_in_as(self, cognito_sub: str) -> None:
        self.app.dependency_overrides[review_routes.get_current_user] = (
            lambda: {"sub": cognito_sub, "email": "", "token_use": "access"}
        )

    def test_playbooks_route_refused_while_seeded_user_unrotated(self):
        self._sign_in_as(demo_auth.local_user_sub("user"))
        response = self.client.get("/api/playbooks")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("shipped default password", response.json()["detail"])

    def test_playbooks_route_succeeds_for_a_rotated_password_row(self):
        """Positive control: a non-seeded / already-rotated password row is
        never gated."""
        self.users_table.put_item(Item={
            **_password_user_row("attorney"),
            "password_hash": "deadbeef$deadbeef",  # never verifies against any seed default
        })
        self._sign_in_as(demo_auth.local_user_sub("attorney"))
        response = self.client.get("/api/playbooks")
        self.assertEqual(response.status_code, 200, response.text)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        MainAppRotationEnforcementTest,
        ReviewRoutesRotationEnforcementTest,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
