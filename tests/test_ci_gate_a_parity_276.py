#!/usr/bin/env python3
"""
Structural + behavioral gate for issue #276: CI GATE A must not diverge from
scripts/check.sh's collect-all failure reporting.

Problem (issue #276): .github/workflows/ci-pipeline.yml's GATE A step used to
run `python3 "$t" || exit 1` in a loop, so it reported only the FIRST failing
test file. scripts/check.sh's loop collects every failure and reports the
full list. The fix extracts the collect-all loop into a single shared script
(scripts/collect_test_failures.sh) that both scripts/check.sh and GATE A
invoke, so the two authoritative gates cannot diverge again.

Checks:
  1. scripts/collect_test_failures.sh exists and is executable — the one
     shared loop implementation.
  2. scripts/check.sh invokes scripts/collect_test_failures.sh (does not
     re-implement its own copy of the loop).
  3. .github/workflows/ci-pipeline.yml's GATE A step invokes
     scripts/collect_test_failures.sh (does not re-implement its own copy of
     the loop, and no longer exits on the first failure).
  4. Behavioral: given a synthetic tree with TWO failing test files,
     scripts/collect_test_failures.sh reports BOTH failing files in one run
     (not just the first) and exits non-zero.

Run with: python3 tests/test_ci_gate_a_parity_276.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_LOOP = REPO_ROOT / "scripts" / "collect_test_failures.sh"
CHECK_SH = REPO_ROOT / "scripts" / "check.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-pipeline.yml"


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


def check_shared_loop_exists() -> list[str]:
    """Check 1: the shared collect-all loop script exists and is executable."""
    print("Check 1: scripts/collect_test_failures.sh exists and is executable …")
    failures = []
    failures += _assert(
        SHARED_LOOP.exists(),
        "scripts/collect_test_failures.sh exists",
        "Per issue #276: GATE A and scripts/check.sh must share one loop "
        "implementation instead of maintaining two copies that can diverge.",
    )
    if SHARED_LOOP.exists():
        mode = SHARED_LOOP.stat().st_mode
        failures += _assert(
            bool(mode & stat.S_IXUSR),
            "scripts/collect_test_failures.sh is executable",
        )
    return failures


def check_check_sh_delegates() -> list[str]:
    """Check 2: scripts/check.sh invokes the shared loop rather than
    re-implementing its own for/collect loop."""
    print("Check 2: scripts/check.sh invokes scripts/collect_test_failures.sh …")
    failures = []
    text = _read(CHECK_SH)
    failures += _assert(
        "collect_test_failures.sh" in text,
        "scripts/check.sh references scripts/collect_test_failures.sh",
    )
    return failures


def check_gate_a_delegates() -> list[str]:
    """Check 3: CI GATE A's step invokes the shared loop, not a private
    `for t in ...; do python3 "$t" || exit 1; done` copy that fails fast on
    the first failing file."""
    print("Check 3: .github/workflows/ci-pipeline.yml GATE A invokes the shared loop …")
    failures = []
    text = _read(CI_WORKFLOW)

    # Isolate the "Run full test suite (GATE A)" step: everything from its
    # `name:` line up to the next `- name:` step (or end of file). Handles
    # both a `run: |` block scalar and a single-line `run: <cmd>`.
    match = re.search(
        r"Run full test suite \(GATE A\)\s*\n(?P<body>(?:.*\n)*?)(?=\n\s*- name:|\Z)",
        text,
    )
    failures += _assert(
        match is not None,
        "GATE A step ('Run full test suite (GATE A)') found in ci-pipeline.yml",
    )
    if match is None:
        return failures

    body = match.group("body")
    failures += _assert(
        "collect_test_failures.sh" in body,
        "GATE A step body invokes scripts/collect_test_failures.sh",
        "Per issue #276: GATE A must call the same shared loop as "
        "scripts/check.sh so the two gates cannot diverge.",
    )

    # Only the executable `run:` command line(s) matter for the fail-fast
    # check — the step is allowed to have explanatory `#` comments that
    # mention the old `|| exit 1` behavior for context (as this fix's own
    # comment does).
    command_lines = "\n".join(
        line for line in body.splitlines() if line.strip() and not line.strip().startswith("#")
    )
    failures += _assert(
        "|| exit 1" not in command_lines,
        "GATE A step's executable command no longer contains the fail-fast "
        "`|| exit 1` per-file short-circuit",
        "Per issue #276: the old inline loop reported only the first "
        "failing file; the shared loop collects and reports all failures.",
    )
    return failures


def check_multi_failure_reporting() -> list[str]:
    """Check 4 (behavioral): run scripts/collect_test_failures.sh against a
    synthetic tree with two failing test files and assert BOTH are reported
    in a single run, and the exit code is non-zero."""
    print("Check 4: shared loop reports every failing file in one run (behavioral) …")
    failures = []

    if not SHARED_LOOP.exists():
        failures.append(
            "  scripts/collect_test_failures.sh does not exist yet — "
            "skipping behavioral check."
        )
        return failures

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tests_dir = root / "tests"
        tests_dir.mkdir()

        (tests_dir / "test_alpha_276.py").write_text(
            "import sys\n"
            "def main():\n"
            "    print('ALPHA_MARKER_FAILURE')\n"
            "    return 1\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        )
        (tests_dir / "test_beta_276.py").write_text(
            "import sys\n"
            "def main():\n"
            "    print('BETA_MARKER_FAILURE')\n"
            "    return 1\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        )

        proc = subprocess.run(
            ["bash", str(SHARED_LOOP), str(root)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = proc.stdout + proc.stderr

        failures += _assert(
            proc.returncode != 0,
            "shared loop exits non-zero when any test file fails",
        )
        failures += _assert(
            "test_alpha_276.py" in out,
            "shared loop report mentions the first failing file (test_alpha_276.py)",
        )
        failures += _assert(
            "test_beta_276.py" in out,
            "shared loop report mentions the SECOND failing file "
            "(test_beta_276.py) — this is the parity fix: the old GATE A "
            "loop would have exited after the first failure and never run "
            "test_beta_276.py at all.",
            detail=f"Full output:\n{out}",
        )

    return failures


def main() -> int:
    checks = [
        ("1", "scripts/collect_test_failures.sh exists (shared loop)", check_shared_loop_exists),
        ("2", "scripts/check.sh delegates to the shared loop", check_check_sh_delegates),
        ("3", "CI GATE A delegates to the shared loop (no fail-fast)", check_gate_a_delegates),
        ("4", "Multi-failure tree reports every failing file in one run", check_multi_failure_reporting),
    ]

    overall_pass = True
    for code, name, fn in checks:
        print(f"\n--- Check {code}: {name} ---")
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} … {status}")
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All CI GATE A parity checks passed.")
        return 0
    else:
        print("One or more CI GATE A parity checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
