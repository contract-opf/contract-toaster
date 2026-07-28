#!/usr/bin/env python3
"""
Structural + synth gate for issue #61 AC: retention purge worker
infrastructure and the AWS Budgets guardrail.

Checks:

  A. `retention_settings` DynamoDB table defined in data-stack.ts, PK
     setting_id, PITR enabled, encrypted with the dynamodbKey CMK.
  B. Purge worker Lambda defined in pipeline-stack.ts, pointing at
     infra/lambda/purge_worker, on a scheduled EventBridge rule.
  C. Least-privilege: the purge worker is granted delete only on the
     uploads/outputs buckets (grantDelete calls), and is NOT granted any
     Bedrock or Step Functions permission.
  D. AWS Budgets `CfnBudget` defined in observability-stack.ts with a
     monthly COST budget, notifications routed to an SNS topic (issue #61
     AC: "AWS Budgets monthly budget + alarm defined in CDK ... routing to
     the alarms SNS topic").
  E. `cdk synth` runs cleanly and the synthesized Pipeline/Observability
     nested-stack templates actually contain the purge worker Lambda, the
     retention_settings table, the SNS topic, and the Budgets resource.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import json
import subprocess
import sys
from pathlib import Path

from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
DATA_STACK = INFRA / "lib" / "nested" / "data-stack.ts"
PIPELINE_STACK = INFRA / "lib" / "nested" / "pipeline-stack.ts"
OBSERVABILITY_STACK = INFRA / "lib" / "nested" / "observability-stack.ts"
PURGE_WORKER_HANDLER = INFRA / "lambda" / "purge_worker" / "handler.py"


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
# Check A — retention_settings table
# ---------------------------------------------------------------------------

def check_a_retention_settings_table() -> list[str]:
    print("\nCheck A: retention_settings DynamoDB table defined …")
    failures: list[str] = []
    text = _read(DATA_STACK)

    failures += _assert(
        "RetentionSettingsTable" in text,
        "retentionSettingsTable construct present in data-stack.ts",
    )
    failures += _assert(
        "contract-toaster-retention-settings-" in text
        or "retention-settings-${envName}" in text,
        "table name follows {appName}-retention-settings-{env} convention "
        "(default appName is 'contract-toaster'; issue #233 made the prefix a "
        "CDK context parameter)",
    )
    failures += _assert(
        "setting_id" in text,
        "partition key setting_id referenced",
    )
    # PITR + CMK on the same construct block.
    idx = text.find("RetentionSettingsTable")
    block = text[idx: idx + 1200] if idx != -1 else ""
    failures += _assert(
        "pointInTimeRecovery: true" in block,
        "retention_settings table has pointInTimeRecovery: true",
    )
    failures += _assert(
        "dynamodbKey" in block or "encryptionKey: dynamodbKey" in block,
        "retention_settings table encrypted with dynamodbKey CMK",
    )
    return failures


# ---------------------------------------------------------------------------
# Check B — purge worker Lambda + schedule
# ---------------------------------------------------------------------------

def check_b_purge_worker_lambda() -> list[str]:
    print("\nCheck B: Purge worker Lambda + schedule defined in pipeline-stack.ts …")
    failures: list[str] = []
    text = _read(PIPELINE_STACK)

    failures += _assert(
        "purgeWorkerFunction" in text,
        "purgeWorkerFunction construct present",
    )
    failures += _assert(
        "lambda/purge_worker" in text,
        "purge worker Lambda code points at infra/lambda/purge_worker",
    )
    failures += _assert(
        "RetentionPurgeWorkerSchedule" in text and "events.Rule" in text,
        "purge worker has an EventBridge schedule rule",
    )
    failures += _assert(
        PURGE_WORKER_HANDLER.is_file(),
        "infra/lambda/purge_worker/handler.py exists",
    )
    return failures


# ---------------------------------------------------------------------------
# Check C — least privilege: delete only in uploads/outputs, no Bedrock/SFN
# ---------------------------------------------------------------------------

def check_c_least_privilege() -> list[str]:
    print("\nCheck C: Purge worker role is least-privilege …")
    failures: list[str] = []
    text = _read(PIPELINE_STACK)

    idx = text.find("this.purgeWorkerFunction = new lambda.Function")
    failures += _assert(idx != -1, "purgeWorkerFunction construction found")
    if idx == -1:
        return failures

    # Look at the grant block immediately following construction (up to the
    # next construct's grants / the schedule rule).
    end_idx = text.find("RetentionPurgeWorkerSchedule", idx)
    block = text[idx:end_idx] if end_idx != -1 else text[idx: idx + 3000]

    failures += _assert(
        "grantDelete(this.purgeWorkerFunction)" in block,
        "grantDelete granted to the purge worker (uploads/outputs loop or explicit calls)",
    )
    failures += _assert(
        "uploadsBucket" in block and "outputsBucket" in block,
        "both uploadsBucket and outputsBucket referenced in the purge worker grant block",
    )
    failures += _assert(
        "corpusBucket" not in block,
        "no corpus bucket grant in the purge worker block",
    )
    failures += _assert(
        "auditArchiveBucket" not in block,
        "no audit-archive bucket grant in the purge worker block",
    )
    failures += _assert(
        "bedrock" not in block.lower(),
        "no Bedrock permission granted to the purge worker",
    )
    failures += _assert(
        "stateMachine.grant" not in block and "states:" not in block,
        "no Step Functions permission granted to the purge worker",
    )
    # Issue #70 AC B (per-data-class KMS principal isolation): the purge
    # worker must not be granted s3:GetObject (grantRead) on uploads/outputs,
    # since that would add a kms:Decrypt grant on BOTH bucket CMKs to the
    # same role/policy alongside the dynamodbKey decrypt it also needs --
    # violating "no single IAM principal decrypts >1 data-class key". Delete
    # and List do not require decrypting object content.
    failures += _assert(
        "grantRead(this.purgeWorkerFunction)" not in block,
        "no grantRead (s3:GetObject / kms:Decrypt) granted to the purge worker "
        "(would violate issue #70 AC B cross-data-class KMS principal isolation)",
    )
    return failures


# ---------------------------------------------------------------------------
# Check D — AWS Budgets construct routed to the alarms SNS topic
# ---------------------------------------------------------------------------

def check_d_aws_budgets() -> list[str]:
    print("\nCheck D: AWS Budgets monthly cost guardrail routed to alarms SNS topic …")
    failures: list[str] = []
    text = _read(OBSERVABILITY_STACK)

    failures += _assert(
        "CfnBudget" in text,
        "budgets.CfnBudget construct present in observability-stack.ts",
    )
    failures += _assert(
        "'COST'" in text or '"COST"' in text,
        "budget type is COST",
    )
    failures += _assert(
        "'MONTHLY'" in text or '"MONTHLY"' in text,
        "budget time unit is MONTHLY",
    )
    failures += _assert(
        "monthlyBudgetUsd" in text and "?? 100" in text,
        "monthly budget target defaults to <= $100/mo (dev)",
    )
    failures += _assert(
        "sns.Topic" in text and "alarmsTopic" in text,
        "alarms SNS topic construct present",
    )
    failures += _assert(
        "this.alarmsTopic.topicArn" in text and "SNS" in text,
        "Budget notification subscriber routes to the alarms SNS topic ARN",
    )
    return failures


# ---------------------------------------------------------------------------
# Check E — cdk synth produces the expected resources
# ---------------------------------------------------------------------------

def check_e_cdk_synth() -> list[str]:
    print("\nCheck E: cdk synth produces purge worker + Budgets/SNS resources …")
    failures: list[str] = []

    if not INFRA.is_dir():
        return _assert(False, "infra/ directory exists (prerequisite for cdk synth)")

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
            )

    result = subprocess.run(
        ["npx", "cdk", "synth", "--context", "envName=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
    )
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context envName=dev exits 0",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    if failures:
        return failures

    cdk_out = INFRA / "cdk.out"
    pipeline_templates = list(cdk_out.glob("*Pipeline*.nested.template.json"))
    observability_templates = list(cdk_out.glob("*Observability*.nested.template.json"))

    failures += _assert(len(pipeline_templates) == 1, "exactly one Pipeline nested template found")
    failures += _assert(len(observability_templates) == 1, "exactly one Observability nested template found")
    if failures:
        return failures

    pipeline_resources = json.loads(pipeline_templates[0].read_text())["Resources"]
    observability_resources = json.loads(observability_templates[0].read_text())["Resources"]

    has_purge_lambda = any(
        r["Type"] == "AWS::Lambda::Function" and "PurgeWorker" in name
        for name, r in pipeline_resources.items()
    )
    failures += _assert(has_purge_lambda, "Pipeline template contains the PurgeWorker Lambda function")

    has_purge_schedule = any(
        r["Type"] == "AWS::Events::Rule" and "PurgeWorker" in name
        for name, r in pipeline_resources.items()
    )
    failures += _assert(has_purge_schedule, "Pipeline template contains the PurgeWorker EventBridge rule")

    has_sns_topic = any(r["Type"] == "AWS::SNS::Topic" for r in observability_resources.values())
    failures += _assert(has_sns_topic, "Observability template contains an SNS::Topic")

    has_budget = any(r["Type"] == "AWS::Budgets::Budget" for r in observability_resources.values())
    failures += _assert(has_budget, "Observability template contains a Budgets::Budget")

    return failures


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        ("A", check_a_retention_settings_table),
        ("B", check_b_purge_worker_lambda),
        ("C", check_c_least_privilege),
        ("D", check_d_aws_budgets),
        ("E", check_e_cdk_synth),
    ]

    overall_pass = True
    for code, fn in checks:
        failures = fn()
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All issue-61 infra checks passed.")
        return 0
    print("One or more issue-61 infra checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
