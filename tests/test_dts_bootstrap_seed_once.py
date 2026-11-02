#!/usr/bin/env python3
"""
The Docker Compose bootstrap seeds demo users once, and leaves the admin's
auth-mode choice alone (QA 2026-10-31, report #4).

`deploy/dts/bootstrap.py` runs on every rollout. It used to call
`demo_auth.seed_demo_users` and overwrite the auth-mode row each time, so a
deleted `admin`/`admin` came back on the next deploy and an admin's choice of
sign-in method was reset.

These tests drive the real bootstrap entry point against moto and pin:

  1. A fresh deployment gets both demo users, the password-enabled auth-mode
     row, and a `bootstrap:seeded` marker row.
  2. A second run after the admin deleted both demo users does not bring
     them back.
  3. A deployment that already has users but predates the marker (an
     existing box) is marked seeded without any demo user being added.
  4. An existing auth-mode row is never overwritten.

Run: python3 tests/test_dts_bootstrap_seed_once.py
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import boto3  # noqa: E402, I001
from moto import mock_aws  # noqa: E402

import src.demo_auth as demo_auth  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "dts_bootstrap_seed_once", REPO_ROOT / "deploy" / "dts" / "bootstrap.py"
)
dts_bootstrap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dts_bootstrap)

ENV = {
    "AWS_DEFAULT_REGION": "us-east-1",
    "USERS_TABLE": "contract-toaster-users-seed-once-test",
    "AUTH_SETTINGS_TABLE": "contract-toaster-auth-settings-seed-once-test",
    "AUDIT_TABLE": "contract-toaster-audit-seed-once-test",
    "AUTH_MODE": "password",
}


class TestSeedOnce(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(os.environ, ENV)
        self._env.start()
        self._mock = mock_aws()
        self._mock.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name, key in (("USERS_TABLE", "cognito_sub"), ("AUTH_SETTINGS_TABLE", "setting_id")):
            self.ddb.create_table(
                TableName=os.environ[name],
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        self.users = self.ddb.Table(ENV["USERS_TABLE"])
        self.settings = self.ddb.Table(ENV["AUTH_SETTINGS_TABLE"])

    def tearDown(self) -> None:
        self._mock.stop()
        self._env.stop()

    def _run_bootstrap(self) -> None:
        with patch.object(dts_bootstrap, "_ddb_resource", return_value=self.ddb):
            dts_bootstrap.seed_users_and_playbook()

    def _user(self, username: str):
        return self.users.get_item(Key={"cognito_sub": demo_auth.local_user_sub(username)}).get("Item")

    def _setting(self, setting_id: str):
        return self.settings.get_item(Key={"setting_id": setting_id}).get("Item")

    def test_fresh_deployment_is_seeded_and_marked(self) -> None:
        self._run_bootstrap()
        self.assertIsNotNone(self._user("admin"))
        self.assertIsNotNone(self._user("user"))
        self.assertEqual(self._setting(demo_auth.AUTH_MODE_SETTING_ID)["auth_mode"], "password")
        self.assertIsNotNone(self._setting(demo_auth.BOOTSTRAP_SEEDED_SETTING_ID))

    def test_deleted_demo_users_stay_deleted_on_the_next_rollout(self) -> None:
        self._run_bootstrap()
        self.users.delete_item(Key={"cognito_sub": demo_auth.local_user_sub("admin")})
        self.users.delete_item(Key={"cognito_sub": demo_auth.local_user_sub("user")})
        self._run_bootstrap()
        self.assertIsNone(self._user("admin"))
        self.assertIsNone(self._user("user"))

    def test_existing_box_without_a_marker_is_not_reseeded(self) -> None:
        self.users.put_item(Item={
            "cognito_sub": "local:boss", "username": "boss", "user_type": "password",
            "role": "admin", "is_admin": True, "status": "active",
        })
        self._run_bootstrap()
        self.assertIsNone(self._user("admin"))
        self.assertIsNone(self._user("user"))
        self.assertIsNotNone(self._setting(demo_auth.BOOTSTRAP_SEEDED_SETTING_ID))

    def test_existing_auth_mode_is_never_overwritten(self) -> None:
        self.settings.put_item(Item={
            "setting_id": demo_auth.AUTH_MODE_SETTING_ID, "auth_mode": "sso",
        })
        self._run_bootstrap()
        self.assertEqual(self._setting(demo_auth.AUTH_MODE_SETTING_ID)["auth_mode"], "sso")
        self._run_bootstrap()
        self.assertEqual(self._setting(demo_auth.AUTH_MODE_SETTING_ID)["auth_mode"], "sso")


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestSeedOnce))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
