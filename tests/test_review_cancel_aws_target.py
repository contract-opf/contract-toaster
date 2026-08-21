#!/usr/bin/env python3
"""
Regression test: pressing Stop on the AWS (Step Functions) target actually
stops the review.

## Root problem this proves fixed

`POST /api/reviews/{review_id}/cancel` and the cancel machinery in
backend/src/reviews.py are shared by BOTH deployment targets, but only the
in-process Docker Compose runner honoured the request: the cooperative
checkpoints live in `pipeline_runner._make_cancel_checkpoint`, which the
Step Functions pipeline never calls. On the AWS target a reviewer could press
Stop, receive a 202, watch "Stopping…" forever, and the review would run to
completion regardless -- the exact false promise the control was added to
remove.

Step Functions gives us a stronger primitive than the cooperative one:
`StopExecution` on the ARN already recorded on the reviews row. It is
immediate and it guarantees no further state runs, so the AWS target does not
need per-stage checkpoints to keep the promise.

Two things the shape of that fix depends on, both asserted here because both
were wrong and neither is visible from the cancel code itself:

  1. The API task role was granted `states:StartExecution` and nothing else.
     Without `states:StopExecution` the call fails AccessDenied in production
     while every offline test passes.
  2. `infra/lambda/persist/handler.py` guarded its terminal write only against
     `ERROR`. An in-flight persist finishing just after the abort would
     therefore overwrite `CANCELLED` with `DONE` -- resurrecting a review the
     user stopped, and handing them a redline they cancelled.

The Step Functions call is exercised through botocore's own `Stubber`, which
validates parameters against the REAL Step Functions service model. A
hand-rolled fake would happily accept `executionArn=None` or a misspelled
key; the shipped code would then fail only in production.

Run with: python3 tests/test_review_cancel_aws_target.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
PERSIST_DIR = REPO_ROOT / "infra" / "lambda" / "persist"
INFRA_DIR = REPO_ROOT / "infra"
CDK_OUT = INFRA_DIR / "cdk.out"

for _dir in (BACKEND_SRC, str(REPO_ROOT / "tests")):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("REVIEWS_TABLE", "test-reviews")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "test-submissions")

import boto3  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

import reviews  # noqa: E402

REAL_ARN = (
    "arn:aws:states:us-east-1:123456789012:execution:contract-toaster-dev:review-abc"
)
INPROCESS_ARN = "inprocess:review-abc"


class FakeTable:
    """Minimal reviews table: only `get_item`, which is all the ARN lookup
    needs. Deliberately NOT a full DynamoDB fake -- the DynamoDB behaviour of
    the cancel path is proven against real DynamoDB Local separately; what is
    under test here is which Step Functions call is made, and when."""

    def __init__(self, item: dict[str, Any] | None) -> None:
        self._item = item
        self.get_item_calls = 0

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.get_item_calls += 1
        return {"Item": self._item} if self._item is not None else {}


class FakeResource:
    def __init__(self, table: FakeTable) -> None:
        self._table = table

    def Table(self, _name: str) -> FakeTable:  # noqa: N802 - boto3's own casing
        return self._table


def _sfn_with_stub():
    client = boto3.client(
        "stepfunctions",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    return client, Stubber(client)


# ---------------------------------------------------------------------------
# 1. The Step Functions execution is actually stopped.
# ---------------------------------------------------------------------------


def test_real_execution_arn_is_stopped(failures: list[str]) -> None:
    resource = FakeResource(FakeTable({"review_id": "r1", "execution_arn": REAL_ARN}))
    client, stubber = _sfn_with_stub()
    # Stubber asserts the parameters against the real service model: a wrong
    # key or a missing required field fails here, not in production.
    stubber.add_response(
        "stop_execution",
        {"stopDate": __import__("datetime").datetime(2026, 8, 5)},
        {"executionArn": REAL_ARN, "cause": "Cancelled by the review owner."},
    )
    with stubber:
        stopped = reviews.stop_running_execution("r1", resource, client)
    if not stopped:
        failures.append("[1a] A real Step Functions execution must report as stopped")
    try:
        stubber.assert_no_pending_responses()
    except AssertionError:
        failures.append("[1b] StopExecution was never called for a real execution ARN")


def test_inprocess_arn_is_left_alone(failures: list[str]) -> None:
    """The Docker Compose runner records `inprocess:<name>` ARNs. Calling
    Step Functions for one would be both meaningless and an error -- that
    target stops via the cooperative checkpoints instead."""
    resource = FakeResource(FakeTable({"review_id": "r1", "execution_arn": INPROCESS_ARN}))
    client, stubber = _sfn_with_stub()
    # No stubbed responses: any Step Functions call at all raises here.
    with stubber:
        stopped = reviews.stop_running_execution("r1", resource, client)
    if stopped:
        failures.append("[2a] An in-process ARN must not be reported as a stopped execution")


def test_missing_arn_is_not_an_error(failures: list[str]) -> None:
    """A review can be cancelled before `ensure_execution_started` recorded an
    ARN. That is an ordinary race, not a failure."""
    resource = FakeResource(FakeTable({"review_id": "r1"}))
    client, stubber = _sfn_with_stub()
    with stubber:
        stopped = reviews.stop_running_execution("r1", resource, client)
    if stopped:
        failures.append("[3a] A review with no recorded ARN has no execution to stop")


def test_a_stop_failure_is_reported_not_swallowed(failures: list[str]) -> None:
    """If StopExecution fails, the caller must find out.

    Swallowing it is what produces the bug this file exists for: the UI would
    show "Stopping…" while the pipeline ran happily to completion.
    """
    from botocore.exceptions import ClientError

    resource = FakeResource(FakeTable({"review_id": "r1", "execution_arn": REAL_ARN}))
    client, stubber = _sfn_with_stub()
    stubber.add_client_error(
        "stop_execution", service_error_code="AccessDeniedException", http_status_code=403
    )
    with stubber:
        try:
            reviews.stop_running_execution("r1", resource, client)
        except ClientError:
            # Specifically the provider's error -- catching bare `Exception`
            # here would let this test pass vacuously against a
            # `stop_running_execution` that does not exist yet (AttributeError),
            # which it did on the first RED run.
            return
    failures.append("[4a] A failed StopExecution must propagate, never be swallowed")


# ---------------------------------------------------------------------------
# 2. persist must not resurrect a cancelled review.
# ---------------------------------------------------------------------------


def _persist_terminal_condition() -> str:
    """The ConditionExpression guarding persist's TERMINAL STATUS write.

    Parsed from the AST, not by string search: a plain `.index(...)` matches
    `KeyConditionExpression` on an unrelated Query first, which is exactly the
    false reading this helper was written wrong once already. The terminal
    write is identified by its own contents -- it is the one whose condition
    talks about `#status`.
    """
    import ast

    source = (PERSIST_DIR / "handler.py").read_text(encoding="utf-8")
    conditions: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "ConditionExpression":
                continue
            value = ast.literal_eval(keyword.value) if isinstance(
                keyword.value, ast.Constant
            ) else None
            if isinstance(value, str) and "#status" in value:
                conditions.append(value)
    if not conditions:
        raise AssertionError("persist has no status-guarded ConditionExpression at all")
    return " || ".join(conditions)


def test_persist_refuses_to_overwrite_a_cancelled_review(failures: list[str]) -> None:
    condition = _persist_terminal_condition()
    if "CANCELLED" not in condition and ":cancelled" not in condition:
        failures.append(
            f"[5a] persist's terminal write must refuse a CANCELLED row, or an in-flight "
            f"persist finishing just after the abort hands the reviewer a redline they "
            f"cancelled. Condition is: {condition.strip()}"
        )


def test_persist_still_refuses_to_overwrite_an_error(failures: list[str]) -> None:
    """The pre-existing guard must survive the new one."""
    condition = _persist_terminal_condition()
    if ":error" not in condition:
        failures.append(f"[5b] persist must still refuse an ERROR row; condition: {condition!r}")


def test_mark_running_can_never_touch_a_cancelled_row(failures: list[str]) -> None:
    """persist is not the only stage that writes review status.

    StopExecution stops SCHEDULING further states, but the stage already in
    flight runs to completion — so every pipeline Lambda that writes the
    reviews row is a candidate for resurrecting a cancelled review. Walking
    them: the error handler is unreachable after an abort (no further state is
    scheduled), the audit stage writes audit rows rather than status, and
    release-slot touches only the semaphore. That leaves persist (guarded
    above) and mark-running, which is safe for a DIFFERENT reason — its write
    is conditional on the row being PENDING, and a CANCELLED row is not.

    That safety is incidental to mark-running's own purpose, so it is pinned
    here rather than assumed: loosening that condition later would silently
    reopen the resurrection bug from a second direction.
    """
    import ast

    source = (REPO_ROOT / "infra" / "lambda" / "mark_running" / "handler.py").read_text(
        encoding="utf-8"
    )
    conditions = [
        ast.literal_eval(keyword.value)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "ConditionExpression" and isinstance(keyword.value, ast.Constant)
    ]
    if not conditions:
        failures.append("[5c] mark_running must keep a ConditionExpression on its status write")
        return
    if not all(":pending" in condition for condition in conditions):
        failures.append(
            f"[5d] mark_running's write must stay conditional on PENDING, or it can "
            f"resurrect a CANCELLED review; conditions: {conditions!r}"
        )


# ---------------------------------------------------------------------------
# 3. The API role can actually make the call.
# ---------------------------------------------------------------------------


def _synth_template() -> dict[str, Any] | None:
    """Run the real `cdk synth` and return the synthesized AppStack template.

    Real CloudFormation, not a string match on the TypeScript: an IAM action
    the CDK never emits would pass a source grep and fail in production.
    """
    if CDK_OUT.exists():
        shutil.rmtree(CDK_OUT)
    from infra_synth_helper import NEUTRAL_CDK_CONTEXT

    result = subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA_DIR,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        print(f"    cdk synth failed: {result.stderr[-1500:]}")
        return None
    for template_file in CDK_OUT.glob("*.template.json"):
        template = json.loads(template_file.read_text(encoding="utf-8"))
        for resource in template.get("Resources", {}).values():
            if resource.get("Type") != "AWS::IAM::Policy":
                continue
            props = resource.get("Properties", {})
            if "api-task-policy" in json.dumps(props.get("PolicyName", "")):
                return props
    return None


def test_api_role_may_stop_an_execution(failures: list[str]) -> None:
    policy = _synth_template()
    if policy is None:
        failures.append(
            "[6a] Could not synthesize the API task-role policy (cdk synth failed or the "
            "policy was not found) -- cannot prove the role may stop an execution"
        )
        return
    actions: list[str] = []
    for statement in policy.get("PolicyDocument", {}).get("Statement", []):
        action = statement.get("Action")
        actions.extend(action if isinstance(action, list) else [action])
    if "states:StopExecution" not in actions:
        failures.append(
            "[6b] The API task role has no states:StopExecution -- cancelling on the AWS "
            f"target would fail AccessDenied in production. Granted: {sorted(set(actions))}"
        )


TESTS = [
    test_real_execution_arn_is_stopped,
    test_inprocess_arn_is_left_alone,
    test_missing_arn_is_not_an_error,
    test_a_stop_failure_is_reported_not_swallowed,
    test_persist_refuses_to_overwrite_a_cancelled_review,
    test_persist_still_refuses_to_overwrite_an_error,
    test_mark_running_can_never_touch_a_cancelled_row,
    test_api_role_may_stop_an_execution,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for failure in failures[before:]:
                print(f"FAIL: {failure}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print(f"PASS: all {len(TESTS)} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
