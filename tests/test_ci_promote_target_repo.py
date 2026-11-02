#!/usr/bin/env python3
"""
ci-pipeline.yml's promote job refuses a target environment whose ECR
repository does not hold the built digest.

WHY THIS GATE EXISTS
--------------------
build-sign-push pushes and signs into ONE repository (the workflow-level
`ECR_REPOSITORY`, contract-toaster-api-dev), but each environment's App
Runner service pulls from its own `contract-toaster-api-<env>` repository
(`infra/lib/nested/cicd-stack.ts`), and prod lives in a separate account.
`promote=prod` therefore verified the signature in the dev repository, wrote
a prod audit row and repointed the prod SSM digest at an image the prod
repository does not contain: the next `cdk deploy --context env=prod` would
fail to pull, after the audit trail already said the promotion happened.

Copying the digest (and its cosign signature) across accounts needs a role
that can read the dev repository and write the prod one, which this
workflow is not given. So until that exists the job refuses, before it
takes AWS credentials, any target whose repository is not the one it built
into — see RUNBOOK.md "Deploying a code change" for the prod path.

  A. The refusal step runs before the job configures AWS credentials and
     before STEP H/I/J.
  B. Run for real under bash: `promote=prod` exits non-zero with a message
     naming the target repository; `promote=dev` passes.
  C. The workflow no longer carries the "Known gap, not closed here" note,
     and RUNBOOK.md documents the refusal.

Run: python3 tests/test_ci_promote_target_repo.py
Exit 0 = pass, 1 = fail.
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-pipeline.yml"
GUARD_MARKER = "STEP G"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _promote_steps() -> list[str]:
    m = re.search(r"(?ms)^  promote:\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)", _text())
    if not m:
        raise AssertionError("ci-pipeline.yml has no promote job")
    return re.split(r"(?m)^      - (?=name:|uses:|run:)", m.group(1))[1:]


def _guard_step() -> str:
    for step in _promote_steps():
        if GUARD_MARKER in step.split("\n", 1)[0]:
            return step
    raise AssertionError(f"promote job has no '{GUARD_MARKER}' step")


def _run_block(step: str) -> str:
    m = re.search(r"(?ms)^        run: \|\n(.*)", step)
    if not m:
        raise AssertionError("guard step has no `run: |` block")
    lines = []
    for line in m.group(1).splitlines():
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line[10:])
    return "\n".join(lines)


def _workflow_ecr_repository() -> str:
    m = re.search(r"(?m)^  ECR_REPOSITORY:\s*(\S+)\s*$", _text())
    if not m:
        raise AssertionError("no workflow-level ECR_REPOSITORY")
    return m.group(1)


class TestPromoteTargetRepo(unittest.TestCase):
    def test_a_guard_runs_before_credentials_and_promotion(self) -> None:
        steps = _promote_steps()
        heads = [s.split("\n", 1)[0] for s in steps]
        guard = next(i for i, h in enumerate(heads) if GUARD_MARKER in h)
        creds = next(i for i, s in enumerate(steps) if "configure-aws-credentials" in s)
        later = [i for i, h in enumerate(heads) if re.search(r"STEP [HIJ]\b", h)]
        self.assertTrue(later, "promote job has no STEP H/I/J")
        self.assertLess(guard, creds)
        self.assertLess(guard, min(later))

    def _run_guard(self, target: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TARGET_ENV": target,
            "ECR_REPOSITORY": _workflow_ecr_repository(),
        }
        return subprocess.run(  # noqa: S603
            ["bash", "-e", "-o", "pipefail", "-c", _run_block(_guard_step())],  # noqa: S607
            env=env,
            capture_output=True,
            text=True,
        )

    def test_b_prod_is_refused_with_a_clear_message(self) -> None:
        result = self._run_guard("prod")
        self.assertNotEqual(result.returncode, 0, "promote=prod was not refused")
        out = result.stdout + result.stderr
        self.assertIn("contract-toaster-api-prod", out)
        self.assertIn("RUNBOOK.md", out)

    def test_b_dev_passes(self) -> None:
        result = self._run_guard("dev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_c_docs_describe_the_refusal(self) -> None:
        self.assertNotIn("Known gap, not closed here", _text())
        runbook = (REPO_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("refuses `promote=prod`", runbook)


def _run_tests() -> int:
    suite = unittest.TestLoader().loadTestsFromTestCase(TestPromoteTargetRepo)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
