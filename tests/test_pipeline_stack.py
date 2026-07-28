#!/usr/bin/env python3
"""
Structural gate for issue #59: async review pipeline (Step Functions
skeleton, started directly by the API) — mock-first MVP scope (epic #123).

Per the epic #123 mock-first scope and the reconciliation section of #59
itself, this issue builds the ORCHESTRATION PLUMBING with a MOCK review
Lambda — not the real extract/retrieve/primary/critic/redline stages.

Verifies:

  A. infra/lib/nested/pipeline-stack.ts defines a Step Functions state
     machine named contract-toaster-{env}. No SQS queue or DLQ anywhere in the
     review entry path (grep the whole infra/lib tree).

  B. State machine has the stage skeleton: extract -> retrieve -> primary
     review -> adversarial review -> redline -> persist -> audit, each a
     stubbed/mock task. The mock review task returns a canned result keyed
     by playbook_id (eiaa -> REQUEST_CHANGE from a pre-baked S3 redline;
     nda -> MANUAL_REVIEW_REQUIRED "coming soon").

  C. Each stage has its own Timeout/retry policy; a Catch-all routes to an
     ERROR-handling state that updates the reviews row and releases the
     concurrency slot + reservation (no review left wedged in PENDING).

  D. State-machine-level (execution) timeout is set, in addition to
     per-step timeouts.

  E. State machine is CMK-encrypted (stateMachineKey wired from KmsKeysStack
     through AppStack/PipelineStack; issue #19).

  F. Concurrency control: a DynamoDB-backed semaphore (or Map with
     maxConcurrency / reserved concurrency) caps simultaneous executions,
     with lease/TTL semantics so a hard-killed execution cannot leak a slot
     forever.

  G. Pipeline task role is least-privilege: pipelineReviewRole is the ONLY
     role in the infra tree granted bedrock:InvokeModel scoped to the
     primary/critic review model ARNs, and the ONLY role granted
     bedrock:Retrieve / bedrock:RetrieveAndGenerate. Reconciled with issue
     #60: a Bedrock Knowledge Base service role (bedrock.amazonaws.com-
     assumed) MAY separately hold bedrock:InvokeModel scoped ONLY to the
     embedding-model ARN (amazon.titan-embed-text-v2 or equivalent) for
     ingestion -- required by AWS KB ingestion and never a grant on the
     primary/critic review models.

  H. RAG / context caps wired as config: max input doc size, max extracted
     tokens, max sections, top-K per section, max output tokens, max
     retries -- present as CfnOutput/env/config constants on the pipeline
     stack (even though the stages are stubbed).

  I. backend/src/reviews.py implements:
       - idempotency key derivation (client-supplied key preferred; else
         owner_sub + file hash + release-bundle hash + fixed-width bucket,
         checking current AND previous bucket)
       - atomic worst-case spend reservation on a conditional DynamoDB
         counter, retry-inclusive formula: passes * (1 + max_retries) *
         (max_input_tokens + max_output_tokens)
       - "ensure execution started" idempotent StartExecution wrapper
         (handles ExecutionAlreadyExists)
       - POST /api/reviews (stub) returns 202 + review id
       - GET /api/reviews/{id} reflects PENDING -> RUNNING -> DONE/ERROR

  J. infra/lib/nested/pipeline-stack.ts (or a dedicated Lambda source file)
     implements the orphan reconciler: DescribeExecution against non-
     terminal reviews with an execution_arn; FAILED/TIMED_OUT/ABORTED ->
     ERROR with reservation/slot release; re-drives "ensure execution
     started" for ARN-less stale submissions. Wired on an EventBridge
     schedule.

  K. cdk synth runs cleanly with the pipeline stack wired into the
     top-level ContractToasterStack.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import re
import subprocess
import sys
from pathlib import Path
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
INFRA_LIB = INFRA / "lib"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
PIPELINE_STACK_PATH = INFRA_LIB / "nested" / "pipeline-stack.ts"
APP_STACK_PATH = INFRA_LIB / "nested" / "app-stack.ts"
DATA_STACK_PATH = INFRA_LIB / "nested" / "data-stack.ts"
TOP_STACK_PATH = INFRA_LIB / "contract-toaster-stack.ts"
REVIEWS_PY_PATH = BACKEND_SRC / "reviews.py"
RECONCILER_PY_PATH = INFRA / "lambda" / "orphan_reconciler" / "handler.py"
MOCK_REVIEW_PY_PATH = INFRA / "lambda" / "mock_review" / "handler.py"


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


def _all_infra_ts_text() -> str:
    texts = []
    for p in INFRA_LIB.rglob("*.ts"):
        if "node_modules" in p.parts:
            continue
        texts.append(_read(p))
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# Check A -- state machine defined, no SQS on the entry path
# ---------------------------------------------------------------------------

def check_a_state_machine_no_sqs() -> list[str]:
    print("\nCheck A: Step Functions state machine defined; NO SQS on the entry path …")
    failures: list[str] = []

    failures += _assert(
        PIPELINE_STACK_PATH.is_file(),
        "infra/lib/nested/pipeline-stack.ts exists",
        f"Expected: {PIPELINE_STACK_PATH}",
    )
    if failures:
        return failures

    text = _read(PIPELINE_STACK_PATH)

    failures += _assert(
        bool(re.search(r"aws-stepfunctions['\"]", text)),
        "pipeline-stack.ts imports aws-cdk-lib/aws-stepfunctions",
    )

    failures += _assert(
        bool(re.search(r"contract-toaster-\$\{envName\}", text)),
        "State machine is named contract-toaster-{env}",
        "Per AC: Step Functions state machine contract-toaster-{env}.",
    )

    failures += _assert(
        bool(re.search(r"StateMachine\s*\(", text)),
        "pipeline-stack.ts instantiates a sfn.StateMachine",
    )

    # No SQS anywhere in the infra tree (whole review entry path).
    all_ts = _all_infra_ts_text()
    has_sqs = bool(re.search(r"aws-sqs|new\s+sqs\.Queue|sqs\.CfnQueue", all_ts))
    failures += _assert(
        not has_sqs,
        "No SQS queue/DLQ anywhere in infra/lib (review entry path has no SQS)",
        "Per AC: API -> Step Functions is direct/synchronous from the caller's perspective.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B -- stage skeleton + mock review task
# ---------------------------------------------------------------------------

def check_b_stage_skeleton_mock_task() -> list[str]:
    print("\nCheck B: stage skeleton (extract..audit) + mock review task …")
    failures: list[str] = []

    if not PIPELINE_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/pipeline-stack.ts exists")

    text = _read(PIPELINE_STACK_PATH)

    for stage in ["extract", "retrieve", "redline", "persist", "audit"]:
        failures += _assert(
            stage in text.lower(),
            f"pipeline-stack.ts references the '{stage}' stage",
        )

    # primary + adversarial review stages (mock)
    has_primary = bool(re.search(r"primary.?review|mock.?review", text, re.IGNORECASE))
    failures += _assert(
        has_primary,
        "pipeline-stack.ts references a primary/mock review stage",
    )
    has_adversarial = bool(re.search(r"adversarial|critic", text, re.IGNORECASE))
    failures += _assert(
        has_adversarial,
        "pipeline-stack.ts references an adversarial/critic review stage",
    )

    # Mock Lambda handler source exists and returns canned results per playbook_id.
    failures += _assert(
        MOCK_REVIEW_PY_PATH.is_file(),
        "infra/lambda/mock_review/handler.py exists",
        f"Expected: {MOCK_REVIEW_PY_PATH}",
    )
    if MOCK_REVIEW_PY_PATH.is_file():
        mock_text = _read(MOCK_REVIEW_PY_PATH)
        failures += _assert(
            "REQUEST_CHANGE" in mock_text and "eiaa" in mock_text,
            "mock_review handler returns REQUEST_CHANGE for playbook_id == 'eiaa'",
        )
        failures += _assert(
            "MANUAL_REVIEW_REQUIRED" in mock_text and "nda" in mock_text,
            "mock_review handler returns MANUAL_REVIEW_REQUIRED 'coming soon' for playbook_id == 'nda'",
        )
        failures += _assert(
            bool(re.search(r"time\.sleep|asyncio\.sleep|delay", mock_text, re.IGNORECASE)),
            "mock_review handler waits briefly (exercises PENDING -> RUNNING -> DONE polling)",
        )
        # Pointer-only check: the handler must reference S3 keys/pointers,
        # and must not read/pass a full document body inline (no doc_text /
        # document_content / document_body style fields in the payload).
        failures += _assert(
            bool(re.search(r"s3_key|s3://|bucket", mock_text, re.IGNORECASE))
            and not bool(
                re.search(r"doc_text|document_content|document_body|full_text", mock_text, re.IGNORECASE)
            ),
            "mock_review handler payload is pointer-only (S3 keys, not inline document text)",
        )

    return failures


# ---------------------------------------------------------------------------
# Check C -- per-stage timeout/retry + Catch-all -> ERROR
# ---------------------------------------------------------------------------

def check_c_timeout_retry_catch() -> list[str]:
    print("\nCheck C: per-stage timeout/retry policy + Catch-all -> ERROR …")
    failures: list[str] = []

    if not PIPELINE_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/pipeline-stack.ts exists")

    text = _read(PIPELINE_STACK_PATH)

    failures += _assert(
        bool(re.search(r"\.addRetry\s*\(", text)),
        "pipeline-stack.ts calls .addRetry(...) on stage tasks",
    )
    failures += _assert(
        bool(re.search(r"\.addCatch\s*\(", text)),
        "pipeline-stack.ts calls .addCatch(...) to route failures to an error handler",
    )
    failures += _assert(
        bool(re.search(r"taskTimeout|timeout:\s*cdk\.Duration|\.timeout\(", text, re.IGNORECASE)),
        "pipeline-stack.ts sets a per-task timeout",
    )
    failures += _assert(
        bool(re.search(r"ERROR", text)) and bool(re.search(r"failing.?stage|failed.?stage|stage.?name", text, re.IGNORECASE)),
        "Error path records the failing stage and transitions the review to ERROR",
        "Per AC: a failed stage transitions execution + reviews row to ERROR with the failing stage recorded.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D -- execution-level (state-machine) timeout
# ---------------------------------------------------------------------------

def check_d_execution_level_timeout() -> list[str]:
    print("\nCheck D: state-machine-level execution timeout …")
    failures: list[str] = []

    if not PIPELINE_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/pipeline-stack.ts exists")

    text = _read(PIPELINE_STACK_PATH)

    failures += _assert(
        bool(re.search(r"timeout:\s*cdk\.Duration\.(minutes|hours)\(", text)),
        "pipeline-stack.ts sets an execution-level timeout on the StateMachine construct",
        "Per ARCHITECTURE.md: the state machine carries an overall execution-level timeout "
        "in addition to per-step timeouts.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check E -- CMK encryption wired
# ---------------------------------------------------------------------------

def check_e_cmk_encryption() -> list[str]:
    print("\nCheck E: state machine is CMK-encrypted (issue #19) …")
    failures: list[str] = []

    if not PIPELINE_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/pipeline-stack.ts exists")

    text = _read(PIPELINE_STACK_PATH)

    failures += _assert(
        bool(re.search(r"stateMachineKey|kms\.IKey|encryptionConfiguration|kmsKey", text, re.IGNORECASE)),
        "pipeline-stack.ts wires a CMK for the state machine",
    )

    if TOP_STACK_PATH.is_file():
        top_text = _read(TOP_STACK_PATH)
        failures += _assert(
            "Pipeline" in top_text and "pipeline-stack" not in top_text or "PipelineStack" in top_text,
            "contract-toaster-stack.ts instantiates PipelineStack",
        )

    return failures


# ---------------------------------------------------------------------------
# Check F -- concurrency semaphore with lease/TTL
# ---------------------------------------------------------------------------

def check_f_concurrency_semaphore() -> list[str]:
    print("\nCheck F: concurrency control (semaphore) with lease/TTL semantics …")
    failures: list[str] = []

    if not PIPELINE_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/pipeline-stack.ts exists")

    text = _read(PIPELINE_STACK_PATH)
    data_text = _read(DATA_STACK_PATH) if DATA_STACK_PATH.is_file() else ""

    has_semaphore_concept = bool(
        re.search(r"semaphore|maxConcurrency|reserved.?concurrency|concurrency.?slot", text, re.IGNORECASE)
    )
    failures += _assert(
        has_semaphore_concept,
        "pipeline-stack.ts implements a concurrency semaphore / cap",
    )

    has_ttl = bool(re.search(r"ttl|TimeToLive|lease", text + data_text, re.IGNORECASE))
    failures += _assert(
        has_ttl,
        "Semaphore/lease has TTL semantics (slot-leak recovery)",
        "Per ARCHITECTURE.md: lease/TTL semantics on semaphore entries so a hard-killed "
        "execution cannot leak a slot permanently.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G -- least-privilege pipeline task role; sole bedrock:InvokeModel holder
# ---------------------------------------------------------------------------

def check_g_least_privilege_bedrock() -> list[str]:
    print("\nCheck G: pipelineReviewRole is the ONLY holder of bedrock:InvokeModel on the "
          "primary/critic model ARNs and of bedrock:Retrieve/RetrieveAndGenerate "
          "(reconciled with issue #60 -- ARN-scoped, not action-name-wide) …")
    failures: list[str] = []

    if not PIPELINE_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/pipeline-stack.ts exists")

    pipeline_text = _read(PIPELINE_STACK_PATH)

    failures += _assert(
        bool(re.search(r"bedrock:InvokeModel", pipeline_text)),
        "pipeline-stack.ts grants bedrock:InvokeModel to the pipeline task role",
    )
    failures += _assert(
        bool(re.search(r"anthropic\.claude-opus", pipeline_text))
        and bool(re.search(r"anthropic\.claude-sonnet", pipeline_text)),
        "pipeline-stack.ts scopes bedrock:InvokeModel to the primary (Opus) and critic (Sonnet) model ARNs",
    )

    # No other infra file should grant bedrock:InvokeModel scoped to the
    # primary/critic (Anthropic Claude) model ARNs. A Bedrock KB service role
    # (bedrock.amazonaws.com-assumed) MAY separately hold bedrock:InvokeModel
    # scoped ONLY to the embedding-model ARN for ingestion (issue #60
    # reconciliation) -- that is not a violation of this invariant.
    offenders = []
    for p in INFRA_LIB.rglob("*.ts"):
        if "node_modules" in p.parts or p == PIPELINE_STACK_PATH:
            continue
        text = _read(p)
        if "bedrock:InvokeModel" in text and "anthropic.claude" in text:
            offenders.append(str(p.relative_to(REPO_ROOT)))

    failures += _assert(
        not offenders,
        "No other infra/lib file grants bedrock:InvokeModel scoped to the primary/critic (Anthropic Claude) model ARNs",
        f"Offending files: {offenders}" if offenders else "",
    )

    # pipelineReviewRole remains the ONLY role granted KB query actions.
    kb_query_offenders = []
    for p in INFRA_LIB.rglob("*.ts"):
        if "node_modules" in p.parts or p == PIPELINE_STACK_PATH:
            continue
        text = _read(p)
        if "bedrock:Retrieve" in text or "bedrock:RetrieveAndGenerate" in text:
            kb_query_offenders.append(str(p.relative_to(REPO_ROOT)))

    failures += _assert(
        not kb_query_offenders,
        "No other infra/lib file grants bedrock:Retrieve/RetrieveAndGenerate",
        f"Offending files: {kb_query_offenders}" if kb_query_offenders else "",
    )

    # app-stack.ts must still explicitly exclude it (regression guard already in #55).
    if APP_STACK_PATH.is_file():
        app_text = _read(APP_STACK_PATH)
        failures += _assert(
            "bedrock:InvokeModel" not in app_text,
            "app-stack.ts (API task role) still does NOT grant bedrock:InvokeModel",
        )

    return failures


# ---------------------------------------------------------------------------
# Check H -- RAG / context caps wired as config
# ---------------------------------------------------------------------------

def check_h_context_caps() -> list[str]:
    print("\nCheck H: RAG / context caps wired as state-machine/config limits …")
    failures: list[str] = []

    if not PIPELINE_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/pipeline-stack.ts exists")

    text = _read(PIPELINE_STACK_PATH)

    for cap in [
        "MAX_INPUT_TOKENS",
        "MAX_OUTPUT_TOKENS",
        "MAX_EXTRACTED_TOKENS",
        "MAX_SECTIONS",
        "TOP_K",
        "MAX_RETRIES",
    ]:
        failures += _assert(
            cap in text,
            f"pipeline-stack.ts defines/wires {cap}",
        )

    return failures


# ---------------------------------------------------------------------------
# Check I -- backend/src/reviews.py: idempotency, reservation, ensure-started
# ---------------------------------------------------------------------------

def check_i_reviews_py() -> list[str]:
    print("\nCheck I: backend/src/reviews.py -- idempotency, reservation, API stub …")
    failures: list[str] = []

    failures += _assert(
        REVIEWS_PY_PATH.is_file(),
        "backend/src/reviews.py exists",
        f"Expected: {REVIEWS_PY_PATH}",
    )
    if failures:
        return failures

    text = _read(REVIEWS_PY_PATH)

    failures += _assert(
        bool(re.search(r"idempotency_key|idempotent", text, re.IGNORECASE)),
        "reviews.py handles idempotency key derivation",
    )
    failures += _assert(
        bool(re.search(r"previous.?bucket|prior.?bucket", text, re.IGNORECASE))
        and bool(re.search(r"current.?bucket", text, re.IGNORECASE)),
        "reviews.py checks BOTH current and previous timestamp bucket",
        "Per AC: derive path checks current AND previous bucket to avoid boundary-straddling double-run.",
    )
    failures += _assert(
        bool(re.search(r"BUCKET_WIDTH|bucket_width_minutes|TIMESTAMP_BUCKET", text, re.IGNORECASE)),
        "reviews.py documents/uses a fixed-width timestamp bucket constant (default 10 min)",
    )
    failures += _assert(
        bool(re.search(r"ConditionExpression|attribute_not_exists|conditional", text, re.IGNORECASE)),
        "reviews.py uses a conditional write for the submission record",
    )
    failures += _assert(
        bool(re.search(r"reserv", text, re.IGNORECASE))
        and bool(re.search(r"worst.?case|passes\s*\*|max_retries", text, re.IGNORECASE)),
        "reviews.py computes a worst-case, retry-inclusive spend reservation",
        "Formula: passes * (1 + max_retries_per_pass) * (max_input_tokens + max_output_tokens).",
    )
    failures += _assert(
        bool(re.search(r"daily.?cap|daily.?ceiling|DAILY_SPEND", text, re.IGNORECASE)),
        "reviews.py enforces (or references) the daily spend ceiling",
    )
    failures += _assert(
        bool(re.search(r"ExecutionAlreadyExists", text)),
        "reviews.py handles ExecutionAlreadyExists (ensure-execution-started)",
    )
    failures += _assert(
        bool(re.search(r"ensure.?execution.?start", text, re.IGNORECASE)),
        "reviews.py implements 'ensure execution started'",
    )
    failures += _assert(
        bool(re.search(r'"?202"?|status_code\s*=\s*202|HTTP_202', text)),
        "reviews.py returns 202 on submission",
    )
    failures += _assert(
        bool(re.search(r"PENDING", text)) and bool(re.search(r"RUNNING", text)) and bool(re.search(r"\bDONE\b", text)),
        "reviews.py references the PENDING -> RUNNING -> DONE/ERROR status lifecycle",
    )
    failures += _assert(
        bool(re.search(r"release.?bundle|bundle_hash", text, re.IGNORECASE))
        and bool(re.search(r"resolve.*once|single.?resolution|resolved.*submission", text, re.IGNORECASE)),
        "reviews.py resolves the release bundle ONCE at submission (never re-resolved by execution)",
        "Reconciliation note #21.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check J -- orphan reconciler
# ---------------------------------------------------------------------------

def check_j_orphan_reconciler() -> list[str]:
    print("\nCheck J: orphan reconciler (dead-execution handling + re-drive) …")
    failures: list[str] = []

    failures += _assert(
        RECONCILER_PY_PATH.is_file(),
        "infra/lambda/orphan_reconciler/handler.py exists",
        f"Expected: {RECONCILER_PY_PATH}",
    )
    if RECONCILER_PY_PATH.is_file():
        text = _read(RECONCILER_PY_PATH)
        failures += _assert(
            bool(re.search(r"DescribeExecution", text)),
            "reconciler calls DescribeExecution on non-terminal reviews with an ARN",
        )
        for term in ["FAILED", "TIMED_OUT", "ABORTED"]:
            failures += _assert(
                term in text,
                f"reconciler checks terminal execution status {term}",
            )
        failures += _assert(
            bool(re.search(r"ERROR", text)),
            "reconciler transitions dead executions to ERROR",
        )
        failures += _assert(
            bool(re.search(r"release.*(slot|reservation)|reservation.*release|slot.*release", text, re.IGNORECASE)),
            "reconciler releases the reservation and concurrency slot on the dead-execution path",
        )
        failures += _assert(
            bool(re.search(r"ensure.?execution.?start|StartExecution", text, re.IGNORECASE)),
            "reconciler re-drives 'ensure execution started' for ARN-less stale submissions",
        )

    if PIPELINE_STACK_PATH.is_file():
        pipeline_text = _read(PIPELINE_STACK_PATH)
        failures += _assert(
            bool(re.search(r"Rule\s*\(|events\.Rule|Schedule\.", pipeline_text)),
            "pipeline-stack.ts wires the reconciler on an EventBridge schedule",
        )

    return failures


# ---------------------------------------------------------------------------
# Check K -- cdk synth
# ---------------------------------------------------------------------------

def check_k_cdk_synth() -> list[str]:
    print("\nCheck K: cdk synth runs cleanly with the pipeline stack wired in …")
    failures: list[str] = []

    if not INFRA.is_dir():
        return _assert(False, "infra/ directory exists (prerequisite for cdk synth)")

    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        print("  (node_modules absent — running npm install first …)")
        install = subprocess.run(
            ["npm", "install"], cwd=INFRA, capture_output=True, text=True
        )
        if install.returncode != 0:
            return _assert(
                False,
                "npm install succeeded in infra/",
                f"stdout: {install.stdout[-500:]}\nstderr: {install.stderr[-500:]}",
            )

    result = subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
    )
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev exits 0",
        f"stdout (last 1200 chars): {result.stdout[-1200:]}\n"
        f"stderr (last 1200 chars): {result.stderr[-1200:]}",
    )

    return failures


def main() -> int:
    print("Async review pipeline structural gate (issue #59, mock-first MVP scope)")
    print("=" * 70)

    all_failures: list[str] = []
    all_failures += check_a_state_machine_no_sqs()
    all_failures += check_b_stage_skeleton_mock_task()
    all_failures += check_c_timeout_retry_catch()
    all_failures += check_d_execution_level_timeout()
    all_failures += check_e_cmk_encryption()
    all_failures += check_f_concurrency_semaphore()
    all_failures += check_g_least_privilege_bedrock()
    all_failures += check_h_context_caps()
    all_failures += check_i_reviews_py()
    all_failures += check_j_orphan_reconciler()
    all_failures += check_k_cdk_synth()

    print("\n" + "=" * 70)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    print("\nPASS: all async review pipeline structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
