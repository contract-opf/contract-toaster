#!/usr/bin/env python3
"""
Executable tests for issue #452: `GET /api/users` 500s when two or more users
have never signed in.

THE SHIPPED BUG. `backend/src/users.py::list_users` ordered its rows with

    users.sort(key=lambda u: u.get("last_auth_at", 0), reverse=True)

`dict.get(key, default)` returns the default only when the key is ABSENT. A
never-signed-in row has the key PRESENT and explicitly `None` -- that is what
`backend/src/demo_auth.py` writes, at three separate sites:

  - `seed_demo_users`      (admission="seed",        the local:admin/local:user rows)
  - `add_user`, SSO leg    (admission="admin_added")
  - `add_user`, password leg (admission="admin_added")

so the sort key is `None`, and the moment TWO rows carry it the comparison
raises `TypeError: '<' not supported between instances of 'NoneType' and
'NoneType'` and the whole Users & access tab goes down. One `None` row is not
enough -- Timsort never compares it against another `None` -- which is why
every test below seeds at least two.

This is not a fresh-boot-only condition: a freshly seeded password-mode
deployment starts with exactly two such rows, and the state recurs for the life
of the deployment every time an admin adds two users who have not yet signed
in.

WHY MOTO AND NOT THE UNIT FAKES (issue Notes; same structural lesson as #440
and #453): tests/test_user_management_92.py's in-memory `FakeTable` is seeded
with integer `last_auth_at` values, so the fake and the real client differ in
precisely the field that breaks and a test written against it reproduces
nothing. These tests therefore drive the REAL code path -- real
`demo_auth.seed_demo_users` writing real `None`s into a moto-mocked DynamoDB,
then real `list_users`, plus the real `GET /api/users` route through a FastAPI
TestClient. moto==5.2.2 is declared in requirements-dev.txt:42 (pattern:
tests/test_playbook_version_routes_430.py).

WATCH IT FAIL FIRST -- observed against the pre-fix tree:

    TestNeverSignedInRowsDoNotBreakTheSort
      test_list_users_returns_when_two_rows_have_never_signed_in   ERROR
      test_never_signed_in_rows_sort_after_every_signed_in_row     ERROR
      test_signed_in_rows_keep_their_most_recent_first_order       ERROR
      test_every_seeded_row_is_returned                            ERROR
    TestGetUsersRouteWithNeverSignedInRows
      test_route_returns_200_not_500                               ERROR
      test_route_serialises_never_signed_in_rows_as_json_null      ERROR
    Ran 10 tests -- FAILED (failures=2, errors=6)

with, on each of the six errors:

    File "backend/src/users.py", line 264, in list_users
      users.sort(key=lambda u: u.get("last_auth_at", 0), reverse=True)
    TypeError: '<' not supported between instances of 'NoneType' and 'int'

and 500 != 200 on the two route legs. The operand pair here is
`NoneType`/`int` rather than the ticket's `NoneType`/`NoneType` only because
this fixture also holds signed-in rows, so Timsort reaches a null-vs-int
comparison first; on a freshly seeded password-mode deployment (the two demo
rows and nothing else) the same line raises the `NoneType`/`NoneType` form.
Same defect, same line.

`TestSeededRowsReallyCarryNone` PASSES before the fix, deliberately: it is the
premise check. If the stored value ever stops being `None`, every assertion
above becomes vacuous and this file would go green while proving nothing --
exactly the failure mode issue #452 exists to close. It is asserted separately
rather than folded into the sort tests so it cannot mask them.

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
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-nullauth452-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-nullauth452-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-nullauth452-test")
os.environ.setdefault("AUTH_SETTINGS_TABLE", "contract-toaster-auth-settings-nullauth452-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-nullauth452-test")
os.environ.setdefault(
    "RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-nullauth452-test"
)
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-nullauth452-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-nullauth452-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.demo_auth as demo_auth  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.users as users_module  # noqa: E402

# The admin caller. `list_users` reads `is_admin` off the row it is handed, so
# this row is the caller identity for the direct-function tests; the route
# tests read the same identity back out of the moto table.
ADMIN_SUB = "admin-1"
ADMIN_ROW = {"cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "is_admin": True}
ADMIN_LAST_AUTH = 5_000

# Two extra signed-in rows, deliberately inserted in non-sorted order so a
# no-op sort cannot accidentally satisfy the ordering assertions.
SIGNED_IN_ROWS = (
    ("signed-in-oldest", 1_000),
    ("signed-in-newest", 9_000),
    ("signed-in-middle", 3_000),
)


class NullLastAuthTestBase(unittest.TestCase):
    """Real `seed_demo_users` into moto DynamoDB: two rows, `last_auth_at` None."""

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

        # REAL seeding. This is the whole point of the file: `seed_demo_users`
        # writes `last_auth_at: None` for both demo rows, which is the
        # production condition. Nothing here fabricates the null.
        demo_auth.seed_demo_users(self.ddb)

        # The caller's own admin row (the route path reads it back through
        # require_active_user). Signed in, so it must sort above the nulls.
        self.users_table.put_item(
            Item={
                "cognito_sub": ADMIN_SUB,
                "email": f"{ADMIN_SUB}@example.com",
                "is_admin": True,
                "status": "active",
                "created_at": 1_000,
                "last_auth_at": ADMIN_LAST_AUTH,
            }
        )
        for sub, last_auth_at in SIGNED_IN_ROWS:
            self.users_table.put_item(
                Item={
                    "cognito_sub": sub,
                    "email": f"{sub}@example.com",
                    "is_admin": False,
                    "status": "active",
                    "created_at": 1_000,
                    "last_auth_at": last_auth_at,
                }
            )

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    # -- helpers ------------------------------------------------------------

    def never_signed_in_subs(self) -> set[str]:
        return {demo_auth.local_user_sub(spec["username"]) for spec in demo_auth.SEED_USERS}

    def signed_in_subs(self) -> set[str]:
        return {ADMIN_SUB} | {sub for sub, _ in SIGNED_IN_ROWS}

    def raw_row(self, sub: str) -> dict:
        row = self.users_table.get_item(Key={"cognito_sub": sub}).get("Item")
        self.assertIsNotNone(row, f"no users row was written for {sub}")
        return row


# ---------------------------------------------------------------------------
# The premise. PASSES before the fix -- if the stored value stopped being None
# every other assertion in this file would be vacuous.
# ---------------------------------------------------------------------------

class TestSeededRowsReallyCarryNone(NullLastAuthTestBase):
    def test_seed_demo_users_writes_at_least_two_null_last_auth_rows(self):
        nulls = [
            sub for sub in self.never_signed_in_subs()
            if self.raw_row(sub)["last_auth_at"] is None
        ]
        self.assertGreaterEqual(
            len(nulls),
            2,
            "the reproduction needs TWO stored `last_auth_at: None` rows -- one "
            "null is never compared against another and cannot trigger the bug",
        )

    def test_the_key_is_present_not_absent(self):
        """`dict.get(k, 0)` would have saved us if the key were absent. It is not."""
        for sub in self.never_signed_in_subs():
            row = self.raw_row(sub)
            self.assertIn("last_auth_at", row, f"{sub}: key absent, not the shipped shape")
            self.assertIsNone(row["last_auth_at"], f"{sub}: expected an explicit None")


# ---------------------------------------------------------------------------
# The defect and the ordering contract.
# ---------------------------------------------------------------------------

class TestNeverSignedInRowsDoNotBreakTheSort(NullLastAuthTestBase):
    def listed(self) -> list[dict]:
        return users_module.list_users(ADMIN_ROW, self.ddb)

    def test_list_users_returns_when_two_rows_have_never_signed_in(self):
        """The bug itself: this raised TypeError before the fix."""
        result = self.listed()
        self.assertIsInstance(result, list)

    def test_every_seeded_row_is_returned(self):
        subs = [u["cognito_sub"] for u in self.listed()]
        self.assertEqual(len(subs), len(set(subs)), "duplicate rows returned")
        self.assertEqual(
            set(subs),
            self.signed_in_subs() | self.never_signed_in_subs(),
            "list_users dropped or invented rows",
        )

    def test_never_signed_in_rows_sort_after_every_signed_in_row(self):
        """Acceptance: never-signed-in rows land at the BOTTOM under reverse=True."""
        subs = [u["cognito_sub"] for u in self.listed()]
        never = self.never_signed_in_subs()
        last_signed_in = max(i for i, sub in enumerate(subs) if sub not in never)
        first_never = min(i for i, sub in enumerate(subs) if sub in never)
        self.assertLess(
            last_signed_in,
            first_never,
            f"a never-signed-in row sorted above a signed-in one: {subs}",
        )

    def test_signed_in_rows_keep_their_most_recent_first_order(self):
        """Acceptance: existing most-recent-first ordering is unchanged."""
        never = self.never_signed_in_subs()
        stamps = [
            u["last_auth_at"] for u in self.listed() if u["cognito_sub"] not in never
        ]
        self.assertEqual(stamps, sorted(stamps, reverse=True), f"not descending: {stamps}")
        self.assertEqual(stamps, [9_000, ADMIN_LAST_AUTH, 3_000, 1_000])

    def test_null_survives_the_projection_so_the_ui_can_render_never(self):
        """`public_user_view` must not coerce the null into an epoch-1970 0 --
        frontend `formatTimestamp` keys 'never' off null/undefined."""
        never = self.never_signed_in_subs()
        for row in self.listed():
            if row["cognito_sub"] in never:
                self.assertIsNone(
                    row["last_auth_at"],
                    f"{row['cognito_sub']}: null was coerced to {row['last_auth_at']!r}",
                )

    def test_a_single_never_signed_in_row_also_works(self):
        """Guard against a fix that only handles the pair. One null must sort
        last too -- and this leg passed before the fix, so it is asserted
        separately rather than standing in for the reproduction."""
        for spec in list(demo_auth.SEED_USERS)[1:]:
            self.users_table.update_item(
                Key={"cognito_sub": demo_auth.local_user_sub(spec["username"])},
                UpdateExpression="SET last_auth_at = :t",
                ExpressionAttributeValues={":t": 4_000},
            )
        subs = [u["cognito_sub"] for u in self.listed()]
        self.assertEqual(subs[-1], demo_auth.local_user_sub(demo_auth.SEED_USERS[0]["username"]))


# ---------------------------------------------------------------------------
# The route: what actually 500'd in production.
# ---------------------------------------------------------------------------

class TestGetUsersRouteWithNeverSignedInRows(NullLastAuthTestBase):
    def setUp(self):
        super().setUp()
        # raise_server_exceptions=False so an unhandled TypeError surfaces as
        # the 500 the browser actually saw, rather than blowing up the runner.
        self.client = TestClient(backend_main.app, raise_server_exceptions=False)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "token_use": "access"}
        )

    def test_route_returns_200_not_500(self):
        response = self.client.get("/api/users")
        self.assertEqual(response.status_code, 200, response.text)

    def test_route_serialises_never_signed_in_rows_as_json_null(self):
        response = self.client.get("/api/users")
        self.assertEqual(response.status_code, 200, response.text)
        listed = response.json()["users"]
        never = self.never_signed_in_subs()
        seen = [u for u in listed if u["cognito_sub"] in never]
        self.assertEqual(len(seen), len(never), "never-signed-in rows missing from the response")
        for row in seen:
            self.assertIsNone(row["last_auth_at"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestSeededRowsReallyCarryNone,
        TestNeverSignedInRowsDoNotBreakTheSort,
        TestGetUsersRouteWithNeverSignedInRows,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
