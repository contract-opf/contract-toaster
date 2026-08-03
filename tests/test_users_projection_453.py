#!/usr/bin/env python3
"""
Executable tests for issue #453: no route may return credential material.

Before this fix, `backend/src/users.py::list_users` did a bare `table.scan()`
and returned whole rows, so `GET /api/users` handed every password-mode user's
`password_hash` ("salt$hash") to any authenticated admin's browser.
`users.py::get_user` returned the raw `Item` for the same reason (unrouted
today, so a latent leak), and `users.py::update_user` returned a dict built
from the raw pre-update `Item` straight into `PATCH /api/users/{sub}`'s
response. `demo_auth.py::add_user` was already defended -- but at the caller,
with a one-line `result.pop("password_hash", None)`: the hazard was patched at
exactly one of four sites, which is why per-caller scrubbing is not the fix.
The fix is one allowlist projection (`users.public_user_view`) applied INSIDE
the row-returning functions.

WHY MOTO AND NOT THE UNIT FAKES (issue Notes; same structural lesson as #440
and #452): tests/test_user_management_92.py's in-memory `FakeTable` never
seeds `password_hash` at all, so a test written against it reproduces nothing
and stays green either way. These tests therefore drive the REAL code path --
real `demo_auth.seed_demo_users` writing real `_hash_password` output into a
moto-mocked DynamoDB, then real `list_users` / `get_user` / `add_user`, plus
the real `GET /api/users` route through a FastAPI TestClient. moto==5.2.2 is
declared in requirements-dev.txt:42 (pattern:
tests/test_playbook_version_routes_430.py).

WATCH IT FAIL FIRST: against the pre-fix tree, `test_list_users_*`,
`test_get_user_*` and `test_update_user_does_not_return_password_hash` fail
with `password_hash` present in the payload, and `test_get_users_route_*` /
`test_patch_user_route_*` fail on the raw response body. The `add_user` legs
PASS before the fix -- it already stripped the hash -- which is exactly why
each function is asserted separately rather than in one combined loop: a
combined loop would let the already-defended leg mask the leaking one.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import os
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-projection453-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-projection453-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-projection453-test")
os.environ.setdefault("AUTH_SETTINGS_TABLE", "contract-toaster-auth-settings-projection453-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-projection453-test")
os.environ.setdefault(
    "RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-projection453-test"
)
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-projection453-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-projection453-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.demo_auth as demo_auth  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.users as users_module  # noqa: E402

ADMIN_SUB = "admin-1"
ADMIN_ROW = {"cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "is_admin": True}

# The seeded demo admin (demo_auth.SEED_USERS) -- a password-mode row, i.e. the
# one that actually carries a `password_hash`.
SEEDED_USERNAME = "admin"

# Acceptance criterion: "No field name in any of those responses matches
# /password|hash|secret|token/i."
SECRET_FIELD_PATTERN = re.compile(r"password|hash|secret|token", re.IGNORECASE)

# Every field the admin Users table actually renders, from `UserRow` in
# frontend/src/AdminUsers.tsx (email/username/admission are optional there --
# a password-mode row has `username` and no `email`).
UI_REQUIRED_FIELDS = {"cognito_sub", "status", "is_admin", "created_at", "last_auth_at"}


class UsersProjectionTestBase(unittest.TestCase):
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

        # REAL seeding: demo_auth writes real _hash_password output.
        demo_auth.seed_demo_users(self.ddb)
        # `seed_demo_users` writes `last_auth_at: None` for every seeded row,
        # and `list_users` sorts on that key. When this file was written, two
        # never-signed-in rows made the sort raise `TypeError: '<' not
        # supported between instances of 'NoneType' and 'NoneType'` before it
        # could return anything -- a DIFFERENT defect, since fixed by issue
        # #452 and regression-tested in tests/test_users_null_last_auth_452.py.
        # The distinct epochs stay: this ticket is about the response
        # projection, and pinning them keeps the ordering here independent of
        # #452's sort behaviour. Only `last_auth_at` is touched -- the real
        # `password_hash` written above is untouched, which is the field under
        # test.
        for offset, spec in enumerate(demo_auth.SEED_USERS):
            self._set_last_auth_at(demo_auth.local_user_sub(spec["username"]), 3000 + offset)
        # The caller's own admin row (require_active_user reads it back).
        self.users_table.put_item(
            Item={
                "cognito_sub": ADMIN_SUB,
                "email": f"{ADMIN_SUB}@example.com",
                "is_admin": True,
                "status": "active",
                "created_at": 1000,
                "last_auth_at": 2000,
            }
        )

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    # -- helpers ------------------------------------------------------------

    def _set_last_auth_at(self, sub: str, value: int) -> None:
        """Give a row a deterministic `last_auth_at` -- see setUp: rows that
        have never signed in used to make `list_users` raise (issue #452, now
        fixed), and pinning the epochs keeps this file's ordering independent
        of that fix."""
        self.users_table.update_item(
            Key={"cognito_sub": sub},
            UpdateExpression="SET last_auth_at = :t",
            ExpressionAttributeValues={":t": value},
        )

    def _seeded_sub(self) -> str:
        return demo_auth.local_user_sub(SEEDED_USERNAME)

    def _raw_seeded_row(self) -> dict:
        row = self.users_table.get_item(Key={"cognito_sub": self._seeded_sub()}).get("Item")
        self.assertIsNotNone(row, "seed_demo_users did not write the demo admin row")
        return row

    def assert_no_credential_fields(self, payload: dict, where: str) -> None:
        self.assertNotIn("password_hash", payload, f"{where} leaked password_hash")
        offenders = [k for k in payload if SECRET_FIELD_PATTERN.search(k)]
        self.assertEqual(offenders, [], f"{where} returned credential-ish field(s): {offenders}")


# ---------------------------------------------------------------------------
# The premise: the stored row really does carry the hash. If this fails, every
# assertion below is vacuous.
# ---------------------------------------------------------------------------

class TestStoredRowCarriesTheHash(UsersProjectionTestBase):
    def test_seeded_password_row_stores_a_password_hash(self):
        row = self._raw_seeded_row()
        self.assertEqual(row["user_type"], demo_auth.USER_TYPE_PASSWORD)
        self.assertIn("password_hash", row)
        self.assertIn("$", row["password_hash"])  # salt$hash
        self.assertNotEqual(row["password_hash"], SEEDED_USERNAME)


# ---------------------------------------------------------------------------
# list_users — the actively-leaking function (GET /api/users).
# ---------------------------------------------------------------------------

class TestListUsersProjection(UsersProjectionTestBase):
    def _seeded_from_list(self) -> dict:
        result = users_module.list_users(ADMIN_ROW, self.ddb)
        seeded = [u for u in result if u.get("cognito_sub") == self._seeded_sub()]
        self.assertEqual(len(seeded), 1, "the seeded demo admin row must be listed")
        return seeded[0]

    def test_list_users_does_not_return_password_hash(self):
        self.assert_no_credential_fields(self._seeded_from_list(), "list_users")

    def test_list_users_scrubs_every_row_not_just_the_first(self):
        created = demo_auth.add_user(
            {"user_type": "password", "username": "pilot", "password": "hunter2"},
            ADMIN_ROW,
            self.ddb,
        )
        self._set_last_auth_at(created["cognito_sub"], 3500)
        for row in users_module.list_users(ADMIN_ROW, self.ddb):
            self.assert_no_credential_fields(row, f"list_users row {row.get('cognito_sub')}")

    def test_list_users_still_returns_every_field_the_ui_renders(self):
        seeded = self._seeded_from_list()
        missing = UI_REQUIRED_FIELDS - set(seeded)
        self.assertEqual(missing, set(), f"Users tab would render blank cells for: {missing}")
        self.assertEqual(seeded["username"], SEEDED_USERNAME)
        self.assertEqual(seeded["status"], "active")
        self.assertEqual(seeded["admission"], "seed")
        self.assertTrue(seeded["is_admin"])

    def test_a_future_row_field_is_not_returned_unless_allowlisted(self):
        """A field added to the users row later must not ride out by default.

        This is the denylist failure mode the ticket names: a `pop`-based
        scrub returns whatever the next migration adds.
        """
        self.users_table.update_item(
            Key={"cognito_sub": self._seeded_sub()},
            UpdateExpression="SET recovery_answer = :v, mfa_seed = :v2",
            ExpressionAttributeValues={":v": "mother's maiden name", ":v2": "JBSWY3DPEHPK3PXP"},
        )
        seeded = self._seeded_from_list()
        self.assertNotIn("recovery_answer", seeded)
        self.assertNotIn("mfa_seed", seeded)
        self.assertEqual(set(seeded) - set(users_module.PUBLIC_USER_FIELDS), set())


# ---------------------------------------------------------------------------
# get_user — the latent leak (unrouted today).
# ---------------------------------------------------------------------------

class TestGetUserProjection(UsersProjectionTestBase):
    def test_get_user_does_not_return_password_hash(self):
        result = users_module.get_user(self._seeded_sub(), ADMIN_ROW, self.ddb)
        self.assert_no_credential_fields(result, "get_user")

    def test_get_user_still_returns_the_identity_fields(self):
        result = users_module.get_user(self._seeded_sub(), ADMIN_ROW, self.ddb)
        self.assertEqual(result["cognito_sub"], self._seeded_sub())
        self.assertEqual(result["username"], SEEDED_USERNAME)
        self.assertEqual(result["status"], "active")


# ---------------------------------------------------------------------------
# add_user — already defended before this ticket (asserted separately, on
# purpose: this leg passes pre-fix and must not be allowed to mask the others).
# ---------------------------------------------------------------------------

class TestAddUserProjection(UsersProjectionTestBase):
    def test_add_password_user_does_not_return_password_hash(self):
        created = demo_auth.add_user(
            {"user_type": "password", "username": "pilot", "password": "hunter2"},
            ADMIN_ROW,
            self.ddb,
        )
        self.assert_no_credential_fields(created, "add_user(password)")
        self.assertEqual(created["username"], "pilot")
        self.assertEqual(created["user_type"], demo_auth.USER_TYPE_PASSWORD)
        # …and the hash is still STORED (this ticket changes the boundary, not
        # how passwords are held).
        stored = self.users_table.get_item(
            Key={"cognito_sub": demo_auth.local_user_sub("pilot")}
        ).get("Item")
        self.assertIn("password_hash", stored)

    def test_add_sso_user_returns_the_identity_fields(self):
        created = demo_auth.add_user(
            {"user_type": "sso", "email": "new-sso@example.com", "is_admin": True},
            ADMIN_ROW,
            self.ddb,
        )
        self.assert_no_credential_fields(created, "add_user(sso)")
        self.assertEqual(created["email"], "new-sso@example.com")
        self.assertEqual(created["user_type"], demo_auth.USER_TYPE_SSO)
        self.assertTrue(created["is_admin"])
        self.assertEqual(created["admission"], "admin_added")


# ---------------------------------------------------------------------------
# update_user — the fourth row-returning function, and a ROUTED one
# (PATCH /api/users/{sub} -> main.py's `JSONResponse(content=updated)`).
#
# Its own class, not a leg folded into a shared loop, for the same reason
# add_user has its own: each of these functions has a different pre-fix status,
# and a combined loop lets an already-defended one mask a leaking one.
#
# NOTE: these exercise `is_admin`, not `status`. `update_user` builds a bare
# `SET status = :status` UpdateExpression, and against real DynamoDB (moto
# included) `status` is a reserved keyword, so a status PATCH raises
# ValidationException before it can return anything. That is a separate,
# pre-existing defect outside this ticket's scope -- and another instance of
# the blind spot in the note above: test_user_management_92.py's in-memory
# FakeTable executes the expression itself and never sees it.
# ---------------------------------------------------------------------------

class TestUpdateUserProjection(UsersProjectionTestBase):
    def test_update_user_does_not_return_password_hash(self):
        result = users_module.update_user(
            self._seeded_sub(), {"is_admin": False}, ADMIN_ROW, self.ddb
        )
        self.assert_no_credential_fields(result, "update_user")

    def test_update_user_still_returns_the_mutated_fields(self):
        result = users_module.update_user(
            self._seeded_sub(), {"is_admin": False}, ADMIN_ROW, self.ddb
        )
        self.assertEqual(result["cognito_sub"], self._seeded_sub())
        self.assertEqual(result["username"], SEEDED_USERNAME)
        self.assertEqual(result["status"], "active")
        self.assertFalse(result["is_admin"])
        # …and the mutation really landed in the table.
        self.assertFalse(self._raw_seeded_row()["is_admin"])


# ---------------------------------------------------------------------------
# The routes themselves: what actually reaches the admin's browser.
# ---------------------------------------------------------------------------

class RoutedTestBase(UsersProjectionTestBase):
    """Shared TestClient wiring: a real admin caller against the moto tables."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "token_use": "access"}
        )


class TestGetUsersRoute(RoutedTestBase):
    def test_get_users_route_body_contains_no_hash(self):
        stored_hash = self._raw_seeded_row()["password_hash"]

        response = self.client.get("/api/users")
        self.assertEqual(response.status_code, 200, response.text)

        # The literal secret must not appear anywhere in the wire bytes.
        self.assertNotIn(stored_hash, response.text)
        self.assertNotIn("password_hash", response.text)

        listed = response.json()["users"]
        seeded = [u for u in listed if u.get("cognito_sub") == self._seeded_sub()]
        self.assertEqual(len(seeded), 1)
        self.assert_no_credential_fields(seeded[0], "GET /api/users")


class TestPatchUserRoute(RoutedTestBase):
    """PATCH /api/users/{sub} — main.py returns `update_user`'s dict verbatim."""

    def test_patch_user_route_body_contains_no_hash(self):
        stored_hash = self._raw_seeded_row()["password_hash"]

        response = self.client.patch(
            f"/api/users/{self._seeded_sub()}", json={"is_admin": False}
        )
        self.assertEqual(response.status_code, 200, response.text)

        self.assertNotIn(stored_hash, response.text)
        self.assertNotIn("password_hash", response.text)
        self.assert_no_credential_fields(response.json(), "PATCH /api/users/{sub}")


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestStoredRowCarriesTheHash,
        TestListUsersProjection,
        TestGetUserProjection,
        TestAddUserProjection,
        TestUpdateUserProjection,
        TestGetUsersRoute,
        TestPatchUserRoute,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
