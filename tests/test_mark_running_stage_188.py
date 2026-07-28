#!/usr/bin/env python3
"""
Unit tests for the mark-running stage — issue #188.

infra/lambda/mark_running/handler.py transitions the reviews row
PENDING -> RUNNING once a concurrency slot is held, conditionally so it never
clobbers a terminal or already-RUNNING status. Driven here with an in-memory
fake DynamoDB (no boto3/live AWS).

Covered:
  1. A PENDING review is flipped to RUNNING (with updated_at), event passed
     through unchanged.
  2. A row already in a terminal/other state (DONE/ERROR/RUNNING) is NOT
     touched -- the conditional write's ConditionalCheckFailedException is a
     silent no-op, never an error that fails the execution.
  3. A non-ConditionalCheckFailed ClientError propagates.

Run: python3 tests/test_mark_running_stage_188.py
Exit 0 = pass, 1 = fail.
"""

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = REPO_ROOT / "infra" / "lambda" / "mark_running" / "handler.py"

REVIEWS_TABLE = "contract-toaster-reviews-test"
os.environ.setdefault("REVIEWS_TABLE", REVIEWS_TABLE)


# ---------------------------------------------------------------------------
# boto3 / botocore stubs (no live AWS). ClientError mirrors botocore's shape.
# ---------------------------------------------------------------------------
class ClientError(Exception):
    def __init__(self, error_response=None, operation_name=""):
        self.response = error_response or {}
        super().__init__(str(error_response))


def _stub_third_party() -> None:
    if "botocore" not in sys.modules:
        botocore_mod = types.ModuleType("botocore")
        exceptions_mod = types.ModuleType("botocore.exceptions")
        exceptions_mod.ClientError = ClientError
        botocore_mod.exceptions = exceptions_mod
        sys.modules["botocore"] = botocore_mod
        sys.modules["botocore.exceptions"] = exceptions_mod
    if "boto3" not in sys.modules:
        boto3_mod = types.ModuleType("boto3")

        def _unset(*_a, **_kw):
            raise AssertionError("boto3.resource called without test patching module._ddb")

        boto3_mod.resource = _unset
        sys.modules["boto3"] = boto3_mod


_stub_third_party()


def _load_handler(module_name: str = "_mark_running_handler_under_test"):
    spec = importlib.util.spec_from_file_location(module_name, HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_mark = _load_handler()

REVIEW_ID = "00000000-0000-4000-a000-000000000001"


class FakeReviewsTable:
    def __init__(self, initial: dict | None = None):
        self.items: dict[str, dict] = {}
        if initial:
            self.items[initial["review_id"]] = dict(initial)
        self.raise_error: ClientError | None = None

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                     ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        if self.raise_error is not None:
            raise self.raise_error
        key = Key["review_id"]
        vals = ExpressionAttributeValues or {}
        item = self.items.get(key)
        # Enforce the PENDING-only condition faithfully.
        if ConditionExpression == "#status = :pending":
            current = item.get("status") if item else None
            if current != vals.get(":pending"):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
        item = self.items.setdefault(key, {"review_id": key})
        item["status"] = vals[":running"]
        item["updated_at"] = vals[":now"]


class FakeDDB:
    def __init__(self, table: FakeReviewsTable):
        self._table = table

    def Table(self, _name):
        return self._table


class TestMarkRunningStage(unittest.TestCase):
    def _run(self, table: FakeReviewsTable, event: dict) -> dict:
        _mark._ddb = lambda: FakeDDB(table)  # type: ignore[assignment]
        return _mark.handler(dict(event))

    def test_pending_is_flipped_to_running(self) -> None:
        table = FakeReviewsTable({"review_id": REVIEW_ID, "status": "PENDING"})
        result = self._run(table, {"review_id": REVIEW_ID, "playbook_id": "eiaa"})
        self.assertEqual(table.items[REVIEW_ID]["status"], "RUNNING")
        self.assertIn("updated_at", table.items[REVIEW_ID])
        self.assertEqual(result["review_id"], REVIEW_ID)
        self.assertEqual(result["playbook_id"], "eiaa")

    def test_terminal_status_is_not_clobbered(self) -> None:
        for existing in ("DONE", "ERROR", "MANUAL_REVIEW_REQUIRED", "RUNNING"):
            with self.subTest(existing=existing):
                table = FakeReviewsTable({"review_id": REVIEW_ID, "status": existing})
                # Must NOT raise, and must leave the status untouched.
                result = self._run(table, {"review_id": REVIEW_ID})
                self.assertEqual(table.items[REVIEW_ID]["status"], existing)
                self.assertEqual(result["review_id"], REVIEW_ID)

    def test_unexpected_client_error_propagates(self) -> None:
        table = FakeReviewsTable({"review_id": REVIEW_ID, "status": "PENDING"})
        table.raise_error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
        )
        with self.assertRaises(ClientError):
            self._run(table, {"review_id": REVIEW_ID})

    def test_missing_review_id_is_noop(self) -> None:
        table = FakeReviewsTable()
        result = self._run(table, {"playbook_id": "eiaa"})
        self.assertEqual(result, {"playbook_id": "eiaa"})
        self.assertEqual(table.items, {})


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestMarkRunningStage)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
