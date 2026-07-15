#!/usr/bin/env python3
"""
Structural gate for issue #50: CDK skeleton AC coverage.

Verifies that all file-system-checkable acceptance criteria for the CDK
skeleton (issue #50) are satisfied:

  A. infra/ directory exists with required CDK bootstrap files.
  B. Stack entry point defines ContractToasterStack with all six nested stacks.
  C. Account/region are environment-scoped context values (not hardcoded);
     dev and prod accounts must be distinct (not the same placeholder).
  D. cdk.json exists and references the correct TypeScript app entry point.
  E. Customer-managed KMS key construct is present.
  F. Base IAM roles are defined (deploy role, app runner task role).
  G. README.md exists inside infra/ and explains the stack layout.
  H. cdk synth runs cleanly (exit code 0) with a simulated environment.

Note: cdk synth is run with --context env=dev so the stack can resolve
environment values.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"


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
# Check A — infra/ directory with CDK bootstrap files
# ---------------------------------------------------------------------------

def check_a_infra_files() -> list[str]:
    print("\nCheck A: infra/ directory and CDK bootstrap files …")
    failures = []
    failures += _assert(INFRA.is_dir(), "infra/ directory exists")
    if failures:
        return failures  # can't check contents if dir missing
    required_files = [
        "cdk.json",
        "package.json",
        "tsconfig.json",
    ]
    for name in required_files:
        path = INFRA / name
        failures += _assert(
            path.is_file(),
            f"infra/{name} exists",
            f"Expected: {path}",
        )
    return failures


# ---------------------------------------------------------------------------
# Check B — ContractToasterStack with all six nested stacks
# ---------------------------------------------------------------------------

# Nested stacks that must be present (per AC):
REQUIRED_NESTED_STACKS = [
    "network",
    "data",
    "auth",
    "app",
    "frontend",
    "observability",
]


def _find_ts_sources() -> list[Path]:
    """Return all TypeScript source files under infra/lib/ and infra/bin/."""
    sources = []
    for subdir in ("lib", "bin"):
        p = INFRA / subdir
        if p.is_dir():
            sources.extend(p.rglob("*.ts"))
    return sources


def check_b_stack_structure() -> list[str]:
    print("\nCheck B: ContractToasterStack with six nested stacks …")
    failures = []

    ts_files = _find_ts_sources()
    if not ts_files:
        failures += _assert(
            False,
            "TypeScript source files found in infra/lib/ or infra/bin/",
            "Expected .ts files defining the CDK stacks.",
        )
        return failures

    all_ts = "\n".join(_read(f) for f in ts_files)

    failures += _assert(
        "ContractToasterStack" in all_ts,
        "ContractToasterStack defined in infra/ TypeScript sources",
    )

    for nested in REQUIRED_NESTED_STACKS:
        # Look for a class or construct that captures this nested stack name
        # Accept: NetworkStack, DataStack, AuthStack, etc. OR string literals
        pattern = re.compile(
            rf"\b{nested.capitalize()}(?:Stack|Nested)?\b|['\"]?{nested}['\"]?\s*(?:nested|stack)",
            re.IGNORECASE,
        )
        found = bool(pattern.search(all_ts))
        failures += _assert(
            found,
            f"Nested stack '{nested}' referenced in infra/ sources",
            f"Expected a reference to a '{nested.capitalize()}Stack' or equivalent.",
        )

    return failures


# ---------------------------------------------------------------------------
# Check C — Environment-scoped account/region (dev != prod)
# ---------------------------------------------------------------------------

def check_c_env_scoped_context() -> list[str]:
    print("\nCheck C: Environment-scoped account/region; dev account != prod account …")
    failures = []

    ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in ts_files)

    # Must use CDK context/tryGetContext for environment-specific values
    uses_context = bool(
        re.search(r"(?:app\.node\.tryGetContext|fromContext|node\.tryGetContext)", all_ts)
    )
    failures += _assert(
        uses_context,
        "App uses CDK context (tryGetContext/fromContext) for environment values",
        "Hard-coding account IDs is forbidden. Use node.tryGetContext('env') etc.",
    )

    # cdk.json must not have the same account for dev and prod
    cdk_json_path = INFRA / "cdk.json"
    if cdk_json_path.is_file():
        cdk_json = json.loads(_read(cdk_json_path))
        context = cdk_json.get("context", {})
        dev_account = context.get("dev", {}).get("account", "")
        prod_account = context.get("prod", {}).get("account", "")

        # Both must be non-empty (not blank)
        failures += _assert(
            bool(dev_account),
            "cdk.json context.dev.account is set",
            "Provide a non-empty placeholder for the dev account ID.",
        )
        failures += _assert(
            bool(prod_account),
            "cdk.json context.prod.account is set",
            "Provide a non-empty placeholder for the prod account ID.",
        )

        # Dev and prod accounts must differ
        if dev_account and prod_account:
            failures += _assert(
                dev_account != prod_account,
                "dev account != prod account (no single-account both-env setup)",
                f"dev='{dev_account}' must differ from prod='{prod_account}'. "
                "Per AC: 'The implementation must not hard-code one account as both dev and prod.'",
            )

        # Region must be us-east-1
        dev_region = context.get("dev", {}).get("region", "")
        failures += _assert(
            dev_region == "us-east-1",
            "cdk.json context.dev.region is 'us-east-1'",
            f"Got: '{dev_region}'",
        )
        prod_region = context.get("prod", {}).get("region", "")
        failures += _assert(
            prod_region == "us-east-1",
            "cdk.json context.prod.region is 'us-east-1'",
            f"Got: '{prod_region}'",
        )

    # Stack names must include the environment name (contract-toaster-dev, contract-toaster-prod)
    failures += _assert(
        "contract-toaster-dev" in all_ts or "contract-toaster-${env}" in all_ts
        or bool(re.search(r"contract-toaster-\$\{", all_ts)),
        "Stack names include the environment name (e.g. 'contract-toaster-dev')",
        "Per AC: stack names should include the environment name.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D — cdk.json app entry point
# ---------------------------------------------------------------------------

def check_d_cdk_json() -> list[str]:
    print("\nCheck D: cdk.json references TypeScript app entry point …")
    failures = []

    cdk_json_path = INFRA / "cdk.json"
    failures += _assert(cdk_json_path.is_file(), "infra/cdk.json exists")
    if failures:
        return failures

    cdk_json = json.loads(_read(cdk_json_path))
    app_cmd = cdk_json.get("app", "")

    failures += _assert(
        bool(app_cmd),
        "cdk.json has an 'app' field",
        "Must point to the entry point (e.g. 'npx ts-node bin/contract-toaster.ts').",
    )
    failures += _assert(
        ".ts" in app_cmd or "ts-node" in app_cmd or "node" in app_cmd,
        "cdk.json 'app' invokes a TypeScript/Node entry point",
        f"Got: '{app_cmd}'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check E — Customer-managed KMS key
# ---------------------------------------------------------------------------

def check_e_kms_key() -> list[str]:
    print("\nCheck E: Customer-managed KMS key construct present …")
    failures = []

    ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in ts_files)

    failures += _assert(
        bool(re.search(r"aws_kms|kms\.Key|KmsKey|aws-cdk-lib/aws-kms", all_ts)),
        "KMS key construct referenced in infra/ sources",
        "Per AC: 'Customer-managed KMS key defined for the environment.'",
    )

    # Must NOT be aws_kms.Alias pointing to alias/aws/s3 (that's AWS-managed)
    # The AC explicitly requires customer-managed (CMK)
    has_cmk = bool(
        re.search(r"new\s+kms\.Key|new\s+Key\s*\(|enableKeyRotation|kms\.Key\s*\(", all_ts)
    )
    failures += _assert(
        has_cmk,
        "KMS customer-managed key (new kms.Key) instantiated",
        "A CMK must be created, not just referenced. Use 'new kms.Key(...)' with enableKeyRotation.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check F — Base IAM roles (deploy role, app runner task role)
# ---------------------------------------------------------------------------

def check_f_iam_roles() -> list[str]:
    print("\nCheck F: Base IAM roles (deploy role + app runner task role) …")
    failures = []

    ts_files = _find_ts_sources()
    all_ts = "\n".join(_read(f) for f in ts_files)

    failures += _assert(
        bool(re.search(r"aws_iam|iam\.Role|new\s+Role\s*\(|aws-cdk-lib/aws-iam", all_ts)),
        "IAM role construct referenced in infra/ sources",
    )

    # Deploy role
    failures += _assert(
        bool(re.search(r"deploy.*[Rr]ole|[Rr]ole.*deploy", all_ts, re.IGNORECASE)),
        "Deploy role defined",
        "Per AC: 'Base IAM roles defined (deploy role …)'",
    )

    # App Runner task role
    failures += _assert(
        bool(re.search(r"apprunner.*[Rr]ole|[Rr]ole.*apprunner|task.*[Rr]ole|[Rr]ole.*task", all_ts, re.IGNORECASE)),
        "App Runner task role defined",
        "Per AC: 'Base IAM roles defined (… app runner task role)'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G — README.md in infra/
# ---------------------------------------------------------------------------

def check_g_infra_readme() -> list[str]:
    print("\nCheck G: infra/README.md exists and describes the stack layout …")
    failures = []

    readme = INFRA / "README.md"
    failures += _assert(readme.is_file(), "infra/README.md exists")
    if failures:
        return failures

    text = _read(readme)

    # Should mention nested stacks
    has_nested = bool(
        re.search(r"nested|stack.*layout|layout.*stack|ContractToaster", text, re.IGNORECASE)
    )
    failures += _assert(
        has_nested,
        "infra/README.md mentions stack layout (nested stacks / ContractToasterStack)",
        "Per AC: 'README in infra/ explaining the stack layout.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check H — cdk synth runs cleanly
# ---------------------------------------------------------------------------

def check_h_cdk_synth() -> list[str]:
    print("\nCheck H: cdk synth runs cleanly …")
    failures = []

    if not INFRA.is_dir():
        return _assert(False, "infra/ directory exists (prerequisite for cdk synth)", "Create infra/ first.")

    # Install node dependencies if node_modules absent
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
            failures += _assert(
                False,
                "npm install succeeded in infra/",
                f"stdout: {install.stdout[-500:]}\nstderr: {install.stderr[-500:]}",
            )
            return failures

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

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("CDK skeleton structural gate (issue #50)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_infra_files()
    all_failures += check_b_stack_structure()
    all_failures += check_c_env_scoped_context()
    all_failures += check_d_cdk_json()
    all_failures += check_e_kms_key()
    all_failures += check_f_iam_roles()
    all_failures += check_g_infra_readme()
    all_failures += check_h_cdk_synth()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all CDK skeleton structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
