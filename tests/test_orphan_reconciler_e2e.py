#!/usr/bin/env python3
"""
Executable tests for the issue #59 orphan reconciler
(infra/lambda/orphan_reconciler/handler.py), covering the two review-round-1
findings that the structural gate (tests/test_orphan_reconciler.py, a
text-grep check against ARCHITECTURE.md/RUNBOOK.md) cannot catch because it
never runs the handler:

  1. Dead-execution reconciliation was dead code: execution_arn was never
     written onto the `reviews` row, so _reconcile_dead_executions' scan
     filter `attribute_exists(execution_arn)` could never match. This test
     proves a dead (FAILED/TIMED_OUT/ABORTED) execution on a non-terminal
     review IS transitioned to ERROR and releases both the spend reservation
     and the concurrency-semaphore slot.

  2. Re-driven executions started with an empty "{}" input: no code path
     persisted `execution_input` on the submission record, so the ARN-less
     re-drive path re-ran StartExecution with a payload missing review_id /
     playbook_id / upload_s3_key -- exactly what the first pipeline stage
     (acquireSlot -> event["review_id"]) needs. This test proves a re-driven
     execution receives the well-formed pointer-only payload that was
     persisted at submission-create time.

These are unit tests against in-memory fakes for DynamoDB and Step
Functions (no live AWS, no moto/boto3 dependency required) -- same
third-party-stubbing convention as tests/test_review_submission_e2e.py and
tests/test_download_auth_attack.py, so the suite runs in CI without extra
installs.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import json
import sys
import time
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RECONCILER_DIR = REPO_ROOT / "infra" / "lambda" / "orphan_reconciler"

if str(RECONCILER_DIR) not in sys.path:
    sys.path.insert(0, str(RECONCILER_DIR))


def _stub_third_party() -> None:
    """Inject minimal stubs for boto3/botocore if absent (handler.py imports
    `boto3` and `botocore.exceptions.ClientError` at module scope)."""
    if "botocore" not in sys.modules:
        botocore_mod = types.ModuleType("botocore")
        exceptions_mod = types.ModuleType("botocore.exceptions")

        class ClientError(Exception):
            def __init__(self, error_response=None, operation_name=""):
                self.response = error_response or {}
                super().__init__(str(error_response))

        exceptions_mod.ClientError = ClientError
        botocore_mod.exceptions = exceptions_mod
        sys.modules["botocore"] = botocore_mod
        sys.modules["botocore.exceptions"] = exceptions_mod

    if "boto3" not in sys.modules:
        boto3_mod = types.ModuleType("boto3")

        def _unset_resource(*_a, **_kw):
            raise AssertionError(
                "boto3.resource() called without being patched by the test -- "
                "tests must monkeypatch handler.boto3 before invoking the handler."
            )

        boto3_mod.resource = _unset_resource
        boto3_mod.client = _unset_resource
        sys.modules["boto3"] = boto3_mod


_stub_third_party()

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("SEMAPHORE_TABLE", "contract-toaster-semaphore-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)
os.environ.setdefault("STALE_PENDING_THRESHOLD_SECONDS", "120")

import handler as _handler_module  # noqa: E402

ClientError = sys.modules["botocore.exceptions"].ClientError


# ---------------------------------------------------------------------------
# In-memory fakes (same shape/contract as tests/test_review_submission_e2e.py)
# ---------------------------------------------------------------------------

class FakeTable:
    """A tiny in-memory stand-in for a boto3 DynamoDB Table resource,
    extended (relative to the reviews.py e2e fake) with a `scan` that
    understands the small set of FilterExpressions handler.py issues."""

    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}
        self.deleted_keys: list[str] = []

    def get_item(self, Key):
        key = Key[self.key_name]
        item = self.items.get(key)
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item[self.key_name]
        if ConditionExpression == "attribute_not_exists(idempotency_key)" and key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                     ConditionExpression=None, ExpressionAttributeNames=None):
        key = Key[self.key_name]
        vals = ExpressionAttributeValues or {}

        if ConditionExpression == "#status IN (:pending, :running)":
            item = self.items.get(key)
            current_status = item.get("status") if item else None
            if current_status not in (vals.get(":pending"), vals.get(":running")):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )

        item = self.items.setdefault(key, dict(Key))

        if "reserved_usd_cents = if_not_exists" in UpdateExpression:
            current = item.get("reserved_usd_cents", 0)
            cap = item.get("daily_cap_usd_cents", vals.get(":cap"))
            amount = vals[":amount"]
            if ConditionExpression and current + amount > cap:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
            item["reserved_usd_cents"] = current + amount
            item.setdefault("daily_cap_usd_cents", vals.get(":cap"))
            return

        if "execution_arn = :arn" in UpdateExpression:
            item["execution_arn"] = vals[":arn"]
            if ":status" in vals:
                item["execution_status"] = vals[":status"]
            return

        if "spend_reservation_id = :rid" in UpdateExpression:
            item["spend_reservation_id"] = vals[":rid"]
            return

        if "reservation_released = :true" in UpdateExpression:
            item["reservation_released"] = vals[":true"]
            return

        if "reserved_usd_cents = reserved_usd_cents + :delta" in UpdateExpression:
            # settle_spend()'s daily_spend update (issue #189).
            item["reserved_usd_cents"] = item.get("reserved_usd_cents", 0) + vals[":delta"]
            item["settled_usd_cents"] = item.get("settled_usd_cents", 0) + vals[":actual"]
            return

        if "#status = :error" in UpdateExpression:
            item["status"] = vals[":error"]
            item["failing_stage"] = vals[":stage"]
            item["error_reason"] = vals[":reason"]
            return

        # Generic fallback: no-op for anything else exercised indirectly.

    def delete_item(self, Key):
        key = Key[self.key_name]
        self.deleted_keys.append(key)
        self.items.pop(key, None)

    def scan(self, FilterExpression=None, ExpressionAttributeNames=None,
              ExpressionAttributeValues=None):
        """Purpose-built interpreter for the specific FilterExpressions
        handler.py issues -- not a general DynamoDB expression engine."""
        vals = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}
        status_attr = names.get("#status", "status")

        def matches(item: dict) -> bool:
            if FilterExpression is None:
                return True
            if FilterExpression == "attribute_not_exists(execution_arn)":
                return "execution_arn" not in item or item["execution_arn"] is None
            if FilterExpression == (
                "#status IN (:pending, :running) AND attribute_exists(execution_arn)"
            ):
                return (
                    item.get(status_attr) in (vals.get(":pending"), vals.get(":running"))
                    and item.get("execution_arn") is not None
                )
            if FilterExpression == "review_id = :rid":
                return item.get("review_id") == vals.get(":rid")
            raise AssertionError(f"FakeTable.scan: unhandled FilterExpression {FilterExpression!r}")

        return {"Items": [dict(v) for v in self.items.values() if matches(v)]}


class FakeDynamoDBResource:
    def __init__(self):
        self._tables: dict[str, FakeTable] = {}

    def Table(self, name: str) -> FakeTable:
        if name not in self._tables:
            key_name = {
                os.environ["REVIEW_SUBMISSIONS_TABLE"]: "idempotency_key",
                os.environ["REVIEWS_TABLE"]: "review_id",
                os.environ["SEMAPHORE_TABLE"]: "lock_name",
                os.environ.get("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test"): "spend_date",
            }.get(name, "id")
            self._tables[name] = FakeTable(key_name)
        return self._tables[name]


class ExecutionAlreadyExists(Exception):
    pass


class FakeSfnExceptions:
    ExecutionAlreadyExists = ExecutionAlreadyExists


class FakeSfnClient:
    """Fake Step Functions client covering start_execution (dedup by name,
    like the real service) and describe_execution (returns a status queued
    per-ARN by the test)."""

    def __init__(self):
        self.exceptions = FakeSfnExceptions()
        self.started_names: set[str] = set()
        self.start_execution_calls: list[dict] = []
        self.describe_execution_statuses: dict[str, str] = {}

    def start_execution(self, stateMachineArn, name, input):
        self.start_execution_calls.append({"name": name, "input": input})
        if name in self.started_names:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started_names.add(name)
        return {"executionArn": f"{stateMachineArn.replace(':stateMachine:', ':execution:')}:{name}"}

    def describe_execution(self, executionArn):
        status = self.describe_execution_statuses.get(executionArn, "RUNNING")
        return {"executionArn": executionArn, "status": status}


class Boto3Stub:
    """Stand-in for the `boto3` module used inside handler.py's `_ddb()` /
    `_sfn()` factories, so every call returns the SAME fake resource/client
    instance for the duration of a test."""

    def __init__(self, ddb: FakeDynamoDBResource, sfn: FakeSfnClient):
        self._ddb = ddb
        self._sfn = sfn

    def resource(self, _service_name):
        return self._ddb

    def client(self, _service_name):
        return self._sfn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeadExecutionReconciliation(unittest.TestCase):
    """Finding 1: dead-execution reconciliation must not be dead code --
    execution_arn must actually be discoverable on the reviews row."""

    def setUp(self):
        self.ddb = FakeDynamoDBResource()
        self.sfn = FakeSfnClient()
        self._orig_boto3 = _handler_module.boto3
        _handler_module.boto3 = Boto3Stub(self.ddb, self.sfn)

    def tearDown(self):
        _handler_module.boto3 = self._orig_boto3

    def _seed_running_review_with_arn(self, review_id: str, execution_arn: str,
                                       reservation_id: str = "res-1",
                                       idempotency_key: str = "idem-1") -> None:
        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        reviews_table.items[review_id] = {
            "review_id": review_id,
            "status": "RUNNING",
            "execution_arn": execution_arn,
        }
        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        submissions_table.items[idempotency_key] = {
            "idempotency_key": idempotency_key,
            "review_id": review_id,
            "execution_arn": execution_arn,
            "spend_reservation_id": reservation_id,
        }
        semaphore_table = self.ddb.Table(os.environ["SEMAPHORE_TABLE"])
        semaphore_table.items[f"review-slot#{review_id}"] = {
            "lock_name": f"review-slot#{review_id}",
        }

    def test_failed_execution_transitions_review_to_error_and_releases(self):
        review_id = "review-dead-1"
        execution_arn = (
            "arn:aws:states:us-east-1:123456789012:execution:contract-toaster-test:review-dead-1"
        )
        self._seed_running_review_with_arn(review_id, execution_arn)
        self.sfn.describe_execution_statuses[execution_arn] = "FAILED"

        # Issue #189: seed today's daily_spend row with the worst-case
        # reservation this dead review would have taken, so we can prove
        # the credit-back actually happens (not just a flag flip).
        reservation_cents = _handler_module.compute_worst_case_reservation_usd_cents()
        spend_date = time.strftime("%Y-%m-%d", time.gmtime())
        daily_spend_table = self.ddb.Table(os.environ["DAILY_SPEND_TABLE"])
        daily_spend_table.items[spend_date] = {
            "spend_date": spend_date,
            "reserved_usd_cents": reservation_cents,
            "daily_cap_usd_cents": 2000,
        }

        resolved = _handler_module._reconcile_dead_executions()

        self.assertEqual(resolved, [review_id])

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        self.assertEqual(reviews_table.items[review_id]["status"], "ERROR")

        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        self.assertTrue(
            submissions_table.items["idem-1"]["reservation_released"],
            "Spend reservation must be released on the dead-execution path.",
        )

        # Issue #189: the credit-back must actually land on daily_spend,
        # not just flip a flag on the submission row -- otherwise the
        # reservation is held (accumulating toward the cap) until UTC
        # midnight regardless of how many reviews die.
        self.assertEqual(
            daily_spend_table.items[spend_date]["reserved_usd_cents"],
            0,
            "Dead-execution reconciliation must credit the full worst-case "
            "reservation back to daily_spend.reserved_usd_cents (issue #189), "
            "not merely flag it as released.",
        )
        self.assertEqual(daily_spend_table.items[spend_date]["settled_usd_cents"], 0)

        semaphore_table = self.ddb.Table(os.environ["SEMAPHORE_TABLE"])
        self.assertNotIn(
            f"review-slot#{review_id}",
            semaphore_table.items,
            "Concurrency-semaphore slot must be reclaimed on the dead-execution path.",
        )

    def test_release_reservation_is_idempotent_against_double_credit(self):
        """If a submission's reservation was already released (e.g. the
        persist stage's normal-completion settlement raced this path),
        _release_reservation must be a no-op -- crediting daily_spend twice
        for the same reservation would corrupt the ledger."""
        review_id = "review-dead-already-released"
        execution_arn = (
            "arn:aws:states:us-east-1:123456789012:execution:contract-toaster-test:"
            "review-dead-already-released"
        )
        self._seed_running_review_with_arn(
            review_id, execution_arn, reservation_id="res-already", idempotency_key="idem-already"
        )
        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        submissions_table.items["idem-already"]["reservation_released"] = True
        self.sfn.describe_execution_statuses[execution_arn] = "FAILED"

        spend_date = time.strftime("%Y-%m-%d", time.gmtime())
        daily_spend_table = self.ddb.Table(os.environ["DAILY_SPEND_TABLE"])
        daily_spend_table.items[spend_date] = {
            "spend_date": spend_date,
            "reserved_usd_cents": 500,
            "daily_cap_usd_cents": 2000,
        }

        _handler_module._reconcile_dead_executions()

        self.assertEqual(
            daily_spend_table.items[spend_date]["reserved_usd_cents"],
            500,
            "Already-released reservations must not be credited to daily_spend again.",
        )

    def test_timed_out_and_aborted_also_resolve(self):
        for status_value, suffix in (("TIMED_OUT", "2"), ("ABORTED", "3")):
            review_id = f"review-dead-{suffix}"
            execution_arn = (
                f"arn:aws:states:us-east-1:123456789012:execution:contract-toaster-test:{review_id}"
            )
            self._seed_running_review_with_arn(
                review_id, execution_arn,
                reservation_id=f"res-{suffix}", idempotency_key=f"idem-{suffix}",
            )
            self.sfn.describe_execution_statuses[execution_arn] = status_value

            resolved = _handler_module._reconcile_dead_executions()
            self.assertIn(review_id, resolved)
            reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
            self.assertEqual(reviews_table.items[review_id]["status"], "ERROR")

    def test_running_execution_still_running_is_left_alone(self):
        review_id = "review-alive-1"
        execution_arn = (
            "arn:aws:states:us-east-1:123456789012:execution:contract-toaster-test:review-alive-1"
        )
        self._seed_running_review_with_arn(review_id, execution_arn)
        self.sfn.describe_execution_statuses[execution_arn] = "RUNNING"

        resolved = _handler_module._reconcile_dead_executions()

        self.assertEqual(resolved, [])
        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        self.assertEqual(reviews_table.items[review_id]["status"], "RUNNING")

    def test_execution_arn_written_by_reviews_module_is_discoverable(self):
        """End-to-end proof that the write path (backend/src/reviews.py's
        ensure_execution_started) and the read path (this reconciler's scan
        filter) now agree on where execution_arn lives: on the reviews row,
        not only on the submission row."""
        backend_src = REPO_ROOT / "backend" / "src"
        if str(backend_src) not in sys.path:
            sys.path.insert(0, str(backend_src))

        # reviews.py expects these exact env var names; already set above.
        import importlib

        import reviews as reviews_module

        importlib.reload(reviews_module)  # ensure a clean import under our env

        ddb = FakeDynamoDBResourceForReviews()
        sfn = FakeSfnClientForReviews()

        result = reviews_module.submit_review(
            owner_sub="owner-dead-exec",
            playbook_id="eiaa",
            file_sha256="filehash-deadexec",
            upload_pointer="uploads/owner-dead-exec/in.docx",
            active_release_bundle_hash="bundle-hash-v1",
            dynamodb_resource=ddb,
            sfn_client=sfn,
        )
        review_id = result["review_id"]

        reviews_table = ddb.Table(os.environ["REVIEWS_TABLE"])
        review_row = reviews_table.items[review_id]
        self.assertIsNotNone(
            review_row.get("execution_arn"),
            "reviews.py must write execution_arn onto the reviews row so the "
            "orphan reconciler's dead-execution scan can find it.",
        )


# Minimal duplicate fakes for exercising backend/src/reviews.py directly from
# this file (kept separate from the handler-focused FakeTable above, which
# models handler.py's own UpdateExpressions/FilterExpressions).
class FakeTableForReviews:
    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        key = Key[self.key_name]
        item = self.items.get(key)
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item[self.key_name]
        if ConditionExpression == "attribute_not_exists(idempotency_key)" and key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                     ConditionExpression=None, ExpressionAttributeNames=None):
        key = Key[self.key_name]
        item = self.items.setdefault(key, dict(Key))
        vals = ExpressionAttributeValues or {}

        if "reserved_usd_cents = if_not_exists" in UpdateExpression:
            current = item.get("reserved_usd_cents", 0)
            cap = item.get("daily_cap_usd_cents", vals.get(":cap"))
            amount = vals[":amount"]
            if ConditionExpression and current + amount > cap:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
            item["reserved_usd_cents"] = current + amount
            item.setdefault("daily_cap_usd_cents", vals.get(":cap"))
            return

        if "execution_arn = :arn" in UpdateExpression:
            item["execution_arn"] = vals[":arn"]
            if ":status" in vals:
                item["execution_status"] = vals[":status"]
            return

        if "spend_reservation_id = :rid" in UpdateExpression:
            item["spend_reservation_id"] = vals[":rid"]
            return


class FakeDynamoDBResourceForReviews:
    def __init__(self):
        self._tables: dict[str, FakeTableForReviews] = {}

    def Table(self, name: str) -> FakeTableForReviews:
        if name not in self._tables:
            key_name = {
                os.environ["REVIEW_SUBMISSIONS_TABLE"]: "idempotency_key",
                os.environ["REVIEWS_TABLE"]: "review_id",
                os.environ.get("DAILY_SPEND_TABLE", "daily-spend-test"): "spend_date",
            }.get(name, "id")
            self._tables[name] = FakeTableForReviews(key_name)
        return self._tables[name]


class FakeSfnClientForReviews:
    def __init__(self):
        self.exceptions = FakeSfnExceptions()
        self.started_names: set[str] = set()

    def start_execution(self, stateMachineArn, name, input):
        if name in self.started_names:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started_names.add(name)
        return {"executionArn": f"{stateMachineArn.replace(':stateMachine:', ':execution:')}:{name}"}


os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")


class TestArnlessRedriveGetsWellFormedInput(unittest.TestCase):
    """Finding 2: a re-driven, ARN-less submission must be started with the
    persisted pointer-only execution_input, never an empty "{}"."""

    def setUp(self):
        self.ddb = FakeDynamoDBResource()
        self.sfn = FakeSfnClient()
        self._orig_boto3 = _handler_module.boto3
        _handler_module.boto3 = Boto3Stub(self.ddb, self.sfn)

    def tearDown(self):
        _handler_module.boto3 = self._orig_boto3

    def test_redrive_uses_stored_execution_input(self):
        review_id = "review-redrive-1"
        stored_input = json.dumps(
            {
                "review_id": review_id,
                "owner_sub": "owner-redrive",
                "playbook_id": "eiaa",
                "upload_s3_key": "uploads/owner-redrive/in.docx",
                "release_bundle_hash": "bundle-hash-v1",
            }
        )
        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        stale_created_at = 0  # epoch 0 is always "old enough"
        submissions_table.items["idem-redrive-1"] = {
            "idempotency_key": "idem-redrive-1",
            "review_id": review_id,
            "execution_name": f"review-{review_id}",
            "execution_input": stored_input,
            "created_at": stale_created_at,
        }

        redriven = _handler_module._reconcile_arnless_submissions()

        self.assertEqual(redriven, ["idem-redrive-1"])
        self.assertEqual(len(self.sfn.start_execution_calls), 1)
        sent_input = json.loads(self.sfn.start_execution_calls[0]["input"])
        self.assertEqual(sent_input["review_id"], review_id)
        self.assertEqual(sent_input["playbook_id"], "eiaa")
        self.assertEqual(sent_input["upload_s3_key"], "uploads/owner-redrive/in.docx")
        self.assertNotEqual(
            self.sfn.start_execution_calls[0]["input"], "{}",
            "Re-driven execution must not be started with an empty payload.",
        )

        # The re-drive must also stamp execution_arn onto the reviews row so
        # the dead-execution scan can find it later.
        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        self.assertIn(review_id, reviews_table.items)
        self.assertIsNotNone(reviews_table.items[review_id]["execution_arn"])

    def test_submission_without_stored_input_is_skipped_not_redriven_empty(self):
        """A legacy/malformed row with no execution_input must be skipped,
        not silently re-driven with "{}"."""
        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        submissions_table.items["idem-legacy-1"] = {
            "idempotency_key": "idem-legacy-1",
            "review_id": "review-legacy-1",
            "execution_name": "review-review-legacy-1",
            "created_at": 0,
            # no execution_input key at all
        }

        redriven = _handler_module._reconcile_arnless_submissions()

        self.assertEqual(redriven, [])
        self.assertEqual(
            len(self.sfn.start_execution_calls), 0,
            "Must not call StartExecution for a submission with no stored execution_input.",
        )


if __name__ == "__main__":
    unittest.main()
