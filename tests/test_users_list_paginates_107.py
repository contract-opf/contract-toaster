#!/usr/bin/env python3
"""
Gate for issue #107: `GET /api/users` returns only the first 1 MB scan page.

`backend/src/users.py::list_users` did a bare `table.scan()` and returned
`resp["Items"]` straight through, never following `LastEvaluatedKey`. This is
the exact defect class #52 fixed for `reviews` ("three of them read ONE page
with no `LastEvaluatedKey` loop"), left behind on `users`. A JIT-provisioned
SSO tenant with group lists at ~500 bytes/row reaches DynamoDB's 1 MB scan
page at roughly 2,000 users; past that point the Admin -> Users panel showed
a complete-looking (but truncated) list, so a user beyond page one could not
be found, suspended, or deprovisioned from the UI.

The same single-page `table.scan()` at `users.py:413` (pre-fix) backs the
last-active-admin guard inside `update_user` (`other_active_admins == 0 ->
409`): whether a demote/suspend/deprovision is allowed depended on which
page the OTHER admin's row happened to land on. `test_last_active_admin_guard_sees_admin_past_first_page`
below pins that second call site directly, seeding the other active admin
onto a later scan page and asserting the guard still finds them (does not
wrongly 409, and does not wrongly allow stripping the sole real admin).

WHY MOTO AND A REAL 1 MB PAGE (not a spy-forced small page): the acceptance
criterion is about DynamoDB's actual 1 MB scan limit, which moto enforces
byte-for-byte (verified empirically: ~400 bytes of filler per row, 3000
rows, forces `LastEvaluatedKey` on page one). `_CallLoggingTable` is copied
from `tests/test_reviews_no_scan.py` (that file's own docstring: "copied
from tests/test_audit_query_api_93.py" -- copying a fixture class between
test files rather than cross-importing is this repo's convention) so the
`ExclusiveStartKey`-continuation assertion is the identical shape #52 uses.

MUST FAIL on the pre-fix tree: `list_users` returns fewer than N rows (the
scan's first-page count, well under N), and no `scan` call in the log
carries `ExclusiveStartKey`.

Run standalone: `python3 tests/test_users_list_paginates_107.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-paginates107-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-paginates107-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-paginates107-test")

import boto3  # noqa: E402, I001
from moto import mock_aws  # noqa: E402

import src.users as users_module  # noqa: E402

REGION = "us-east-1"

# Row count and filler size are picked from an empirical check (moto
# actually enforces DynamoDB's 1 MB scan page): ~400 bytes of filler per row
# and 3000 rows forces `LastEvaluatedKey` on page one at ~1994 items, well
# short of N -- a single-page read comes up short by more than 1000 rows,
# not a borderline flake.
ROW_COUNT = 3000
FILLER = "x" * 400

ADMIN_SUB = "admin-1"
ADMIN_ROW = {"cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "is_admin": True}


class _CallLoggingTable:
    """A boto3 Table that records which access method was used. Copied
    as-is from `tests/test_reviews_no_scan.py` (issue #52's own fixture),
    minus the query-side page forcing that file needs and this one does
    not -- `users` has no secondary index, and the 1 MB scan cutoff here is
    real, not spy-forced."""

    def __init__(self, inner: Any, log: list[tuple[str, str, Any]]):
        self._inner = inner
        self._log = log

    def scan(self, **kwargs: Any) -> Any:
        self._log.append(("scan", self._inner.name, kwargs.get("ExclusiveStartKey")))
        return self._inner.scan(**kwargs)

    def get_item(self, **kwargs: Any) -> Any:
        return self._inner.get_item(**kwargs)

    def put_item(self, **kwargs: Any) -> Any:
        return self._inner.put_item(**kwargs)

    def update_item(self, **kwargs: Any) -> Any:
        return self._inner.update_item(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CallLoggingResource:
    def __init__(self, inner: Any):
        self._inner = inner
        self.calls: list[tuple[str, str, Any]] = []

    def Table(self, name: str) -> Any:  # noqa: N802 (matches boto3's method name)
        return _CallLoggingTable(self._inner.Table(name), self.calls)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class UsersListPaginatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.addCleanup(self._mock_aws.stop)

        raw_ddb = boto3.resource("dynamodb", region_name=REGION)
        raw_ddb.create_table(
            TableName=os.environ["USERS_TABLE"],
            KeySchema=[{"AttributeName": "cognito_sub", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cognito_sub", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # `update_user` writes an audit row on every mutation (ARCHITECTURE.md
        # -> "Audit posture") -- needed for the last-active-admin-guard test,
        # which calls update_user for real.
        raw_ddb.create_table(
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
        self.raw_table = raw_ddb.Table(os.environ["USERS_TABLE"])

        # Seed enough real rows to force a real DynamoDB/moto 1 MB scan page.
        with self.raw_table.batch_writer() as writer:
            writer.put_item(Item={**ADMIN_ROW, "status": "active", "created_at": 0, "last_auth_at": None})
            for i in range(ROW_COUNT):
                writer.put_item(
                    Item={
                        "cognito_sub": f"user-{i:05d}",
                        "email": f"user{i}@example.com",
                        "status": "active",
                        "is_admin": False,
                        "created_at": i + 1,
                        "last_auth_at": None,
                        "group_membership_filler": FILLER,
                    }
                )

        self.ddb = _CallLoggingResource(raw_ddb)

    def test_list_users_returns_every_row_past_the_first_scan_page(self) -> None:
        result = users_module.list_users(ADMIN_ROW, self.ddb)
        self.assertEqual(
            len(result),
            ROW_COUNT + 1,
            "list_users returned fewer rows than were seeded -- it stopped "
            "at the first 1 MB scan page instead of following LastEvaluatedKey",
        )
        returned_subs = {u["cognito_sub"] for u in result}
        self.assertIn(f"user-{ROW_COUNT - 1:05d}", returned_subs)

    def test_list_users_scan_follows_last_evaluated_key(self) -> None:
        users_module.list_users(ADMIN_ROW, self.ddb)
        scans = [call for call in self.ddb.calls if call[0] == "scan"]
        continuations = [call for call in scans if call[2] is not None]
        self.assertTrue(len(scans) >= 2, f"expected more than one scan page, got {scans}")
        self.assertTrue(
            continuations,
            "no scan call carried ExclusiveStartKey -- list_users read only "
            "the first page and never followed LastEvaluatedKey",
        )

    def test_last_active_admin_guard_sees_admin_past_first_page(self) -> None:
        # A second active admin, seeded so its cognito_sub sorts to the very
        # end of the table -- past the first scan page under the real 1 MB
        # cutoff exercised above. Demoting ADMIN_SUB must succeed because
        # this other admin is still active, wherever their row lands.
        other_admin_sub = f"zz-other-admin-{ROW_COUNT + 1}"
        self.raw_table.put_item(
            Item={
                "cognito_sub": other_admin_sub,
                "email": f"{other_admin_sub}@example.com",
                "is_admin": True,
                "status": "active",
                "created_at": ROW_COUNT + 2,
                "last_auth_at": None,
            }
        )

        result = users_module.update_user(
            target_sub=ADMIN_SUB,
            updates={"is_admin": False},
            caller_user_row=ADMIN_ROW,
            dynamodb_resource=self.ddb,
            now_epoch=1000.0,
            event_id="event-107",
        )
        self.assertEqual(result["cognito_sub"], ADMIN_SUB)

        scans = [call for call in self.ddb.calls if call[0] == "scan"]
        continuations = [call for call in scans if call[2] is not None]
        self.assertTrue(
            continuations,
            "the last-active-admin guard's scan never followed "
            "LastEvaluatedKey -- it would miscount admins past page one",
        )


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
