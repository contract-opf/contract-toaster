#!/usr/bin/env python3
"""
Structural gate for issue #634: the frontend suite must be behind an
AUTOMATED signal, not the luck of ticket authorship.

Background (issue #634, evidence gathered 2026-08-26):
  `scripts/check-frontend.sh` sat RED on clean `origin/main` — issue #599
  renamed a tab ("Model & API key" -> "Models") and two assertions in
  frontend/src/__tests__/review-failure-diagnosis.test.tsx kept asserting the
  old label. Nothing noticed, because nothing ran the suite:

    * scripts/check.sh (the local green gate and the AFK loop's check
      command) is Python-only — it delegates to
      scripts/collect_test_failures.sh, which globs tests/test_*.py,
      tests/*/test_*.py and tests/lint-*.py.
    * No .github/workflows/*.yml referenced check-frontend.sh, build:ci or
      vitest.

  The regression was fixed only because a LATER, unrelated ticket happened to
  name `bash scripts/check-frontend.sh` in its own verification block. That is
  not a signal.

This file is the signal's own guard. It is a tests/test_*.py module, so
scripts/collect_test_failures.sh discovers it — which means BOTH
scripts/check.sh and CI GATE A fail if the frontend gate is ever unwired
again. It does not run the frontend suite itself (that is CI GATE E's job,
and it needs a Node toolchain); it asserts that something automatic does, and
that a failure there is LOUD.

Checks:
  0. Self-checks, so this file's own machinery cannot go quietly blind:
     (a) the finder attributes the gate to the job that RUNS it, not to the
         job whose trailing banner comment merely mentions it (a job's banner
         sits inside the PREVIOUS job's block);
     (b) Check 3's paths-filter branch really rejects a filter that omits
         frontend/**. This repo's own workflow has no paths filter, so that
         branch is otherwise never taken — and never-taken means never known
         to work.
  1. scripts/check-frontend.sh exists, is executable, fails closed
     (`set -euo pipefail`), and actually runs the typecheck+build, the vitest
     suite and the three CTDS audits.
  2. Some .github/workflows/*.yml job invokes scripts/check-frontend.sh.
  3. That workflow runs on pull_request AND on push to main, and does not
     path-filter the frontend gate out of existence (a `paths:` filter is
     allowed only if it covers frontend/** and the gate script itself).
  4. The gate step does not pipe the script's output. A pipeline reports the
     exit status of its LAST command, so `bash scripts/check-frontend.sh |
     tail` exits 0 no matter what the gate found — the exact trap
     scripts/check.sh's own header warns about in capitals, and the one #634's
     own comment asks to be built into whatever gate gets added ("a `| tail`
     pipeline reports tail's exit code, not the script's").
  5. The gate is wired into the main-red reporting job: it is in
     report-main-failure's `needs` and named in its FAILED_GATES expression,
     so a red frontend suite on main files/updates the `ci-main-red` issue
     the triage loop already watches.
  6. The gate blocks the image pipeline: build-sign-push `needs` it, so an
     unbuildable / test-red SPA cannot be signed and pushed.

Run with: python3 tests/test_frontend_gate_wired_634.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

import re
import stat
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = REPO_ROOT / "scripts" / "check-frontend.sh"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# The relative path a workflow step must invoke.
GATE_INVOCATION = "scripts/check-frontend.sh"


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


def _strip_comments(block: str) -> str:
    """Drop whole-line YAML comments from a job block.

    Load-bearing, not cosmetic. A job's banner comment sits ABOVE its `job-id:`
    header and therefore inside the PRECEDING job's block, so a plain
    "does this block mention check-frontend.sh?" search attributes the gate to
    whichever job happens to be listed before it. That is an element-identity
    error, and it silently passes every downstream check against the WRONG
    job — this file caught itself doing exactly that while being written.
    _self_check_job_attribution() below fails if this stripping is removed.
    """
    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))


def _split_jobs(text: str) -> dict[str, str]:
    """Map job id -> the job's own YAML block.

    Deliberately a small line scanner rather than a YAML parse: PyYAML is not
    in requirements-dev.txt, and every other workflow gate in tests/ reads
    these files as text (see tests/test_ci_pipeline.py).
    """
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "jobs:")
    except StopIteration:
        return {}

    jobs: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start + 1 :]:
        header = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if header:
            current = header.group(1)
            jobs[current] = []
            continue
        # A non-indented, non-blank line ends the jobs: mapping.
        if line.strip() and not line.startswith("  "):
            break
        if current is not None:
            jobs[current].append(line)
    return {job: "\n".join(body) for job, body in jobs.items()}


def check_gate_script() -> list[str]:
    """Check 1: the gate script exists, fails closed, and runs the real suite."""
    print("Check 1: scripts/check-frontend.sh exists, fails closed, runs the real suite …")
    failures = []
    failures += _assert(GATE_SCRIPT.exists(), "scripts/check-frontend.sh exists")
    if not GATE_SCRIPT.exists():
        print("  (skipping content checks — file does not exist)")
        return failures

    failures += _assert(
        bool(GATE_SCRIPT.stat().st_mode & stat.S_IXUSR),
        "scripts/check-frontend.sh is executable",
    )
    text = _read(GATE_SCRIPT)
    failures += _assert(
        "set -euo pipefail" in text,
        "scripts/check-frontend.sh sets -euo pipefail (fails closed)",
        "Without -e a failing npm step would not stop the script, and the "
        "gate would print ALL GREEN over a red suite.",
    )
    for label, needle in (
        ("typecheck + production build (build:ci)", "npm run build:ci"),
        ("vitest component suite (npm test)", "npm test"),
        ("contrast audit", "npm run audit:contrast"),
        ("focus/reduced-motion audit", "npm run audit:focus"),
        ("layout audit", "npm run audit:layout"),
    ):
        failures += _assert(
            needle in text,
            f"scripts/check-frontend.sh runs the {label}",
            f"Expected to find `{needle}` in the gate script.",
        )
    return failures


def _find_gate_job(text: str) -> tuple[str | None, str]:
    """Return (job id, job body) for the job that really RUNS the gate.

    "Really runs" = the invocation appears on a non-comment line of the job's
    own block. See _strip_comments() for why the comment filter is required.
    """
    for job_id, body in _split_jobs(text).items():
        if GATE_INVOCATION in _strip_comments(body):
            return job_id, body
    return None, ""


def _find_gate_workflow() -> tuple[Path | None, str | None, str]:
    """Return (workflow path, job id, job body) for the job that runs the gate."""
    if not WORKFLOWS_DIR.is_dir():
        return None, None, ""
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml")):
        text = _read(path)
        if GATE_INVOCATION not in text:
            continue
        job_id, body = _find_gate_job(text)
        if job_id is not None:
            return path, job_id, body
    return None, None, ""


# A workflow shaped exactly like ci-pipeline.yml: the frontend gate's banner
# comment sits above its own `job-id:` header, i.e. textually inside the
# PREVIOUS job's block. Anything that attributes the gate by "which block
# mentions check-frontend.sh" answers `earlier-job` here — the wrong job — and
# then happily verifies that wrong job's needs/FAILED_GATES wiring.
_ATTRIBUTION_FIXTURE = """\
name: fixture
jobs:
  earlier-job:
    name: Some other gate
    steps:
      - name: Do something else
        run: python3 tests/test_something.py

  # ---------------------------------------------------------------------------
  # GATE E: Frontend suite — this banner mentions scripts/check-frontend.sh
  # while sitting inside earlier-job's block.
  # ---------------------------------------------------------------------------
  frontend-gate:
    name: Frontend suite
    steps:
      - name: Run the frontend gate
        run: bash scripts/check-frontend.sh
"""


# A gate workflow that DOES run check-frontend.sh but path-filters frontend/**
# out of its own trigger — "has a CI job", still blind to #599's rename.
_PATH_FILTERED_FIXTURE = """\
name: path-filtered gate
on:
  pull_request:
    paths:
      - 'backend/**'
  push:
    branches:
      - main
    paths:
      - 'backend/**'

jobs:
  frontend-gate:
    steps:
      - run: bash scripts/check-frontend.sh
"""


def _self_check_job_attribution() -> list[str]:
    """Check 0: the finder names the job that RUNS the gate, not the one whose
    trailing comment merely mentions it."""
    print("Check 0: self-check — the gate is attributed to the job that runs it …")
    job_id, body = _find_gate_job(_ATTRIBUTION_FIXTURE)
    failures = _assert(
        job_id == "frontend-gate",
        "a banner comment above the job header does not misattribute the gate",
        f"Expected `frontend-gate`, got `{job_id}`. Whole-line comments must be "
        "stripped before a job block is searched (see _strip_comments).",
    )
    failures += _assert(
        "bash scripts/check-frontend.sh" in body,
        "the attributed job's body carries the real invocation",
    )

    # ci-pipeline.yml has no `paths:` filter, so Check 3's filter branch never
    # runs against the real tree — an untaken branch stays green forever, and
    # a path-filtered gate is precisely how this hole could be reopened while
    # still "having a CI job". Exercise it against a workflow that filters
    # frontend/** out.
    print("  … probing Check 3's paths-filter branch (the [FAIL] below is the expected result):")
    with tempfile.TemporaryDirectory() as tmp:
        probe_path = Path(tmp) / "path-filtered-gate.yml"
        probe_path.write_text(_PATH_FILTERED_FIXTURE, encoding="utf-8")
        probe_failures = check_gate_triggers(probe_path)
    failures += _assert(
        len(probe_failures) == 1,
        "a paths filter that omits frontend/** is rejected by Check 3",
        "Check 3's filter branch is unreachable against this repo's own "
        "workflow, so it is proven here instead. Expected exactly one failure "
        f"from the probe, got {probe_failures}.",
    )
    return failures


def check_workflow_runs_gate(path: Path | None, job_id: str | None) -> list[str]:
    """Check 2: some workflow job actually invokes the gate."""
    print("Check 2: a .github/workflows job invokes scripts/check-frontend.sh …")
    detail = (
        "Issue #634: the frontend suite was executed ONLY when a ticket "
        "happened to name it in its own verification block, which is how "
        "#599's tab rename sat red on main unnoticed. Add a CI job that runs "
        "`bash scripts/check-frontend.sh`."
    )
    failures = _assert(path is not None and job_id is not None, "a workflow job runs the frontend gate", detail)
    if path is not None and job_id is not None:
        print(f"         (found: {path.name} job `{job_id}`)")
    return failures


def check_gate_triggers(path: Path | None) -> list[str]:
    """Check 3: the gate workflow runs on PRs and on push to main, unfiltered."""
    print("Check 3: the gate workflow triggers on pull_request and push to main …")
    if path is None:
        print("  (skipping — no workflow runs the gate)")
        return ["gate workflow triggers (no workflow found)"]

    text = _read(path)
    # Only the trigger header, not the whole file: a `paths:` filter must be
    # judged on what the `on:` block says, not on some job step that happens
    # to mention the same path elsewhere.
    header = text.split("\njobs:", 1)[0]
    failures = []
    failures += _assert(
        re.search(r"^\s*pull_request:\s*$", header, re.MULTILINE) is not None,
        f"{path.name} triggers on pull_request",
    )
    push_block = re.search(r"^  push:\n(?:.*\n)*?(?=^  \w|^\w|\Z)", header, re.MULTILINE)
    failures += _assert(
        push_block is not None and "main" in push_block.group(0),
        f"{path.name} triggers on push to main",
        "Post-merge breakage on main is the case #634 is about; a PR-only "
        "trigger cannot see two independently-green PRs breaking each other.",
    )
    # A paths: filter is allowed only if it cannot hide a frontend change.
    if re.search(r"^\s*paths(-ignore)?:\s*$", header, re.MULTILINE):
        failures += _assert(
            "frontend/**" in header and GATE_INVOCATION in header,
            f"{path.name}'s paths filter still covers frontend/** and the gate script",
            "A paths filter that omits frontend/** re-opens exactly the hole "
            "#634 exists to close.",
        )
    else:
        print("  [PASS] the gate workflow has no paths filter to hide a change behind")
    return failures


def check_gate_step_not_piped(path: Path | None, body: str) -> list[str]:
    """Check 4: the gate invocation is not piped (exit-code masking)."""
    print("Check 4: the gate invocation is not piped into another command …")
    if path is None:
        print("  (skipping — no workflow runs the gate)")
        return ["gate step not piped (no workflow found)"]

    offenders = [
        line.strip()
        for line in body.splitlines()
        if GATE_INVOCATION in line and not line.lstrip().startswith("#") and "|" in line.split(GATE_INVOCATION, 1)[1]
    ]
    return _assert(
        not offenders,
        "the gate invocation is not piped into tail/tee/head",
        "A pipeline reports the exit status of its LAST command, so "
        "`bash scripts/check-frontend.sh | tail` exits 0 over a red gate "
        "(scripts/check.sh's header warns about this in capitals). "
        f"Offending line(s): {offenders}",
    )


def check_main_red_reporting(path: Path | None, job_id: str | None) -> list[str]:
    """Check 5: a red frontend suite on main is LOUD (files the ci-main-red issue)."""
    print("Check 5: the frontend gate is wired into the main-red reporting job …")
    if path is None or job_id is None:
        print("  (skipping — no workflow runs the gate)")
        return ["main-red reporting (no workflow found)"]

    jobs = _split_jobs(_read(path))
    reporter = jobs.get("report-main-failure")
    failures = []
    failures += _assert(
        reporter is not None,
        f"{path.name} has a report-main-failure job",
        "Issue #634 is about a gate nobody watches. A gate whose failure on "
        "main files no issue is a gate nobody watches.",
    )
    if reporter is None:
        return failures

    needs = re.search(r"^\s*needs:\s*\[(.*?)\]", reporter, re.MULTILINE)
    failures += _assert(
        needs is not None and job_id in [n.strip() for n in needs.group(1).split(",")],
        f"report-main-failure needs `{job_id}`",
        "Without it, `contains(needs.*.result, 'failure')` never sees the "
        "frontend gate and no ci-main-red issue is filed.",
    )
    failures += _assert(
        f"needs.{job_id}.result" in reporter,
        f"report-main-failure names `{job_id}` in FAILED_GATES",
        "The filed issue must say WHICH gate went red.",
    )
    return failures


def check_blocks_image_pipeline(path: Path | None, job_id: str | None) -> list[str]:
    """Check 6: a red frontend gate blocks build-sign-push."""
    print("Check 6: the frontend gate blocks the image build/sign/push job …")
    if path is None or job_id is None:
        print("  (skipping — no workflow runs the gate)")
        return ["image pipeline blocking (no workflow found)"]

    jobs = _split_jobs(_read(path))
    build = jobs.get("build-sign-push")
    if build is None:
        print("  (skipping — this workflow has no build-sign-push job)")
        return []

    needs = re.search(r"^\s*needs:\s*\[(.*?)\]", build, re.MULTILINE)
    return _assert(
        needs is not None and job_id in [n.strip() for n in needs.group(1).split(",")],
        f"build-sign-push needs `{job_id}`",
        "An SPA that does not typecheck, build, or pass its own suite must "
        "not be signed and pushed as a release candidate.",
    )


def main() -> int:
    print("=" * 60)
    print("Frontend gate wiring (issue #634)")
    print("=" * 60)
    print()

    path, job_id, body = _find_gate_workflow()

    all_failures: list[str] = []
    all_failures += _self_check_job_attribution()
    print()
    all_failures += check_gate_script()
    print()
    all_failures += check_workflow_runs_gate(path, job_id)
    print()
    all_failures += check_gate_triggers(path)
    print()
    all_failures += check_gate_step_not_piped(path, body)
    print()
    all_failures += check_main_red_reporting(path, job_id)
    print()
    all_failures += check_blocks_image_pipeline(path, job_id)
    print()

    print("=" * 60)
    print()
    if all_failures:
        print(f"FAIL: {len(all_failures)} check(s) failed.")
        return 1
    print("PASS: the frontend suite is behind an automated, loud CI signal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
