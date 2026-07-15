#!/usr/bin/env python3
"""
Structural gate for issue #66: CI pipeline — CodeBuild → tests/scans →
signed image → ECR → digest deploy.

Reconciliation (2026-06-11 architecture review):
  CI additionally runs the docs-lint job (#43) and the detector-correctness
  gates once the engine exists (#1, #2).

Verifies that all acceptance criteria for issue #66 are satisfied:

  A. infra/lib/nested/cicd-stack.ts exists and defines:
       - An ECR repository with image scanning and tag immutability.
       - A CodeBuild project that runs the test suite, docs-lint, and
         detector-correctness gates.
       - Image signing (ECR image scanning / signer reference) so an
         unsigned or unverifiable digest cannot be promoted.
       - A digest-promotion mechanism (IAM, SSM, or equivalent) that
         updates the App Runner service to a new signed digest.
       - No auto-deploy from GitHub main (AutoDeploymentsEnabled = false).

  B. .github/workflows/ci-pipeline.yml (or equivalent) exists and:
       - Runs the repo test suite (Python tests).
       - Runs the docs-lint gate (scripts/docs-lint.py).
       - Runs the detector-correctness gate (.github/workflows/detector-correctness.yml
         or equivalent inline steps referencing the detector test scripts).
       - Runs a security/dependency scan step.
       - Builds a container image, signs it, and pushes it to ECR.
       - Records a promotion audit row (who promoted which digest, when)
         — either as a build step annotation or via DynamoDB/CloudTrail write.
       - Verifies image signatures before promotion; an unsigned digest is rejected.

  C. ARCHITECTURE.md documents the CI pipeline:
       - References docs-lint AND detector-correctness as CI gates.
       - States that a promotion writes an audit row (actor + digest + timestamp).
       - States that an unsigned or unverifiable digest cannot be promoted
         (signature verification before promote step).

  D. ContractToasterStack parent stack wires the CicdStack nested stack.

  E. cdk synth runs cleanly with the CICD stack included.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.

GATE_KIND (issue #196): labeled documentation-lint because most of this
module's checks (A-D) are regex scans over infra/CDK source and
ARCHITECTURE.md prose asserting things are *documented* or *named*, not
that they behave correctly at runtime — matching issue #196's Concern that
green here can imply enforced invariants that are actually prose. Check E
(cdk synth) is a real behavioral/structural check (it runs the CDK CLI),
but the module as a whole is dominated by prose scans, so it carries the
documentation-lint marker per issue #196's bounded scope (label the three
named docs-gates; do not attempt a per-check split here). See
tests/test_docs_gate_labeling.py, which enforces that this marker exists.
"""

import re
import subprocess
import sys
from pathlib import Path
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

# Machine-readable marker (issue #196): distinguishes a documentation-lint
# gate (asserts docs SAY something) from a behavioral test (asserts running
# code DOES something) — see the module docstring for why this module,
# despite Check E's real `cdk synth` call, is labeled documentation-lint.
# Enforced by tests/test_docs_gate_labeling.py.
GATE_KIND = "documentation-lint"

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
CICD_STACK_PATH = INFRA / "lib" / "nested" / "cicd-stack.ts"
MAIN_STACK_PATH = INFRA / "lib" / "contract-toaster-stack.ts"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ARCHITECTURE_MD = REPO_ROOT / "ARCHITECTURE.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    else:
        msg = f"  [FAIL] {label}"
        if detail:
            msg += f"\n         {detail}"
        print(msg)
        return [label]


def check_cicd_stack() -> list[str]:
    """Check A: cicd-stack.ts exists with required constructs."""
    print("Check A: infra/lib/nested/cicd-stack.ts — ECR repo, CodeBuild, signing, promotion …")
    failures = []

    failures += _assert(CICD_STACK_PATH.exists(), "cicd-stack.ts exists")

    if not CICD_STACK_PATH.exists():
        print("  (skipping content checks — file does not exist)")
        return failures

    text = _read(CICD_STACK_PATH)

    # ECR repository with tag immutability
    failures += _assert(
        bool(re.search(r"ecr|ECR|Repository|ContainerRepository", text, re.IGNORECASE)),
        "cicd-stack.ts defines an ECR repository",
        "Per AC: on success, a container image is pushed to ECR with an immutable tag/digest.",
    )
    failures += _assert(
        bool(re.search(r"IMMUTABLE|tagMutability|immutable", text, re.IGNORECASE)),
        "cicd-stack.ts enables ECR tag immutability",
        "Per AC: ECR repository must be immutable so pushed images cannot be overwritten.",
    )

    # CodeBuild project
    failures += _assert(
        bool(re.search(r"CodeBuild|codebuild|Project|CfnProject", text, re.IGNORECASE)),
        "cicd-stack.ts defines a CodeBuild project",
        "Per AC: CodeBuild (or equivalent CI) runs the test suite and scans on every change.",
    )

    # Image signing
    failures += _assert(
        bool(re.search(r"sign|cosign|Cosign|Notation|notation|signer|Signer|ECRSigning", text, re.IGNORECASE)),
        "cicd-stack.ts references image signing",
        "Per AC: on success, the CI builds a container image, signs it, and pushes it to ECR.",
    )

    # Signature verification before promote
    failures += _assert(
        bool(re.search(r"verif|Verif|unsigned.*cannot|cannot.*unsigned|verify.*sign|sign.*verify", text, re.IGNORECASE)),
        "cicd-stack.ts references signature verification (unsigned digest cannot be promoted)",
        "Per AC: image signatures are verified at/before deploy; unsigned digest cannot be promoted.",
    )

    # Digest-promotion mechanism
    failures += _assert(
        bool(re.search(r"promot|Promot|digest.*update|update.*digest|SSM|ParameterStore|parameter.*store", text, re.IGNORECASE)),
        "cicd-stack.ts defines a digest-promotion mechanism",
        "Per AC: promotion to a new digest is a deliberate, audited step.",
    )

    # Promotion audit row
    failures += _assert(
        bool(re.search(r"audit|Audit|promotion.*log|log.*promotion|who.*promot|promot.*who", text, re.IGNORECASE)),
        "cicd-stack.ts references promotion audit (who promoted which digest, when)",
        "Per AC: the promotion writes an audit row (who promoted which digest, when).",
    )

    return failures


def check_ci_workflow() -> list[str]:
    """Check B: GitHub Actions CI workflow exists with required steps."""
    print("Check B: .github/workflows/ci-pipeline.yml — tests, lint, scan, build/sign/push, audit …")
    failures = []

    # Find the CI pipeline workflow file
    ci_workflow_path = WORKFLOWS_DIR / "ci-pipeline.yml"
    failures += _assert(ci_workflow_path.exists(), ".github/workflows/ci-pipeline.yml exists")

    if not ci_workflow_path.exists():
        print("  (skipping content checks — file does not exist)")
        return failures

    text = _read(ci_workflow_path)

    # Runs test suite
    failures += _assert(
        bool(re.search(r"test_.*\.py|python.*test|pytest|test.*suite", text, re.IGNORECASE)),
        "ci-pipeline.yml runs the Python test suite",
        "Per AC: CI runs the test suite on every change.",
    )

    # Runs docs-lint (reconciliation AC from architecture review)
    failures += _assert(
        bool(re.search(r"docs.lint|docs_lint", text, re.IGNORECASE)),
        "ci-pipeline.yml runs the docs-lint gate (reconciliation #43)",
        "Per reconciliation: CI additionally runs the docs-lint job (#43).",
    )

    # Runs detector-correctness (reconciliation AC from architecture review)
    failures += _assert(
        bool(re.search(r"detector.correctness|detector_correctness", text, re.IGNORECASE)),
        "ci-pipeline.yml runs the detector-correctness gate (reconciliation #1, #2)",
        "Per reconciliation: CI additionally runs the detector-correctness gates (#1, #2).",
    )

    # Runs security/dependency scan
    failures += _assert(
        bool(re.search(r"scan|trivy|snyk|bandit|safety|audit|dependabot|vulnerability", text, re.IGNORECASE)),
        "ci-pipeline.yml runs a security/dependency scan",
        "Per AC: CI runs security/dependency scans on every change.",
    )

    # Builds and pushes to ECR
    failures += _assert(
        bool(re.search(r"docker.*build|build.*docker|docker.*push|push.*ecr|ecr.*push", text, re.IGNORECASE)),
        "ci-pipeline.yml builds and pushes a container image to ECR",
        "Per AC: on success, CI builds a container image and pushes it to ECR.",
    )

    # Signs image
    failures += _assert(
        bool(re.search(r"sign|cosign|Cosign|notation|Notation|signer", text, re.IGNORECASE)),
        "ci-pipeline.yml signs the container image",
        "Per AC: CI signs the image before pushing to ECR.",
    )

    # Promotion audit row
    failures += _assert(
        bool(re.search(r"audit|promotion.*log|log.*promotion|who.*promot|promot.*who|digest.*audit", text, re.IGNORECASE)),
        "ci-pipeline.yml references promotion audit (who promoted which digest, when)",
        "Per AC: the promotion writes an audit row (who promoted which digest, when).",
    )

    # Signature verification before promote
    failures += _assert(
        bool(re.search(r"verif|unsigned.*reject|reject.*unsigned|verify.*sign|sign.*verify", text, re.IGNORECASE)),
        "ci-pipeline.yml verifies image signature before promotion",
        "Per AC: unsigned or unverifiable digest cannot be promoted.",
    )

    return failures


def check_architecture_md() -> list[str]:
    """Check C: ARCHITECTURE.md documents the CI pipeline constraints."""
    print("Check C: ARCHITECTURE.md — docs-lint, detector-correctness, promotion audit, signature verification …")
    failures = []

    failures += _assert(ARCHITECTURE_MD.exists(), "ARCHITECTURE.md exists")
    if not ARCHITECTURE_MD.exists():
        return failures

    text = _read(ARCHITECTURE_MD)

    # docs-lint referenced as a CI gate in the context of the deploy pipeline
    failures += _assert(
        bool(re.search(r"docs.lint", text, re.IGNORECASE)),
        "ARCHITECTURE.md references docs-lint as a CI gate (reconciliation #43)",
        "Per reconciliation: CI additionally runs the docs-lint job.",
    )

    # detector-correctness referenced as a CI gate in the context of the deploy pipeline
    failures += _assert(
        bool(re.search(r"detector.correctness", text, re.IGNORECASE)),
        "ARCHITECTURE.md references detector-correctness as a CI gate (reconciliation #1, #2)",
        "Per reconciliation: CI additionally runs the detector-correctness gates.",
    )

    # Promotion audit documented
    failures += _assert(
        bool(re.search(
            r"promot.*audit|audit.*promot|who.*promot.*digest|digest.*promot.*audit"
            r"|promotion.*writes.*audit|audit.*row.*promot",
            text,
            re.IGNORECASE | re.DOTALL,
        )),
        "ARCHITECTURE.md documents promotion audit row (who promoted which digest, when)",
        "Per AC: the promotion writes an audit row (who promoted which digest, when).",
    )

    # Signature verification before promote
    failures += _assert(
        bool(re.search(
            r"unsigned.*cannot.*promot|cannot.*promot.*unsigned"
            r"|sign.*verif.*before.*promot|verif.*sign.*before.*deploy"
            r"|unsigned.*unverifiable.*cannot|unverifiable.*cannot.*promot",
            text,
            re.IGNORECASE | re.DOTALL,
        )),
        "ARCHITECTURE.md documents that unsigned/unverifiable digest cannot be promoted",
        "Per AC: image signatures are verified at/before deploy; unsigned digest cannot be promoted.",
    )

    return failures


def check_main_stack_wires_cicd() -> list[str]:
    """Check D: ContractToasterStack wires the CicdStack nested stack."""
    print("Check D: ContractToasterStack (contract-toaster-stack.ts) wires the CicdStack …")
    failures = []

    failures += _assert(MAIN_STACK_PATH.exists(), "infra/lib/contract-toaster-stack.ts exists")
    if not MAIN_STACK_PATH.exists():
        return failures

    text = _read(MAIN_STACK_PATH)

    failures += _assert(
        bool(re.search(r"CicdStack|cicd.?stack|cicd-stack", text, re.IGNORECASE)),
        "contract-toaster-stack.ts imports/references CicdStack",
        "Per ARCHITECTURE.md: ContractToasterStack-cicd must be a nested stack under ContractToasterStack.",
    )

    return failures


def check_cdk_synth() -> list[str]:
    """Check E: cdk synth runs cleanly with the CICD stack."""
    print("Check E: cdk synth runs cleanly with the CICD stack included …")
    failures = []

    result = subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=str(INFRA),
        capture_output=True,
        text=True,
        timeout=120,
    )
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev exits 0",
        f"stderr: {result.stderr[:400]}" if result.returncode != 0 else "",
    )

    return failures


def main() -> int:
    print("CI pipeline structural gate (issue #66)")
    print("=" * 60)
    print()

    all_failures: list[str] = []

    all_failures += check_cicd_stack()
    print()
    all_failures += check_ci_workflow()
    print()
    all_failures += check_architecture_md()
    print()
    all_failures += check_main_stack_wires_cicd()
    print()
    all_failures += check_cdk_synth()
    print()

    print("=" * 60)
    print()

    if all_failures:
        print(f"FAIL: {len(all_failures)} check(s) failed.")
        return 1

    print("PASS: all CI pipeline structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
