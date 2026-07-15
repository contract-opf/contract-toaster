#!/usr/bin/env python3
"""
Executable tests for issue #92: user management — allowlist UI, lifecycle
actions, sync visibility.

Covers the issue's TDD plan / acceptance criteria against the real
enforcement code in backend/src/users.py, using in-memory fakes for
DynamoDB — same third-party-stubbing convention as
tests/test_disposition_capture_74.py and tests/test_download_auth_attack.py
so the suite runs in CI without extra installs.

  Red (from the issue):
    - non-admin 403 on GET /api/users and PATCH /api/users/{sub}
    - suspend -> next request from that user 403 (backend-side re-check,
      independent of sign-in / token TTL)
    - deprovisioned user invisible to JIT re-admission unless re-added to
      the group (this module never re-admits; only the pre-token Lambda's
      JIT-create path, covered by #33, does — asserted here as "this module
      has no admission code path")
    - audit rows on every mutation

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"

if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


def _stub_third_party() -> None:
    """Inject minimal stubs for boto3 and fastapi if absent."""
    if "fastapi" not in sys.modules:
        fastapi_mod = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail: str = "") -> None:
                self.status_code = status_code
                self.detail = detail
                super().__init__(detail)

        class status:  # noqa: N801
            HTTP_200_OK = 200
            HTTP_400_BAD_REQUEST = 400
            HTTP_403_FORBIDDEN = 403
            HTTP_404_NOT_FOUND = 404
            HTTP_409_CONFLICT = 409
            HTTP_503_SERVICE_UNAVAILABLE = 503

        fastapi_mod.HTTPException = HTTPException
        fastapi_mod.status = status
        sys.modules["fastapi"] = fastapi_mod

    if "boto3" not in sys.modules:
        boto3_mod = types.ModuleType("boto3")
        sys.modules["boto3"] = boto3_mod


_stub_third_party()

import os  # noqa: E402

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-test")

import users as _users_module  # noqa: E402

HTTPException = sys.modules["fastapi"].HTTPException


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------

class FakeTable:
    """A tiny in-memory stand-in for a DynamoDB Table resource, keyed by the
    given partition-key attribute name."""

    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}
        self.raise_on_get: Exception | None = None

    def get_item(self, Key):
        if self.raise_on_get is not None:
            raise self.raise_on_get
        key = Key[self.key_name]
        item = self.items.get(key)
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item[self.key_name]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None):
        key = Key[self.key_name]
        item = self.items.setdefault(key, dict(Key))
        vals = ExpressionAttributeValues or {}
        # Parse "SET field = :field, other = :other" into attribute writes.
        for clause in UpdateExpression.replace("SET", "", 1).split(","):
            field, _, placeholder = clause.strip().partition("=")
            field = field.strip()
            placeholder = placeholder.strip()
            if placeholder in vals:
                item[field] = vals[placeholder]

    def scan(self):
        return {"Items": [dict(v) for v in self.items.values()]}


class FakeDynamoDBResource:
    def __init__(self, users: FakeTable, audit: FakeTable, sync_status: FakeTable):
        self._tables = {
            os.environ["USERS_TABLE"]: users,
            os.environ["AUDIT_TABLE"]: audit,
            os.environ["SYNC_STATUS_TABLE"]: sync_status,
        }

    def Table(self, name: str) -> FakeTable:
        return self._tables[name]


def _seed_user(table: FakeTable, sub: str, email: str, status_: str = "active",
                is_admin: bool = False, last_auth_at: int = 1000) -> None:
    table.items[sub] = {
        "cognito_sub": sub,
        "email": email,
        "status": status_,
        "is_admin": is_admin,
        "last_auth_at": last_auth_at,
        "created_at": 900,
        "admission": "jit",
    }


def _new_ddb() -> tuple[FakeDynamoDBResource, FakeTable, FakeTable, FakeTable]:
    users = FakeTable("cognito_sub")
    audit = FakeTable("timestamp")
    sync_status = FakeTable("sync_type")
    return FakeDynamoDBResource(users, audit, sync_status), users, audit, sync_status


# ---------------------------------------------------------------------------
# require_active_user — the backend-side per-request re-check
# ---------------------------------------------------------------------------

class TestRequireActiveUser(unittest.TestCase):
    def test_active_user_is_allowed_and_row_returned(self):
        ddb, users, _, _ = _new_ddb()
        _seed_user(users, "sub-1", "reviewer@teamexos.com", status_="active")
        row = _users_module.require_active_user("sub-1", ddb)
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["cognito_sub"], "sub-1")

    def test_suspended_user_denied_403(self):
        ddb, users, _, _ = _new_ddb()
        _seed_user(users, "sub-2", "suspended@teamexos.com", status_="suspended")
        with self.assertRaises(HTTPException) as ctx:
            _users_module.require_active_user("sub-2", ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_deprovisioned_user_denied_403(self):
        ddb, users, _, _ = _new_ddb()
        _seed_user(users, "sub-3", "gone@teamexos.com", status_="deprovisioned")
        with self.assertRaises(HTTPException) as ctx:
            _users_module.require_active_user("sub-3", ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_no_row_denied_403_not_admitted(self):
        """A sub with no users row must be denied, never silently admitted —
        JIT-create is exclusively the pre-token Lambda's job (#33)."""
        ddb, _, _, _ = _new_ddb()
        with self.assertRaises(HTTPException) as ctx:
            _users_module.require_active_user("no-such-sub", ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_dynamodb_failure_fails_closed_503(self):
        ddb, users, _, _ = _new_ddb()
        users.raise_on_get = RuntimeError("table unavailable")
        with self.assertRaises(HTTPException) as ctx:
            _users_module.require_active_user("sub-4", ddb)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_suspend_takes_effect_on_next_request_within_ttl(self):
        """RED scenario from the issue: suspend -> next request from that
        user 403 within TTL. This models the backend-side re-check firing
        on the very next call, independent of token expiry."""
        ddb, users, audit, _ = _new_ddb()
        _seed_user(users, "sub-5", "reviewer@teamexos.com", status_="active")
        admin_sub = "admin-1"
        _seed_user(users, admin_sub, "admin@teamexos.com", status_="active", is_admin=True)

        # First request succeeds.
        row = _users_module.require_active_user("sub-5", ddb)
        self.assertEqual(row["status"], "active")

        # Admin suspends the user.
        admin_row = _users_module.require_active_user(admin_sub, ddb)
        _users_module.update_user("sub-5", {"status": "suspended"}, admin_row, ddb)

        # The very next request from that user (still holding a structurally
        # valid token) is denied.
        with self.assertRaises(HTTPException) as ctx:
            _users_module.require_active_user("sub-5", ddb)
        self.assertEqual(ctx.exception.status_code, 403)


# ---------------------------------------------------------------------------
# list_users — GET /api/users
# ---------------------------------------------------------------------------

class TestListUsers(unittest.TestCase):
    def test_non_admin_403(self):
        ddb, users, _, _ = _new_ddb()
        _seed_user(users, "sub-1", "reviewer@teamexos.com", is_admin=False)
        caller = _users_module.require_active_user("sub-1", ddb)
        with self.assertRaises(HTTPException) as ctx:
            _users_module.list_users(caller, ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_sees_all_users_including_jit_created_rows(self):
        ddb, users, _, _ = _new_ddb()
        _seed_user(users, "admin-1", "admin@teamexos.com", is_admin=True, last_auth_at=5000)
        _seed_user(users, "sub-jit", "newreviewer@teamexos.com", is_admin=False, last_auth_at=4000)
        _seed_user(users, "sub-gone", "left@teamexos.com", status_="deprovisioned", last_auth_at=3000)
        caller = _users_module.require_active_user("admin-1", ddb)

        result = _users_module.list_users(caller, ddb)
        subs = {u["cognito_sub"] for u in result}
        self.assertEqual(subs, {"admin-1", "sub-jit", "sub-gone"})
        # Deprovisioned rows remain visible (allowlist view shows lifecycle
        # state, it does not hide non-active users).
        gone = next(u for u in result if u["cognito_sub"] == "sub-gone")
        self.assertEqual(gone["status"], "deprovisioned")

    def test_list_is_ordered_most_recent_auth_first(self):
        ddb, users, _, _ = _new_ddb()
        _seed_user(users, "admin-1", "admin@teamexos.com", is_admin=True, last_auth_at=100)
        _seed_user(users, "sub-a", "a@teamexos.com", last_auth_at=50)
        _seed_user(users, "sub-b", "b@teamexos.com", last_auth_at=200)
        caller = _users_module.require_active_user("admin-1", ddb)

        result = _users_module.list_users(caller, ddb)
        self.assertEqual([u["cognito_sub"] for u in result], ["sub-b", "admin-1", "sub-a"])


# ---------------------------------------------------------------------------
# update_user — PATCH /api/users/{sub}
# ---------------------------------------------------------------------------

class TestUpdateUser(unittest.TestCase):
    def setUp(self):
        self.ddb, self.users, self.audit, self.sync_status = _new_ddb()
        _seed_user(self.users, "admin-1", "admin@teamexos.com", is_admin=True)
        _seed_user(self.users, "sub-1", "reviewer@teamexos.com", is_admin=False)
        self.admin = _users_module.require_active_user("admin-1", self.ddb)

    def test_non_admin_403(self):
        non_admin = _users_module.require_active_user("sub-1", self.ddb)
        with self.assertRaises(HTTPException) as ctx:
            _users_module.update_user("sub-1", {"status": "suspended"}, non_admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_can_suspend_a_user(self):
        result = _users_module.update_user("sub-1", {"status": "suspended"}, self.admin, self.ddb)
        self.assertEqual(result["status"], "suspended")
        self.assertEqual(self.users.items["sub-1"]["status"], "suspended")

    def test_admin_can_deprovision_a_user(self):
        result = _users_module.update_user("sub-1", {"status": "deprovisioned"}, self.admin, self.ddb)
        self.assertEqual(result["status"], "deprovisioned")

    def test_admin_can_grant_admin_flag(self):
        result = _users_module.update_user("sub-1", {"is_admin": True}, self.admin, self.ddb)
        self.assertTrue(result["is_admin"])

    def test_invalid_status_rejected_400(self):
        with self.assertRaises(HTTPException) as ctx:
            _users_module.update_user("sub-1", {"status": "disabled"}, self.admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_non_boolean_is_admin_rejected_400(self):
        with self.assertRaises(HTTPException) as ctx:
            _users_module.update_user("sub-1", {"is_admin": "yes"}, self.admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_unknown_field_rejected_400(self):
        with self.assertRaises(HTTPException) as ctx:
            _users_module.update_user("sub-1", {"email": "new@teamexos.com"}, self.admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_empty_update_rejected_400(self):
        with self.assertRaises(HTTPException) as ctx:
            _users_module.update_user("sub-1", {}, self.admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_unknown_target_user_404(self):
        with self.assertRaises(HTTPException) as ctx:
            _users_module.update_user("no-such-sub", {"status": "suspended"}, self.admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_admin_cannot_modify_own_row_409(self):
        with self.assertRaises(HTTPException) as ctx:
            _users_module.update_user("admin-1", {"is_admin": False}, self.admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_every_mutation_writes_an_audit_row(self):
        self.assertEqual(len(self.audit.items), 0)
        _users_module.update_user("sub-1", {"status": "suspended"}, self.admin, self.ddb)
        self.assertEqual(len(self.audit.items), 1)
        entry = next(iter(self.audit.items.values()))
        self.assertEqual(entry["actor"], "admin-1")
        self.assertEqual(entry["target"], "sub-1")
        self.assertEqual(entry["action"], "user_lifecycle_update")
        self.assertEqual(entry["before_status"], "active")
        self.assertEqual(entry["after_status"], "suspended")

    def test_audit_row_never_contains_document_substance(self):
        """Audit rows for user mutations are identifiers and lifecycle
        values only (ARCHITECTURE.md -> Audit posture)."""
        _users_module.update_user("sub-1", {"is_admin": True}, self.admin, self.ddb)
        entry = next(iter(self.audit.items.values()))
        forbidden_keys = {"document", "content", "rationale", "clause_text", "prompt"}
        self.assertTrue(forbidden_keys.isdisjoint(entry.keys()))


# ---------------------------------------------------------------------------
# get_sync_status — sync-job visibility panel
# ---------------------------------------------------------------------------

class TestSyncStatus(unittest.TestCase):
    def setUp(self):
        self.ddb, self.users, self.audit, self.sync_status = _new_ddb()
        _seed_user(self.users, "admin-1", "admin@teamexos.com", is_admin=True)
        _seed_user(self.users, "sub-1", "reviewer@teamexos.com", is_admin=False)
        self.admin = _users_module.require_active_user("admin-1", self.ddb)

    def test_non_admin_403(self):
        non_admin = _users_module.require_active_user("sub-1", self.ddb)
        with self.assertRaises(HTTPException) as ctx:
            _users_module.get_sync_status(non_admin, self.ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_never_run_yet_returns_well_formed_shape(self):
        result = _users_module.get_sync_status(self.admin, self.ddb)
        self.assertIsNone(result["last_run_at"])
        self.assertIsNone(result["last_run_outcome"])
        self.assertEqual(result["users_deprovisioned_count"], 0)

    def test_reflects_last_successful_run(self):
        self.sync_status.items[_users_module.SYNC_TYPE_USER_DEPROVISION] = {
            "sync_type": _users_module.SYNC_TYPE_USER_DEPROVISION,
            "last_run_at": 123456,
            "last_run_outcome": "ok",
            "users_deprovisioned_count": 2,
            "next_run_at": 127056,
        }
        result = _users_module.get_sync_status(self.admin, self.ddb)
        self.assertEqual(result["last_run_outcome"], "ok")
        self.assertEqual(result["users_deprovisioned_count"], 2)

    def test_reflects_fail_closed_state(self):
        """Sync-job status visible includes the fail-closed state — a run
        that could not reach the Directory API made no changes."""
        self.sync_status.items[_users_module.SYNC_TYPE_USER_DEPROVISION] = {
            "sync_type": _users_module.SYNC_TYPE_USER_DEPROVISION,
            "last_run_at": 200000,
            "last_run_outcome": "directory_unavailable",
            "users_deprovisioned_count": 0,
            "next_run_at": 203600,
        }
        result = _users_module.get_sync_status(self.admin, self.ddb)
        self.assertEqual(result["last_run_outcome"], "directory_unavailable")
        self.assertEqual(result["users_deprovisioned_count"], 0)

    def test_this_module_has_no_admission_code_path(self):
        """A deprovisioned user must stay invisible to JIT re-admission
        unless re-added to the group — i.e. this module must not expose any
        function that creates or reactivates a users row from 'deprovisioned'
        back to 'active' other than the audited admin update_user path
        (which requires an explicit admin action, not an automatic re-admit)."""
        module_functions = {
            name for name in dir(_users_module)
            if callable(getattr(_users_module, name)) and not name.startswith("_")
        }
        self.assertNotIn("jit_create_user", module_functions)
        self.assertNotIn("reactivate_user", module_functions)
        self.assertNotIn("readmit_user", module_functions)


# ---------------------------------------------------------------------------
# get_user — single-row fetch (admin), used by the PATCH form / detail view
# ---------------------------------------------------------------------------

class TestGetUser(unittest.TestCase):
    def test_non_admin_403(self):
        ddb, users, _, _ = _new_ddb()
        _seed_user(users, "sub-1", "reviewer@teamexos.com", is_admin=False)
        caller = _users_module.require_active_user("sub-1", ddb)
        with self.assertRaises(HTTPException) as ctx:
            _users_module.get_user("sub-1", caller, ddb)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_can_fetch_a_user(self):
        ddb, users, _, _ = _new_ddb()
        _seed_user(users, "admin-1", "admin@teamexos.com", is_admin=True)
        _seed_user(users, "sub-1", "reviewer@teamexos.com", is_admin=False)
        admin = _users_module.require_active_user("admin-1", ddb)
        result = _users_module.get_user("sub-1", admin, ddb)
        self.assertEqual(result["email"], "reviewer@teamexos.com")

    def test_unknown_user_404(self):
        ddb, users, _, _ = _new_ddb()
        _seed_user(users, "admin-1", "admin@teamexos.com", is_admin=True)
        admin = _users_module.require_active_user("admin-1", ddb)
        with self.assertRaises(HTTPException) as ctx:
            _users_module.get_user("no-such-sub", admin, ddb)
        self.assertEqual(ctx.exception.status_code, 404)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestRequireActiveUser))
    suite.addTests(loader.loadTestsFromTestCase(TestListUsers))
    suite.addTests(loader.loadTestsFromTestCase(TestUpdateUser))
    suite.addTests(loader.loadTestsFromTestCase(TestSyncStatus))
    suite.addTests(loader.loadTestsFromTestCase(TestGetUser))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
