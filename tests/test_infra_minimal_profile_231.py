#!/usr/bin/env python3
"""
Structural gate for issue #231: `--context profile=minimal|hardened` deploy
profile switch.

The repo's infra stack (10 nested CDK stacks: 6 CMKs with encryption-context
DENY policies, a 2-AZ VPC with NAT + Bedrock interface endpoints, WAF,
CloudTrail + Object-Lock archive, cosign image signing, budgets) is justified
for the production legal-data story but is a non-starter for a demo or an
open-source adopter evaluating the tool: every deploy must satisfy Google
DWD, image signing, per-class KMS, and VPC wiring simultaneously, and NAT +
interface endpoints alone put idle cost well above the documented ~$25/mo
goal (ARCHITECTURE.md).

This test verifies, entirely offline via `cdk synth` + structural JSON
assertions on `infra/cdk.out/` (no live AWS/Bedrock calls):

  MINIMAL  (`--context profile=minimal`):
    A. `cdk synth` exits 0.
    B. The Waf nested-stack template is NOT produced (no WAF WebACL).
    C. The Network nested-stack template has NO `AWS::EC2::NatGateway`
       resource (no NAT-gateway idle cost).
    D. The Observability nested-stack template has NO
       `AWS::CloudTrail::Trail` resource (no CloudTrail).
    E. No S3 bucket in the Data nested-stack template has
       `ObjectLockEnabled: true` (no Object-Lock archive).
    F. Every S3 bucket in the Data nested-stack template uses AWS-managed
       encryption (`SSEAlgorithm: AES256`, no `KMSMasterKeyID`) -- no
       per-data-class CMK.
    G. Every DynamoDB table in the Data nested-stack template uses
       AWS-managed encryption (no `KMSMasterKeyId` in `SSESpecification`).
    H. The KmsKeys nested-stack template defines exactly ONE
       `AWS::KMS::Key` (the Step Functions state-machine key, which is not
       gated by profile) -- the five per-data-class CMKs are absent.

  HARDENED (the default -- `--context env=dev` with no `profile` context,
  exactly the command every other infra test in this repo already runs):
    I. `cdk synth` exits 0.
    J. The Waf nested-stack template IS produced, unchanged.
    K. The Network nested-stack template HAS an `AWS::EC2::NatGateway`.
    L. The Observability nested-stack template HAS an
       `AWS::CloudTrail::Trail`.
    M. At least one S3 bucket in the Data nested-stack template has
       `ObjectLockEnabled: true` (corpus + audit-archive, unchanged).
    N. S3 buckets in the Data nested-stack template are KMS-encrypted with a
       `KMSMasterKeyID` reference (customer-managed), unchanged.
    O. DynamoDB tables in the Data nested-stack template have
       `SSESpecification.KMSMasterKeyId` set (customer-managed), unchanged.
    P. The KmsKeys nested-stack template defines all SIX `AWS::KMS::Key`
       resources (five data-class CMKs + the state-machine key), unchanged.

Ordering note: this test runs the MINIMAL synth first and the HARDENED
(default-context) synth LAST, so `infra/cdk.out/` is left in the exact
hardened state every other `tests/test_infra_*.py` file expects when it
inspects pre-existing `cdk.out/` content without re-synthesizing itself
first (see e.g. tests/test_infra_kms_keys.py Check B).

It must FAIL on the pre-#231 tree (no `profile` context switch exists --
every synth is the hardened stack, so checks B-H above fail) and PASS once
the profile switch is implemented.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

from __future__ import annotations

import glob
import json
import subprocess
import sys
from pathlib import Path

from infra_synth_helper import NEUTRAL_CDK_CONTEXT

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


def _run_synth(context_args: list[str]) -> subprocess.CompletedProcess:
    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        print("  (node_modules absent — running npm install first …)")
        install = subprocess.run(
            ["npm", "install"], cwd=INFRA, capture_output=True, text=True
        )
        if install.returncode != 0:
            raise RuntimeError(
                f"npm install failed:\nstdout: {install.stdout[-800:]}\n"
                f"stderr: {install.stderr[-800:]}"
            )
    return subprocess.run(
        ["npx", "cdk", "synth", *context_args, *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _find_template(fragment: str) -> Path | None:
    """Return the nested-stack template whose filename contains `fragment`,
    or None if no such template was produced by the last synth."""
    candidates = sorted(CDK_OUT.glob(f"*{fragment}*.nested.template.json"))
    return candidates[-1] if candidates else None


def _load_template(fragment: str) -> dict:
    path = _find_template(fragment)
    if path is None:
        raise FileNotFoundError(
            f"No nested-stack template matching '*{fragment}*.nested.template.json' "
            f"found in {CDK_OUT} after cdk synth."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _resources_of_type(template: dict, cfn_type: str) -> list[dict]:
    return [r for r in template.get("Resources", {}).values() if r.get("Type") == cfn_type]


# ---------------------------------------------------------------------------
# MINIMAL profile checks
# ---------------------------------------------------------------------------

def check_minimal() -> list[str]:
    print("\n--- profile=minimal ---")
    failures: list[str] = []

    print("Check A: cdk synth --context profile=minimal exits 0 …")
    result = _run_synth(["--context", "env=dev", "--context", "profile=minimal"])
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev --context profile=minimal exits 0",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    if result.returncode != 0:
        return failures  # nothing further to inspect

    print("\nCheck B: Waf nested-stack template is NOT produced …")
    failures += _assert(
        _find_template("Waf") is None,
        "No *Waf*.nested.template.json in cdk.out under profile=minimal",
        "Per issue #231: minimal profile must omit the WAF WebACL entirely.",
    )

    print("\nCheck C: Network nested-stack template has NO NAT gateway …")
    network_tmpl = _load_template("Network")
    nat_gateways = _resources_of_type(network_tmpl, "AWS::EC2::NatGateway")
    failures += _assert(
        len(nat_gateways) == 0,
        "No AWS::EC2::NatGateway resource under profile=minimal",
        f"Found {len(nat_gateways)} NAT gateway(s). Per issue #231: minimal profile "
        "must have zero NAT gateways (idle-cost reduction).",
    )

    print("\nCheck D: Observability nested-stack template has NO CloudTrail trail …")
    observability_tmpl = _load_template("Observability")
    trails = _resources_of_type(observability_tmpl, "AWS::CloudTrail::Trail")
    failures += _assert(
        len(trails) == 0,
        "No AWS::CloudTrail::Trail resource under profile=minimal",
        f"Found {len(trails)} trail(s). Per issue #231: minimal profile omits CloudTrail.",
    )

    print("\nCheck E: No S3 bucket has ObjectLockEnabled=true under profile=minimal …")
    data_tmpl = _load_template("Data")
    buckets = _resources_of_type(data_tmpl, "AWS::S3::Bucket")
    locked_buckets = [
        b for b in buckets if b.get("Properties", {}).get("ObjectLockEnabled") is True
    ]
    failures += _assert(
        len(locked_buckets) == 0,
        "No S3 bucket has ObjectLockEnabled=true under profile=minimal",
        f"Found {len(locked_buckets)} Object-Lock-enabled bucket(s). Per issue #231: "
        "minimal profile omits Object Lock.",
    )
    failures += _assert(
        len(buckets) >= 4,
        "Data nested-stack template still defines the S3 buckets under profile=minimal",
        f"Found {len(buckets)} AWS::S3::Bucket resource(s); expected >= 4 "
        "(uploads/outputs/corpus/audit-archive) — only Object Lock/encryption "
        "should differ, not bucket presence.",
    )

    print("\nCheck F: S3 buckets use AWS-managed (not customer-managed) encryption …")
    kms_encrypted = []
    for b in buckets:
        sse_cfg = (
            b.get("Properties", {})
            .get("BucketEncryption", {})
            .get("ServerSideEncryptionConfiguration", [{}])
        )
        for rule in sse_cfg:
            default = rule.get("ServerSideEncryptionByDefault", {})
            if "KMSMasterKeyID" in default or default.get("SSEAlgorithm") == "aws:kms":
                kms_encrypted.append(b)
    failures += _assert(
        len(kms_encrypted) == 0,
        "No S3 bucket references a customer-managed KMS key under profile=minimal",
        f"Found {len(kms_encrypted)} bucket(s) with aws:kms/KMSMasterKeyID. "
        "Per issue #231: minimal profile must use AWS-managed (S3_MANAGED) encryption.",
    )

    print("\nCheck G: DynamoDB tables use AWS-managed (not customer-managed) encryption …")
    tables = _resources_of_type(data_tmpl, "AWS::DynamoDB::Table")
    cmk_tables = [
        t for t in tables if "KMSMasterKeyId" in t.get("Properties", {}).get("SSESpecification", {})
    ]
    failures += _assert(
        len(cmk_tables) == 0,
        "No DynamoDB table references a customer-managed KMS key under profile=minimal",
        f"Found {len(cmk_tables)} table(s) with SSESpecification.KMSMasterKeyId set. "
        "Per issue #231: minimal profile must use AWS-managed (AWS_MANAGED) encryption.",
    )
    failures += _assert(
        len(tables) >= 10,
        "Data nested-stack template still defines the DynamoDB tables under profile=minimal",
        f"Found {len(tables)} AWS::DynamoDB::Table resource(s); expected >= 10 — "
        "only encryption should differ, not table presence.",
    )

    print("\nCheck H: KmsKeys nested-stack template defines exactly ONE CMK (state-machine only) …")
    kms_tmpl = _load_template("KmsKeys")
    keys = _resources_of_type(kms_tmpl, "AWS::KMS::Key")
    failures += _assert(
        len(keys) == 1,
        "Exactly one AWS::KMS::Key (the state-machine key) under profile=minimal",
        f"Found {len(keys)} kms.Key resource(s); expected exactly 1. Per issue #231: "
        "the five per-data-class CMKs (uploads/outputs/corpus/audit/dynamodb) must be "
        "absent under minimal; the state-machine CMK is not gated by profile.",
    )

    return failures


# ---------------------------------------------------------------------------
# HARDENED (default) profile checks — must remain byte-for-byte unchanged.
# ---------------------------------------------------------------------------

def check_hardened_unchanged() -> list[str]:
    print("\n--- profile=hardened (default; no --context profile=… passed) ---")
    failures: list[str] = []

    print("Check I: cdk synth --context env=dev (default profile) exits 0 …")
    # Deliberately the exact command every other tests/test_infra_*.py file
    # runs, so this leaves cdk.out in the state those tests expect.
    result = _run_synth(["--context", "env=dev"])
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev exits 0 (default profile == hardened)",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    if result.returncode != 0:
        return failures

    print("\nCheck J: Waf nested-stack template IS produced …")
    failures += _assert(
        _find_template("Waf") is not None,
        "A *Waf*.nested.template.json exists in cdk.out under the default profile",
        "Default (hardened) profile must synthesize the WAF WebACL unchanged.",
    )

    print("\nCheck K: Network nested-stack template HAS a NAT gateway …")
    network_tmpl = _load_template("Network")
    nat_gateways = _resources_of_type(network_tmpl, "AWS::EC2::NatGateway")
    failures += _assert(
        len(nat_gateways) >= 1,
        "At least one AWS::EC2::NatGateway resource under the default (hardened) profile",
        f"Found {len(nat_gateways)}; expected >= 1 (unchanged pre-#231 behavior).",
    )

    print("\nCheck L: Observability nested-stack template HAS a CloudTrail trail …")
    observability_tmpl = _load_template("Observability")
    trails = _resources_of_type(observability_tmpl, "AWS::CloudTrail::Trail")
    failures += _assert(
        len(trails) == 1,
        "Exactly one AWS::CloudTrail::Trail resource under the default (hardened) profile",
        f"Found {len(trails)}; expected exactly 1 (unchanged pre-#231 behavior).",
    )

    print("\nCheck M: At least one S3 bucket has ObjectLockEnabled=true (default profile) …")
    data_tmpl = _load_template("Data")
    buckets = _resources_of_type(data_tmpl, "AWS::S3::Bucket")
    locked_buckets = [
        b for b in buckets if b.get("Properties", {}).get("ObjectLockEnabled") is True
    ]
    failures += _assert(
        len(locked_buckets) >= 2,
        "At least two S3 buckets have ObjectLockEnabled=true under the default (hardened) profile",
        f"Found {len(locked_buckets)}; expected >= 2 (corpus + audit-archive, unchanged).",
    )

    print("\nCheck N: S3 buckets use customer-managed KMS encryption (default profile) …")
    kms_encrypted = []
    for b in buckets:
        sse_cfg = (
            b.get("Properties", {})
            .get("BucketEncryption", {})
            .get("ServerSideEncryptionConfiguration", [{}])
        )
        for rule in sse_cfg:
            default = rule.get("ServerSideEncryptionByDefault", {})
            if "KMSMasterKeyID" in default:
                kms_encrypted.append(b)
    failures += _assert(
        len(kms_encrypted) == len(buckets) and len(buckets) >= 4,
        "Every S3 bucket references a customer-managed KMS key under the default (hardened) profile",
        f"{len(kms_encrypted)} of {len(buckets)} buckets have KMSMasterKeyID "
        "(unchanged pre-#231 behavior expects all of them).",
    )

    print("\nCheck O: DynamoDB tables use customer-managed KMS encryption (default profile) …")
    tables = _resources_of_type(data_tmpl, "AWS::DynamoDB::Table")
    cmk_tables = [
        t for t in tables if "KMSMasterKeyId" in t.get("Properties", {}).get("SSESpecification", {})
    ]
    failures += _assert(
        len(cmk_tables) == len(tables) and len(tables) >= 10,
        "Every DynamoDB table references a customer-managed KMS key under the default (hardened) profile",
        f"{len(cmk_tables)} of {len(tables)} tables have SSESpecification.KMSMasterKeyId "
        "(unchanged pre-#231 behavior expects all of them).",
    )

    print("\nCheck P: KmsKeys nested-stack template defines all six CMKs (default profile) …")
    kms_tmpl = _load_template("KmsKeys")
    keys = _resources_of_type(kms_tmpl, "AWS::KMS::Key")
    failures += _assert(
        len(keys) == 6,
        "Exactly six AWS::KMS::Key resources under the default (hardened) profile",
        f"Found {len(keys)}; expected exactly 6 (five data-class CMKs + the "
        "state-machine CMK, unchanged pre-#231 behavior).",
    )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Minimal deploy profile structural gate (issue #231)")
    print("=" * 60)

    if not INFRA.is_dir():
        print("[FAIL] infra/ directory does not exist")
        return 1

    all_failures: list[str] = []
    # Clean slate: don't let a stale cdk.out from a previous run mask a
    # missing/extra nested-stack template.
    if CDK_OUT.is_dir():
        import shutil

        shutil.rmtree(CDK_OUT)

    all_failures += check_minimal()
    # Runs LAST and with the exact default context every other infra test
    # uses, so cdk.out is left hardened for downstream tests in the
    # scripts/check.sh loop that inspect pre-existing cdk.out content.
    all_failures += check_hardened_unchanged()

    print("\n" + "=" * 60)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    print("\nPASS: all minimal deploy profile structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
