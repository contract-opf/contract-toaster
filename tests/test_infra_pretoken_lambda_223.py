#!/usr/bin/env python3
"""
Slice test for issue #223: every sign-in fails closed because the Cognito
pre-token-generation Lambda has three independent runtime defects.

Background
----------
`AuthStack`'s pre-token-generation Lambda (infra/lib/nested/auth-stack.ts) is
the admission gate for EVERY sign-in, and had three independent bugs that
each deny 100% of sign-ins:

  (a) The inline Lambda code imported `cryptography.hazmat` to RS256-sign the
      domain-wide-delegation JWT. The AWS Lambda Python 3.12 managed runtime
      does not include `cryptography`, and inline `Code.fromInline` cannot
      bundle third-party dependencies — so `_check_group_membership` raised
      ImportError on every invocation -> deny.

  (b) The domain-wide-delegation JWT's `sub` claim was set to the GROUP
      address `legal-admin@example.com` (auth-stack.ts:258, pre-fix). DWD
      requires impersonating a real Workspace USER, not a group -> Google's
      token endpoint rejects the exchange -> deny.

  (c) The Lambda's execution role was granted only
      `directoryApiSecret.grantRead(...)` -- no `dynamodb:PutItem` /
      `dynamodb:UpdateItem` on the users table -- so the JIT-create at
      `_jit_create_user_row` throws AccessDenied on every first (and
      repeat) sign-in -> deny.

This test is OFFLINE and DETERMINISTIC: it runs `cdk synth` (no AWS/Bedrock/
network calls), then asserts structurally against the synthesized
CloudFormation template (and the inline Lambda source it contains) that:

  1. `cdk synth --context env=dev` exits 0.
  2. The PreTokenLambda's inline source contains no REAL `cryptography`
     import statement (a real `from cryptography...` / `import
     cryptography` line, not merely the word appearing in a comment/
     docstring) -- the unavailable dependency is avoided entirely (per the
     issue's acceptance criterion (a), satisfied via the "or that the
     unavailable dep is avoided" branch rather than moving to a bundled
     Code.fromAsset asset).
  3. The inline source does not set the JWT `sub` claim to the literal
     `LEGAL_ADMIN_GROUP` (the group address) -- it must impersonate a real
     Workspace admin user's email instead (a `delegated_admin_email` read
     out of the Directory API secret).
  4. The PreTokenLambda's IAM role has an attached policy granting BOTH
     `dynamodb:PutItem` and `dynamodb:UpdateItem` on (a resource referencing)
     the users table.

Deferred to a human (not offline-testable): the LIVE Google token-exchange
happy path (real DWD impersonation + Directory API round-trip). This slice
test asserts only the structural fixes in the synthesized template.

It must FAIL on the pre-fix tree and PASS after the fix.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

from __future__ import annotations
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
CDK_OUT = INFRA / "cdk.out"


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def _run_cdk_synth() -> subprocess.CompletedProcess:
    """
    Run `npx cdk synth --context env=dev` into the default infra/cdk.out
    directory. cdk.out is cleared first so a stale artifact from a prior
    (possibly RED) run cannot cause a false pass or fail — mirrors
    scripts/check.sh's top-of-run `rm -rf infra/cdk.out`.
    """
    if CDK_OUT.exists():
        shutil.rmtree(CDK_OUT)

    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        print("  (node_modules absent — running npm install first …)")
        install = subprocess.run(
            ["npm", "install"],
            cwd=INFRA,
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            raise RuntimeError(f"npm install failed: {install.stderr[-500:]}")

    return subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
    )


def _load_auth_template() -> dict | None:
    if not CDK_OUT.is_dir():
        return None
    candidates = sorted(CDK_OUT.glob("contracttoaster*Auth*.nested.template.json"))
    if not candidates:
        return None
    dev = [f for f in candidates if "dev" in f.name.lower()]
    template_file = dev[0] if dev else candidates[0]
    return json.loads(template_file.read_text(encoding="utf-8"))


def _find_pre_token_lambda(template: dict) -> tuple[str, dict] | None:
    """Return (logical_id, resource) for the PreTokenLambda AWS::Lambda::Function."""
    for logical_id, resource in template.get("Resources", {}).items():
        if resource.get("Type") != "AWS::Lambda::Function":
            continue
        if "PreToken" in logical_id:
            return logical_id, resource
        function_name = resource.get("Properties", {}).get("FunctionName", "")
        if "pre-token" in str(function_name):
            return logical_id, resource
    return None


def _find_role_policies(template: dict, role_logical_id: str) -> list[dict]:
    """Return all AWS::IAM::Policy resources attached to the given Role logical id."""
    policies = []
    for _logical_id, resource in template.get("Resources", {}).items():
        if resource.get("Type") != "AWS::IAM::Policy":
            continue
        roles = resource.get("Properties", {}).get("Roles", [])
        for role_ref in roles:
            if isinstance(role_ref, dict) and role_ref.get("Ref") == role_logical_id:
                policies.append(resource)
    return policies


def check_synth_exits_zero() -> tuple[list[str], subprocess.CompletedProcess | None]:
    print("\nCheck 1: cdk synth --context env=dev exits 0 …")
    try:
        result = _run_cdk_synth()
    except RuntimeError as exc:
        return _assert(False, "cdk synth prerequisite (npm install) succeeded", str(exc)), None

    failures = _assert(
        result.returncode == 0,
        "cdk synth --context env=dev exits 0",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    return failures, result


def check_defect_a_no_cryptography_dependency(zip_file_source: str) -> list[str]:
    print("\nCheck 2 (defect a): pre-token Lambda source has no unavailable "
          "'cryptography' dependency …")
    failures: list[str] = []

    real_import_lines = [
        line for line in zip_file_source.splitlines()
        if line.strip().startswith("from cryptography")
        or line.strip().startswith("import cryptography")
    ]
    failures += _assert(
        not real_import_lines,
        "No real 'import cryptography' / 'from cryptography...' statement in the "
        "inline Lambda source",
        "Per issue #223 defect (a): the Lambda Python 3.12 managed runtime does not "
        "bundle 'cryptography', and inline Code.fromInline cannot bundle third-party "
        f"dependencies. Offending line(s): {real_import_lines!r}",
    )

    # Positive check: the pure-Python replacement signer must actually be present
    # (not merely deleted with nothing standing in for it).
    has_pure_python_signer = (
        "_rsa_sign_sha256" in zip_file_source
        and "_rsa_private_key_from_pem" in zip_file_source
    )
    failures += _assert(
        has_pure_python_signer,
        "Pure-Python RSA PKCS#1 v1.5 / SHA-256 signer "
        "(_rsa_private_key_from_pem / _rsa_sign_sha256) present in the inline source",
        "The unavailable 'cryptography' dependency must be replaced by a working "
        "stdlib-only signer, not simply removed.",
    )

    return failures


def check_defect_b_impersonates_real_user(zip_file_source: str) -> list[str]:
    print("\nCheck 3 (defect b): domain-wide-delegation JWT impersonates a real "
          "Workspace admin user, not the group address …")
    failures: list[str] = []

    sets_sub_to_group = '"sub": LEGAL_ADMIN_GROUP' in zip_file_source
    failures += _assert(
        not sets_sub_to_group,
        "JWT payload does NOT set 'sub' to the LEGAL_ADMIN_GROUP address",
        "Per issue #223 defect (b): domain-wide delegation requires impersonating a "
        "real Workspace USER; Google's token endpoint rejects a group as 'sub'. "
        "Found literal '\"sub\": LEGAL_ADMIN_GROUP' in the inline source.",
    )

    impersonates_real_user = (
        "delegated_admin_email" in zip_file_source
        and '"sub": delegated_admin_email' in zip_file_source
    )
    failures += _assert(
        impersonates_real_user,
        "JWT payload sets 'sub' to a real user email sourced from the Directory "
        "API secret (delegated_admin_email)",
        "Expected a 'delegated_admin_email' field read from the service-account "
        "secret and used as the JWT 'sub' claim.",
    )

    return failures


def check_defect_c_dynamodb_write_grant(template: dict, role_logical_id: str) -> list[str]:
    print("\nCheck 4 (defect c): pre-token Lambda role is granted "
          "dynamodb:PutItem + dynamodb:UpdateItem on the users table …")
    failures: list[str] = []

    policies = _find_role_policies(template, role_logical_id)
    failures += _assert(
        bool(policies),
        "At least one AWS::IAM::Policy is attached to the PreTokenLambda role",
        f"No AWS::IAM::Policy resource found with Roles containing a Ref to "
        f"{role_logical_id!r}.",
    )
    if not policies:
        return failures

    granted_actions: set[str] = set()
    dynamodb_resources: list = []
    for policy in policies:
        for statement in policy.get("Properties", {}).get("PolicyDocument", {}).get("Statement", []):
            if statement.get("Effect") != "Allow":
                continue
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if any(a.startswith("dynamodb:") for a in actions):
                granted_actions.update(actions)
                resource = statement.get("Resource")
                dynamodb_resources.append(resource)

    has_put = "dynamodb:PutItem" in granted_actions
    has_update = "dynamodb:UpdateItem" in granted_actions

    failures += _assert(
        has_put and has_update,
        "PreTokenLambda role policy grants both dynamodb:PutItem and "
        "dynamodb:UpdateItem",
        "Per issue #223 defect (c): the JIT-create path "
        "(_jit_create_user_row) needs PutItem (conditional create on first "
        f"sign-in) and UpdateItem (last_auth_at bump on repeat sign-in). "
        f"Granted DynamoDB actions found: {sorted(granted_actions)!r}",
    )

    # The grant should reference the users table specifically (not '*').
    resources_str = json.dumps(dynamodb_resources)
    references_users_table = "users" in resources_str.lower() and "*" not in dynamodb_resources
    failures += _assert(
        references_users_table,
        "DynamoDB grant resource references the users table (scoped, not '*')",
        f"Resource(s) found: {dynamodb_resources!r}",
    )

    return failures


def main() -> int:
    print("Pre-token Lambda structural gate (issue #223)")
    print("=" * 60)

    all_failures: list[str] = []

    synth_failures, _result = check_synth_exits_zero()
    all_failures += synth_failures
    if synth_failures:
        # No point inspecting a template that doesn't exist / is stale.
        print("\n" + "=" * 60)
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    template = _load_auth_template()
    all_failures += _assert(
        template is not None,
        "Synthesized AuthStack nested-stack template found in infra/cdk.out",
        "No contracttoaster*Auth*.nested.template.json found — cdk synth may have "
        "failed or the AuthStack nested stack is not synthesized separately.",
    )
    if template is None:
        print("\n" + "=" * 60)
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    found = _find_pre_token_lambda(template)
    all_failures += _assert(
        found is not None,
        "PreTokenLambda AWS::Lambda::Function resource found in the synthesized "
        "AuthStack template",
    )
    if found is None:
        print("\n" + "=" * 60)
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    _logical_id, lambda_resource = found
    zip_file_source = lambda_resource.get("Properties", {}).get("Code", {}).get("ZipFile", "")
    all_failures += _assert(
        len(zip_file_source.strip()) > 100,
        "PreTokenLambda inline source is non-trivial (> 100 chars)",
        "Expected the full enforcement handler inline, not a stub.",
    )

    all_failures += check_defect_a_no_cryptography_dependency(zip_file_source)
    all_failures += check_defect_b_impersonates_real_user(zip_file_source)

    role_ref = lambda_resource.get("Properties", {}).get("Role", {})
    role_logical_id = None
    if isinstance(role_ref, dict):
        get_att = role_ref.get("Fn::GetAtt")
        if isinstance(get_att, list) and get_att:
            role_logical_id = get_att[0]
    all_failures += _assert(
        role_logical_id is not None,
        "PreTokenLambda has a resolvable execution-role logical id (Fn::GetAtt)",
        f"Role property found: {role_ref!r}",
    )
    if role_logical_id is not None:
        all_failures += check_defect_c_dynamodb_write_grant(template, role_logical_id)

    print("\n" + "=" * 60)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    print("\nPASS: all pre-token Lambda structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
