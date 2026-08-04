#!/usr/bin/env python3
"""
Tests for issue #469: POST /api/auth/login has zero brute-force protection,
and the seeded admin/admin + user/user credentials ship with no way to
rotate them.

This test MUST FAIL on the pre-fix tree (no throttle state, no
POST /api/me/password, no default-credentials warning) and PASS after the
fix. Covers, at both the function and route level:

  1. Throttle: below the soft-fail threshold no delay applies; from the 5th
     failure an exponential delay kicks in; from the 10th a flat ~15-minute
     lockout applies (HTTP 429, `Retry-After` header); a CORRECT password
     submitted mid-lockout is still refused until the lockout passes;
     independent usernames never share a bucket.
  2. Change password: POST /api/me/password rotates the caller's OWN
     password -- wrong current_password is 401, a too-short new_password is
     400, an SSO row is 400 (nothing to change), and after a successful
     change the OLD password stops working on the next login while the NEW
     one succeeds. One `password_changed` audit row is appended, carrying
     no plaintext.
  3. Default-credentials warning: a seeded row whose password_hash still
     verifies against the shipped default carries
     `default_credentials_warning: true` on login, on GET /api/me, and in
     the admin GET /api/users listing; it clears the instant the password
     is rotated.
  4. Constant-time compare: `_verify_password` still compares via
     `hmac.compare_digest` (locks in the property the ticket's AC #4 named,
     rather than trusting it silently stays true).

WHY MOTO AND NOT THE UNIT FAKES: same reason as tests/test_users_projection_453.py
-- the in-memory FakeTable used elsewhere never seeds password_hash, so a
throttle/change-password test against it would reproduce nothing.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import inspect
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-loginhardening-test")
os.environ.setdefault(
    "RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-loginhardening-test"
)
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-loginhardening-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-loginhardening-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-loginhardening-test")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-loginhardening-test")
os.environ.setdefault("AUTH_SETTINGS_TABLE", "contract-toaster-auth-settings-loginhardening-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-loginhardening-test")
os.environ.setdefault("DEMO_TOKEN_SECRET", "unit-test-demo-secret")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.demo_auth as demo_auth  # noqa: E402
import src.main as backend_main  # noqa: E402

ADMIN_SUB = "admin-1"
ADMIN = {"cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "is_admin": True}


class LoginHardeningTestBase(unittest.TestCase):
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

    def tearDown(self):
        self._mock_aws.stop()


# ---------------------------------------------------------------------------
# (1) Throttle -- pure delay-schedule function, then the full login path.
# ---------------------------------------------------------------------------

class TestThrottleSchedule(unittest.TestCase):
    def test_no_delay_below_soft_threshold(self):
        for fail_count in range(0, demo_auth._THROTTLE_SOFT_FAIL_THRESHOLD):
            self.assertEqual(demo_auth._throttle_delay_seconds(fail_count), 0)

    def test_exponential_delay_from_soft_threshold(self):
        soft = demo_auth._THROTTLE_SOFT_FAIL_THRESHOLD
        delays = [demo_auth._throttle_delay_seconds(soft + i) for i in range(4)]
        for earlier, later in zip(delays, delays[1:]):
            self.assertGreater(later, earlier, "delay must grow with each additional failure")
        self.assertTrue(all(d > 0 for d in delays))

    def test_hard_lockout_from_hard_threshold(self):
        hard = demo_auth._THROTTLE_HARD_LOCKOUT_THRESHOLD
        self.assertEqual(
            demo_auth._throttle_delay_seconds(hard), demo_auth._THROTTLE_HARD_LOCKOUT_SECONDS
        )
        self.assertEqual(
            demo_auth._throttle_delay_seconds(hard + 5), demo_auth._THROTTLE_HARD_LOCKOUT_SECONDS
        )


class TestLoginThrottle(LoginHardeningTestBase):
    def test_ten_rapid_failures_locks_out_with_backoff(self):
        # Drive ten real wrong-password attempts through the real login path,
        # back to back with no wait between them. Once the soft threshold's
        # exponential delay engages, EVERY subsequent rapid attempt is
        # thrown back with 429 before it ever reaches the row lookup — so a
        # rapid-fire attacker cannot actually rack up ten RECORDED failures
        # (that only happens for an attacker who waits out each delay; see
        # test_hard_lockout_reachable_by_an_attacker_who_waits_out_each_delay
        # below). What matters here is that both regimes are observed: some
        # early attempts still read as plain bad-credentials (401), and rapid
        # retries past the soft threshold are throttled (429).
        seen_statuses = []
        for _ in range(demo_auth._THROTTLE_HARD_LOCKOUT_THRESHOLD):
            try:
                demo_auth.login_with_password("admin", "not-the-password", self.ddb, client_ip="1.2.3.4")
                seen_statuses.append(200)
            except Exception as exc:  # noqa: BLE001
                seen_statuses.append(getattr(exc, "status_code", None))

        self.assertIn(401, seen_statuses, "early failures must still read as bad credentials")
        self.assertIn(429, seen_statuses, "later failures must be throttled")

        state = demo_auth._get_login_attempt_state(self.ddb, "admin", "1.2.3.4")
        self.assertGreaterEqual(state["fail_count"], demo_auth._THROTTLE_SOFT_FAIL_THRESHOLD)
        now = int(__import__("time").time())
        self.assertGreater(state["locked_until"], now, "must be throttled")

    def test_hard_lockout_reachable_by_an_attacker_who_waits_out_each_delay(self):
        # Simulate an attacker who DOES wait out each exponential delay
        # (the only way to actually accumulate ten recorded failures) by
        # calling the recording primitive directly instead of sleeping in
        # the test. Once fail_count reaches the hard threshold, the flat
        # ~15-minute lockout applies and even the correct password is
        # refused.
        for _ in range(demo_auth._THROTTLE_HARD_LOCKOUT_THRESHOLD):
            demo_auth._record_login_failure(self.ddb, "admin", "8.8.8.8")

        state = demo_auth._get_login_attempt_state(self.ddb, "admin", "8.8.8.8")
        self.assertEqual(state["fail_count"], demo_auth._THROTTLE_HARD_LOCKOUT_THRESHOLD)
        now = int(__import__("time").time())
        self.assertGreaterEqual(
            state["locked_until"] - now, demo_auth._THROTTLE_HARD_LOCKOUT_SECONDS - 2
        )

        with self.assertRaises(Exception) as ctx:
            demo_auth.login_with_password("admin", "admin", self.ddb, client_ip="8.8.8.8")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 429)

    def test_correct_password_during_lockout_is_still_refused(self):
        for _ in range(demo_auth._THROTTLE_HARD_LOCKOUT_THRESHOLD):
            try:
                demo_auth.login_with_password("admin", "wrong", self.ddb, client_ip="9.9.9.9")
            except Exception:  # noqa: BLE001
                pass

        with self.assertRaises(Exception) as ctx:
            demo_auth.login_with_password("admin", "admin", self.ddb, client_ip="9.9.9.9")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 429)
        self.assertIn("Retry-After", getattr(ctx.exception, "headers", {}) or {})

    def test_lockout_clears_once_locked_until_passes(self):
        for _ in range(demo_auth._THROTTLE_HARD_LOCKOUT_THRESHOLD):
            try:
                demo_auth.login_with_password("admin", "wrong", self.ddb, client_ip="5.5.5.5")
            except Exception:  # noqa: BLE001
                pass

        with self.assertRaises(Exception) as ctx:
            demo_auth.login_with_password("admin", "admin", self.ddb, client_ip="5.5.5.5")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 429)

        future = int(__import__("time").time()) + demo_auth._THROTTLE_HARD_LOCKOUT_SECONDS + 1
        with patch.object(demo_auth.time, "time", return_value=future):
            result = demo_auth.login_with_password("admin", "admin", self.ddb, client_ip="5.5.5.5")
        self.assertEqual(result["username"], "admin")

    def test_independent_usernames_never_share_a_bucket(self):
        for _ in range(demo_auth._THROTTLE_HARD_LOCKOUT_THRESHOLD):
            try:
                demo_auth.login_with_password("admin", "wrong", self.ddb, client_ip="7.7.7.7")
            except Exception:  # noqa: BLE001
                pass

        # "admin" is now locked out at this IP; "user" at the SAME IP must
        # be completely unaffected.
        result = demo_auth.login_with_password("user", "user", self.ddb, client_ip="7.7.7.7")
        self.assertEqual(result["username"], "user")

    def test_successful_login_clears_prior_failures(self):
        for _ in range(3):
            try:
                demo_auth.login_with_password("admin", "wrong", self.ddb, client_ip="3.3.3.3")
            except Exception:  # noqa: BLE001
                pass
        demo_auth.login_with_password("admin", "admin", self.ddb, client_ip="3.3.3.3")

        state = demo_auth._get_login_attempt_state(self.ddb, "admin", "3.3.3.3")
        self.assertEqual(state["fail_count"], 0)
        self.assertEqual(state["locked_until"], 0)

    def test_not_active_status_does_not_count_as_a_throttle_failure(self):
        self.users_table.update_item(
            Key={"cognito_sub": demo_auth.local_user_sub("user")},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "suspended"},
        )
        for _ in range(3):
            with self.assertRaises(Exception) as ctx:
                demo_auth.login_with_password("user", "user", self.ddb, client_ip="4.4.4.4")
            self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

        state = demo_auth._get_login_attempt_state(self.ddb, "user", "4.4.4.4")
        self.assertEqual(state["fail_count"], 0)


# ---------------------------------------------------------------------------
# (2) Change password.
# ---------------------------------------------------------------------------

class TestChangeOwnPassword(LoginHardeningTestBase):
    def _admin_row(self) -> dict:
        return self.users_table.get_item(
            Key={"cognito_sub": demo_auth.local_user_sub("admin")}
        )["Item"]

    def test_wrong_current_password_rejected(self):
        with self.assertRaises(Exception) as ctx:
            demo_auth.change_own_password("not-the-password", "brand-new-pw", self._admin_row(), self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 401)

    def test_new_password_too_short_rejected(self):
        with self.assertRaises(Exception) as ctx:
            demo_auth.change_own_password("admin", "short", self._admin_row(), self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    def test_sso_row_cannot_change_password(self):
        sso_row = {"cognito_sub": "sso-1", "user_type": "sso", "email": "a@example.com"}
        with self.assertRaises(Exception) as ctx:
            demo_auth.change_own_password("whatever", "brand-new-pw", sso_row, self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    def test_successful_change_rotates_the_hash_old_password_stops_working(self):
        result = demo_auth.change_own_password("admin", "brand-new-pw", self._admin_row(), self.ddb)
        self.assertTrue(result["changed"])

        with self.assertRaises(Exception) as ctx:
            demo_auth.login_with_password("admin", "admin", self.ddb, client_ip="6.6.6.6")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 401)

        logged_in = demo_auth.login_with_password("admin", "brand-new-pw", self.ddb, client_ip="6.6.6.6")
        self.assertEqual(logged_in["username"], "admin")

    def test_change_writes_one_audit_row_with_no_plaintext(self):
        audit_table = self.ddb.Table(os.environ["AUDIT_TABLE"])
        before = audit_table.scan().get("Items", [])

        demo_auth.change_own_password("admin", "brand-new-pw", self._admin_row(), self.ddb)

        after = audit_table.scan().get("Items", [])
        new_rows = [r for r in after if r not in before]
        password_changed_rows = [r for r in new_rows if r.get("action") == "password_changed"]
        self.assertEqual(len(password_changed_rows), 1)
        row = password_changed_rows[0]
        self.assertNotIn("brand-new-pw", str(row))
        self.assertNotIn("password_hash", row)
        self.assertEqual(row["target"], demo_auth.local_user_sub("admin"))


# ---------------------------------------------------------------------------
# (3) Default-credentials warning.
# ---------------------------------------------------------------------------

class TestDefaultCredentialsWarning(LoginHardeningTestBase):
    def test_login_response_warns_for_unrotated_seed_default(self):
        result = demo_auth.login_with_password("admin", "admin", self.ddb, client_ip="2.2.2.2")
        self.assertTrue(result["default_credentials_warning"])

    def test_warning_clears_after_password_change(self):
        admin_row = self.users_table.get_item(
            Key={"cognito_sub": demo_auth.local_user_sub("admin")}
        )["Item"]
        demo_auth.change_own_password("admin", "brand-new-pw", admin_row, self.ddb)

        result = demo_auth.login_with_password("admin", "brand-new-pw", self.ddb, client_ip="2.2.2.2")
        self.assertFalse(result["default_credentials_warning"])

    def test_admin_added_user_never_warns(self):
        created = demo_auth.add_user(
            {"user_type": "password", "username": "pilot", "password": "hunter2-not-default"},
            ADMIN,
            self.ddb,
        )
        row = self.users_table.get_item(Key={"cognito_sub": created["cognito_sub"]}).get("Item")
        self.assertFalse(demo_auth.default_credentials_warning(row))

    def test_sso_row_never_warns(self):
        sso_row = {"cognito_sub": "sso-1", "user_type": "sso", "email": "a@example.com"}
        self.assertFalse(demo_auth.default_credentials_warning(sso_row))

    def test_admin_users_listing_flags_unrotated_seed_rows(self):
        import src.users as users_module

        listed = users_module.list_users(ADMIN, self.ddb)
        seeded_admin = next(u for u in listed if u.get("username") == "admin")
        seeded_user = next(u for u in listed if u.get("username") == "user")
        self.assertTrue(seeded_admin["default_credentials_warning"])
        self.assertTrue(seeded_user["default_credentials_warning"])

        # Field name must not collide with the credential-leak canary in
        # tests/test_users_projection_453.py (SECRET_FIELD_PATTERN).
        import re
        secret_pattern = re.compile(r"password|hash|secret|token", re.IGNORECASE)
        offenders = [k for k in seeded_admin if secret_pattern.search(k) and k != "user_type"]
        self.assertEqual(offenders, [])


# ---------------------------------------------------------------------------
# (4) Constant-time compare -- locks in AC #4.
# ---------------------------------------------------------------------------

class TestConstantTimeCompare(unittest.TestCase):
    def test_verify_password_uses_hmac_compare_digest(self):
        source = inspect.getsource(demo_auth._verify_password)
        self.assertIn("hmac.compare_digest", source)

    def test_correct_and_incorrect_passwords_both_verify_correctly(self):
        stored = demo_auth._hash_password("correct-horse-battery-staple")
        self.assertTrue(demo_auth._verify_password("correct-horse-battery-staple", stored))
        self.assertFalse(demo_auth._verify_password("wrong", stored))


# ---------------------------------------------------------------------------
# Route level (issue #469 AC: "Tests for all of the above at the route level").
# ---------------------------------------------------------------------------

class RoutedTestBase(LoginHardeningTestBase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        super().tearDown()

    def _sign_in_as(self, cognito_sub: str) -> None:
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": cognito_sub, "email": "", "token_use": "access"}
        )


class TestLoginRouteThrottle(RoutedTestBase):
    def test_repeated_wrong_password_eventually_429s_with_retry_after(self):
        statuses = []
        for _ in range(demo_auth._THROTTLE_HARD_LOCKOUT_THRESHOLD):
            response = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "not-the-password"},
                headers={"X-Forwarded-For": "10.0.0.1"},
            )
            statuses.append(response.status_code)
        self.assertIn(401, statuses)
        self.assertIn(429, statuses)
        last = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
        self.assertEqual(last.status_code, 429)
        self.assertIn("Retry-After", last.headers)

    def test_trusted_proxy_different_forwarded_ip_is_not_throttled(self):
        """`X-Forwarded-For` is only ever consulted when
        `TRUST_PROXY_HEADERS=1` -- the explicit opt-in reserved for the
        nginx-fronted topology where nginx itself sets that header from its
        own view of the peer (deploy/dts/nginx.conf), never forwarding a
        client-supplied value. With that flag on, two attempts carrying
        genuinely different forwarded IPs are correctly treated as two
        independent buckets."""
        with patch.dict(os.environ, {"TRUST_PROXY_HEADERS": "1"}):
            for _ in range(demo_auth._THROTTLE_HARD_LOCKOUT_THRESHOLD):
                self.client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "not-the-password"},
                    headers={"X-Forwarded-For": "10.0.0.2"},
                )
            response = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "admin"},
                headers={"X-Forwarded-For": "10.0.0.3"},
            )
        self.assertEqual(response.status_code, 200, response.text)

    def test_untrusted_caller_rotating_forwarded_for_is_still_throttled(self):
        """Attack/regression test for issue #469 finding 1: without
        `TRUST_PROXY_HEADERS` set (the default -- this process is not known
        to sit behind the trusted nginx hop), an untrusted direct caller
        cannot pick their own throttle bucket by rotating
        `X-Forwarded-For` per request. Every attempt below comes from the
        SAME direct TestClient connection but claims a different forwarded
        IP; `client_ip_from_request` must ignore that header entirely and
        key on the real peer, so this must still throttle exactly as if no
        `X-Forwarded-For` header were sent at all."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRUST_PROXY_HEADERS", None)
            self.assertFalse(demo_auth._trust_proxy_headers())
            statuses = []
            for i in range(demo_auth._THROTTLE_HARD_LOCKOUT_THRESHOLD):
                response = self.client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "not-the-password"},
                    headers={"X-Forwarded-For": f"10.0.0.{i}"},
                )
                statuses.append(response.status_code)
            self.assertIn(429, statuses)
            # The correct password, still claiming yet another fresh
            # forwarded IP, must still be refused -- the bypass this
            # finding named.
            blocked = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "admin"},
                headers={"X-Forwarded-For": "10.0.0.99"},
            )
        self.assertEqual(blocked.status_code, 429, blocked.text)

    def test_successful_login_response_carries_default_credentials_warning(self):
        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
            headers={"X-Forwarded-For": "10.0.0.9"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["default_credentials_warning"])


class TestMePasswordRoute(RoutedTestBase):
    def test_wrong_current_password_401(self):
        self._sign_in_as(demo_auth.local_user_sub("admin"))
        response = self.client.post(
            "/api/me/password",
            json={"current_password": "not-the-password", "new_password": "brand-new-pw"},
        )
        self.assertEqual(response.status_code, 401)

    def test_too_short_new_password_400(self):
        self._sign_in_as(demo_auth.local_user_sub("admin"))
        response = self.client.post(
            "/api/me/password",
            json={"current_password": "admin", "new_password": "short"},
        )
        self.assertEqual(response.status_code, 400)

    def test_successful_change_then_get_me_no_longer_warns(self):
        self._sign_in_as(demo_auth.local_user_sub("admin"))

        before = self.client.get("/api/me")
        self.assertTrue(before.json()["default_credentials_warning"])

        changed = self.client.post(
            "/api/me/password",
            json={"current_password": "admin", "new_password": "brand-new-pw"},
        )
        self.assertEqual(changed.status_code, 200, changed.text)

        after = self.client.get("/api/me")
        self.assertFalse(after.json()["default_credentials_warning"])

        # And the old password no longer works at the login route.
        stale_login = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
            headers={"X-Forwarded-For": "10.0.0.5"},
        )
        self.assertEqual(stale_login.status_code, 401)

    def test_sso_caller_cannot_change_password(self):
        self.users_table.put_item(Item={
            "cognito_sub": "sso-caller",
            "email": "sso-caller@example.com",
            "user_type": "sso",
            "status": "active",
            "is_admin": False,
        })
        self._sign_in_as("sso-caller")
        response = self.client.post(
            "/api/me/password",
            json={"current_password": "whatever", "new_password": "brand-new-pw"},
        )
        self.assertEqual(response.status_code, 400)


class TestUsersRouteDefaultCredentialsWarning(RoutedTestBase):
    def test_get_users_route_flags_seeded_rows(self):
        self._sign_in_as(ADMIN_SUB)
        response = self.client.get("/api/users")
        self.assertEqual(response.status_code, 200, response.text)
        listed = response.json()["users"]
        seeded_admin = next(u for u in listed if u.get("username") == "admin")
        self.assertTrue(seeded_admin["default_credentials_warning"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestThrottleSchedule,
        TestLoginThrottle,
        TestChangeOwnPassword,
        TestDefaultCredentialsWarning,
        TestConstantTimeCompare,
        TestLoginRouteThrottle,
        TestMePasswordRoute,
        TestUsersRouteDefaultCredentialsWarning,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
