#!/usr/bin/env python3
"""
Executable regression tests: `backend/src/users.py::update_user` must not
interpolate patched field names literally into its UpdateExpression.

THE BUG THIS GUARDS
-------------------
`update_user` built its UpdateExpression as::

    update_expr_parts.append(f"{field} = :{field}")

`status` is one of the two PATCHABLE_FIELDS and is a **DynamoDB reserved
keyword**. Against real DynamoDB (and against moto, which enforces the same
rule) that raises::

    botocore.exceptions.ClientError: An error occurred (ValidationException)
    when calling the UpdateItem operation: Invalid UpdateExpression:
    Attribute name is a reserved keyword; reserved keyword: status

So `PATCH /api/users/{sub}` with `{"status": "suspended"}` — the suspend /
deprovision lifecycle action — failed in production. `is_admin` happens not
to be a reserved word, so the admin-flag half of the same code path worked,
which is why the failure looked field-specific rather than structural.

WHY THE SUITE MISSED IT
-----------------------
`tests/test_user_management_92.py` drives an in-memory table double that
does not enforce reserved keywords, so every UpdateExpression it sees is
accepted. This file therefore uses **moto**, which does enforce them: that
is the whole point of adding it rather than extending the existing file.

`backend/src/playbook_versions.py` already had the correct shape
(`ExpressionAttributeNames={"#status": "status"}`); `users.py` did not.

These tests FAIL on the pre-fix tree (ValidationException on the `status`
cases) and PASS after it.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from src import users as users_module  # noqa: E402

CALLER_SUB = "sub-caller-admin"
TARGET_SUB = "sub-target-regular"
OTHER_ADMIN_SUB = "sub-other-admin"


def _admin_caller_row() -> dict[str, Any]:
    """The caller row `update_user` gates on (an active admin)."""
    return {"cognito_sub": CALLER_SUB, "is_admin": True, "status": "active"}


class _Harness:
    """A moto-backed `users` + `audit` pair.

    moto is load-bearing here: an in-memory double would accept the reserved
    keyword and the regression would go unnoticed (see the module docstring).
    """

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
        # A second active admin so the last-active-admin guard (issue #473)
        # never fires and mask the ValidationException we are testing for.
        for sub in (CALLER_SUB, OTHER_ADMIN_SUB):
            self.users_table.put_item(
                Item={"cognito_sub": sub, "is_admin": True, "status": "active", "email": f"{sub}@example.test"}
            )
        self.users_table.put_item(
            Item={
                "cognito_sub": TARGET_SUB,
                "is_admin": False,
                "status": "active",
                "email": "target@example.test",
            }
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        self._mock_aws.stop()

    def row(self, sub: str) -> dict[str, Any]:
        return self.users_table.get_item(Key={"cognito_sub": sub}).get("Item") or {}


def test_patch_status_survives_the_reserved_keyword(failures: list[str]) -> None:
    """[1] PATCH {"status": "suspended"} must not raise ValidationException.

    This is the actual production bug: the suspend/deprovision lifecycle
    action on a real DynamoDB table.
    """
    with _Harness() as h:
        try:
            result = users_module.update_user(
                target_sub=TARGET_SUB,
                updates={"status": "suspended"},
                caller_user_row=_admin_caller_row(),
                dynamodb_resource=h.ddb,
                now_epoch=1_700_000_000.0,
                event_id="evt-status",
            )
        except Exception as exc:  # noqa: BLE001 — the bug surfaces as ClientError
            failures.append(
                f"[1a] update_user(status=suspended) raised {type(exc).__name__}: {exc}. "
                "`status` is a DynamoDB reserved keyword — the UpdateExpression must "
                "alias it via ExpressionAttributeNames."
            )
            return

        if result.get("status") != "suspended":
            failures.append(f"[1b] returned row status is {result.get('status')!r}, expected 'suspended'")
        # The write must actually have landed, not merely been returned.
        persisted = h.row(TARGET_SUB)
        if persisted.get("status") != "suspended":
            failures.append(f"[1c] persisted status is {persisted.get('status')!r}, expected 'suspended'")
        if "updated_at" not in persisted:
            failures.append("[1d] updated_at was not written")


def test_patch_every_valid_status_value(failures: list[str]) -> None:
    """[2] Every VALID_STATUSES value round-trips, not just one."""
    for value in sorted(users_module.VALID_STATUSES):
        with _Harness() as h:
            try:
                users_module.update_user(
                    target_sub=TARGET_SUB,
                    updates={"status": value},
                    caller_user_row=_admin_caller_row(),
                    dynamodb_resource=h.ddb,
                    now_epoch=1_700_000_000.0,
                    event_id=f"evt-{value}",
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"[2a] update_user(status={value!r}) raised {type(exc).__name__}: {exc}")
                continue
            persisted = h.row(TARGET_SUB)
            if persisted.get("status") != value:
                failures.append(f"[2b] status={value!r} did not persist (got {persisted.get('status')!r})")


def test_patch_is_admin_still_works(failures: list[str]) -> None:
    """[3] The non-reserved field must keep working — no regression from the fix."""
    with _Harness() as h:
        try:
            users_module.update_user(
                target_sub=TARGET_SUB,
                updates={"is_admin": True},
                caller_user_row=_admin_caller_row(),
                dynamodb_resource=h.ddb,
                now_epoch=1_700_000_000.0,
                event_id="evt-admin",
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[3a] update_user(is_admin=True) raised {type(exc).__name__}: {exc}")
            return
        if h.row(TARGET_SUB).get("is_admin") is not True:
            failures.append("[3b] is_admin did not persist as True")


def test_patch_both_fields_in_one_call(failures: list[str]) -> None:
    """[4] Both PATCHABLE_FIELDS together — the multi-alias path.

    Guards the `#f0`/`#f1` aliasing specifically: a fix that hard-codes a
    single `#status` alias would pass [1] but can still collide here.
    """
    with _Harness() as h:
        try:
            users_module.update_user(
                target_sub=TARGET_SUB,
                updates={"status": "suspended", "is_admin": True},
                caller_user_row=_admin_caller_row(),
                dynamodb_resource=h.ddb,
                now_epoch=1_700_000_000.0,
                event_id="evt-both",
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[4a] update_user(status+is_admin) raised {type(exc).__name__}: {exc}")
            return
        persisted = h.row(TARGET_SUB)
        if persisted.get("status") != "suspended":
            failures.append(f"[4b] status did not persist (got {persisted.get('status')!r})")
        if persisted.get("is_admin") is not True:
            failures.append("[4c] is_admin did not persist as True")


def test_every_patchable_field_is_alias_safe(failures: list[str]) -> None:
    """[5] Forward guard: EVERY current PATCHABLE_FIELD patches cleanly.

    PATCHABLE_FIELDS may grow. This drives each field with a type-appropriate
    value so a future reserved-keyword addition (`name`, `timestamp`, `size`
    …) fails here rather than in production.
    """
    sample: dict[str, Any] = {"status": "suspended", "is_admin": True}
    unhandled = users_module.PATCHABLE_FIELDS - set(sample)
    if unhandled:
        failures.append(
            f"[5a] PATCHABLE_FIELDS grew ({sorted(unhandled)}) without a sample value here — "
            "add one so this forward guard keeps covering every patchable field."
        )
    for field in sorted(users_module.PATCHABLE_FIELDS & set(sample)):
        with _Harness() as h:
            try:
                users_module.update_user(
                    target_sub=TARGET_SUB,
                    updates={field: sample[field]},
                    caller_user_row=_admin_caller_row(),
                    dynamodb_resource=h.ddb,
                    now_epoch=1_700_000_000.0,
                    event_id=f"evt-field-{field}",
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    f"[5b] patchable field {field!r} raised {type(exc).__name__}: {exc} — "
                    "likely a DynamoDB reserved keyword needing an ExpressionAttributeNames alias."
                )


TESTS = (
    test_patch_status_survives_the_reserved_keyword,
    test_patch_every_valid_status_value,
    test_patch_is_admin_still_works,
    test_patch_both_fields_in_one_call,
    test_every_patchable_field_is_alias_safe,
)


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        try:
            test(failures)
        except Exception:  # noqa: BLE001 — a crashing test is a failing test
            failures.append(f"{test.__name__} raised:\n{traceback.format_exc()}")
    if failures:
        print(f"FAIL ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"PASS ({len(TESTS)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
