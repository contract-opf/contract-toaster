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
      ARCHITECTURE.md's documented $2.46 worst-case/review (issue #625
      raised MAX_INPUT_TOKENS to 100_000; it was $2.11 at 80_000) -- NOT the
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
      per-model fix, $2.11 -- $2.46 since issue #625) reserved against the
      day's cap PERMANENTLY.

The `review_submissions` table is a REAL (moto) table built by
`tests/ddb_fixtures.py::create_submissions_table`: issue #67 deleted the
duck-typed scan fallback both Lambda handlers took when a table had no
`.query()`, so their keyed `review_id-index` lookup now needs a table that
HAS that index. The daily_spend / reviews / semaphore tables stay in-memory
stand-ins -- nothing in that removal touches them, and moto 5.2.2 cannot
parse the reservation's atomic ConditionExpression.

Run with: python3 tests/test_spend_reservation_settlement.py
Exit 0 = all tests pass; non-zero = one or more tests failed (or the
concern reproduces).
"""

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = REPO_ROOT / "backend" / "src"
MODEL_POLICY_PATH = REPO_ROOT / "model-policy" / "bedrock-us-east-1.json"
PERSIST_HANDLER_PATH = REPO_ROOT / "infra" / "lambda" / "persist" / "handler.py"
RECONCILER_HANDLER_PATH = REPO_ROOT / "infra" / "lambda" / "orphan_reconciler" / "handler.py"

TESTS_DIR = REPO_ROOT / "tests"

for _path in (str(BACKEND_SRC), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ---------------------------------------------------------------------------
# Real third-party dependencies.
#
# Issue #67: this file used to inject a bare `types.ModuleType("boto3")`
# stub. The Lambda handlers under test now do `from boto3.dynamodb.conditions
# import Key` for their keyed `review_id-index` lookup -- there is no
# scan+filter fallback left for a stand-in without `.query()` -- and that
# import cannot resolve against a stub module. Real boto3 (with moto
# intercepting it) and real fastapi/botocore are in requirements-dev.txt.
# ---------------------------------------------------------------------------

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
# moto needs a region and (fake) credentials for boto3.resource("dynamodb").
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

import config as _config_module  # noqa: E402
import reviews as _reviews_module  # noqa: E402

from ddb_fixtures import create_submissions_table  # noqa: E402


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
    def __init__(self, submissions_table=None):
        self._tables: dict[str, FakeTable] = {}
        # Issue #67: the REAL (moto) review_submissions table, when the test
        # supplies one. Both Lambda handlers look a submission up through the
        # `review_id-index` GSI with no scan fallback left, so a stand-in
        # without that index cannot stand in for it any more.
        if submissions_table is not None:
            self._tables[os.environ["REVIEW_SUBMISSIONS_TABLE"]] = submissions_table

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


class MotoSubmissionsMixin:
    """A REAL (moto) `review_submissions` table per test, wired into
    `FakeDynamoDBResource` (issue #67 -- see this module's docstring)."""

    def setUp(self) -> None:  # noqa: N802 - unittest API
        super().setUp()
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.addCleanup(self._mock_aws.stop)
        self.boto_ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.submissions_table = create_submissions_table(self.boto_ddb)

    def seed_submission(self, submission: dict) -> None:
        """The shape `reviews.submit_review` writes: an idempotency_key PK
        plus the review_id the `review_id-index` GSI keys on."""
        self.submissions_table.put_item(Item=dict(submission))

    def submission_row(self, idempotency_key: str) -> dict:
        return (
            self.submissions_table.get_item(
                Key={"idempotency_key": idempotency_key}
            ).get("Item")
            or {}
        )


# ---------------------------------------------------------------------------
# (a) Reservation formula: per-model rates, matches ARCHITECTURE.md's $2.46
# ---------------------------------------------------------------------------

class TestReservationFormulaMatchesDocumentedWorstCase(unittest.TestCase):
    def test_reservation_is_686_cents_not_the_blended_rate_figure(self):
        """Issue #189: the pre-fix formula applied a single blended 'Opus
        output' rate to ALL tokens, reserving 4.6x the documented worst case
        and 429'ing the third review of any day against the $20/day cap.
        ARCHITECTURE.md -> Cost shape now documents $6.86 (686 cents) at
        MAX_INPUT_TOKENS=100_000 (issue #625) and the issue-#658 worst-case
        output budget of 32_000 over three attempts per pass (it was $2.46
        at a flat 8_000 output over two attempts)."""
        cents = _reviews_module.compute_worst_case_reservation_usd_cents()
        self.assertEqual(cents, 686, "Must match ARCHITECTURE.md's $6.86 worst-case/review.")
        blended = int(round(
            (1 + _reviews_module.MAX_RETRIES_PER_PASS
             + _reviews_module.MAX_TRUNCATION_RETRIES_PER_PASS)
            * 2
            * (_reviews_module.MAX_INPUT_TOKENS + _reviews_module.MAX_OUTPUT_TOKENS)
            * _reviews_module.PRIMARY_OUTPUT_RATE_USD_PER_MILLION
            / 1_000_000
            * 100
        ))
        self.assertNotEqual(
            cents, blended,
            "Must NOT reproduce the pre-fix single-blended-rate reservation.",
        )

    def test_the_deleted_repair_flag_no_longer_moves_the_reservation(self):
        """Issue #628 deleted the bounded address-repair pass, its module and
        its `REQUOTE_ENABLED` flag, so `compute_worst_case_reservation_usd_
        cents()` is once again exactly `attempts_per_pass * (primary +
        critic)`.

        Issue #569 had added `if config.requote_enabled(): usd += primary_usd`
        here, reserving for a third model call. Setting the env var must now
        change nothing at all -- if someone re-adds an env-gated term to this
        function, this fails, and the flag-off baseline below is the same
        documented worst case that branch was measured against.

        The flag is also asserted GONE from `backend/src/config.py`: a
        reservation that ignores the var while the accessor survives would
        leave a live-looking switch wired to nothing.
        """
        baseline_cents = _reviews_module.compute_worst_case_reservation_usd_cents()
        self.assertEqual(baseline_cents, 686, "Flag-off baseline is the documented $6.86.")

        with patch.dict(os.environ, {"REQUOTE_ENABLED": "1"}):
            cents_with_flag_set = _reviews_module.compute_worst_case_reservation_usd_cents()
        self.assertEqual(
            cents_with_flag_set,
            baseline_cents,
            "REQUOTE_ENABLED must no longer move the reservation -- the pass "
            "it reserved for was deleted by issue #628.",
        )

        self.assertFalse(
            hasattr(_config_module, "requote_enabled"),
            "backend/src/config.py must not still expose requote_enabled().",
        )

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
            "MAX_TRUNCATION_RETRIES_PER_PASS",
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

        self.assertEqual(_persist_module.compute_worst_case_reservation_usd_cents(), 686)
        self.assertEqual(_reconciler_module.compute_worst_case_reservation_usd_cents(), 686)

    def test_reservation_parity_across_all_three_copies(self):
        """Issue #569 fix round 2, finding 2: the parity test above only
        compares the mirrored rate CONSTANTS and the mirrors' hardcoded
        worst-case baseline -- it never calls
        `_reviews_module.compute_worst_case_reservation_usd_cents()` and
        compares it against the persist/orphan_reconciler mirrors' own
        `compute_worst_case_reservation_usd_cents()`. That let reviews.py's
        reservation drift from the two Lambda mirrors without failing CI:
        reserve (reviews.py) and settle (the persist-stage /
        orphan-reconciler mirrors) would permanently disagree on every
        review, leaking that difference from
        `daily_spend.reserved_usd_cents` forever.

        Issue #628 removed the env-gated third-pass term from all three
        copies in one commit, so the parity assertion no longer has a flag
        dimension -- but it keeps ONE: `REQUOTE_ENABLED=1` must leave all
        three unmoved, which is what proves the term is gone from the
        MIRRORS too and not merely from reviews.py.
        """
        reviews_off = _reviews_module.compute_worst_case_reservation_usd_cents()
        persist_off = _persist_module.compute_worst_case_reservation_usd_cents()
        reconciler_off = _reconciler_module.compute_worst_case_reservation_usd_cents()
        self.assertEqual(reviews_off, 686)
        self.assertEqual(persist_off, 686)
        self.assertEqual(reconciler_off, 686)

        with patch.dict(os.environ, {"REQUOTE_ENABLED": "1"}):
            reviews_on = _reviews_module.compute_worst_case_reservation_usd_cents()
            persist_on = _persist_module.compute_worst_case_reservation_usd_cents()
            reconciler_on = _reconciler_module.compute_worst_case_reservation_usd_cents()

        self.assertEqual(
            (reviews_on, persist_on, reconciler_on),
            (reviews_off, persist_off, reconciler_off),
            "No copy may still read REQUOTE_ENABLED -- issue #628 deleted the "
            "pass it reserved for, and a mirror that still adds the term "
            "would leak the difference from daily_spend.reserved_usd_cents.",
        )


# ---------------------------------------------------------------------------
# (b) settle_spend() has a real caller and decrements daily_spend
# ---------------------------------------------------------------------------

class TestSettleSpendDecrementsDailySpend(MotoSubmissionsMixin, unittest.TestCase):
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
        ddb = FakeDynamoDBResource(self.submissions_table)
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
        self.seed_submission({
            "idempotency_key": "idem-persist-1",
            "review_id": review_id,
            "spend_reservation_id": "res-persist-1",
        })

        event = {
            "review_id": review_id,
            "decision": "REQUEST_CHANGE",
            "reason": None,
            "output_s3_key": f"outputs/{review_id}/out.docx",
            "summary": "mock",
        }
        result = _persist_module.handler(dict(event))

        self.assertEqual(
            daily_spend_table.items[spend_date]["reserved_usd_cents"],
            0,
            "The persist stage must settle (credit back) a completed "
            "review's worst-case reservation, not leave it held.",
        )
        self.assertTrue(self.submission_row("idem-persist-1")["reservation_released"])
        # Pass-through contract: the event is returned unchanged (plus the
        # settlement side effect), same as every other Phase-0 stage stub.
        self.assertEqual(result, event)

    def test_persist_stage_is_idempotent_against_double_settlement(self):
        """Calling the persist stage twice for the same review (e.g. a
        Step Functions task retry) must not credit daily_spend twice."""
        ddb = FakeDynamoDBResource(self.submissions_table)
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
        self.seed_submission({
            "idempotency_key": "idem-persist-2",
            "review_id": review_id,
            "spend_reservation_id": "res-persist-2",
        })

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

class TestOrphanReconcilerCreditsDailySpend(MotoSubmissionsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ddb = FakeDynamoDBResource(self.submissions_table)
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
        self.seed_submission({
            "idempotency_key": "idem-dead-1",
            "review_id": review_id,
            "execution_arn": execution_arn,
            "spend_reservation_id": "res-dead-1",
        })

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
        self.assertTrue(self.submission_row("idem-dead-1")["reservation_released"])
        self.assertEqual(
            daily_spend_table.items[spend_date]["reserved_usd_cents"],
            0,
            "Dead-execution reconciliation must credit the reservation back "
            "to daily_spend.reserved_usd_cents, not just flip a flag.",
        )


if __name__ == "__main__":
    unittest.main()
