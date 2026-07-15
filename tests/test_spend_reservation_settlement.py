#!/usr/bin/env python3
"""
TDD slice test for issue #189: "Daily spend guardrail will kill a live demo
after 2 reviews: reservation is 4.6x the documented figure and is never
settled or released."

Reproduces the three concerns cited in the issue's Evidence section and
proves the fix:

  (a) compute_worst_case_reservation_usd_cents() must price EACH pass at
      its OWN model's rate (per-model input/output rates mirroring
      model-policy/bedrock-us-east-1.json's base rates + the ~10% regional
      premium documented in docs/design-notes.md) and must match
      ARCHITECTURE.md's documented $2.11 worst-case/review -- NOT the
      pre-fix $9.68 (a single blended "Opus output" rate applied to every
      token of every pass, backend/src/reviews.py:70-81,273-295 as filed).
      Also cross-checks that the per-model rate constants are numerically
      identical across backend/src/reviews.py, infra/lambda/persist/
      handler.py, and infra/lambda/orphan_reconciler/handler.py (three
      self-contained deployables that cannot import a shared module -- see
      each file's module docstring) so a change to one that is not
      mirrored to the others fails CI rather than silently drifting.

  (b) settle_spend() must have a real caller on the pipeline's
      persist/finally path (backend/src/reviews.py:387-418 was cited as
      "zero callers per grep"). Proven two ways: a direct unit test of
      reviews.settle_spend() itself, and an end-to-end test of
      infra/lambda/persist/handler.py (the new persist-stage Lambda) that
      proves invoking it actually decrements daily_spend.reserved_usd_cents
      for a completed review's reservation, not just PENDING at $0 activity.

  (c) The orphan reconciler's dead-execution path
      (infra/lambda/orphan_reconciler/handler.py:68-88) must credit
      daily_spend.reserved_usd_cents for real, not merely set a
      `reservation_released` flag that nothing downstream ever consumes --
      the pre-fix behavior left a dead review's $9.68 (or, post the
      per-model fix, $2.11) reserved against the day's cap PERMANENTLY.

These are unit tests against in-memory DynamoDB fakes (no live AWS, no
moto/boto3 dependency required) -- same third-party-stubbing convention as
tests/test_review_submission_e2e.py and tests/test_orphan_reconciler_e2e.py.

Run with: python3 tests/test_spend_reservation_settlement.py
Exit 0 = all tests pass; non-zero = one or more tests failed (or the
concern reproduces).
"""

import importlib.util
import json
import sys
import time
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = REPO_ROOT / "backend" / "src"
MODEL_POLICY_PATH = REPO_ROOT / "model-policy" / "bedrock-us-east-1.json"
PERSIST_HANDLER_PATH = REPO_ROOT / "infra" / "lambda" / "persist" / "handler.py"
RECONCILER_HANDLER_PATH = REPO_ROOT / "infra" / "lambda" / "orphan_reconciler" / "handler.py"

if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


# ---------------------------------------------------------------------------
# Third-party stubs (no live boto3/fastapi dependency required in CI)
# ---------------------------------------------------------------------------

def _stub_third_party() -> None:
    if "fastapi" not in sys.modules:
        fastapi_mod = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail: str = "") -> None:
                self.status_code = status_code
                self.detail = detail
                super().__init__(detail)

        class status:  # noqa: N801
            HTTP_202_ACCEPTED = 202
            HTTP_404_NOT_FOUND = 404
            HTTP_409_CONFLICT = 409
            HTTP_429_TOO_MANY_REQUESTS = 429
            HTTP_503_SERVICE_UNAVAILABLE = 503

        fastapi_mod.HTTPException = HTTPException
        fastapi_mod.status = status
        sys.modules["fastapi"] = fastapi_mod

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

        def _unset(*_a, **_kw):
            raise AssertionError(
                "boto3.resource()/client() called without being patched by "
                "the test -- monkeypatch <module>.boto3 first."
            )

        boto3_mod.resource = _unset
        boto3_mod.client = _unset
        sys.modules["boto3"] = boto3_mod


_stub_third_party()

import os  # noqa: E402

os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("SEMAPHORE_TABLE", "contract-toaster-semaphore-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)
os.environ.setdefault("STALE_PENDING_THRESHOLD_SECONDS", "120")

import reviews as _reviews_module  # noqa: E402

ClientError = sys.modules["botocore.exceptions"].ClientError


def _load_module(path: Path, module_name: str):
    """Load infra/lambda/{persist,orphan_reconciler}/handler.py under
    distinct module names -- both files are literally named `handler.py`
    (each is its own self-contained Lambda deployment asset), so a plain
    `sys.path.insert` + `import handler` for both in the same process would
    collide on sys.modules["handler"]."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_persist_module = _load_module(PERSIST_HANDLER_PATH, "_persist_handler_under_test")
_reconciler_module = _load_module(RECONCILER_HANDLER_PATH, "_reconciler_handler_under_test")


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------

class FakeTable:
    """Purpose-built interpreter for the specific UpdateExpressions/
    FilterExpressions exercised by reviews.py, persist/handler.py, and
    orphan_reconciler/handler.py in this file -- not a general DynamoDB
    expression engine."""

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
        vals = ExpressionAttributeValues or {}

        if ConditionExpression == "#status IN (:pending, :running)":
            item = self.items.get(key)
            current_status = item.get("status") if item else None
            if current_status not in (vals.get(":pending"), vals.get(":running")):
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

        item = self.items.setdefault(key, dict(Key))

        if "reserved_usd_cents = if_not_exists" in UpdateExpression:
            current = item.get("reserved_usd_cents", 0)
            cap = item.get("daily_cap_usd_cents", vals.get(":cap"))
            amount = vals[":amount"]
            if ConditionExpression and current + amount > cap:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
            item["reserved_usd_cents"] = current + amount
            item.setdefault("daily_cap_usd_cents", vals.get(":cap"))
            return

        if "reserved_usd_cents = reserved_usd_cents + :delta" in UpdateExpression:
            # settle_spend()'s daily_spend update (issue #189, concern b/c).
            item["reserved_usd_cents"] = item.get("reserved_usd_cents", 0) + vals[":delta"]
            item["settled_usd_cents"] = item.get("settled_usd_cents", 0) + vals[":actual"]
            return

        if "spend_reservation_id = :rid" in UpdateExpression:
            item["spend_reservation_id"] = vals[":rid"]
            return

        if "reservation_released = :true" in UpdateExpression:
            item["reservation_released"] = vals[":true"]
            return

        if "execution_arn = :arn" in UpdateExpression:
            item["execution_arn"] = vals[":arn"]
            if ":status" in vals:
                item["execution_status"] = vals[":status"]
            return

        if "#status = :error" in UpdateExpression:
            item["status"] = vals[":error"]
            item["failing_stage"] = vals[":stage"]
            item["error_reason"] = vals[":reason"]
            return

        # Generic fallback: no-op for anything else exercised indirectly.

    def delete_item(self, Key):
        self.items.pop(Key[self.key_name], None)

    def scan(self, FilterExpression=None, ExpressionAttributeNames=None,
              ExpressionAttributeValues=None):
        vals = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}
        status_attr = names.get("#status", "status")

        def matches(item: dict) -> bool:
            if FilterExpression is None:
                return True
            if FilterExpression == "review_id = :rid":
                return item.get("review_id") == vals.get(":rid")
            if FilterExpression == "attribute_not_exists(execution_arn)":
                return "execution_arn" not in item or item["execution_arn"] is None
            if FilterExpression == (
                "#status IN (:pending, :running) AND attribute_exists(execution_arn)"
            ):
                return (
                    item.get(status_attr) in (vals.get(":pending"), vals.get(":running"))
                    and item.get("execution_arn") is not None
                )
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
                os.environ["DAILY_SPEND_TABLE"]: "spend_date",
                os.environ["SEMAPHORE_TABLE"]: "lock_name",
            }.get(name, "id")
            self._tables[name] = FakeTable(key_name)
        return self._tables[name]


class ExecutionAlreadyExists(Exception):
    pass


class FakeSfnExceptions:
    ExecutionAlreadyExists = ExecutionAlreadyExists


class FakeSfnClient:
    def __init__(self):
        self.exceptions = FakeSfnExceptions()

    def describe_execution(self, executionArn):
        return {"executionArn": executionArn, "status": self.status}


class Boto3Stub:
    """Stand-in for the `boto3` module used inside handler.py's `_ddb()`."""

    def __init__(self, ddb, sfn=None):
        self._ddb = ddb
        self._sfn = sfn

    def resource(self, _service_name):
        return self._ddb

    def client(self, _service_name):
        return self._sfn


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


# ---------------------------------------------------------------------------
# (a) Reservation formula: per-model rates, matches ARCHITECTURE.md's $2.11
# ---------------------------------------------------------------------------

class TestReservationFormulaMatchesDocumentedWorstCase(unittest.TestCase):
    def test_reservation_is_211_cents_not_968(self):
        """Issue #189: the pre-fix formula reserved $9.68 (968 cents) --
        4.6x ARCHITECTURE.md's documented $2.11 (211 cents) worst case --
        which 429'd the third review of any day against the $20/day cap."""
        cents = _reviews_module.compute_worst_case_reservation_usd_cents()
        self.assertEqual(cents, 211, "Must match ARCHITECTURE.md's $2.11 worst-case/review.")
        self.assertNotEqual(cents, 968, "Must NOT reproduce the pre-fix $9.68 reservation.")

    def test_rates_mirror_model_policy_base_rates_times_regional_premium(self):
        """'so code and policy cannot drift' (issue #189 suggested
        direction): reviews.py's hardcoded per-model rates must equal
        model-policy/bedrock-us-east-1.json's cost_per_million_*_usd base
        rates times the documented regional premium -- a policy change that
        isn't mirrored here must fail this test, not silently drift."""
        with open(MODEL_POLICY_PATH, encoding="utf-8") as f:
            policy = json.load(f)

        primary = policy["models"]["primary"]
        critic = policy["models"]["critic"]
        premium = _reviews_module.REGIONAL_PRICING_PREMIUM

        self.assertAlmostEqual(
            _reviews_module.PRIMARY_INPUT_RATE_USD_PER_MILLION,
            primary["cost_per_million_input_usd"] * premium,
        )
        self.assertAlmostEqual(
            _reviews_module.PRIMARY_OUTPUT_RATE_USD_PER_MILLION,
            primary["cost_per_million_output_usd"] * premium,
        )
        self.assertAlmostEqual(
            _reviews_module.CRITIC_INPUT_RATE_USD_PER_MILLION,
            critic["cost_per_million_input_usd"] * premium,
        )
        self.assertAlmostEqual(
            _reviews_module.CRITIC_OUTPUT_RATE_USD_PER_MILLION,
            critic["cost_per_million_output_usd"] * premium,
        )

    def test_rate_constants_identical_across_all_three_self_contained_copies(self):
        """backend/src/reviews.py, infra/lambda/persist/handler.py, and
        infra/lambda/orphan_reconciler/handler.py each ship as separate
        deployables and cannot import a shared module (see each file's
        module docstring) -- their mirrored copies of the cost-model
        constants must stay numerically identical."""
        rate_constants = [
            "MAX_INPUT_TOKENS",
            "MAX_OUTPUT_TOKENS",
            "MAX_RETRIES_PER_PASS",
            "REGIONAL_PRICING_PREMIUM",
            "PRIMARY_INPUT_RATE_USD_PER_MILLION",
            "PRIMARY_OUTPUT_RATE_USD_PER_MILLION",
            "CRITIC_INPUT_RATE_USD_PER_MILLION",
            "CRITIC_OUTPUT_RATE_USD_PER_MILLION",
        ]
        for const_name in rate_constants:
            reviews_value = getattr(_reviews_module, const_name)
            persist_value = getattr(_persist_module, const_name)
            reconciler_value = getattr(_reconciler_module, const_name)
            self.assertEqual(
                reviews_value, persist_value,
                f"{const_name}: reviews.py={reviews_value!r} != persist/handler.py={persist_value!r}",
            )
            self.assertEqual(
                reviews_value, reconciler_value,
                f"{const_name}: reviews.py={reviews_value!r} != "
                f"orphan_reconciler/handler.py={reconciler_value!r}",
            )

        self.assertEqual(_persist_module.compute_worst_case_reservation_usd_cents(), 211)
        self.assertEqual(_reconciler_module.compute_worst_case_reservation_usd_cents(), 211)


# ---------------------------------------------------------------------------
# (b) settle_spend() has a real caller and decrements daily_spend
# ---------------------------------------------------------------------------

class TestSettleSpendDecrementsDailySpend(unittest.TestCase):
    def test_settle_spend_directly_credits_back_unspent_reservation(self):
        """Direct unit test of the canonical settle_spend(): settling a
        review that cost less than its worst-case reservation must credit
        the difference back to today's daily_spend row."""
        ddb = FakeDynamoDBResource()
        spend_date = _today()
        reservation_cents = _reviews_module.compute_worst_case_reservation_usd_cents()
        table = ddb.Table(os.environ["DAILY_SPEND_TABLE"])
        table.items[spend_date] = {
            "spend_date": spend_date,
            "reserved_usd_cents": reservation_cents,
            "daily_cap_usd_cents": 2000,
        }

        actual_cents = 50  # a typical (much cheaper than worst-case) review
        _reviews_module.settle_spend("review-settle-1", "res-1", actual_cents, ddb)

        self.assertEqual(
            table.items[spend_date]["reserved_usd_cents"],
            actual_cents,
            "settle_spend must reverse the worst-case reservation and apply "
            "the actual settled cost (reserved_usd_cents should equal "
            "actual_usd_cents once the worst-case hold is released).",
        )
        self.assertEqual(table.items[spend_date]["settled_usd_cents"], actual_cents)

    def test_persist_stage_settles_a_completed_reviews_reservation(self):
        """infra/lambda/persist/handler.py (issue #189's new persist-stage
        Lambda -- the previously-generic pass-through stub carried no
        settlement logic at all) must settle the reservation for a review
        that reaches this stage, crediting the reservation back to
        daily_spend rather than leaving it held until UTC midnight."""
        ddb = FakeDynamoDBResource()
        self._orig_boto3 = _persist_module.boto3
        _persist_module.boto3 = Boto3Stub(ddb)
        self.addCleanup(lambda: setattr(_persist_module, "boto3", self._orig_boto3))

        review_id = "review-persist-1"
        reservation_cents = _persist_module.compute_worst_case_reservation_usd_cents()
        spend_date = _today()

        daily_spend_table = ddb.Table(os.environ["DAILY_SPEND_TABLE"])
        daily_spend_table.items[spend_date] = {
            "spend_date": spend_date,
            "reserved_usd_cents": reservation_cents,
            "daily_cap_usd_cents": 2000,
        }
        submissions_table = ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        submissions_table.items["idem-persist-1"] = {
            "idempotency_key": "idem-persist-1",
            "review_id": review_id,
            "spend_reservation_id": "res-persist-1",
        }

        event = {
            "review_id": review_id,
            "decision": "REQUEST_CHANGE",
            "reason": None,
            "output_s3_key": f"outputs/{review_id}/out.docx",
            "summary": "mock",
            "watermark": "tool recommendation only - attorney approval required",
        }
        result = _persist_module.handler(dict(event))

        self.assertEqual(
            daily_spend_table.items[spend_date]["reserved_usd_cents"],
            0,
            "The persist stage must settle (credit back) a completed "
            "review's worst-case reservation, not leave it held.",
        )
        self.assertTrue(submissions_table.items["idem-persist-1"]["reservation_released"])
        # Pass-through contract: the event is returned unchanged (plus the
        # settlement side effect), same as every other Phase-0 stage stub.
        self.assertEqual(result, event)

    def test_persist_stage_is_idempotent_against_double_settlement(self):
        """Calling the persist stage twice for the same review (e.g. a
        Step Functions task retry) must not credit daily_spend twice."""
        ddb = FakeDynamoDBResource()
        orig_boto3 = _persist_module.boto3
        _persist_module.boto3 = Boto3Stub(ddb)
        self.addCleanup(lambda: setattr(_persist_module, "boto3", orig_boto3))

        review_id = "review-persist-2"
        reservation_cents = _persist_module.compute_worst_case_reservation_usd_cents()
        spend_date = _today()

        daily_spend_table = ddb.Table(os.environ["DAILY_SPEND_TABLE"])
        daily_spend_table.items[spend_date] = {
            "spend_date": spend_date,
            "reserved_usd_cents": reservation_cents,
            "daily_cap_usd_cents": 2000,
        }
        submissions_table = ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        submissions_table.items["idem-persist-2"] = {
            "idempotency_key": "idem-persist-2",
            "review_id": review_id,
            "spend_reservation_id": "res-persist-2",
        }

        event = {"review_id": review_id}
        _persist_module.handler(dict(event))
        _persist_module.handler(dict(event))  # retry / re-invoke

        self.assertEqual(
            daily_spend_table.items[spend_date]["reserved_usd_cents"],
            0,
            "A second persist-stage invocation for the same review must not "
            "credit daily_spend a second time.",
        )


# ---------------------------------------------------------------------------
# (c) Orphan reconciler's dead-execution path credits daily_spend for real
# ---------------------------------------------------------------------------

class TestOrphanReconcilerCreditsDailySpend(unittest.TestCase):
    def setUp(self):
        self.ddb = FakeDynamoDBResource()
        self.sfn = FakeSfnClient()
        self._orig_boto3 = _reconciler_module.boto3
        _reconciler_module.boto3 = Boto3Stub(self.ddb, self.sfn)

    def tearDown(self):
        _reconciler_module.boto3 = self._orig_boto3

    def test_dead_execution_credits_reserved_usd_cents_not_just_a_flag(self):
        """Issue #189 concern (c): infra/lambda/orphan_reconciler/
        handler.py's _release_reservation() previously only set
        `reservation_released = True` on the submission row and never
        touched daily_spend.reserved_usd_cents -- a dead review's
        reservation was held PERMANENTLY (until UTC midnight) regardless of
        how many reviews actually completed. This proves the real credit
        happens."""
        review_id = "review-dead-settle-1"
        execution_arn = (
            "arn:aws:states:us-east-1:123456789012:execution:contract-toaster-test:"
            f"{review_id}"
        )
        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        reviews_table.items[review_id] = {
            "review_id": review_id,
            "status": "RUNNING",
            "execution_arn": execution_arn,
        }
        submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        submissions_table.items["idem-dead-1"] = {
            "idempotency_key": "idem-dead-1",
            "review_id": review_id,
            "execution_arn": execution_arn,
            "spend_reservation_id": "res-dead-1",
        }

        spend_date = _today()
        reservation_cents = _reconciler_module.compute_worst_case_reservation_usd_cents()
        daily_spend_table = self.ddb.Table(os.environ["DAILY_SPEND_TABLE"])
        daily_spend_table.items[spend_date] = {
            "spend_date": spend_date,
            "reserved_usd_cents": reservation_cents,
            "daily_cap_usd_cents": 2000,
        }

        self.sfn.status = "FAILED"
        resolved = _reconciler_module._reconcile_dead_executions()

        self.assertEqual(resolved, [review_id])
        self.assertTrue(submissions_table.items["idem-dead-1"]["reservation_released"])
        self.assertEqual(
            daily_spend_table.items[spend_date]["reserved_usd_cents"],
            0,
            "Dead-execution reconciliation must credit the reservation back "
            "to daily_spend.reserved_usd_cents, not just flip a flag.",
        )


if __name__ == "__main__":
    unittest.main()
