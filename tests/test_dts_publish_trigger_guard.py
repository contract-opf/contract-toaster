#!/usr/bin/env python3
"""
Who may publish the DTS images, and where pull-request code may run.

.github/workflows/dts-image-publish.yml rolls the Docker Compose deployment
forward automatically once "CI pipeline" completes (deploy/dts/README.md). A
`workflow_run` trigger fires for every completed CI run its filter admits,
and its `branches:` filter matches the triggering run's head branch NAME, not
where that branch lives. So the jobs that push to GHCR decide for themselves
that the run they are following is a successful `push` to this repository's
`main`, and nothing else.

What this file pins:

  1. Every job in the publish workflow that runs `docker push` carries an
     `if:` that admits exactly two cases: a manual `workflow_dispatch` (the
     operator is the gate), or a `workflow_run` whose run concluded
     `success`, was a `push`, on `main`, from this repository. No other
     disjunct, and none of the four conditions missing.
  2. The `deploy` job depends on every pushing job, so it follows their gate.
  3. Every job in ci-pipeline.yml and the publish workflow that would run on
     the self-hosted runner (vars.CI_RUNS_ON) is sent to a GitHub-hosted
     runner when the event is `pull_request`, so pull-request code never runs
     on the home-lab box.

Run: python3 tests/test_dts_publish_trigger_guard.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PUBLISH = WORKFLOWS / "dts-image-publish.yml"
CI = WORKFLOWS / "ci-pipeline.yml"

DISPATCH = "github.event_name == 'workflow_dispatch'"
WORKFLOW_RUN_CONDITIONS = frozenset(
    {
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    }
)
PR_RUNNER = (
    "${{ github.event_name == 'pull_request' && 'ubuntu-latest' "
    "|| vars.CI_RUNS_ON || 'ubuntu-latest' }}"
)


def jobs(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def squash(expr: str) -> str:
    return re.sub(r"\s+", " ", expr).strip()


def strip_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        expr = expr[1:-1].strip()
    return expr


def split_top(expr: str, op: str) -> list[str]:
    """Split on `op` outside parentheses and quotes."""
    parts, depth, quote, start, i = [], 0, False, 0, 0
    while i < len(expr):
        ch = expr[i]
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
        elif not quote and depth == 0 and expr.startswith(op, i):
            parts.append(expr[start:i].strip())
            i += len(op)
            start = i
            continue
        i += 1
    parts.append(expr[start:].strip())
    return parts


def pushes(job: dict) -> bool:
    return any("docker push" in (step.get("run") or "") for step in job.get("steps", []))


class PublishTriggerGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.jobs = jobs(PUBLISH)
        self.pushers = {name: job for name, job in self.jobs.items() if pushes(job)}

    def test_the_jobs_that_push_are_the_ones_expected(self) -> None:
        self.assertEqual(set(self.pushers), {"publish", "publish-minio"})

    def test_every_pushing_job_admits_only_dispatch_or_a_main_push(self) -> None:
        for name, job in self.pushers.items():
            with self.subTest(job=name):
                cond = squash(str(job.get("if", "")))
                self.assertTrue(cond, f"{name} has no if:")
                disjuncts = [strip_parens(d) for d in split_top(cond, "||")]
                self.assertEqual(len(disjuncts), 2, f"{name}: {cond}")
                self.assertIn(DISPATCH, disjuncts, f"{name}: {cond}")
                other = next(d for d in disjuncts if d != DISPATCH)
                conjuncts = {strip_parens(c) for c in split_top(other, "&&")}
                self.assertEqual(conjuncts, WORKFLOW_RUN_CONDITIONS, f"{name}: {cond}")

    def test_deploy_follows_every_pushing_job(self) -> None:
        needs = self.jobs["deploy"].get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        self.assertEqual(set(needs), set(self.pushers))

    def test_trigger_is_ci_pipeline_on_main_or_manual(self) -> None:
        on = yaml.safe_load(PUBLISH.read_text(encoding="utf-8"))[True]
        self.assertEqual(set(on), {"workflow_run", "workflow_dispatch"})
        self.assertEqual(on["workflow_run"]["workflows"], ["CI pipeline"])
        self.assertEqual(on["workflow_run"]["types"], ["completed"])


class PullRequestsUseHostedRunners(unittest.TestCase):
    def test_self_hosted_jobs_fall_back_to_hosted_for_pull_requests(self) -> None:
        for path in (CI, PUBLISH):
            for name, job in jobs(path).items():
                runs_on = str(job.get("runs-on", ""))
                if "CI_RUNS_ON" not in runs_on and "self-hosted" not in runs_on:
                    continue
                with self.subTest(workflow=path.name, job=name):
                    self.assertEqual(squash(runs_on), PR_RUNNER)

    def test_some_job_is_actually_checked(self) -> None:
        guarded = [
            name
            for path in (CI, PUBLISH)
            for name, job in jobs(path).items()
            if "CI_RUNS_ON" in str(job.get("runs-on", ""))
        ]
        self.assertGreaterEqual(len(guarded), 3, guarded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
