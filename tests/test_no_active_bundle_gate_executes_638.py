#!/usr/bin/env python3
"""
Structural + behavioral guard for issue #638: the no-active-bundle gate must
actually EXECUTE in CI, not skip and report success.

Background (issue #638, evidence from run 33456932705):
  .github/workflows/docs-lint.yml's `no-active-bundle` job runs
  `python3 tests/test_no_active_bundle.py` on a bare `actions/setup-python`
  interpreter — no `pip install`, no requirements file. Gate 1a of that file
  is the BEHAVIORAL check issue #196 introduced precisely so the gate would
  stop asserting on ARCHITECTURE.md prose: it imports backend/src/main.py and
  drives POST /api/reviews with a real fastapi TestClient. On a bare
  interpreter that import raises ModuleNotFoundError, Gate 1a takes its
  documented-skip branch, and the job exits 0:

      Gate 1a: SKIP (documented reason) — could not import
      backend/src/main.py (ModuleNotFoundError("No module named 'boto3'"))
      ...
      PASS (with documented skip above)

  The skip was honestly reported and its reasoning was sound — but it was
  PERMANENT and INVISIBLE: the one environment that ran this gate on every
  push was the one environment that could never satisfy it. A regression in
  the refusal path would have shipped with a green tick.

  Issue #638 fixed both halves: the job installs backend/requirements.txt,
  and a Gate 1a skip is FATAL when $CI is set. This file is that fix's own
  guard. It is a tests/test_*.py module, so scripts/collect_test_failures.sh
  discovers it — meaning BOTH scripts/check.sh and CI GATE A go red if either
  half is undone.

Checks:
  1. The workflow job that RUNS tests/test_no_active_bundle.py installs the
     backend dependencies from backend/requirements.txt, in a step that comes
     BEFORE the step running the gate, in the SAME job.
  2. RED HALF — with the gate's import poisoned (a `boto3` shim that raises,
     reproducing the exact CI condition) and $CI set, the gate exits NON-ZERO
     and names the skip as fatal. This is the assertion that fails if the
     CI-strict rule is deleted.
  3. Off-CI the SAME poisoned run still exits 0 with its documented skip: the
     courtesy to a contributor laptop is preserved, and the rule is proven to
     be driven by $CI rather than by the poisoning.
  4. GREEN HALF — with the backend dependencies actually importable and $CI
     set, the gate exits 0, prints the Gate 1a EXECUTED line, and does NOT
     print a Gate 1a skip line. This is the check that the CI log carries the
     assertion, not the skip.
  5. ci_is_strict() reads $CI with both variants seeded: truthy values that
     make a skip fatal and falsey values ("", "0", "false", ...) that do not.
     A one-variant fixture would leave the branch green forever.

Every input this file constructs is a real environment for a real subprocess
run of tests/test_no_active_bundle.py — the same command
.github/workflows/docs-lint.yml runs. Nothing here hand-builds a gate result;
the failure text asserted on is produced by the production gate module.

Run with: python3 tests/test_no_active_bundle_gate_executes_638.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
GATE_FILE = REPO_ROOT / "tests" / "test_no_active_bundle.py"

# The command the workflow step must run, and the requirements file its job
# must install first.
GATE_INVOCATION = "tests/test_no_active_bundle.py"
REQUIREMENTS_FILE = "backend/requirements.txt"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── workflow parsing ─────────────────────────────────────────────────────────
#
# Deliberately a line scanner rather than a YAML parse: the workflow files are
# consumed by other gates in this repo the same way (see
# tests/test_frontend_gate_wired_634.py), PyYAML is not a declared dependency,
# and the property under test is textual ("this job has these two steps in
# this order").


def _job_blocks(text: str) -> dict:
    """Split a workflow's `jobs:` mapping into {job_id: block_text}.

    A job block runs from its own `  <id>:` line up to the next one. Trailing
    banner comments therefore belong to the PREVIOUS job's block, which is why
    Check 1 attributes the gate to the job whose `run:` line invokes it, not to
    a job that merely mentions it in a comment.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "jobs:")
    except StopIteration:
        return {}

    blocks: dict = {}
    current_id = None
    current: list = []
    job_header = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
    for ln in lines[start + 1 :]:
        m = job_header.match(ln)
        if m:
            if current_id is not None:
                blocks[current_id] = "\n".join(current)
            current_id = m.group(1)
            current = []
        elif current_id is not None:
            current.append(ln)
    if current_id is not None:
        blocks[current_id] = "\n".join(current)
    return blocks


def _uncommented(block: str) -> str:
    """The block with whole-line `#` comments stripped.

    A `run:` line inside a comment is not a step, and an install mentioned
    only in prose is not an install.
    """
    return "\n".join(
        ln for ln in block.splitlines() if not ln.lstrip().startswith("#")
    )


# ── Check 1: the job that runs the gate installs its dependencies ───────────


def check_workflow_installs_gate_dependencies() -> list:
    failures = []

    running_jobs = []  # (workflow_path, job_id, block)
    for wf in sorted(WORKFLOWS_DIR.glob("*.yml")):
        text = _read(wf)
        for job_id, block in _job_blocks(text).items():
            body = _uncommented(block)
            if re.search(
                r"run:.*\b" + re.escape(GATE_INVOCATION) + r"\b", body
            ):
                running_jobs.append((wf, job_id, body))

    if not running_jobs:
        failures.append(
            f"  No .github/workflows/*.yml job runs {GATE_INVOCATION}.\n"
            "  The no-active-bundle gate must stay wired to an automated\n"
            "  signal. (issue #638)"
        )
        return failures

    for wf, job_id, body in running_jobs:
        install_idx = None
        gate_idx = None
        for i, ln in enumerate(body.splitlines()):
            if install_idx is None and re.search(
                r"pip install\b.*-r\s+" + re.escape(REQUIREMENTS_FILE), ln
            ):
                install_idx = i
            if gate_idx is None and re.search(
                r"run:.*\b" + re.escape(GATE_INVOCATION) + r"\b", ln
            ):
                gate_idx = i

        if install_idx is None:
            failures.append(
                f"  {wf.relative_to(REPO_ROOT)} job '{job_id}' runs\n"
                f"  {GATE_INVOCATION} but never installs\n"
                f"  {REQUIREMENTS_FILE}. Gate 1a imports backend/src/main.py\n"
                "  and drives POST /api/reviews with a real TestClient; on an\n"
                "  interpreter without those deps it can only take its\n"
                "  documented-skip branch — which is how this gate sat\n"
                "  permanently green asserting nothing. (issue #638)"
            )
        elif gate_idx is not None and install_idx > gate_idx:
            failures.append(
                f"  {wf.relative_to(REPO_ROOT)} job '{job_id}' installs\n"
                f"  {REQUIREMENTS_FILE} AFTER running {GATE_INVOCATION}.\n"
                "  The install must come first or the gate still skips.\n"
                "  (issue #638)"
            )

    return failures


# ── subprocess helpers for Checks 2–4 ───────────────────────────────────────


def _run_gate(env_overrides: dict, poison_import: bool) -> subprocess.CompletedProcess:
    """Run tests/test_no_active_bundle.py exactly as the workflow step does.

    poison_import=True reproduces the CI condition issue #638 found: a `boto3`
    that cannot be imported, so `import src.main` raises and Gate 1a reaches
    its skip branch. It is injected on PYTHONPATH (ahead of site-packages,
    behind the paths the gate itself inserts), which is why it shadows a real
    boto3 without touching the environment this process runs in.
    """
    env = dict(os.environ)
    env.pop("CI", None)
    env.update(env_overrides)

    with tempfile.TemporaryDirectory() as tmp:
        if poison_import:
            Path(tmp, "boto3.py").write_text(
                "raise ImportError(\"No module named 'boto3'\")\n",
                encoding="utf-8",
            )
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = tmp + (os.pathsep + existing if existing else "")
        return subprocess.run(
            [sys.executable, str(GATE_FILE)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )


SKIP_LINE = "Gate 1a: SKIP (documented reason)"
FATAL_LINE = "Gate 1a: SKIP IS FATAL IN CI"
EXECUTED_LINE = "Gate 1a: EXECUTED (behavioral check"


# ── Check 2: a skip is FATAL when $CI is set ────────────────────────────────


def check_ci_skip_is_fatal() -> list:
    failures = []
    proc = _run_gate({"CI": "true"}, poison_import=True)
    out = proc.stdout + proc.stderr

    if proc.returncode == 0:
        failures.append(
            "  With Gate 1a's import poisoned (the exact CI condition in\n"
            "  issue #638) and CI=true, tests/test_no_active_bundle.py exited\n"
            "  0. A skip on the gate of record must FAIL the job — that is\n"
            "  the whole point of #638.\n"
            f"  ---- gate output ----\n{out}"
        )
        return failures

    if SKIP_LINE not in out:
        failures.append(
            "  The poisoned CI run did not print the documented skip reason.\n"
            "  A fatal skip must still say WHY it skipped, or the CI log\n"
            "  cannot be acted on. (issue #638)\n"
            f"  ---- gate output ----\n{out}"
        )
    if FATAL_LINE not in out:
        failures.append(
            "  The poisoned CI run failed but did not name the skip as the\n"
            f"  cause ({FATAL_LINE!r} absent). The failure must be\n"
            "  self-explaining. (issue #638)\n"
            f"  ---- gate output ----\n{out}"
        )
    return failures


# ── Check 3: off-CI the documented skip is still tolerated ──────────────────


def check_local_skip_still_tolerated() -> list:
    failures = []
    proc = _run_gate({}, poison_import=True)
    out = proc.stdout + proc.stderr

    if proc.returncode != 0:
        failures.append(
            "  With Gate 1a's import poisoned and $CI unset, the gate exited\n"
            f"  {proc.returncode}. Issue #638 made the skip fatal on CI\n"
            "  specifically — a contributor laptop without backend deps must\n"
            "  still get the documented skip, not a hard failure.\n"
            f"  ---- gate output ----\n{out}"
        )
    if SKIP_LINE not in out:
        failures.append(
            "  The off-CI poisoned run did not print the documented skip.\n"
            "  (issue #196's skip path must survive #638.)\n"
            f"  ---- gate output ----\n{out}"
        )
    if FATAL_LINE in out:
        failures.append(
            "  The off-CI poisoned run treated the skip as fatal. The rule\n"
            "  must be driven by $CI, not by the missing dependency itself.\n"
            "  (issue #638)\n"
            f"  ---- gate output ----\n{out}"
        )
    return failures


# ── Check 4: with deps present the behavioral assertion actually runs ───────


def check_gate_actually_executes_under_ci() -> list:
    failures = []
    proc = _run_gate({"CI": "true"}, poison_import=False)
    out = proc.stdout + proc.stderr

    if proc.returncode != 0:
        failures.append(
            f"  tests/test_no_active_bundle.py exited {proc.returncode} under\n"
            "  CI=true with this interpreter's real dependencies. If the\n"
            "  failure is a Gate 1a skip, this environment is missing the\n"
            "  backend deps the gate needs: `pip install -r\n"
            "  backend/requirements.txt`. That is a hard requirement now, not\n"
            "  an optional extra — the gate exists to exercise the live\n"
            "  route. (issue #638)\n"
            f"  ---- gate output ----\n{out}"
        )
        return failures

    if EXECUTED_LINE not in out:
        failures.append(
            "  The gate passed under CI=true without printing the Gate 1a\n"
            f"  execution line ({EXECUTED_LINE!r}). The CI log must carry the\n"
            "  assertion the gate made, not just a bare PASS. (issue #638)\n"
            f"  ---- gate output ----\n{out}"
        )
    if SKIP_LINE in out:
        failures.append(
            "  The gate printed a Gate 1a skip line on a passing CI run.\n"
            "  (issue #638)\n"
            f"  ---- gate output ----\n{out}"
        )
    return failures


# ── Check 5: ci_is_strict() reads $CI, both variants ────────────────────────


def check_ci_is_strict_variants() -> list:
    failures = []
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    try:
        import test_no_active_bundle as gate_module
    except Exception as e:  # pragma: no cover - import of a sibling test module
        return [
            f"  Could not import tests/test_no_active_bundle.py ({e!r}) to\n"
            "  exercise ci_is_strict(). (issue #638)"
        ]

    fn = getattr(gate_module, "ci_is_strict", None)
    if fn is None:
        return [
            "  tests/test_no_active_bundle.py has no ci_is_strict() function.\n"
            "  The CI-strict rule must be a named, testable unit. (issue #638)"
        ]

    truthy = ["true", "TRUE", "1", "yes", "on"]
    falsey = ["", "0", "false", "FALSE", "no", "off"]
    saved = os.environ.get("CI")
    try:
        for value in truthy:
            os.environ["CI"] = value
            if not fn():
                failures.append(
                    f"  ci_is_strict() returned False for CI={value!r}; a skip\n"
                    "  would not be fatal on that runner. (issue #638)"
                )
        for value in falsey:
            os.environ["CI"] = value
            if fn():
                failures.append(
                    f"  ci_is_strict() returned True for CI={value!r}. That is\n"
                    "  not a CI signal, and treating it as one turns a\n"
                    "  contributor's documented skip into a failure.\n"
                    "  (issue #638)"
                )
        os.environ.pop("CI", None)
        if fn():
            failures.append(
                "  ci_is_strict() returned True with $CI unset. (issue #638)"
            )
    finally:
        os.environ.pop("CI", None)
        if saved is not None:
            os.environ["CI"] = saved

    return failures


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    checks = [
        (
            "1",
            "The workflow job running the gate installs backend/requirements.txt first",
            check_workflow_installs_gate_dependencies,
        ),
        (
            "2",
            "A Gate 1a skip FAILS when $CI is set (red half)",
            check_ci_skip_is_fatal,
        ),
        (
            "3",
            "Off-CI the documented skip is still tolerated",
            check_local_skip_still_tolerated,
        ),
        (
            "4",
            "With deps present the Gate 1a assertion executes and is logged",
            check_gate_actually_executes_under_ci,
        ),
        (
            "5",
            "ci_is_strict() reads $CI (truthy and falsey variants seeded)",
            check_ci_is_strict_variants,
        ),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} … {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("PASS: the no-active-bundle gate executes in CI and says so.")
        return 0
    print("FAIL: see issue #638.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
