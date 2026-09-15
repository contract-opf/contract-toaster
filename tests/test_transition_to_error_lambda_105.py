#!/usr/bin/env python3
"""
Executable tests for issue #105: the AWS Step Functions shared Catch target
(`TransitionToError`, infra/lib/nested/pipeline-stack.ts) had four independent
defects, all specific to the AWS target -- the DTS/Docker-Compose path never
runs this Lambda.

  1. NO `DONE` GUARD. `AuditStage` / `ReleaseConcurrencySlot` run their
     `.addCatch` AFTER `PersistStage` has already written `status=DONE`, so a
     failure there overwrote a finished, downloadable review with `ERROR` --
     the #446 clobber, fixed on the in-process writer
     (`reviews.record_stage_failure`'s `ConditionExpression`) and never fixed
     here.
  2. NO `reason` / `failed_at`. The frontend's `explainFailure()` keys its
     copy off `reason` (falling back to `failing_stage` only when `reason` is
     absent or the unclassified token) -- #472's blank-column case reproduced.
  3. `failing_stage` was a single hardcoded `'pipeline'` LITERAL baked into
     ONE shared error-transition Task state that every stage's `.addCatch`
     pointed at -- the `stageName` parameter `withStageErrorHandling` took
     was never actually used. So every failure, regardless of which of the
     nine stages actually raised, recorded the same useless
     `failing_stage='pipeline'`, unknown to `STAGE_EXPLANATIONS`.
  4. `error_reason` persisted `str(event["error"])` -- the FULL Step
     Functions error object, including Lambda's `Cause` JSON (a raw
     exception message and stack trace that can quote document text) --
     onto the reviews row in plaintext.

Structural checks run against the SYNTHESIZED CloudFormation template (cdk
synth; offline, no AWS calls) -- both the state-machine ASL (defect 3: each
stage's own error-transition Task state carries its OWN real stage name, not
a shared 'pipeline' literal) and the errorHandlerFn's inline Lambda source
(defects 1/2/4: ConditionExpression, reason/failed_at, error-name reduction).

Behavioral checks run the REAL inline Lambda source (extracted verbatim from
pipeline-stack.ts, not re-implemented) against moto's real DynamoDB semantics
-- never a hand-rolled fake table, per the #446 test's own rule: a fake that
ignores its `ConditionExpression` cannot prove the DONE guard actually works,
and this is exactly the class of bug (an unconditional update_item) that
shipped through a green suite backed by such a fake.

The READER-FACING half of this issue -- that `reason` is deliberately the
unclassified sentinel, so every stage name this Lambda writes must resolve to
its own `STAGE_EXPLANATIONS` copy, and that the admin incident banner clusters
on the stage rather than on a token every AWS failure shares -- is pinned by
frontend/src/__tests__/aws-catch-failure-rows-105.test.tsx, which reads this
file's stage vocabulary out of pipeline-stack.ts for the same no-drift reason.

Must FAIL on the pre-#105 tree; PASS after the fix.

Run standalone: `python3 tests/test_transition_to_error_lambda_105.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
CDK_OUT = INFRA / "cdk.out"
PIPELINE_STACK_PATH = INFRA / "lib" / "nested" / "pipeline-stack.ts"

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from infra_synth_helper import NEUTRAL_CDK_CONTEXT  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-105")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402, I001
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

from ddb_fixtures import create_reviews_table  # noqa: E402

REVIEW_ID = "00000000-0000-4000-a000-000000000105"

# The nine real pipeline stages -- each must get its OWN error-transition
# Task state carrying its OWN real stage name (defect 3). Keyed by the
# `idSuffix` construct-id fragment -> the `failing_stage` token
# `withStageErrorHandling` is called with in pipeline-stack.ts.
EXPECTED_STAGE_TOKENS = {
    "TransitionToErrorAcquireSemaphoreSlot": "acquire_semaphore_slot",
    "TransitionToErrorMarkRunning": "mark_running",
    "TransitionToErrorExtract": "extract",
    "TransitionToErrorRetrieve": "retrieve",
    "TransitionToErrorPrimaryReviewMock": "primary_review_mock",
    "TransitionToErrorRedline": "redline",
    "TransitionToErrorPersist": "persist",
    "TransitionToErrorAudit": "audit",
    "TransitionToErrorReleaseSemaphoreSlot": "release_semaphore_slot",
}


# ---------------------------------------------------------------------------
# cdk synth + template/ASL extraction helpers.
# ---------------------------------------------------------------------------

def _run_cdk_synth() -> subprocess.CompletedProcess:
    if CDK_OUT.exists():
        shutil.rmtree(CDK_OUT)
    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        install = subprocess.run(
            ["npm", "install"], cwd=INFRA, capture_output=True, text=True,  # noqa: S607
        )
        if install.returncode != 0:
            raise RuntimeError(f"npm install failed: {install.stderr[-500:]}")
    return subprocess.run(  # noqa: S603
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],  # noqa: S607
        cwd=INFRA,
        capture_output=True,
        text=True,
    )


def _load_pipeline_template() -> dict:
    candidates = sorted(CDK_OUT.glob("*Pipeline*.nested.template.json"))
    assert len(candidates) == 1, f"expected exactly one Pipeline nested template, got {candidates}"
    return json.loads(candidates[0].read_text(encoding="utf-8"))


def _state_machine_definition(template: dict) -> dict:
    sm = None
    for _name, resource in template["Resources"].items():
        if resource.get("Type") == "AWS::StepFunctions::StateMachine":
            sm = resource
            break
    assert sm is not None, "no AWS::StepFunctions::StateMachine resource in Pipeline template"
    def_string = sm["Properties"]["DefinitionString"]
    if isinstance(def_string, dict) and "Fn::Join" in def_string:
        parts = def_string["Fn::Join"][1]
        text = "".join(p if isinstance(p, str) else "" for p in parts)
    else:
        text = def_string
    return json.loads(text)


def _find_transition_to_error_function(template: dict) -> dict:
    """The single shared `errorHandlerFn` AWS::Lambda::Function resource."""
    for _logical_id, resource in template["Resources"].items():
        if resource.get("Type") != "AWS::Lambda::Function":
            continue
        function_name = str(resource.get("Properties", {}).get("FunctionName", ""))
        if "transition-to-error" in function_name:
            return resource
    raise AssertionError("no *-transition-to-error Lambda function in Pipeline template")


def _extract_error_handler_source(ts_text: str) -> str:
    """Extracts the Python source embedded in errorHandlerFn's
    `code: lambda.Code.fromInline(...)` -- anchored on the
    'TransitionToErrorFunction' construct id, NOT the first `fromInline(` in
    the file (pipeline-stack.ts has two earlier, unrelated ones:
    `stubHandlerCode` and `semaphoreCode` -- a naive first-match regex silently
    grabs the wrong Lambda's source)."""
    anchor = ts_text.index("'TransitionToErrorFunction'")
    tail = ts_text[anchor:]
    match = re.search(r"fromInline\(\s*`\n(.*?)`\.trim\(\)", tail, re.DOTALL)
    assert match is not None, "could not locate errorHandlerFn's inline source"
    return match.group(1)


# cdk synth is expensive (~5-15s) -- run it ONCE for the whole module (every
# TestCase class below shares this cache via SynthFixture.setUpClass) rather
# than once per class.
_SYNTH_CACHE: dict[str, Any] = {}


def _get_synth_fixture() -> dict[str, Any]:
    if not _SYNTH_CACHE:
        result = _run_cdk_synth()
        assert result.returncode == 0, (
            f"cdk synth --context env=dev exited {result.returncode}\n"
            f"stdout: {result.stdout[-1000:]}\nstderr: {result.stderr[-1000:]}"
        )
        template = _load_pipeline_template()
        asl_states = _state_machine_definition(template)["States"]
        ts_text = PIPELINE_STACK_PATH.read_text(encoding="utf-8")
        error_handler_source = _extract_error_handler_source(ts_text)
        lambda_resource = _find_transition_to_error_function(template)
        error_handler_zip = lambda_resource["Properties"]["Code"]["ZipFile"]
        _SYNTH_CACHE.update(
            template=template,
            asl_states=asl_states,
            error_handler_source=error_handler_source,
            error_handler_zip=error_handler_zip,
        )
    return _SYNTH_CACHE


class SynthFixture:
    """Mixin: pulls the (module-cached) parsed template / ASL / Lambda source
    onto the class for every test below."""

    template: dict
    asl_states: dict
    error_handler_source: str
    error_handler_zip: str

    @classmethod
    def setUpClass(cls) -> None:
        fixture = _get_synth_fixture()
        cls.template = fixture["template"]
        cls.asl_states = fixture["asl_states"]
        cls.error_handler_source = fixture["error_handler_source"]
        cls.error_handler_zip = fixture["error_handler_zip"]


# ---------------------------------------------------------------------------
# Structural checks -- synthesized ASL (defect 3).
# ---------------------------------------------------------------------------

class TestEachStageRecordsItsOwnRealName(SynthFixture, unittest.TestCase):
    def test_nine_distinct_error_transition_states_exist(self) -> None:
        missing = [name for name in EXPECTED_STAGE_TOKENS if name not in self.asl_states]
        self.assertFalse(missing, f"missing error-transition states: {missing}")

    def test_each_states_own_failing_stage_payload_is_its_real_stage_not_pipeline(self) -> None:
        for state_name, expected_token in EXPECTED_STAGE_TOKENS.items():
            state = self.asl_states[state_name]
            payload = state["Parameters"]["Payload"]
            actual = payload.get("failing_stage")
            with self.subTest(state=state_name):
                self.assertEqual(
                    actual, expected_token,
                    f"{state_name}'s Payload.failing_stage is {actual!r}, "
                    f"expected {expected_token!r}",
                )
                self.assertNotEqual(
                    actual, "pipeline",
                    f"{state_name} still carries the hardcoded 'pipeline' literal",
                )

    def test_no_two_stages_share_the_same_error_transition_state(self) -> None:
        # Defect 3 in its original form: every `.addCatch()` pointed at the
        # SAME single Task state. Assert each stage's Catch names a distinct
        # state (an equal-Next check would still pass a shared state).
        catch_targets = {}
        for real_stage_state in (
            "AcquireConcurrencySlot", "MarkReviewRunning", "ExtractStage",
            "RetrieveStage", "MockReviewStage", "RedlineStage", "PersistStage",
            "AuditStage", "ReleaseConcurrencySlot",
        ):
            state = self.asl_states.get(real_stage_state)
            self.assertIsNotNone(state, f"missing state {real_stage_state}")
            catches = state.get("Catch", [])
            self.assertEqual(len(catches), 1, f"{real_stage_state} should have exactly 1 Catch")
            catch_targets[real_stage_state] = catches[0]["Next"]
        self.assertEqual(
            len(set(catch_targets.values())), len(catch_targets),
            f"two or more stages still share one error-transition state: {catch_targets}",
        )


# ---------------------------------------------------------------------------
# Structural checks -- errorHandlerFn's inline Lambda source (defects 1/2/4).
# ---------------------------------------------------------------------------

class TestErrorHandlerSourceStructure(SynthFixture, unittest.TestCase):
    def test_source_extracted_from_template_matches_source_extracted_from_ts(self) -> None:
        # Sanity: both extraction paths (raw .ts regex vs. synthesized
        # template ZipFile) must agree -- if they diverge, one of the two
        # extraction anchors is wrong and every check below is void.
        self.assertEqual(self.error_handler_source.strip(), self.error_handler_zip.strip())

    def test_inline_source_fits_cloudformations_zipfile_limit(self) -> None:
        """CloudFormation caps `AWS::Lambda::Function` `Code.ZipFile` at 4096
        characters, and CDK does not check it at synth (`InlineCode` validates
        only that the string is non-empty) -- so an over-long inline handler
        synthesizes clean, passes every structural check in this file, and
        then fails at `cdk deploy`. The #105 fix quadrupled this handler's
        length; the rationale therefore lives in the TypeScript comment above
        `fromInline`, which is NOT deployed source. Guarding the real
        synthesized `ZipFile`, not the .ts extraction.
        """
        deployed = self.error_handler_zip.strip()
        self.assertLessEqual(
            len(deployed), 4096,
            f"inline handler is {len(deployed)} chars; CloudFormation rejects "
            f"Code.ZipFile over 4096 at deploy time. Move prose into the "
            f"TypeScript comment above fromInline.",
        )

    def test_has_condition_expression_guarding_done(self) -> None:
        self.assertIn("ConditionExpression", self.error_handler_source)
        self.assertIn('"DONE"', self.error_handler_source)

    def test_condition_expression_also_guards_cancelled(self) -> None:
        # StopExecution cannot kill a Lambda already in flight, so a
        # TransitionToError<Stage> invoked by a Catch before the Stop can
        # land its update_item moments after the row went CANCELLED
        # (mirrors infra/lambda/persist/handler.py's identical guard). The
        # ConditionExpression must refuse to overwrite that row too, not
        # just a DONE one.
        self.assertIn('"CANCELLED"', self.error_handler_source)
        self.assertIsNotNone(
            re.search(r"#status\s*<>\s*:cancelled", self.error_handler_source)
        )

    def test_writes_reason_and_failed_at(self) -> None:
        self.assertIsNotNone(re.search(r"\breason\s*=\s*:", self.error_handler_source))
        self.assertIn("failed_at", self.error_handler_source)

    def test_error_reason_is_not_the_raw_full_error_object(self) -> None:
        # Pre-fix source wrote `":reason": str(error_info)` unconditionally --
        # serializing the WHOLE event["error"] dict (Error + Cause, Cause
        # carrying the raw exception message/stack trace) straight onto the
        # row, with no dict/non-dict distinction at all.
        self.assertNotIn(': str(error_info),', self.error_handler_source)
        self.assertNotIn('": str(error_info)', self.error_handler_source)
        # Post-fix source must reduce a dict-shaped error to the Error NAME
        # only, conditioned on it actually being a dict.
        self.assertIn("isinstance(error_info, dict)", self.error_handler_source)
        self.assertIn('.get("Error")', self.error_handler_source)


# ---------------------------------------------------------------------------
# Behavioral checks -- the REAL extracted source, real moto DynamoDB
# semantics (never a hand-rolled fake -- see #446's own test for why).
# ---------------------------------------------------------------------------

class TestErrorHandlerBehavior(SynthFixture, unittest.TestCase):
    def setUp(self) -> None:
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.table = create_reviews_table(ddb)

        ns: dict[str, Any] = {}
        exec(  # noqa: S102
            compile(self.error_handler_source, "<pipeline-stack.ts:errorHandlerFn>", "exec"),
            ns,
        )
        self.handler = ns["handler"]

    def _put(self, **fields: Any) -> None:
        item = {"review_id": REVIEW_ID, "owner_sub": "user-1", "playbook_id": "eiaa"}
        item.update(fields)
        self.table.put_item(Item=item)

    def _get(self) -> dict:
        return self.table.get_item(Key={"review_id": REVIEW_ID})["Item"]

    # -- Defect 1: DONE guard -------------------------------------------

    def test_done_row_is_not_overwritten_by_a_late_catch(self) -> None:
        self._put(status="DONE", output_s3_key="outputs/105/out.docx")
        result = self.handler(
            {"review_id": REVIEW_ID, "failing_stage": "audit", "error": {"Error": "KeyError"}},
            None,
        )
        item = self._get()
        self.assertEqual(item["status"], "DONE")
        self.assertEqual(item["output_s3_key"], "outputs/105/out.docx")
        self.assertNotIn("failing_stage", item)
        self.assertNotIn("reason", item)
        self.assertEqual(result["status"], "DONE")

    def test_cancelled_row_is_not_overwritten_by_a_late_catch(self) -> None:
        # The reviewer pressed Stop; backend/src/review_routes.py's
        # mark_cancelled wrote CANCELLED before this Catch's Lambda
        # invocation -- already in flight, unkillable by StopExecution --
        # lands its own update_item moments later (mirrors
        # infra/lambda/persist/handler.py's identical race/guard).
        self._put(status="CANCELLED", cancelled_at="1700000000")
        result = self.handler(
            {"review_id": REVIEW_ID, "failing_stage": "redline", "error": {"Error": "KeyError"}},
            None,
        )
        item = self._get()
        self.assertEqual(item["status"], "CANCELLED")
        self.assertEqual(item["cancelled_at"], "1700000000")
        self.assertNotIn("failing_stage", item)
        self.assertNotIn("reason", item)
        self.assertEqual(result["status"], "CANCELLED")

    def test_non_done_row_is_still_recorded_as_failed(self) -> None:
        # The guard must not disarm the normal failure path.
        self._put(status="RUNNING")
        self.handler(
            {"review_id": REVIEW_ID, "failing_stage": "extract", "error": {"Error": "KeyError"}},
            None,
        )
        item = self._get()
        self.assertEqual(item["status"], "ERROR")

    def test_a_row_with_no_status_attribute_is_still_recorded_as_failed(self) -> None:
        """The `attribute_not_exists(#status)` arm of the ConditionExpression.

        On real DynamoDB a comparison against an attribute that does not
        exist does not match, so `#status <> :done AND #status <> :cancelled`
        alone REFUSES a row carrying no `status` at all -- which is why
        `attribute_not_exists(x) OR x <> :v` is the standard idiom -- and the
        review would be left wedged with no terminal forever. Such a row is
        producible: a submission whose StartExecution beat the row's own
        status write, and any row written before the attribute existed.

        Two assertions, because ONE OF THEM CANNOT SEE THE BUG: moto is more
        permissive than DynamoDB here (verified: it accepts `#s <> :v`
        against a missing attribute), so the outcome check below passes with
        the arm deleted. The second assertion reads the ConditionExpression
        actually put on the wire by this handler -- not a substring of the
        .ts file -- and that is the one that goes red if the arm is removed.
        """
        sent: list[dict[str, Any]] = []

        def _capture(params: Any = None, model: Any = None, **_kwargs: Any) -> None:
            if model is not None and model.name == "UpdateItem":
                sent.append(json.loads(params["body"]))

        boto3.setup_default_session(region_name="us-east-1")
        self.addCleanup(setattr, boto3, "DEFAULT_SESSION", None)
        events = boto3.DEFAULT_SESSION.events
        events.register("before-call.dynamodb", _capture, unique_id="105-capture")
        self.addCleanup(events.unregister, "before-call.dynamodb", unique_id="105-capture")

        self._put()  # no `status` key at all
        result = self.handler(
            {"review_id": REVIEW_ID, "failing_stage": "extract", "error": {"Error": "KeyError"}},
            None,
        )
        item = self._get()
        self.assertEqual(item["status"], "ERROR")
        self.assertEqual(item["failing_stage"], "extract")
        self.assertEqual(result["status"], "ERROR")

        self.assertEqual(len(sent), 1, "exactly one UpdateItem call was made")
        condition = sent[0]["ConditionExpression"]
        self.assertIn(
            "attribute_not_exists(#status)", condition,
            f"the guard refuses a row with no status on real DynamoDB: {condition!r}",
        )
        # And the absence arm must be an OR with the terminal comparisons --
        # ANDing it would refuse every row that HAS a status, i.e. all of them.
        self.assertRegex(condition, r"attribute_not_exists\(#status\)\s+OR\b")

    def test_a_non_conditional_dynamodb_error_is_not_swallowed(self) -> None:
        """The `except ClientError` block must re-raise anything that is NOT
        the conditional-check failure.

        A bare `except ClientError: pass` would pass every other test in this
        class while silently eating a throttle, a missing table, or an access
        denial -- the review would be reported ERROR to Step Functions and
        the row would never actually be written. The error is injected at the
        real botocore call boundary (a `before-call` hook raising the
        ClientError botocore itself would raise), so the handler sees exactly
        the exception shape production would hand it; the table underneath
        stays moto's real one.
        """
        self._put(status="RUNNING")
        throttle = ClientError(
            {
                "Error": {
                    "Code": "ProvisionedThroughputExceededException",
                    "Message": "The level of configured provisioned throughput was exceeded.",
                }
            },
            "UpdateItem",
        )

        def _raise_on_update(model: Any = None, **_kwargs: Any) -> None:
            if model is not None and model.name == "UpdateItem":
                raise throttle

        boto3.setup_default_session(region_name="us-east-1")
        self.addCleanup(setattr, boto3, "DEFAULT_SESSION", None)
        events = boto3.DEFAULT_SESSION.events
        events.register("before-call.dynamodb", _raise_on_update, unique_id="105-throttle")
        self.addCleanup(events.unregister, "before-call.dynamodb", unique_id="105-throttle")

        with self.assertRaises(ClientError) as caught:
            self.handler(
                {"review_id": REVIEW_ID, "failing_stage": "extract",
                 "error": {"Error": "KeyError"}},
                None,
            )
        self.assertEqual(
            caught.exception.response["Error"]["Code"],
            "ProvisionedThroughputExceededException",
        )
        # And nothing was written: the row is exactly as it was.
        self.assertEqual(self._get()["status"], "RUNNING")

    # -- Defect 2: reason / failed_at ------------------------------------

    def test_writes_the_unclassified_reason_token_and_failed_at(self) -> None:
        """`reason` is written, and it is the UNCLASSIFIED sentinel on purpose.

        A Step Functions Catch sees an error NAME and a stage, never the
        model/document facts `classify_failure_reason` reads, so any
        classified-looking token here would be invented -- and, being the
        same on every AWS failure row, would bury the per-stage names this
        issue just made accurate. The reader-facing half of that bargain
        (every reader falls through this token to `failing_stage`, which now
        has copy for all nine stage names) is pinned in
        frontend/src/__tests__/aws-catch-failure-rows-105.test.tsx.
        """
        self._put(status="RUNNING")
        self.handler(
            {"review_id": REVIEW_ID, "failing_stage": "extract", "error": {"Error": "KeyError"}},
            None,
        )
        item = self._get()
        self.assertEqual(item["reason"], "unhandled_exception")
        self.assertIn("failed_at", item)
        self.assertTrue(str(item["failed_at"]).isdigit())

    # -- Defect 3: real per-stage failing_stage ---------------------------

    def test_records_the_real_stage_name_it_is_given(self) -> None:
        self._put(status="RUNNING")
        self.handler(
            {"review_id": REVIEW_ID, "failing_stage": "release_semaphore_slot",
             "error": {"Error": "TimeoutError"}},
            None,
        )
        item = self._get()
        self.assertEqual(item["failing_stage"], "release_semaphore_slot")
        self.assertNotEqual(item["failing_stage"], "pipeline")

    # -- Defect 4: error_reason reduced to the Error name only -----------

    def test_error_reason_is_reduced_to_the_error_name_and_no_raw_text_leaks(self) -> None:
        sensitive_cause = (
            '{"errorMessage": "KeyError: \'paragraphs\' in document '
            'Acme Corp MSA sec 8.2 (\'Limitation of Liability\')", '
            '"errorType": "KeyError", "stackTrace": ["  File ..."]}'
        )
        self._put(status="RUNNING")
        self.handler(
            {
                "review_id": REVIEW_ID,
                "failing_stage": "extract",
                "error": {"Error": "KeyError", "Cause": sensitive_cause},
            },
            None,
        )
        item = self._get()
        self.assertEqual(item["error_reason"], "KeyError")
        serialized = json.dumps(item, default=str)
        self.assertNotIn("Acme Corp MSA", serialized)
        self.assertNotIn("Limitation of Liability", serialized)
        self.assertNotIn("stackTrace", serialized)

    def test_error_reason_falls_back_when_error_is_not_a_dict(self) -> None:
        # Some Catch payloads carry a bare string in $.error rather than the
        # Lambda {"Error", "Cause"} shape -- must not raise.
        self._put(status="RUNNING")
        self.handler(
            {"review_id": REVIEW_ID, "failing_stage": "extract", "error": "boom"},
            None,
        )
        item = self._get()
        self.assertEqual(item["error_reason"], "boom")


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestEachStageRecordsItsOwnRealName,
        TestErrorHandlerSourceStructure,
        TestErrorHandlerBehavior,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
