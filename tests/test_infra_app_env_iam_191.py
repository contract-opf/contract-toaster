#!/usr/bin/env python3
"""
Structural gate for issue #191: deployed App Runner service lacks the env
vars and IAM grants for the admin routes that ARE mounted.

Prior bug (issue #191 concern): the only runtime env vars injected into the
App Runner container were COGNITO_USER_POOL_ID, COGNITO_APP_CLIENT_ID, and
AWS_REGION (app-stack.ts:404-417). But every mounted /api/* route
dereferences os.environ['USERS_TABLE'] / AUDIT_TABLE / SYNC_STATUS_TABLE
(backend/src/users.py:71-79) and REVIEWS_TABLE / RETENTION_SETTINGS_TABLE /
UPLOADS_BUCKET / OUTPUTS_BUCKET (backend/src/retention.py:80-88,357-358) --
a KeyError -> HTTP 500 on first authenticated request in a deployed
environment. The API task role policy also granted DynamoDB access only to
reviews/review-submissions/users tables (app-stack.ts:348-363) -- no grant
for audit, sync_status, or retention_settings tables, so even with env vars
set, retention admin actions and audit writes would be IAM-denied.

This gate verifies, against the SYNTHESIZED App nested-stack template (not
just the .ts source):

  A. cdk synth runs cleanly.
  B. The App Runner CfnService's RuntimeEnvironmentVariables contains every
     env var the mounted /api/* routes dereference: USERS_TABLE,
     AUDIT_TABLE, SYNC_STATUS_TABLE, REVIEWS_TABLE,
     RETENTION_SETTINGS_TABLE, UPLOADS_BUCKET, OUTPUTS_BUCKET (in addition
     to the pre-existing COGNITO_USER_POOL_ID / COGNITO_APP_CLIENT_ID /
     AWS_REGION).
  C. The API task-role IAM policy grants dynamodb access to the audit
     table, scoped to PutItem ONLY (append-only, per the documented audit
     posture in ARCHITECTURE.md "Audit posture" -- no GetItem, Query,
     UpdateItem, or DeleteItem).
  D. The API task-role IAM policy grants dynamodb:GetItem on the
     sync_status table.
  E. The API task-role IAM policy grants dynamodb:GetItem and
     dynamodb:UpdateItem on the retention_settings table.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"


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
# Check A -- cdk synth runs cleanly; locate the App nested template.
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
    app_templates = list(cdk_out.glob("*App*.nested.template.json"))
    failures += _assert(len(app_templates) == 1, "exactly one App nested template found",
                         f"found: {[p.name for p in app_templates]}")
    if failures:
        return failures, None

    return failures, app_templates[0]


def _load_template(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _app_runner_service(resources: dict[str, Any]) -> dict[str, Any] | None:
    for _name, r in resources.items():
        if r.get("Type") == "AWS::AppRunner::Service":
            return r
    return None


def _api_task_role_policy(resources: dict[str, Any]) -> dict[str, Any] | None:
    """The API task role policy is named `ApiTaskRolePolicy` in app-stack.ts
    (iam.Policy construct id) -- CDK synthesizes it to an AWS::IAM::Policy
    resource whose logical id starts with that construct id. There is also
    an AWS::IAM::Policy for the App Runner ECR access role
    (AppRunnerAccessRoleDefaultPolicy...) -- distinguish by looking for the
    'StartReview' / 'ReviewDynamoDb' sids that only the API task role policy
    carries.
    """
    for _name, r in resources.items():
        if r.get("Type") != "AWS::IAM::Policy":
            continue
        statements = r.get("Properties", {}).get("PolicyDocument", {}).get("Statement", [])
        sids = {s.get("Sid") for s in statements}
        if "ReviewDynamoDb" in sids:
            return r
    return None


def _statements_for_resource_substring(statements: list[dict[str, Any]], substring: str) -> list[dict[str, Any]]:
    """Return policy statements whose Resource (str or list of str) contains
    a string matching `substring`."""
    matches = []
    for s in statements:
        resource = s.get("Resource")
        resources_list = resource if isinstance(resource, list) else [resource]
        for r in resources_list:
            if isinstance(r, str) and substring in r:
                matches.append(s)
                break
    return matches


def _actions(statement: dict[str, Any]) -> set[str]:
    action = statement.get("Action")
    if isinstance(action, list):
        return set(action)
    if isinstance(action, str):
        return {action}
    return set()


# ---------------------------------------------------------------------------
# Check B -- RuntimeEnvironmentVariables contains every required var.
# ---------------------------------------------------------------------------

REQUIRED_ENV_VARS = {
    "USERS_TABLE": "contract-toaster-users-dev",
    "AUDIT_TABLE": "contract-toaster-audit-dev",
    "SYNC_STATUS_TABLE": "contract-toaster-sync-status-dev",
    "REVIEWS_TABLE": "contract-toaster-reviews-dev",
    "RETENTION_SETTINGS_TABLE": "contract-toaster-retention-settings-dev",
    "UPLOADS_BUCKET": "contract-toaster-uploads-dev",
    "OUTPUTS_BUCKET": "contract-toaster-outputs-dev",
}


def check_b_runtime_env_vars(service: dict[str, Any]) -> list[str]:
    print("\nCheck B: App Runner RuntimeEnvironmentVariables carries every "
          "env var the mounted /api/* routes dereference …")
    failures: list[str] = []

    image_config = (
        service.get("Properties", {})
        .get("SourceConfiguration", {})
        .get("ImageRepository", {})
        .get("ImageConfiguration", {})
    )
    env_vars = image_config.get("RuntimeEnvironmentVariables", [])
    by_name = {e.get("Name"): e.get("Value") for e in env_vars}

    # Pre-existing vars must still be present (regression guard).
    for name in ("COGNITO_USER_POOL_ID", "COGNITO_APP_CLIENT_ID", "AWS_REGION"):
        failures += _assert(name in by_name, f"pre-existing env var {name} still present")

    for name, expected_value in REQUIRED_ENV_VARS.items():
        failures += _assert(
            name in by_name,
            f"RuntimeEnvironmentVariables includes {name}",
            f"present names: {sorted(by_name.keys())}",
        )
        if name in by_name:
            failures += _assert(
                by_name[name] == expected_value,
                f"{name} value follows the contract-toaster-<resource>-<env> naming convention",
                f"got: {by_name[name]!r}, expected: {expected_value!r}",
            )

    return failures


# ---------------------------------------------------------------------------
# Checks C-E -- API task role IAM policy grants the tables each mounted
# route needs.
# ---------------------------------------------------------------------------


def check_cde_task_role_grants(policy: dict[str, Any]) -> list[str]:
    print("\nChecks C-E: API task-role IAM policy grants audit/sync_status/"
          "retention_settings table access …")
    failures: list[str] = []

    statements = policy.get("Properties", {}).get("PolicyDocument", {}).get("Statement", [])

    # C: audit table -- PutItem ONLY (append-only posture).
    audit_statements = _statements_for_resource_substring(statements, "table/contract-toaster-audit-")
    failures += _assert(
        len(audit_statements) >= 1,
        "a policy statement grants access to the audit table",
        f"statements: {json.dumps(statements, indent=2)[:2000]}",
    )
    if audit_statements:
        audit_actions: set[str] = set()
        for s in audit_statements:
            audit_actions |= _actions(s)
        failures += _assert(
            audit_actions == {"dynamodb:PutItem"},
            "audit table grant is PutItem ONLY (append-only; no GetItem/Query/UpdateItem/DeleteItem)",
            f"got actions: {sorted(audit_actions)}",
        )

    # D: sync_status table -- GetItem.
    sync_status_statements = _statements_for_resource_substring(statements, "table/contract-toaster-sync-status-")
    failures += _assert(
        len(sync_status_statements) >= 1,
        "a policy statement grants access to the sync_status table",
    )
    if sync_status_statements:
        sync_status_actions: set[str] = set()
        for s in sync_status_statements:
            sync_status_actions |= _actions(s)
        failures += _assert(
            "dynamodb:GetItem" in sync_status_actions,
            "sync_status table grant includes GetItem",
            f"got actions: {sorted(sync_status_actions)}",
        )

    # E: retention_settings table -- GetItem + UpdateItem.
    retention_statements = _statements_for_resource_substring(
        statements, "table/contract-toaster-retention-settings-"
    )
    failures += _assert(
        len(retention_statements) >= 1,
        "a policy statement grants access to the retention_settings table",
    )
    if retention_statements:
        retention_actions: set[str] = set()
        for s in retention_statements:
            retention_actions |= _actions(s)
        failures += _assert(
            {"dynamodb:GetItem", "dynamodb:UpdateItem"} <= retention_actions,
            "retention_settings table grant includes GetItem and UpdateItem",
            f"got actions: {sorted(retention_actions)}",
        )

    # Regression guard: the pre-existing reviews/review-submissions/users
    # grant must still be present.
    review_statements = [s for s in statements if s.get("Sid") == "ReviewDynamoDb"]
    failures += _assert(
        len(review_statements) == 1,
        "pre-existing ReviewDynamoDb statement (reviews/review-submissions/users) still present",
    )

    return failures


def main() -> int:
    all_failures: list[str] = []

    synth_failures, app_template_path = _run_cdk_synth()
    all_failures += synth_failures
    if synth_failures or app_template_path is None:
        print(f"\n{len(all_failures)} check(s) failed.")
        return 1

    data = _load_template(app_template_path)
    resources = data.get("Resources", {})

    service = _app_runner_service(resources)
    if service is None:
        all_failures += _assert(False, "AWS::AppRunner::Service resource found in App template")
    else:
        all_failures += check_b_runtime_env_vars(service)

    policy = _api_task_role_policy(resources)
    if policy is None:
        all_failures += _assert(False, "API task-role AWS::IAM::Policy resource found in App template")
    else:
        all_failures += check_cde_task_role_grants(policy)

    if all_failures:
        print(f"\n{len(all_failures)} check(s) failed:")
        for f in all_failures:
            print(f"  - {f}")
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
