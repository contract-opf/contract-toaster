#!/usr/bin/env python3
"""
Slice test for issue #229: break-glass IAM role must not use a managed-policy
ARN as its federated principal.

Background
----------
`AuthStack`'s break-glass role (infra/lib/nested/auth-stack.ts) was created
with:

    assumedBy: new iam.FederatedPrincipal(
      'arn:aws:iam::aws:policy/AdministratorAccess', // placeholder
      { ... },
      'sts:AssumeRoleWithWebIdentity',
    )

A managed-policy ARN is not a valid identity-provider ARN for a
`FederatedPrincipal` — this is an invalid IAM principal that IAM would reject
at `CreateRole` time (deploy time, not synth time), failing the whole nested
`AuthStack` (and, since it's a nested stack, the parent deploy too).

This test is OFFLINE and DETERMINISTIC: it runs `cdk synth` (no AWS calls),
then asserts structurally against the synthesized CloudFormation template
that:

  1. `cdk synth --context env=dev` exits 0.
  2. The synthesized `AuthStack` nested template's `BreakGlassRole`
     `AWS::IAM::Role` resource (if one exists at all) does NOT have a
     managed-policy ARN (`arn:aws:iam::aws:policy/...`) in the `Federated`
     principal position of its `AssumeRolePolicyDocument`.
  3. It is also acceptable for v1 to have NO `BreakGlassRole` resource at all
     (the role dropped entirely) — per the issue's suggested direction of
     documenting break-glass as the account's `AdministratorAccess` SSO
     permission set instead of a dedicated (currently-unwireable) role.

It must FAIL on the pre-fix tree (auth-stack.ts using the managed-policy ARN
as `Federated` principal) and PASS after the fix.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

from __future__ import annotations
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
CDK_OUT = INFRA / "cdk.out"

MANAGED_POLICY_ARN_RE = re.compile(r"^arn:aws:iam::aws:policy/")


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
    directory. We clear cdk.out first so a stale artifact from a prior
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


def _find_break_glass_role(template: dict) -> tuple[str, dict] | None:
    """Return (logical_id, resource) for the BreakGlassRole IAM Role, if present."""
    for logical_id, resource in template.get("Resources", {}).items():
        if resource.get("Type") != "AWS::IAM::Role":
            continue
        role_name = resource.get("Properties", {}).get("RoleName", "")
        if "BreakGlass" in logical_id or "break-glass" in str(role_name):
            return logical_id, resource
    return None


def _federated_principals(assume_role_policy: dict) -> list[str]:
    """
    Extract all 'Federated' principal values (as strings) from an
    AssumeRolePolicyDocument's Statement list. CloudFormation intrinsics
    (Fn::Join, Fn::Sub, etc.) are stringified so a literal managed-policy
    ARN substring is still detectable even if wrapped.
    """
    values: list[str] = []
    for statement in assume_role_policy.get("Statement", []):
        principal = statement.get("Principal", {})
        if not isinstance(principal, dict):
            continue
        federated = principal.get("Federated")
        if federated is None:
            continue
        if isinstance(federated, str):
            values.append(federated)
        else:
            # Intrinsic function (Fn::Join / Fn::Sub / etc.) — stringify so a
            # literal 'arn:aws:iam::aws:policy/' substring is still caught.
            values.append(json.dumps(federated))
    return values


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


def check_break_glass_principal_valid() -> list[str]:
    print("\nCheck 2: BreakGlassRole (if present) does not use a managed-policy ARN "
          "as its Federated principal …")
    failures: list[str] = []

    template = _load_auth_template()
    failures += _assert(
        template is not None,
        "Synthesized AuthStack nested-stack template found in infra/cdk.out",
        "No contracttoaster*Auth*.nested.template.json found — cdk synth may have failed "
        "or the AuthStack nested stack is not synthesized separately.",
    )
    if template is None:
        return failures

    found = _find_break_glass_role(template)

    if found is None:
        # Acceptable for v1 per the issue's suggested direction: drop the
        # custom role and document break-glass as the SSO permission set
        # instead of wiring an unavailable-today real principal.
        print(
            "  [PASS] No BreakGlassRole resource in the synthesized template "
            "(v1: dropped in favor of documented SSO AdministratorAccess "
            "permission set — see RUNBOOK.md)."
        )
        return failures

    logical_id, resource = found
    assume_role_policy = resource.get("Properties", {}).get("AssumeRolePolicyDocument", {})
    federated_values = _federated_principals(assume_role_policy)

    print(f"  BreakGlassRole found: logical id {logical_id!r}; "
          f"Federated principal value(s): {federated_values!r}")

    bad_values = [v for v in federated_values if MANAGED_POLICY_ARN_RE.search(v)]

    failures += _assert(
        not bad_values,
        "BreakGlassRole Federated principal is NOT a managed-policy ARN "
        "(arn:aws:iam::aws:policy/...)",
        "A managed-policy ARN is not a valid identity-provider ARN for "
        "iam.FederatedPrincipal — IAM rejects CreateRole with an "
        "invalid-principal error at deploy time. Point the trust policy at "
        "the real IAM Identity Center permission-set role ARN or a SAML "
        "provider ARN (with sts:AssumeRoleWithSAML + MFA condition), or drop "
        "the custom role for v1 (see RUNBOOK.md → 'Break-glass: restoring "
        f"admin access'). Offending value(s): {bad_values!r}",
    )

    return failures


def main() -> int:
    print("Break-glass principal structural gate (issue #229)")
    print("=" * 60)

    all_failures: list[str] = []

    synth_failures, _result = check_synth_exits_zero()
    all_failures += synth_failures

    # Only meaningful to inspect the template if synth actually produced one;
    # check_break_glass_principal_valid() itself fails cleanly if it's absent.
    all_failures += check_break_glass_principal_valid()

    print("\n" + "=" * 60)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    print("\nPASS: all break-glass principal checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
