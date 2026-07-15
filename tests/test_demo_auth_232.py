#!/usr/bin/env python3
"""
Slice test for issue #232 (Current scope (v1), maintainer-confirmed
2026-07-10): a demo feature with an admin-configurable auth METHOD toggle
-- "access only (SSO)", "username + password", and "both" -- backed by a
stored+served auth-mode setting, seeded demo credentials, and a users table
that supports BOTH SSO and username/password users with add/remove CRUD.

This loop's slice is BACKEND + moto-mocked DynamoDB only (no live AWS, no
network, no CDK). The admin toggle UI and Cognito username/password IdP
support in infra/lib/nested/auth-stack.ts are explicit follow-ons (noted in
the ticket itself) -- this test exercises the real `backend/src/demo_auth.py`
surface (mode config store/serve, seeded credentials, user CRUD for both
user types) plus the HTTP routes wired into `backend/src/main.py`, same
convention as tests/test_retention_window_config_34.py.

This test MUST FAIL on the pre-fix tree (no `src/demo_auth.py`, no
`AUTH_SETTINGS_TABLE`-backed mode setting, no seeded admin/admin & user/user
credentials, no `/api/admin/auth-mode` or `/api/users` POST/DELETE routes)
and PASS after the fix.

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
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")
os.environ.setdefault("AUTH_SETTINGS_TABLE", "contract-toaster-auth-settings-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.demo_auth as demo_auth  # noqa: E402
import src.main as backend_main  # noqa: E402

ADMIN_SUB = "admin-1"
ADMIN = {"cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "is_admin": True}
NON_ADMIN_SUB = "reviewer-1"
NON_ADMIN = {"cognito_sub": NON_ADMIN_SUB, "email": f"{NON_ADMIN_SUB}@example.com", "is_admin": False}


class DemoAuthTestBase(unittest.TestCase):
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
        # Seed a real SSO admin row for the admin-gated calls in these tests
        # (independent of demo_auth's OWN seeded admin/admin credentials).
        self.users_table.put_item(Item={
            "cognito_sub": ADMIN_SUB, "email": ADMIN["email"],
            "status": "active", "is_admin": True,
        })
        self.users_table.put_item(Item={
            "cognito_sub": NON_ADMIN_SUB, "email": NON_ADMIN["email"],
            "status": "active", "is_admin": False,
        })

    def tearDown(self):
        self._mock_aws.stop()


# ---------------------------------------------------------------------------
# (1) Auth-mode logic: stored mode is served, and each mode gates the
#     expected credential path.
# ---------------------------------------------------------------------------

class TestAuthModeLogic(DemoAuthTestBase):
    def test_default_mode_is_sso_when_no_row_exists(self):
        settings = demo_auth.get_auth_mode_settings(ADMIN, self.ddb)
        self.assertEqual(settings["auth_mode"], demo_auth.AUTH_MODE_SSO)

    def test_set_auth_mode_persists_and_is_served_back(self):
        result = demo_auth.set_auth_mode(demo_auth.AUTH_MODE_BOTH, ADMIN, self.ddb)
        self.assertEqual(result["auth_mode"], demo_auth.AUTH_MODE_BOTH)

        settings = demo_auth.get_auth_mode_settings(ADMIN, self.ddb)
        self.assertEqual(settings["auth_mode"], demo_auth.AUTH_MODE_BOTH)

    def test_set_auth_mode_rejects_non_admin(self):
        with self.assertRaises(Exception) as ctx:
            demo_auth.set_auth_mode(demo_auth.AUTH_MODE_PASSWORD, NON_ADMIN, self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    def test_set_auth_mode_rejects_invalid_value(self):
        with self.assertRaises(Exception) as ctx:
            demo_auth.set_auth_mode("carrier-pigeon", ADMIN, self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    def test_password_login_rejected_under_sso_only_mode(self):
        demo_auth.seed_demo_users(self.ddb)
        demo_auth.set_auth_mode(demo_auth.AUTH_MODE_SSO, ADMIN, self.ddb)

        with self.assertRaises(Exception) as ctx:
            demo_auth.login_with_password("admin", "admin", self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    def test_password_login_accepted_under_password_mode(self):
        demo_auth.seed_demo_users(self.ddb)
        demo_auth.set_auth_mode(demo_auth.AUTH_MODE_PASSWORD, ADMIN, self.ddb)

        result = demo_auth.login_with_password("admin", "admin", self.ddb)
        self.assertEqual(result["username"], "admin")
        self.assertTrue(result["is_admin"])

    def test_password_login_accepted_under_both_mode(self):
        demo_auth.seed_demo_users(self.ddb)
        demo_auth.set_auth_mode(demo_auth.AUTH_MODE_BOTH, ADMIN, self.ddb)

        result = demo_auth.login_with_password("user", "user", self.ddb)
        self.assertEqual(result["username"], "user")
        self.assertFalse(result["is_admin"])

    def test_sso_admission_allowed_under_sso_and_both_not_password(self):
        self.assertTrue(demo_auth.sso_admission_allowed(demo_auth.AUTH_MODE_SSO))
        self.assertTrue(demo_auth.sso_admission_allowed(demo_auth.AUTH_MODE_BOTH))
        self.assertFalse(demo_auth.sso_admission_allowed(demo_auth.AUTH_MODE_PASSWORD))

    def test_password_login_wrong_password_rejected(self):
        demo_auth.seed_demo_users(self.ddb)
        demo_auth.set_auth_mode(demo_auth.AUTH_MODE_BOTH, ADMIN, self.ddb)

        with self.assertRaises(Exception) as ctx:
            demo_auth.login_with_password("admin", "not-the-password", self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 401)


# ---------------------------------------------------------------------------
# (2) Seeded users: admin/admin (admin role) and user/user (user role) exist.
# ---------------------------------------------------------------------------

class TestSeededDemoUsers(DemoAuthTestBase):
    def test_seed_creates_admin_and_user_credentials(self):
        demo_auth.seed_demo_users(self.ddb)

        admin_row = self.users_table.get_item(
            Key={"cognito_sub": demo_auth.local_user_sub("admin")}
        )["Item"]
        self.assertEqual(admin_row["username"], "admin")
        self.assertTrue(admin_row["is_admin"])
        self.assertEqual(admin_row["role"], "admin")
        self.assertEqual(admin_row["user_type"], demo_auth.USER_TYPE_PASSWORD)
        self.assertNotEqual(admin_row["password_hash"], "admin")  # never stored plaintext

        user_row = self.users_table.get_item(
            Key={"cognito_sub": demo_auth.local_user_sub("user")}
        )["Item"]
        self.assertEqual(user_row["username"], "user")
        self.assertFalse(user_row["is_admin"])
        self.assertEqual(user_row["role"], "user")

    def test_seed_is_idempotent_and_does_not_clobber_admin_changes(self):
        demo_auth.seed_demo_users(self.ddb)
        # Simulate an admin having changed the seeded admin's status.
        self.users_table.update_item(
            Key={"cognito_sub": demo_auth.local_user_sub("admin")},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "suspended"},
        )
        demo_auth.seed_demo_users(self.ddb)  # re-run seed; must not clobber
        admin_row = self.users_table.get_item(
            Key={"cognito_sub": demo_auth.local_user_sub("admin")}
        )["Item"]
        self.assertEqual(admin_row["status"], "suspended")


# ---------------------------------------------------------------------------
# (3) CRUD for both user types: add + remove an SSO user and a
#     username/password user.
# ---------------------------------------------------------------------------

class TestUserCrudBothTypes(DemoAuthTestBase):
    def test_add_and_remove_sso_user(self):
        created = demo_auth.add_user(
            {"user_type": "sso", "email": "new-sso@example.com", "is_admin": False},
            ADMIN,
            self.ddb,
        )
        self.assertEqual(created["user_type"], demo_auth.USER_TYPE_SSO)
        sub = created["cognito_sub"]
        self.assertIsNotNone(self.users_table.get_item(Key={"cognito_sub": sub}).get("Item"))

        result = demo_auth.remove_user(sub, ADMIN, self.ddb)
        self.assertTrue(result["removed"])
        self.assertIsNone(self.users_table.get_item(Key={"cognito_sub": sub}).get("Item"))

    def test_add_and_remove_password_user(self):
        created = demo_auth.add_user(
            {"user_type": "password", "username": "pilot", "password": "hunter2", "is_admin": False},
            ADMIN,
            self.ddb,
        )
        self.assertEqual(created["user_type"], demo_auth.USER_TYPE_PASSWORD)
        sub = created["cognito_sub"]
        self.assertEqual(sub, demo_auth.local_user_sub("pilot"))

        # New password user can log in once password mode is enabled.
        demo_auth.set_auth_mode(demo_auth.AUTH_MODE_PASSWORD, ADMIN, self.ddb)
        login_result = demo_auth.login_with_password("pilot", "hunter2", self.ddb)
        self.assertEqual(login_result["username"], "pilot")

        result = demo_auth.remove_user(sub, ADMIN, self.ddb)
        self.assertTrue(result["removed"])
        with self.assertRaises(Exception) as ctx:
            demo_auth.login_with_password("pilot", "hunter2", self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 401)

    def test_add_user_rejects_non_admin(self):
        with self.assertRaises(Exception) as ctx:
            demo_auth.add_user(
                {"user_type": "password", "username": "x", "password": "y"},
                NON_ADMIN,
                self.ddb,
            )
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    def test_add_user_rejects_unknown_user_type(self):
        with self.assertRaises(Exception) as ctx:
            demo_auth.add_user({"user_type": "carrier-pigeon"}, ADMIN, self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    def test_remove_user_404_for_unknown_sub(self):
        with self.assertRaises(Exception) as ctx:
            demo_auth.remove_user("no-such-sub", ADMIN, self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 404)

    def test_remove_user_rejects_self_removal(self):
        with self.assertRaises(Exception) as ctx:
            demo_auth.remove_user(ADMIN_SUB, ADMIN, self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)


# ---------------------------------------------------------------------------
# (4) De-brand: rendered/served UI text + logs contain no "Exos"/"EXOS".
# ---------------------------------------------------------------------------

class TestDeBrand(DemoAuthTestBase):
    def test_auth_mode_options_labels_are_unbranded(self):
        settings = demo_auth.get_auth_mode_settings(ADMIN, self.ddb)
        for opt in settings["auth_mode_options"]:
            self.assertNotIn("exos", opt["label"].lower(), f"branded label: {opt['label']!r}")

    def test_error_messages_are_unbranded(self):
        demo_auth.set_auth_mode(demo_auth.AUTH_MODE_SSO, ADMIN, self.ddb)
        try:
            demo_auth.login_with_password("admin", "admin", self.ddb)
            self.fail("expected HTTPException")
        except Exception as exc:
            detail = str(getattr(exc, "detail", exc))
            self.assertNotIn("exos", detail.lower())

    def test_logging_emits_no_branding(self):
        import logging
        import io

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("src.demo_auth")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            demo_auth.seed_demo_users(self.ddb)
            demo_auth.set_auth_mode(demo_auth.AUTH_MODE_BOTH, ADMIN, self.ddb)
            demo_auth.login_with_password("admin", "admin", self.ddb)
            demo_auth.add_user(
                {"user_type": "password", "username": "z", "password": "z"}, ADMIN, self.ddb
            )
        finally:
            logger.removeHandler(handler)
        log_output = stream.getvalue()
        self.assertNotIn("exos", log_output.lower())
        # Never log a plaintext password.
        self.assertNotIn("hunter2", log_output)
        self.assertNotIn("'password': 'z'", log_output)


# ---------------------------------------------------------------------------
# (5) Real HTTP surface: /api/admin/auth-mode, /api/users (POST/DELETE),
#     /api/auth/login -- through backend/src/main.py.
# ---------------------------------------------------------------------------

class TestHttpSurface(DemoAuthTestBase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        super().tearDown()

    def _as(self, sub: str, email: str):
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": sub, "email": email, "token_use": "access"}
        )

    def test_get_and_post_admin_auth_mode(self):
        self._as(ADMIN_SUB, ADMIN["email"])

        response = self.client.get("/api/admin/auth-mode")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["auth_mode"], demo_auth.AUTH_MODE_SSO)

        response = self.client.post("/api/admin/auth-mode", json={"auth_mode": "both"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["auth_mode"], "both")

        response = self.client.get("/api/admin/auth-mode")
        self.assertEqual(response.json()["auth_mode"], "both")

    def test_post_and_delete_users(self):
        self._as(ADMIN_SUB, ADMIN["email"])

        response = self.client.post(
            "/api/users",
            json={"user_type": "password", "username": "pilot2", "password": "hunter2"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        sub = response.json()["cognito_sub"]

        response = self.client.delete(f"/api/users/{sub}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["removed"])

    def test_post_auth_login_unauthenticated_endpoint(self):
        demo_auth.seed_demo_users(self.ddb)
        # Enable password mode via the admin-authenticated route first.
        self._as(ADMIN_SUB, ADMIN["email"])
        self.client.post("/api/admin/auth-mode", json={"auth_mode": "password"})

        # The login route itself takes no Cognito bearer token.
        backend_main.app.dependency_overrides.pop(backend_main.get_current_user, None)
        # A successful login now mints a demo session token (needs the secret).
        os.environ["DEMO_TOKEN_SECRET"] = "test-demo-secret"
        response = self.client.post("/api/auth/login", json={"username": "user", "password": "user"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["username"], "user")
        # The response carries a verifiable session token whose sub is the
        # user's local:<username> row key (what get_current_user returns).
        self.assertTrue(body.get("token"))
        claims = demo_auth.verify_demo_token(body["token"])
        self.assertEqual(claims["sub"], demo_auth.local_user_sub("user"))
        self.assertFalse(claims["is_admin"])

    def test_post_auth_login_rejected_when_sso_only(self):
        demo_auth.seed_demo_users(self.ddb)
        # Default mode is sso -- no admin-mode change needed.
        backend_main.app.dependency_overrides.pop(backend_main.get_current_user, None)
        response = self.client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        self.assertEqual(response.status_code, 403, response.text)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestAuthModeLogic))
    suite.addTests(loader.loadTestsFromTestCase(TestSeededDemoUsers))
    suite.addTests(loader.loadTestsFromTestCase(TestUserCrudBothTypes))
    suite.addTests(loader.loadTestsFromTestCase(TestDeBrand))
    suite.addTests(loader.loadTestsFromTestCase(TestHttpSurface))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
