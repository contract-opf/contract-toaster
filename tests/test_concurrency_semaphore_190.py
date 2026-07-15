#!/usr/bin/env python3
"""
Structural gate for issue #190: concurrency semaphore acquisition-failure
handling and release-drift guard.

Prior bug (issue #190 concern): the acquire Lambda set
event['semaphore_acquired'] = False when the cap was hit and returned
normally, but the state machine chain (acquireSlot.next(extractStage)...)
had no Choice state checking that flag, so the execution proceeded anyway --
the advertised MAX_CONCURRENT_EXECUTIONS cap (issue #59 AC) was not enforced
at all. Additionally, an execution that failed to acquire still ran
ReleaseConcurrencySlot at the end, decrementing current_count for a slot it
never held (guarded only by current_count > 0), corrupting the counter
downward and enabling future over-admission.

This gate verifies, against the SYNTHESIZED state machine definition (not
just the .ts source):

  A. cdk synth runs cleanly.
  B. A Choice state immediately follows AcquireConcurrencySlot (named
     'SemaphoreSlotAcquired?') -- AcquireConcurrencySlot's own `Next` no
     longer points directly at ExtractStage.
  C. That Choice state routes semaphore_acquired == false away from
     ExtractStage: to a Wait/retry loop and/or a fail branch, never
     proceeding straight into the real pipeline.
  D. The false branch does not skip forward past ExtractStage into any other
     real pipeline stage either (RetrieveStage, MockReviewStage,
     RedlineStage, PersistStage, AuditStage) -- i.e. only the
     semaphore_acquired == true branch reaches ExtractStage, and no other
     state in the Choice's targets is a real pipeline stage.
  E. The give-up/fail branch (if present) updates the reviews row (routes to
     the shared error-transition Lambda, i.e. errorHandlerFn / a "Transition
     To Error"-shaped task) rather than silently vanishing.
  F. release() in the semaphore Lambda source guards its DynamoDB decrement
     on the payload's own semaphore_acquired flag -- not just
     `current_count > 0` -- so a never-acquired execution cannot decrement
     the counter even if it were ever able to reach the release state.
  G. BEHAVIORAL (review round 1): a full successful review, run through the
     REAL acquire()/release() Lambda source (extracted from the inline TS
     string, not re-implemented) and the REAL infra/lambda/mock_review/
     handler.py and infra/lambda/persist/handler.py, round-trips
     current_count back to its pre-acquire baseline. Check F only regexed
     release()'s SOURCE for the flag-guard; it never exercised the actual
     acquire -> extract -> retrieve -> mock_review -> redline -> persist ->
     audit -> release payload flow. Every stage LambdaInvoke in
     pipeline-stack.ts uses `outputPath: '$.Payload'` with no `resultPath`
     merge, so a stage that returns a fresh dict (mock_review's canned
     results) silently drops any field -- including semaphore_acquired --
     that isn't explicitly threaded through it. That would make Check F's
     guard a permanent no-op on every successful review: current_count
     would leak upward by one on every success and never come back down,
     eventually wedging the whole pipeline behind the concurrency cap. Check
     G is the regression test for that failure mode.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
PIPELINE_STACK_PATH = INFRA / "lib" / "nested" / "pipeline-stack.ts"

REAL_PIPELINE_STAGE_NAMES = {
    "ExtractStage",
    "RetrieveStage",
    "MockReviewStage",
    "RedlineStage",
    "PersistStage",
    "AuditStage",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


# ---------------------------------------------------------------------------
# Check A -- cdk synth runs cleanly; locate the Pipeline nested template.
# ---------------------------------------------------------------------------

def _run_cdk_synth() -> tuple[list[str], Path | None]:
    print("\nCheck A: cdk synth runs cleanly …")
    failures: list[str] = []

    if not INFRA.is_dir():
        return _assert(False, "infra/ directory exists (prerequisite for cdk synth)"), None

    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        print("  (node_modules absent -- running npm install first …)")
        install = subprocess.run(
            ["npm", "install"], cwd=INFRA, capture_output=True, text=True,
        )
        if install.returncode != 0:
            return _assert(
                False, "npm install succeeded in infra/",
                f"stderr: {install.stderr[-500:]}",
            ), None

    result = subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
    )
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev exits 0",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    if failures:
        return failures, None

    cdk_out = INFRA / "cdk.out"
    pipeline_templates = list(cdk_out.glob("*Pipeline*.nested.template.json"))
    failures += _assert(len(pipeline_templates) == 1, "exactly one Pipeline nested template found")
    if failures:
        return failures, None

    return failures, pipeline_templates[0]


def _state_machine_definition(pipeline_template_path: Path) -> dict:
    data = json.loads(pipeline_template_path.read_text())
    resources = data["Resources"]
    sm = None
    for _name, r in resources.items():
        if r.get("Type") == "AWS::StepFunctions::StateMachine":
            sm = r
            break
    assert sm is not None, "no AWS::StepFunctions::StateMachine resource in Pipeline template"
    def_string = sm["Properties"]["DefinitionString"]
    # CDK renders this as an Fn::Join over string fragments (Lambda ARNs are
    # {"Fn::GetAtt": ...} substitutions elided to "" -- fine, we only need
    # the state graph shape, not the resolved ARNs).
    if isinstance(def_string, dict) and "Fn::Join" in def_string:
        parts = def_string["Fn::Join"][1]
        text = "".join(p if isinstance(p, str) else "" for p in parts)
    else:
        text = def_string
    return json.loads(text)


# ---------------------------------------------------------------------------
# Checks B-E -- Choice state gates acquisition failure (synthesized ASL)
# ---------------------------------------------------------------------------

def check_bcde_choice_gates_acquisition(states: dict) -> list[str]:
    print("\nChecks B-E: Choice state gates semaphore_acquired == false …")
    failures: list[str] = []

    acquire = states.get("AcquireConcurrencySlot")
    failures += _assert(
        acquire is not None,
        "synthesized definition contains an AcquireConcurrencySlot state",
    )
    if failures:
        return failures

    # B: AcquireConcurrencySlot's own Next is a Choice, not ExtractStage.
    acquire_next = acquire.get("Next")
    failures += _assert(
        acquire_next != "ExtractStage",
        "AcquireConcurrencySlot.Next is NOT ExtractStage directly",
        f"Got Next={acquire_next!r}; acquisition outcome must be gated first.",
    )

    choice_name = acquire_next
    choice = states.get(choice_name) if choice_name else None
    failures += _assert(
        choice is not None and choice.get("Type") == "Choice",
        "AcquireConcurrencySlot routes into a Choice state",
        f"Next state {choice_name!r} -> {choice}",
    )
    if failures:
        return failures

    choices = choice.get("Choices", [])

    # C: a choice branch keys off $.semaphore_acquired and only that branch
    # (boolean true) may target ExtractStage.
    acquired_true_branches = [
        c for c in choices
        if c.get("Variable") == "$.semaphore_acquired" and c.get("BooleanEquals") is True
    ]
    failures += _assert(
        len(acquired_true_branches) == 1,
        "Choice has exactly one branch on $.semaphore_acquired == true",
        f"Choices: {choices}",
    )
    if acquired_true_branches:
        failures += _assert(
            acquired_true_branches[0].get("Next") == "ExtractStage",
            "The semaphore_acquired == true branch routes to ExtractStage",
        )

    # No branch (and no Default) reaches a real pipeline stage other than via
    # the semaphore_acquired == true branch above.
    all_targets_except_true_branch = [
        c.get("Next") for c in choices
        if not (c.get("Variable") == "$.semaphore_acquired" and c.get("BooleanEquals") is True)
    ]
    default_target = choice.get("Default")
    if default_target:
        all_targets_except_true_branch.append(default_target)

    stray_pipeline_targets = [
        t for t in all_targets_except_true_branch if t in REAL_PIPELINE_STAGE_NAMES
    ]
    failures += _assert(
        not stray_pipeline_targets,
        "No non-acquired branch of the Choice routes into a real pipeline stage",
        f"Offending targets: {stray_pipeline_targets}",
    )

    # D: ExtractStage is unreachable from any state except via the Choice's
    # true branch (i.e. nothing else in the whole state machine still points
    # straight at ExtractStage -- guards against a stray leftover edge).
    other_edges_into_extract = [
        name for name, s in states.items()
        if name != choice_name and s.get("Next") == "ExtractStage"
    ]
    failures += _assert(
        not other_edges_into_extract,
        "No state other than the Choice points directly at ExtractStage",
        f"Offending states: {other_edges_into_extract}",
    )

    # C (continued): the non-acquired path must be a Wait/retry loop and/or a
    # fail branch -- never silently dropped. Resolve every non-true-branch
    # target (Default + false-ish branches) and confirm each is either a
    # Wait state (retry loop) or eventually reaches a Fail state.
    def _eventually_fails_or_waits(state_name: str, seen: set[str]) -> bool:
        if state_name in seen:
            return True  # cyclic Wait/retry loop -- acceptable "retry" shape
        seen.add(state_name)
        state = states.get(state_name)
        if state is None:
            return False
        if state.get("Type") in ("Wait", "Fail"):
            return True
        nxt = state.get("Next")
        if nxt:
            return _eventually_fails_or_waits(nxt, seen)
        return False

    for target in all_targets_except_true_branch:
        failures += _assert(
            _eventually_fails_or_waits(target, set()),
            f"Non-acquired branch target {target!r} is a Wait/retry loop or reaches a Fail state",
        )

    # E: the give-up/fail branch (a target that is not a Wait state) updates
    # the reviews row via the shared error-transition Lambda invoke before
    # failing -- i.e. it is not a bare Fail with no state update.
    fail_branch_targets = [
        t for t in all_targets_except_true_branch
        if states.get(t, {}).get("Type") != "Wait"
    ]
    for target in fail_branch_targets:
        target_state = states.get(target, {})
        looks_like_error_transition = (
            target_state.get("Type") == "Task"
            and "lambda:invoke" in json.dumps(target_state.get("Resource", ""))
        )
        # Or the Wait/loop eventually reaches a distinct Task before Fail.
        if not looks_like_error_transition:
            # Walk forward looking for a Task en route to the eventual Fail.
            seen: set[str] = set()
            cur = target
            found_task = False
            while cur and cur not in seen:
                seen.add(cur)
                s = states.get(cur, {})
                if s.get("Type") == "Task":
                    found_task = True
                    break
                cur = s.get("Next")
            looks_like_error_transition = found_task
        failures += _assert(
            looks_like_error_transition,
            f"Give-up branch {target!r} updates the reviews row via a Task before failing",
        )

    return failures


# ---------------------------------------------------------------------------
# Check F -- release() guards the decrement on the payload's own flag.
# ---------------------------------------------------------------------------

def check_f_release_guarded_on_payload_flag() -> list[str]:
    print("\nCheck F: release() guards decrement on payload semaphore_acquired flag …")
    failures: list[str] = []

    failures += _assert(
        PIPELINE_STACK_PATH.is_file(),
        "infra/lib/nested/pipeline-stack.ts exists",
    )
    if failures:
        return failures

    text = _read(PIPELINE_STACK_PATH)

    release_match = re.search(r"def release\(event, context\):(.*?)(?=\ndef |\Z)", text, re.DOTALL)
    failures += _assert(
        release_match is not None,
        "semaphore inline Lambda source defines release(event, context)",
    )
    if not release_match:
        return failures

    release_body = release_match.group(1)

    # Must check the payload flag BEFORE doing the table.update_item decrement.
    flag_check = re.search(r"event\.get\(\s*[\"']semaphore_acquired[\"']\s*\)", release_body)
    failures += _assert(
        flag_check is not None,
        "release() reads event.get('semaphore_acquired')",
    )

    if flag_check:
        decrement_match = re.search(r"UpdateExpression=.*ADD current_count :neg_one", release_body, re.DOTALL)
        failures += _assert(
            decrement_match is not None and flag_check.start() < decrement_match.start(),
            "release() checks the semaphore_acquired flag before the decrement UpdateExpression",
        )

    # Still keep current_count > 0 as defense-in-depth (not removed).
    failures += _assert(
        "current_count > :zero" in release_body,
        "release() retains the current_count > 0 condition as defense-in-depth",
    )

    # The acquire() function must track a bounded give-up signal so the
    # Choice state's false branch cannot loop forever.
    acquire_match = re.search(r"def acquire\(event, context\):(.*?)(?=\ndef |\Z)", text, re.DOTALL)
    failures += _assert(
        acquire_match is not None
        and "semaphore_give_up" in acquire_match.group(1)
        and "semaphore_wait_attempts" in acquire_match.group(1),
        "acquire() tracks semaphore_wait_attempts and reports semaphore_give_up",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G -- behavioral: current_count round-trips through the REAL stage
# chain (acquire -> extract -> retrieve -> mock_review -> redline -> persist
# -> audit -> release), catching the leak Check F's source-shape regex
# cannot see.
# ---------------------------------------------------------------------------

def _extract_inline_ts_python_source(ts_text: str, const_name: str) -> str | None:
    """Extracts the Python source embedded in a `lambda.Code.fromInline(...)`
    template-string assignment in pipeline-stack.ts, e.g. `semaphoreCode` or
    `stubHandlerCode`. Returns None if the constant/shape isn't found."""
    pattern = r"const " + re.escape(const_name) + r" = lambda\.Code\.fromInline\(\s*`\n(.*?)`\.trim\(\),"
    match = re.search(pattern, ts_text, re.DOTALL)
    return match.group(1) if match else None


class _FakeSemaphoreTable:
    """Minimal DynamoDB Table stand-in for the `pipeline_semaphore` table --
    interprets exactly the two UpdateExpressions acquire()/release() issue
    (ADD current_count :one / :neg_one) plus put_item/delete_item for the
    per-review slot row. Not a general DynamoDB emulator; purpose-built for
    this test, same convention as tests/test_spend_reservation_settlement.py
    FakeTable."""

    def __init__(self, client_error_cls: type[Exception]) -> None:
        self.items: dict[str, dict] = {}
        self._client_error_cls = client_error_cls

    def put_item(self, Item):
        self.items[Item["lock_name"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["lock_name"], None)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                     ConditionExpression=None):
        lock_name = Key["lock_name"]
        item = self.items.setdefault(lock_name, {"lock_name": lock_name})
        current = item.get("current_count", 0)
        if ":one" in ExpressionAttributeValues:
            max_v = ExpressionAttributeValues.get(":max")
            if "current_count" in item and max_v is not None and current >= max_v:
                raise self._client_error_cls(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem",
                )
            item["current_count"] = current + ExpressionAttributeValues[":one"]
        elif ":neg_one" in ExpressionAttributeValues:
            zero = ExpressionAttributeValues.get(":zero", 0)
            if not current > zero:
                raise self._client_error_cls(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem",
                )
            item["current_count"] = current + ExpressionAttributeValues[":neg_one"]
        else:
            raise NotImplementedError(f"unhandled UpdateExpression: {UpdateExpression!r}")
        return {}


class _FakeSubmissionsTable:
    """Stand-in for the persist stage's REVIEW_SUBMISSIONS_TABLE lookup --
    always empty, so infra/lambda/persist/handler.py hits its documented
    no-op branch (no spend_reservation_id found -> pass event through
    unchanged). Spend-settlement behavior itself is covered by
    tests/test_spend_reservation_settlement.py."""

    def scan(self, FilterExpression=None, ExpressionAttributeValues=None):
        return {"Items": []}


def _run_real_stage_chain_and_get_final_count(semaphore_src: str, stub_src: str) -> int:
    """Runs one full successful review through the REAL acquire()/release()
    source (extracted from pipeline-stack.ts) and the REAL mock_review/
    persist Lambda handlers, threading each stage's return value into the
    next exactly as `outputPath: '$.Payload'` does (full replacement, no
    merge). Returns the semaphore table's current_count after release()."""
    env_keys = [
        "PIPELINE_SEMAPHORE_TABLE", "MAX_CONCURRENT_EXECUTIONS",
        "LEASE_SECONDS", "MAX_SEMAPHORE_WAIT_ATTEMPTS",
        "REVIEW_SUBMISSIONS_TABLE", "DAILY_SPEND_TABLE",
    ]
    saved_env = {k: os.environ.get(k) for k in env_keys}
    os.environ["PIPELINE_SEMAPHORE_TABLE"] = "test-pipeline-semaphore-190-check-g"
    os.environ["MAX_CONCURRENT_EXECUTIONS"] = "5"
    os.environ["LEASE_SECONDS"] = "900"
    os.environ["MAX_SEMAPHORE_WAIT_ATTEMPTS"] = "20"
    os.environ["REVIEW_SUBMISSIONS_TABLE"] = "test-review-submissions-190-check-g"
    os.environ["DAILY_SPEND_TABLE"] = "test-daily-spend-190-check-g"
    try:
        # 1. The REAL acquire()/release() functions, exec'd from the exact
        #    source embedded in pipeline-stack.ts (not a re-implementation).
        semaphore_ns: dict = {}
        exec(compile(semaphore_src, "<pipeline-stack.ts:semaphoreCode>", "exec"), semaphore_ns)

        tables: dict[str, _FakeSemaphoreTable] = {}
        client_error_cls = semaphore_ns["ClientError"]

        class _FakeSemaphoreDynamoDBResource:
            def Table(self, name):
                return tables.setdefault(name, _FakeSemaphoreTable(client_error_cls))

        class _FakeSemaphoreBoto3:
            def resource(self, service_name):
                assert service_name == "dynamodb"
                return _FakeSemaphoreDynamoDBResource()

        semaphore_ns["boto3"] = _FakeSemaphoreBoto3()
        acquire = semaphore_ns["acquire"]
        release = semaphore_ns["release"]
        counter_key = semaphore_ns["COUNTER_KEY"]

        # 2. The REAL Phase-0 stub source (extract/retrieve/redline/audit),
        #    exec'd from the exact source embedded in pipeline-stack.ts.
        stub_ns: dict = {}
        exec(compile(stub_src, "<pipeline-stack.ts:stubHandlerCode>", "exec"), stub_ns)
        stub_handler = stub_ns["handler"]

        # 3. The REAL mock_review handler -- the culprit stage (issue #190
        #    review round 1): it returns a fresh dict with no semaphore_*
        #    fields unless the handler explicitly carries them forward.
        mock_review_path = REPO_ROOT / "infra" / "lambda" / "mock_review" / "handler.py"
        mock_review_spec = importlib.util.spec_from_file_location(
            "mock_review_handler_190_check_g", mock_review_path,
        )
        mock_review_module = importlib.util.module_from_spec(mock_review_spec)
        mock_review_spec.loader.exec_module(mock_review_module)
        mock_review_module.MOCK_REVIEW_DELAY_SECONDS = 0  # keep the test fast

        # 4. The REAL persist handler, pointed at an always-empty fake
        #    submissions table (its documented no-op branch).
        persist_path = REPO_ROOT / "infra" / "lambda" / "persist" / "handler.py"
        persist_spec = importlib.util.spec_from_file_location(
            "persist_handler_190_check_g", persist_path,
        )
        persist_module = importlib.util.module_from_spec(persist_spec)
        persist_spec.loader.exec_module(persist_module)

        class _FakePersistDynamoDBResource:
            def Table(self, name):
                return _FakeSubmissionsTable()

        class _FakePersistBoto3:
            def resource(self, service_name):
                return _FakePersistDynamoDBResource()

        persist_module.boto3 = _FakePersistBoto3()

        # Drive one full successful review through the exact stage order
        # pipeline-stack.ts wires: acquireSlot -> extract -> retrieve ->
        # mockReview -> redline -> persist -> audit -> releaseSlot.
        review_id = f"review-190-check-g-{uuid.uuid4()}"
        event = {
            "review_id": review_id,
            "owner_sub": "owner-190-check-g",
            "playbook_id": "nda",
            "upload_s3_key": f"uploads/owner-190-check-g/{review_id}/in.docx",
        }

        event = acquire(dict(event), None)
        if event.get("semaphore_acquired") is not True:
            raise AssertionError(f"acquire() did not report semaphore_acquired == True: {event!r}")

        event = stub_handler(event, None)                  # ExtractStage
        event = stub_handler(event, None)                  # RetrieveStage
        event = mock_review_module.handler(event, None)    # MockReviewStage
        event = stub_handler(event, None)                  # RedlineStage
        event = persist_module.handler(event, None)        # PersistStage
        event = stub_handler(event, None)                  # AuditStage
        event = release(event, None)                       # ReleaseConcurrencySlot

        counter_item = tables[os.environ["PIPELINE_SEMAPHORE_TABLE"]].items.get(counter_key, {})
        return counter_item.get("current_count", 0)
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def check_g_counter_round_trips_through_real_stage_chain() -> list[str]:
    print("\nCheck G: current_count round-trips to baseline through the REAL stage chain …")
    failures: list[str] = []

    failures += _assert(
        PIPELINE_STACK_PATH.is_file(),
        "infra/lib/nested/pipeline-stack.ts exists",
    )
    if failures:
        return failures

    text = _read(PIPELINE_STACK_PATH)

    semaphore_src = _extract_inline_ts_python_source(text, "semaphoreCode")
    failures += _assert(
        semaphore_src is not None,
        "extracted the semaphoreCode inline Python source from pipeline-stack.ts",
    )
    stub_src = _extract_inline_ts_python_source(text, "stubHandlerCode")
    failures += _assert(
        stub_src is not None,
        "extracted the stubHandlerCode inline Python source from pipeline-stack.ts",
    )
    if failures:
        return failures

    try:
        final_count = _run_real_stage_chain_and_get_final_count(semaphore_src, stub_src)
    except Exception as exc:  # noqa: BLE001
        return _assert(
            False,
            "acquire -> extract -> retrieve -> mock_review -> redline -> persist -> "
            "audit -> release runs cleanly end-to-end",
            f"{type(exc).__name__}: {exc}",
        )

    failures += _assert(
        final_count == 0,
        "current_count returns to its pre-acquire baseline (0) after one full "
        "successful review",
        f"current_count after ReleaseConcurrencySlot = {final_count!r} -- nonzero "
        "means the counter leaked (release() no-op'd because semaphore_acquired "
        "did not survive the real stage chain).",
    )

    return failures


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Concurrency semaphore acquisition-gate + release-drift guard (issue #190)")
    print("=" * 60)

    all_failures: list[str] = []

    synth_failures, pipeline_template_path = _run_cdk_synth()
    all_failures += synth_failures

    if pipeline_template_path is not None:
        try:
            definition = _state_machine_definition(pipeline_template_path)
        except Exception as exc:  # noqa: BLE001
            all_failures += _assert(False, "synthesized state machine definition parses", str(exc))
            definition = None
        if definition is not None:
            all_failures += check_bcde_choice_gates_acquisition(definition.get("States", {}))

    all_failures += check_f_release_guarded_on_payload_flag()
    all_failures += check_g_counter_round_trips_through_real_stage_chain()

    print("\n" + "=" * 60)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    print("\nPASS: all issue-190 concurrency semaphore checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
