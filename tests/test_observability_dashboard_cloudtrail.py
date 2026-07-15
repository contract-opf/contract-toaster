#!/usr/bin/env python3
"""
Red gate for issue #57: CloudWatch dashboard, CloudTrail trail, and the
5xx / Bedrock-error alarms.

This test runs `cdk synth` against the ObservabilityStack nested template and
inspects the synthesized CloudFormation JSON directly, rather than parsing
docs prose, because the acceptance criteria here are concrete infrastructure
resources (a dashboard, a trail, two alarms) that must actually exist in the
template -- not just be described.

Acceptance criteria checked here (all fail against the pre-#57 repo state,
where ObservabilityStack only creates the alarms SNS topic and AWS Budgets
guardrail from issue #61):

  AC1 -- A CloudWatch dashboard named `contract-toaster-{env}` exists in the
         Observability nested stack template, with widgets covering (at
         least) App Runner request rate, App Runner 4xx/5xx error rate,
         App Runner p99 latency, and Bedrock invocations.

  AC2 -- A CloudTrail trail exists, logging to the `audit-archive` bucket.

  AC3 -- Two CloudWatch alarms exist:
         - App Runner 5xx rate > 5% for 5 minutes.
         - Any Bedrock invocation error (genuine errors only -- distinct
           from ThrottlingException retries per issue #17's reconciliation
           note).
         Both alarms must publish to the shared `contract-toaster-alarms` SNS topic
         (issue #61's AlarmsTopic).

Usage:
    python3 tests/test_observability_dashboard_cloudtrail.py
    Exit code 0 = all ACs pass; non-zero = one or more ACs fail.
"""

from __future__ import annotations

import glob
import json
import subprocess
import sys
from pathlib import Path

from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parent.parent
INFRA_DIR = REPO_ROOT / "infra"
CDK_OUT_DIR = INFRA_DIR / "cdk.out"


def run_cdk_synth() -> None:
    """Synthesize the dev environment so cdk.out/*.nested.template.json is fresh."""
    result = subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "-q"],
        cwd=INFRA_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "cdk synth failed (exit "
            f"{result.returncode}):\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def load_observability_template() -> dict:
    """Find and parse the ObservabilityStack nested-stack template."""
    candidates = sorted(
        glob.glob(str(CDK_OUT_DIR / "*Observability*.nested.template.json"))
    )
    if not candidates:
        raise FileNotFoundError(
            "No Observability nested-stack template found in "
            f"{CDK_OUT_DIR} after cdk synth. Expected a file matching "
            "'*Observability*.nested.template.json'."
        )
    with open(candidates[-1], encoding="utf-8") as fh:
        return json.load(fh)


def resources_of_type(template: dict, cfn_type: str) -> list[dict]:
    resources = template.get("Resources", {})
    return [r for r in resources.values() if r.get("Type") == cfn_type]


def flatten(obj) -> str:
    """Flatten a CFN JSON fragment (with Fn::* intrinsics) to a searchable string."""
    return json.dumps(obj)


def check_ac1_dashboard(template: dict) -> list[str]:
    failures = []
    dashboards = resources_of_type(template, "AWS::CloudWatch::Dashboard")
    if not dashboards:
        failures.append(
            "  AC1 FAIL: no AWS::CloudWatch::Dashboard resource found in the "
            "Observability nested stack template.\n"
            "  Required: a CloudWatch dashboard named contract-toaster-{env}."
        )
        return failures

    dash = dashboards[0]
    name_blob = flatten(dash["Properties"].get("DashboardName", ""))
    if "contract-toaster-" not in name_blob:
        failures.append(
            "  AC1 FAIL: dashboard DashboardName does not follow the "
            "'contract-toaster-{env}' naming convention.\n"
            f"  Got: {name_blob}"
        )

    body_blob = flatten(dash["Properties"].get("DashboardBody", ""))
    required_terms = [
        "RequestCount",
        ("4xx", "4XX", "HTTP4"),  # accept any 4xx naming variant
        ("5xx", "5XX", "HTTP5"),
        "Latency",
        "Bedrock",
    ]
    for term in required_terms:
        variants = term if isinstance(term, tuple) else (term,)
        if not any(v in body_blob for v in variants):
            failures.append(
                f"  AC1 FAIL: dashboard body does not reference {variants[0]!r} "
                "(or an accepted variant).\n"
                "  Required tiles: App Runner request rate, 4xx/5xx error rate, "
                "p99 latency, Bedrock invocations."
            )
    return failures


def check_ac2_cloudtrail(template: dict) -> list[str]:
    failures = []
    trails = resources_of_type(template, "AWS::CloudTrail::Trail")
    if not trails:
        failures.append(
            "  AC2 FAIL: no AWS::CloudTrail::Trail resource found in the "
            "Observability nested stack template.\n"
            "  Required: a CloudTrail trail logging to the audit-archive bucket."
        )
        return failures

    trail = trails[0]
    bucket_blob = flatten(trail["Properties"].get("S3BucketName", ""))
    if "audit-archive" not in bucket_blob and "AuditArchive" not in bucket_blob:
        failures.append(
            "  AC2 FAIL: CloudTrail trail S3BucketName does not reference the "
            "audit-archive bucket.\n"
            f"  Got: {bucket_blob}"
        )
    if not trail["Properties"].get("IsLogging", True):
        failures.append("  AC2 FAIL: CloudTrail trail is not enabled (IsLogging=false).")
    return failures


def check_ac3_alarms(template: dict) -> list[str]:
    failures = []
    alarms = resources_of_type(template, "AWS::CloudWatch::Alarm")
    if not alarms:
        failures.append(
            "  AC3 FAIL: no AWS::CloudWatch::Alarm resources found in the "
            "Observability nested stack template.\n"
            "  Required: a 5xx-rate alarm and a Bedrock-invocation-error alarm."
        )
        return failures

    alarm_blobs = [flatten(a) for a in alarms]

    has_5xx_alarm = any(
        ("5xx" in b or "5XX" in b or "HTTP5" in b) for b in alarm_blobs
    )
    if not has_5xx_alarm:
        failures.append(
            "  AC3 FAIL: no CloudWatch alarm references App Runner 5xx errors.\n"
            "  Required: App Runner 5xx rate > 5% for 5 minutes alarm."
        )

    has_bedrock_error_alarm = any("Bedrock" in b and "Error" in b for b in alarm_blobs)
    if not has_bedrock_error_alarm:
        failures.append(
            "  AC3 FAIL: no CloudWatch alarm references Bedrock invocation errors.\n"
            "  Required: any-Bedrock-invocation-error alarm, distinct from "
            "ThrottlingException retries (issue #17)."
        )

    # Both alarms must route to the shared contract-toaster-alarms SNS topic (AlarmsTopic,
    # issue #61) -- not a new, disconnected topic.
    topics = resources_of_type(template, "AWS::SNS::Topic")
    if not topics:
        failures.append(
            "  AC3 FAIL: no AWS::SNS::Topic found -- expected the existing "
            "contract-toaster-alarms topic (issue #61) to still be present."
        )
    else:
        alarms_with_action = [a for a in alarms if a["Properties"].get("AlarmActions")]
        if len(alarms_with_action) < 2:
            failures.append(
                "  AC3 FAIL: fewer than 2 alarms have AlarmActions wired to an "
                "SNS topic.\n"
                "  Required: both the 5xx alarm and the Bedrock-error alarm "
                "publish to the contract-toaster-alarms SNS topic."
            )

    return failures


def main() -> int:
    try:
        run_cdk_synth()
        template = load_observability_template()
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"FAIL: {exc}")
        return 1

    all_failures: list[str] = []

    ac1 = check_ac1_dashboard(template)
    ac2 = check_ac2_cloudtrail(template)
    ac3 = check_ac3_alarms(template)

    print("AC1: CloudWatch dashboard contract-toaster-{env} with required tiles")
    if ac1:
        for f in ac1:
            print(f)
        all_failures.extend(ac1)
    else:
        print("  PASS")

    print()
    print("AC2: CloudTrail trail logging to audit-archive bucket")
    if ac2:
        for f in ac2:
            print(f)
        all_failures.extend(ac2)
    else:
        print("  PASS")

    print()
    print("AC3: 5xx alarm + Bedrock-error alarm, routed to contract-toaster-alarms SNS topic")
    if ac3:
        for f in ac3:
            print(f)
        all_failures.extend(ac3)
    else:
        print("  PASS")

    print()
    if all_failures:
        print(
            f"FAIL: {len(all_failures)} issue(s) found. See issue #57 for the "
            "full remediation plan."
        )
        return 1
    else:
        print("PASS: dashboard/CloudTrail/alarm gates satisfied.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
