#!/usr/bin/env python3
"""
Structural gate for issue #224: image-digest promotion is documented but not
wired — App Runner was pinned to a placeholder digest that could never be
replaced via the documented flow.

Pre-fix behaviour (the bug):
  - infra/lib/nested/app-stack.ts pinned App Runner unconditionally to
    `props.ecrImageDigest ?? 'sha256:0000...0'` (app-stack.ts:144).
  - The composition root (infra/lib/contract-toaster-stack.ts) never passed
    `ecrImageDigest` or `ecrRepositoryUri` into AppStack.
  - RUNBOOK.md documents promotion as
    `cdk deploy --context imageDigest=sha256:<digest>`, but no code anywhere
    read an `imageDigest` CDK context key.
  - Net effect: the first `cdk deploy --all` tries to create an App Runner
    service from a nonexistent image `...@sha256:0000...0`, and there is no
    code path to ever point App Runner at a real, promoted digest.

This test verifies, entirely offline via `cdk synth` + structural JSON
assertions on `infra/cdk.out/` (no live AWS calls):

  A. `cdk synth --context env=dev --context imageDigest=sha256:1111...1111`
     exits 0, and the App nested-stack template's
     `AWS::AppRunner::Service` `SourceConfiguration.ImageRepository`
     resolves `ImageIdentifier` to the supplied digest (proving the digest
     flows CDK context -> composition root -> AppStack -> CfnService), is
     NOT the zeroed `sha256:0000...0` placeholder, and is sourced from a
     private ECR repository (`ImageRepositoryType: "ECR"`) with an
     `AuthenticationConfiguration.AccessRoleArn` (proving the real
     `ecrRepositoryUri` -- not the hard-coded placeholder account -- was
     threaded in from CicdStack).

  B. A plain `cdk synth --context env=dev` (no `imageDigest` context) still
     exits 0 -- proving the first deploy is not pinned to a nonexistent
     image -- and the App nested-stack template shows App Runner sourced
     from a public, always-existing "hello world" bootstrap image
     (`ImageRepositoryType: "ECR_PUBLIC"`), NOT the zeroed placeholder
     digest.

It must FAIL on the pre-#224 tree: `imageDigest` context is ignored, so
Check A's assertion that the supplied digest appears in `ImageIdentifier`
fails, and Check B's assertion that the no-context template is NOT the
zeroed placeholder also fails (the old code emits the zeroed placeholder
unconditionally, digest or not). It must PASS after the fix.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
CDK_OUT = INFRA / "cdk.out"

SUPPLIED_DIGEST = (
    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
)
ZEROED_PLACEHOLDER = (
    "sha256:0000000000000000000000000000000000000000000000000000000000000000"
)


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


def _app_runner_source_configuration(app_template: dict) -> dict:
    for resource in app_template.get("Resources", {}).values():
        if resource.get("Type") == "AWS::AppRunner::Service":
            return resource["Properties"]["SourceConfiguration"]
    raise AssertionError("No AWS::AppRunner::Service resource found in App template.")


# ---------------------------------------------------------------------------
# Check A — explicit --context imageDigest=... promotes App Runner to the
# supplied digest, sourced from the real (CicdStack) private ECR repo.
# ---------------------------------------------------------------------------

def check_a_explicit_digest_flows_to_cfn_service() -> list[str]:
    print("\nCheck A: --context imageDigest=... flows through to the CfnService …")
    failures: list[str] = []

    result = _run_synth(
        ["--context", "env=dev", "--context", f"imageDigest={SUPPLIED_DIGEST}"]
    )
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev --context imageDigest=... exits 0",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    if result.returncode != 0:
        return failures

    source_config = _app_runner_source_configuration(_load_template("App"))
    image_repo = source_config.get("ImageRepository", {})
    identifier_json = json.dumps(image_repo.get("ImageIdentifier"))

    failures += _assert(
        SUPPLIED_DIGEST in identifier_json,
        "App Runner ImageIdentifier resolves to the supplied --context imageDigest",
        f"Expected '{SUPPLIED_DIGEST}' to appear in ImageIdentifier; got: {identifier_json}",
    )
    failures += _assert(
        ZEROED_PLACEHOLDER not in identifier_json,
        "App Runner ImageIdentifier is NOT the zeroed sha256:0000...0 placeholder",
        f"ImageIdentifier: {identifier_json}",
    )
    failures += _assert(
        image_repo.get("ImageRepositoryType") == "ECR",
        "ImageRepositoryType is 'ECR' (private repo) when a digest is promoted",
        f"Got: {image_repo.get('ImageRepositoryType')!r}",
    )
    failures += _assert(
        "AuthenticationConfiguration" in source_config
        and "AccessRoleArn"
        in source_config.get("AuthenticationConfiguration", {}),
        "SourceConfiguration includes an AccessRoleArn for the private ECR pull",
        f"SourceConfiguration keys: {sorted(source_config.keys())}",
    )
    # The real CicdStack ECR repo URI is a cross-stack CFN reference (not the
    # literal hard-coded placeholder account '123456789012'), proving
    # ecrRepositoryUri was actually threaded in from CicdStack rather than
    # left on its placeholder default.
    failures += _assert(
        "123456789012" not in identifier_json,
        "ImageIdentifier does not use the hard-coded placeholder account/repo URI",
        f"ImageIdentifier: {identifier_json}",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B — plain `cdk synth` (no imageDigest) still exits 0, via a
# deploy-safe public bootstrap image, not the zeroed placeholder.
# ---------------------------------------------------------------------------

def check_b_no_digest_bootstraps_safely() -> list[str]:
    print("\nCheck B: plain cdk synth (no imageDigest) exits 0 via a bootstrap image …")
    failures: list[str] = []

    result = _run_synth(["--context", "env=dev"])
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev (no imageDigest) exits 0",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    if result.returncode != 0:
        return failures

    source_config = _app_runner_source_configuration(_load_template("App"))
    image_repo = source_config.get("ImageRepository", {})
    identifier_json = json.dumps(image_repo.get("ImageIdentifier"))

    failures += _assert(
        ZEROED_PLACEHOLDER not in identifier_json,
        "Day-one (no imageDigest) ImageIdentifier is NOT the zeroed sha256:0000...0 placeholder",
        f"ImageIdentifier: {identifier_json}",
    )
    failures += _assert(
        image_repo.get("ImageRepositoryType") == "ECR_PUBLIC",
        "Day-one deploy sources App Runner from a public bootstrap image (ECR_PUBLIC)",
        f"Got: {image_repo.get('ImageRepositoryType')!r}. Per issue #224: the first "
        "deploy must not be pinned to a nonexistent private-ECR image.",
    )
    failures += _assert(
        "hello-app-runner" in identifier_json or "public.ecr.aws" in identifier_json,
        "Bootstrap image is a real, always-existing public image",
        f"ImageIdentifier: {identifier_json}",
    )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("AppStack digest-promotion wiring gate (issue #224)")
    print("=" * 60)

    if not INFRA.is_dir():
        print("FAIL: infra/ directory does not exist.")
        return 1

    all_failures: list[str] = []
    # Order matters: run the explicit-digest synth first, then finish on the
    # plain `--context env=dev` synth (no imageDigest) — the exact command
    # every other tests/test_infra_*.py file already runs — so infra/cdk.out
    # is left in the state those tests expect.
    all_failures += check_a_explicit_digest_flows_to_cfn_service()
    all_failures += check_b_no_digest_bootstraps_safely()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all digest-promotion wiring checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
