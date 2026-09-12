#!/usr/bin/env python3
"""
Executable tests for issue #66: ONE admin-authorization predicate, in
`backend/src/authz.py`, and no local copies anywhere else.

WHAT THIS GUARDS
----------------
The same three-line admin check used to be re-declared 17 times across
`backend/src` (`_is_admin` / `_require_admin` / `_is_admin_caller` in
admin_dashboard, audit_queries, bundle_authoring, corpus, demo_auth,
download, entity_roster, main, model_settings, retention, reviews and
users). Seventeen copies of an authorization predicate is seventeen chances
for one of them to drift, and one drifted copy opens a route. Check 1 below
AST-parses every module under `backend/src` and fails if any of them grows a
local copy back -- module-level or nested inside a function.

`authz.is_admin` is deliberately STRICTER than the copies it replaced:
`row.get("is_admin", False) is True`, not `bool(row.get("is_admin", False))`.
A hand-edited or break-glass row carrying the string `"false"` used to count
as an admin. Check 2 pins that truth table.

Check 4 is the other half of that argument: the strict predicate is only
safe if no legitimate writer ever stores a non-boolean. So this file drives
the REAL users-table writers -- `demo_auth.seed_demo_users`,
`demo_auth.add_user` and `users.update_user` -- against moto and asserts on
the persisted item that `is_admin` is a real `bool`, then feeds the row that
came back out of the table to `authz.is_admin`. moto is load-bearing: an
in-memory double would happily store whatever Python object it was handed
and round-trip it unchanged, so it could not tell a real DynamoDB BOOL from
a string that merely looks like one.

Red (pre-fix): `src.authz` does not exist and 17 local definitions remain.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import ast
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_SRC = BACKEND_ROOT / "src"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")

import boto3  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from moto import mock_aws  # noqa: E402

from src import authz  # noqa: E402
from src import demo_auth as demo_auth_module  # noqa: E402
from src import users as users_module  # noqa: E402

#: The names that may exist in exactly ONE module. `_is_admin_caller` was
#: `reviews.py`'s spelling of the same predicate; the underscored variants
#: were everyone else's.
FORBIDDEN_DEFS = frozenset(
    {"_is_admin", "_require_admin", "_is_admin_caller", "is_admin", "require_admin"}
)

#: The one module allowed to define them.
AUTHZ_MODULE = "authz.py"


# ---------------------------------------------------------------------------
# Check 1 -- the AST single-source rule
# ---------------------------------------------------------------------------

def test_no_module_outside_authz_defines_the_admin_predicate() -> None:
    """Every `backend/src` module is parsed; only `authz.py` may `def` one of
    FORBIDDEN_DEFS. `ast.walk` is used rather than a scan of `tree.body` so a
    definition nested inside a function or a class is caught too.

    An import binding is deliberately NOT a violation: `download.py` keeps
    `from src.authz import is_admin as _is_admin` so
    `tests/test_download_auth_attack.py` (which binds
    `_download_module._is_admin` and asserts on it) stays byte-unchanged. An
    alias cannot drift -- it IS the single source, under another name --
    which check 3 below proves object-identically.
    """
    offenders: list[str] = []
    modules = sorted(BACKEND_SRC.rglob("*.py"))
    assert modules, f"no modules found under {BACKEND_SRC}"

    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in FORBIDDEN_DEFS:
                continue
            if path.name == AUTHZ_MODULE:
                continue
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}:{node.lineno} defines {node.name!r}"
            )

    assert not offenders, (
        "the admin predicate must live only in backend/src/authz.py "
        "(issue #66); local copies found:\n  " + "\n  ".join(offenders)
    )


def test_authz_module_actually_defines_both_functions() -> None:
    """Guards check 1 against passing vacuously. If `authz.py` were deleted
    or renamed, the scan above would find zero offenders and go green over a
    tree with no admin predicate at all."""
    tree = ast.parse((BACKEND_SRC / AUTHZ_MODULE).read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"is_admin", "require_admin"} <= defined, sorted(defined)


# ---------------------------------------------------------------------------
# Check 2 -- the truth table
# ---------------------------------------------------------------------------

def test_is_admin_is_true_only_for_the_boolean_true() -> None:
    assert authz.is_admin({"cognito_sub": "s", "is_admin": True}) is True


def test_is_admin_rejects_every_non_true_value() -> None:
    """`1` and `"true"` used to pass under `bool(...)`. They must not now."""
    for value in (False, "true", "false", "", 1, 0, "yes", "TRUE", [], {}, None):
        row = {"cognito_sub": "s", "is_admin": value}
        assert authz.is_admin(row) is False, f"{value!r} must not grant admin"


def test_is_admin_rejects_a_missing_flag() -> None:
    assert authz.is_admin({"cognito_sub": "s"}) is False


def test_is_admin_ignores_a_forged_token_style_claim() -> None:
    """`is_admin` is a users-row flag, never a JWT claim: a crafted row
    carrying a Cognito-style group/role claim grants nothing."""
    assert authz.is_admin({"cognito_sub": "s", "custom:role": "admin"}) is False
    assert authz.is_admin({"cognito_sub": "s", "cognito:groups": ["admin"]}) is False


# ---------------------------------------------------------------------------
# Check 3 -- require_admin's 403, and the modules that now share it
# ---------------------------------------------------------------------------

def test_require_admin_raises_403_with_a_byte_identical_detail() -> None:
    detail = "Admin privilege required to view the spend ledger."
    try:
        authz.require_admin({"cognito_sub": "s", "is_admin": False}, detail)
    except HTTPException as exc:
        assert exc.status_code == 403, exc.status_code
        assert exc.detail == detail, repr(exc.detail)
        assert exc.detail is detail, "detail must be passed through untouched"
    else:
        raise AssertionError("require_admin did not raise for a non-admin row")


def test_require_admin_is_silent_for_an_admin_row() -> None:
    assert authz.require_admin({"cognito_sub": "s", "is_admin": True}, "nope") is None


def test_every_former_copy_now_resolves_to_the_authz_objects() -> None:
    """Name-level proof that the replacement actually happened: importing a
    module and finding some other function bound to `is_admin` would pass
    check 1 (no `def`) while still being a second predicate."""
    from src import admin_dashboard, audit_queries, bundle_authoring, corpus
    from src import download, entity_roster, main, model_settings
    from src import retention, reviews

    for module, attr in (
        (admin_dashboard, "require_admin"),
        (audit_queries, "require_admin"),
        (corpus, "require_admin"),
        (demo_auth_module, "require_admin"),
        (entity_roster, "require_admin"),
        (model_settings, "require_admin"),
        (retention, "require_admin"),
    ):
        assert getattr(module, attr) is authz.require_admin, module.__name__

    for module, attr in (
        (bundle_authoring, "is_admin"),
        (download, "_is_admin"),  # the alias test_download_auth_attack.py binds
        (main, "is_admin"),
        (reviews, "is_admin"),
        (users_module, "is_admin"),
    ):
        assert getattr(module, attr) is authz.is_admin, module.__name__


# ---------------------------------------------------------------------------
# Check 4 -- the users-table writers only ever persist a real boolean
# ---------------------------------------------------------------------------

CALLER_SUB = "sub-caller-admin"
OTHER_ADMIN_SUB = "sub-other-admin"
TARGET_SUB = "sub-target-regular"


def _admin_caller_row() -> dict[str, Any]:
    return {"cognito_sub": CALLER_SUB, "is_admin": True, "status": "active"}


class _Harness:
    """A moto-backed `users` + `audit` pair, same shape as
    tests/test_user_update_reserved_keyword.py's harness."""

    def __enter__(self) -> "_Harness":
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
        # Two active admins so update_user's last-active-admin guard (#473)
        # never fires and masks what we are actually asserting.
        for sub in (CALLER_SUB, OTHER_ADMIN_SUB):
            self.users_table.put_item(
                Item={"cognito_sub": sub, "is_admin": True, "status": "active"}
            )
        self.users_table.put_item(
            Item={"cognito_sub": TARGET_SUB, "is_admin": False, "status": "active"}
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        self._mock_aws.stop()

    def row(self, sub: str) -> dict[str, Any]:
        return self.users_table.get_item(Key={"cognito_sub": sub})["Item"]


def test_update_user_rejects_a_non_boolean_is_admin_with_400() -> None:
    """The 400 guard is the reason `is True` is safe: a string can never
    reach the table through this route."""
    with _Harness() as h:
        for bad in ("yes", "true", "false", 1, 0, None):
            try:
                users_module.update_user(
                    TARGET_SUB, {"is_admin": bad}, _admin_caller_row(), h.ddb
                )
            except HTTPException as exc:
                assert exc.status_code == 400, (bad, exc.status_code)
                assert exc.detail == "is_admin must be a boolean.", exc.detail
            else:
                raise AssertionError(f"update_user accepted is_admin={bad!r}")
            assert h.row(TARGET_SUB)["is_admin"] is False, "row must be untouched"


def test_update_user_persists_a_real_bool_on_both_branches() -> None:
    """Both directions of the flag, because the promote and the demote paths
    run different guards (`would_strip_admin_access` only fires on demote)."""
    with _Harness() as h:
        users_module.update_user(
            TARGET_SUB, {"is_admin": True}, _admin_caller_row(), h.ddb
        )
        promoted = h.row(TARGET_SUB)
        assert type(promoted["is_admin"]) is bool, type(promoted["is_admin"])
        assert authz.is_admin(promoted) is True

        users_module.update_user(
            TARGET_SUB, {"is_admin": False}, _admin_caller_row(), h.ddb
        )
        demoted = h.row(TARGET_SUB)
        assert type(demoted["is_admin"]) is bool, type(demoted["is_admin"])
        assert authz.is_admin(demoted) is False


def test_add_user_persists_a_real_bool_on_both_user_types() -> None:
    """`add_user` is the admin-add create path. It coerces the payload field
    with `bool(...)` before persisting, so a truthy string becomes a real
    `True` in the table -- which is exactly what `is True` needs."""
    with _Harness() as h:
        cases = (
            ({"user_type": "sso", "email": "a@example.test", "is_admin": True}, True),
            ({"user_type": "sso", "email": "b@example.test", "is_admin": False}, False),
            ({"user_type": "sso", "email": "c@example.test"}, False),
            ({"user_type": "sso", "email": "d@example.test", "is_admin": "yes"}, True),
            (
                {
                    "user_type": "password",
                    "username": "pw-admin",
                    "password": "hunter2hunter2",
                    "is_admin": True,
                },
                True,
            ),
            (
                {
                    "user_type": "password",
                    "username": "pw-plain",
                    "password": "hunter2hunter2",
                    "is_admin": False,
                },
                False,
            ),
        )
        for payload, expected in cases:
            created = demo_auth_module.add_user(payload, _admin_caller_row(), h.ddb)
            stored = h.row(created["cognito_sub"])
            assert type(stored["is_admin"]) is bool, (payload, type(stored["is_admin"]))
            assert authz.is_admin(stored) is expected, payload


def test_seed_demo_users_persists_a_real_bool() -> None:
    """The other non-test writer of the flag: the seeded demo credentials."""
    with _Harness() as h:
        demo_auth_module.seed_demo_users(h.ddb)
        for spec in demo_auth_module.SEED_USERS:
            stored = h.row(demo_auth_module.local_user_sub(spec["username"]))
            assert type(stored["is_admin"]) is bool, spec["username"]
            assert authz.is_admin(stored) is (spec["is_admin"] is True), spec["username"]
        # Both branches of the seed vocabulary are exercised, not just one.
        assert {spec["is_admin"] for spec in demo_auth_module.SEED_USERS} == {True, False}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS: tuple[Callable[[], None], ...] = (
    test_no_module_outside_authz_defines_the_admin_predicate,
    test_authz_module_actually_defines_both_functions,
    test_is_admin_is_true_only_for_the_boolean_true,
    test_is_admin_rejects_every_non_true_value,
    test_is_admin_rejects_a_missing_flag,
    test_is_admin_ignores_a_forged_token_style_claim,
    test_require_admin_raises_403_with_a_byte_identical_detail,
    test_require_admin_is_silent_for_an_admin_row,
    test_every_former_copy_now_resolves_to_the_authz_objects,
    test_update_user_rejects_a_non_boolean_is_admin_with_400,
    test_update_user_persists_a_real_bool_on_both_branches,
    test_add_user_persists_a_real_bool_on_both_user_types,
    test_seed_demo_users_persists_a_real_bool,
)


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        try:
            test()
        except Exception:  # noqa: BLE001 -- a test runner reports everything
            failures.append(f"{test.__name__}\n{traceback.format_exc()}")
            print(f"FAIL {test.__name__}")
        else:
            print(f"ok   {test.__name__}")

    if failures:
        print(f"\n{len(failures)} of {len(TESTS)} tests FAILED\n")
        for failure in failures:
            print(failure)
        return 1

    print(f"\nAll {len(TESTS)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
