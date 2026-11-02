#!/usr/bin/env python3
"""
Password-mode sessions can be revoked (QA 2026-10-31, report #3 and the
backend finding on non-revocable tokens).

A demo session token used to be valid until its `exp` whatever happened to
the account behind it: changing the password, signing out, or deleting the
user and creating it again under the same username all left an issued token
working. Each token now carries a `tv` (token-version) claim copied from the
user row, and the per-request active-user check rejects a token whose `tv`
differs from the row's current value. The row is already read on every
request, so this adds no read.

These tests drive the real HTTP surface with the real `get_current_user`
(no dependency override), AUTH_MODE=password, a strong DEMO_TOKEN_SECRET and
moto-backed tables. They pin:

  1. A token whose `tv` is stale returns 401; one with no `tv` returns 401.
  2. Changing your own password revokes earlier tokens and hands the caller
     a fresh cookie that works.
  3. Signing out revokes the token that signed out (and stays a safe 200
     with no session, or with a token that does not verify).
  4. Deleting a user and creating it again does not honour the old tokens.
  5. The review router's own copy of the dependency enforces the same check.
  6. A pre-existing row with no `token_version` keeps working (treated as 0).
  7. `add_user` rejects a non-boolean `is_admin` with 400 instead of
     coercing it, the same as `users.update_user`.

Run: python3 tests/test_demo_session_revocation.py
"""

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-revocation-test")
os.environ.setdefault(
    "RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-revocation-test"
)
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-revocation-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-revocation-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-revocation-test")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-revocation-test")
os.environ.setdefault("AUTH_SETTINGS_TABLE", "contract-toaster-auth-settings-revocation-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-revocation-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-revocation-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-revocation-test"
)

import boto3  # noqa: E402, I001
import jwt  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.demo_auth as demo_auth  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.review_routes as review_routes  # noqa: E402

SECRET = "9a1e5c3f7b2d84e06c9f1a3b5d7e2c4f8a0b6d1e3f5a7c9b2d4e6f8a0c1b3d5e"
COOKIE = demo_auth.DEMO_SESSION_COOKIE_NAME
ADMIN_SUB = "local:boss"
ALICE_PASSWORD = "alice-original-pw"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ, {"AUTH_MODE": "password", "DEMO_TOKEN_SECRET": SECRET}
        )
        self._env.start()
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name, key in (("USERS_TABLE", "cognito_sub"), ("AUTH_SETTINGS_TABLE", "setting_id")):
            self.ddb.create_table(
                TableName=os.environ[name],
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
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
        self.users = self.ddb.Table(os.environ["USERS_TABLE"])
        # A rotated (non-seed) admin, created the way bootstrap/admins do.
        self.admin_row = {
            "cognito_sub": ADMIN_SUB, "username": "boss", "user_type": "password",
            "password_hash": demo_auth._hash_password("boss-strong-pw"),
            "role": "admin", "is_admin": True, "status": "active",
        }
        self.users.put_item(Item=self.admin_row)
        demo_auth.set_auth_mode(demo_auth.AUTH_MODE_PASSWORD, self.admin_row, self.ddb)
        demo_auth.add_user(
            {"user_type": "password", "username": "alice", "password": ALICE_PASSWORD},
            self.admin_row, self.ddb,
        )
        self.client = TestClient(backend_main.app, raise_server_exceptions=False)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        backend_main.app.dependency_overrides[review_routes.get_dynamodb_resource] = (
            lambda: self.ddb
        )

    def tearDown(self) -> None:
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()
        self._env.stop()

    def _login(self, username: str, password: str) -> str:
        response = self.client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.cookies[COOKIE]

    def _me(self, token: str) -> int:
        return self.client.get("/api/me", cookies={COOKIE: token}).status_code


class TestTokenVersionCheck(_Base):
    def test_fresh_login_is_accepted(self) -> None:
        self.assertEqual(self._me(self._login("alice", ALICE_PASSWORD)), 200)

    def test_stale_token_version_is_401(self) -> None:
        token = self._login("alice", ALICE_PASSWORD)
        claims = demo_auth.verify_demo_token(token)
        self.assertIn("tv", claims)
        self.users.update_item(
            Key={"cognito_sub": "local:alice"},
            UpdateExpression="SET token_version = :v",
            ExpressionAttributeValues={":v": int(claims["tv"]) + 1},
        )
        self.assertEqual(self._me(token), 401)

    def test_token_without_a_version_claim_is_401(self) -> None:
        now = int(time.time())
        token = jwt.encode(
            {"sub": "local:alice", "iss": demo_auth.DEMO_TOKEN_ISSUER,
             "iat": now, "exp": now + 600},
            SECRET, algorithm="HS256",
        )
        self.assertEqual(self._me(token), 401)

    def test_row_without_a_stored_version_reads_as_zero(self) -> None:
        self.users.put_item(Item={
            "cognito_sub": "local:legacy", "username": "legacy", "user_type": "password",
            "password_hash": demo_auth._hash_password("legacy-strong-pw"),
            "role": "user", "is_admin": False, "status": "active",
        })
        token = self._login("legacy", "legacy-strong-pw")
        self.assertEqual(demo_auth.verify_demo_token(token)["tv"], 0)
        self.assertEqual(self._me(token), 200)

    def test_review_router_copy_enforces_the_same_check(self) -> None:
        token = self._login("alice", ALICE_PASSWORD)
        # Any status other than 401 proves the token got past auth.
        self.assertNotEqual(
            self.client.get("/api/reviews", cookies={COOKIE: token}).status_code, 401
        )
        self.client.post("/api/auth/logout", cookies={COOKIE: token})
        self.assertEqual(
            self.client.get("/api/reviews", cookies={COOKIE: token}).status_code, 401
        )


class TestRevocationEvents(_Base):
    def test_password_change_revokes_old_tokens_and_reissues(self) -> None:
        old = self._login("alice", ALICE_PASSWORD)
        other_device = self._login("alice", ALICE_PASSWORD)
        response = self.client.post(
            "/api/me/password",
            json={"current_password": ALICE_PASSWORD, "new_password": "alice-new-password"},
            cookies={COOKIE: old},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"changed": True})
        self.assertEqual(self._me(old), 401)
        self.assertEqual(self._me(other_device), 401)
        fresh = response.cookies[COOKIE]
        self.assertEqual(self._me(fresh), 200)

    def test_logout_revokes_the_token_that_signed_out(self) -> None:
        token = self._login("alice", ALICE_PASSWORD)
        response = self.client.post("/api/auth/logout", cookies={COOKIE: token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._me(token), 401)
        self.assertEqual(self._me(self._login("alice", ALICE_PASSWORD)), 200)

    def test_logout_is_still_a_safe_200_without_a_valid_session(self) -> None:
        for cookies in ({}, {COOKIE: "not-a-jwt"}):
            with self.subTest(cookies=cookies):
                response = self.client.post("/api/auth/logout", cookies=cookies)
                self.assertEqual(response.status_code, 200, response.text)

    def test_logout_with_a_stale_token_does_not_revoke_the_current_one(self) -> None:
        stale = self._login("alice", ALICE_PASSWORD)
        self.client.post("/api/auth/logout", cookies={COOKIE: stale})
        current = self._login("alice", ALICE_PASSWORD)
        self.client.post("/api/auth/logout", cookies={COOKIE: stale})
        self.assertEqual(self._me(current), 200)

    def test_delete_then_recreate_does_not_honour_old_tokens(self) -> None:
        old = self._login("alice", ALICE_PASSWORD)
        demo_auth.remove_user("local:alice", self.admin_row, self.ddb)
        demo_auth.add_user(
            {"user_type": "password", "username": "alice", "password": ALICE_PASSWORD},
            self.admin_row, self.ddb,
        )
        self.assertEqual(self._me(old), 401)
        self.assertEqual(self._me(self._login("alice", ALICE_PASSWORD)), 200)

    def test_recreated_rows_start_at_an_unpredictable_version(self) -> None:
        versions = set()
        for _ in range(3):
            demo_auth.add_user(
                {"user_type": "password", "username": "bob", "password": "bob-strong-pw"},
                self.admin_row, self.ddb,
            )
            row = self.users.get_item(Key={"cognito_sub": "local:bob"})["Item"]
            versions.add(int(row["token_version"]))
            demo_auth.remove_user("local:bob", self.admin_row, self.ddb)
        self.assertEqual(len(versions), 3)
        self.assertNotIn(0, versions)


class TestAddUserIsAdminType(_Base):
    def test_non_boolean_is_admin_is_400(self) -> None:
        for value in ("false", "true", 1, 0, None, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as caught:
                    demo_auth.add_user(
                        {"user_type": "password", "username": f"u{id(value)}",
                         "password": "strong-enough", "is_admin": value},
                        self.admin_row, self.ddb,
                    )
                self.assertEqual(caught.exception.status_code, 400)
                self.assertNotIn(
                    "Item",
                    self.users.get_item(Key={"cognito_sub": f"local:u{id(value)}"}),
                )

    def test_boolean_and_absent_is_admin_still_work(self) -> None:
        made = demo_auth.add_user(
            {"user_type": "sso", "email": "e@example.com", "is_admin": True},
            self.admin_row, self.ddb,
        )
        self.assertTrue(made["is_admin"])
        made = demo_auth.add_user(
            {"user_type": "password", "username": "carol", "password": "carol-strong-pw"},
            self.admin_row, self.ddb,
        )
        self.assertFalse(made["is_admin"])


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (TestTokenVersionCheck, TestRevocationEvents, TestAddUserIsAdminType):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
