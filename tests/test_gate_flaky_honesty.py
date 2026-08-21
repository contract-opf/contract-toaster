#!/usr/bin/env python3
"""
Gate-honesty invariants for scripts/collect_test_failures.sh and
scripts/check.sh.

PROBLEM THIS LOCKS DOWN
-----------------------
The shared loop re-runs each failing test file once, alone, and a file that
passes on that isolated re-run used to be reported as
`FLAKY (passed on isolated re-run, not failing the gate)` while the run still
printed `CHECK: ALL GREEN` and exited 0.

That laundered real failures into a green gate. Observed twice on 2026-08-20:
two concurrent `scripts/check.sh` runs in different git worktrees corrupted
each other's CDK synth step (the `~/.npm/_npx` cache is shared across every
worktree, and worktrees have no local infra/node_modules so `npx` falls back
to it), the loser's infra tests failed, passed when re-run alone after the
other run had finished, and the gate reported ALL GREEN / exit 0. The same
path would equally launder a GENUINE intermittent infra regression, and this
gate is the repo's only landing signal.

NOTE FOR EDITORS: always spell it "CDK synth", never in lowercase, anywhere in
this file. SKIP_INFRA=1 decides which files to skip by grepping test sources
for the lowercase CDK synth invocation (see the case-sensitive regex in
scripts/collect_test_failures.sh), so a lowercase mention here would exile
this pure-shell, no-CDK test from the fast gate — which is the gate agents
actually run.

The fix: a file in the FLAKY bucket fails the gate unless a human explicitly
opts in with ALLOW_FLAKY=1. A human decides, not the script.

Checks:
  1. A flaky file (fails first run, passes the isolated re-run) makes the
     shared loop exit non-zero and NOT print `CHECK: ALL GREEN`.
  2. ALLOW_FLAKY=1 restores the old lenient behaviour: exit 0, ALL GREEN, and
     an explicit line saying the flaky files were allowed through.
  3. An all-passing tree is still green (no regression in the happy path).
  4. A persistent failure (fails both runs) still fails the gate and is still
     reported as a persistent failure, not as flaky.
  5. Concurrent runs cannot silently corrupt each other: scripts/check.sh
     takes a repo-wide lock for full (infra) runs, and refuses — loudly and
     non-zero — rather than running alongside another holder.
  6. Both scripts document the ALLOW_FLAKY contract and their exit codes.

Run with: python3 tests/test_gate_flaky_honesty.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_LOOP = REPO_ROOT / "scripts" / "collect_test_failures.sh"
CHECK_SH = REPO_ROOT / "scripts" / "check.sh"

# Exit-code contract of the shared loop (documented in its header).
RC_GREEN = 0
RC_PERSISTENT_FAILURE = 1
RC_FLAKY = 2
# Exit-code contract of check.sh's repo-wide gate lock.
RC_LOCK_BUSY = 3

# A test file that fails its first run and passes every run after it: it drops
# a sentinel next to itself on the first run and short-circuits on the sentinel
# thereafter. This is exactly the shape the retry pass classifies as FLAKY.
FLAKY_SOURCE = """\
import os
import sys

SENTINEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flaky_sentinel")
if os.path.exists(SENTINEL):
    print("FLAKY_SYNTH_SECOND_RUN_PASS")
    sys.exit(0)
open(SENTINEL, "w").close()
print("FLAKY_SYNTH_FIRST_RUN_FAILURE")
sys.exit(1)
"""

PASSING_SOURCE = """\
import sys
print("PASSING_SYNTH_OK")
sys.exit(0)
"""

PERSISTENT_SOURCE = """\
import sys
print("PERSISTENT_SYNTH_FAILURE")
sys.exit(1)
"""


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def _tree(tmp: str, files: dict[str, str]) -> Path:
    """Build a synthetic ROOT_DIR with a tests/ dir holding `files`."""
    root = Path(tmp)
    tests_dir = root / "tests"
    tests_dir.mkdir()
    for name, source in files.items():
        (tests_dir / name).write_text(source)
    return root


def _run_loop(root: Path, env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    env.pop("ALLOW_FLAKY", None)
    env.pop("SKIP_INFRA", None)
    env["PYTHON"] = sys.executable
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SHARED_LOOP), str(root)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def check_flaky_fails_the_gate() -> list[str]:
    """Check 1: a flaky file must turn the gate RED by default."""
    print("Check 1: a FLAKY file fails the gate by default …")
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp, {"test_flaky_synth.py": FLAKY_SOURCE})
        proc = _run_loop(root)
        out = proc.stdout + proc.stderr

        failures += _assert(
            proc.returncode != 0,
            "shared loop exits non-zero when a file lands in the FLAKY bucket",
            detail=f"returncode={proc.returncode}\nFull output:\n{out}",
        )
        failures += _assert(
            proc.returncode == RC_FLAKY,
            f"flaky-only run exits with the documented flaky code {RC_FLAKY}",
            detail=f"returncode={proc.returncode}",
        )
        failures += _assert(
            "CHECK: ALL GREEN" not in out,
            "a flaky run does NOT print 'CHECK: ALL GREEN'",
            detail=f"Full output:\n{out}",
        )
        failures += _assert(
            "test_flaky_synth.py" in out,
            "the flaky file is named in the report",
            detail=f"Full output:\n{out}",
        )
        failures += _assert(
            "ALLOW_FLAKY" in out,
            "the report tells the operator how to opt in (ALLOW_FLAKY)",
            detail=f"Full output:\n{out}",
        )
    return failures


def check_allow_flaky_opt_in() -> list[str]:
    """Check 2: ALLOW_FLAKY=1 is the explicit human opt-in."""
    print("Check 2: ALLOW_FLAKY=1 lets a flaky run stay green, loudly …")
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp, {"test_flaky_synth.py": FLAKY_SOURCE})
        proc = _run_loop(root, {"ALLOW_FLAKY": "1"})
        out = proc.stdout + proc.stderr

        failures += _assert(
            proc.returncode == RC_GREEN,
            "ALLOW_FLAKY=1 exits 0 on a flaky-only run",
            detail=f"returncode={proc.returncode}\nFull output:\n{out}",
        )
        failures += _assert(
            "CHECK: ALL GREEN" in out,
            "ALLOW_FLAKY=1 still prints 'CHECK: ALL GREEN'",
            detail=f"Full output:\n{out}",
        )
        failures += _assert(
            "test_flaky_synth.py" in out and "ALLOW_FLAKY" in out,
            "the allowed-through flaky file is still named, with the reason",
            detail=f"Full output:\n{out}",
        )
    return failures


def check_all_passing_still_green() -> list[str]:
    """Check 3: the happy path is unchanged."""
    print("Check 3: an all-passing tree is still green …")
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp, {"test_pass_synth.py": PASSING_SOURCE})
        proc = _run_loop(root)
        out = proc.stdout + proc.stderr
        failures += _assert(
            proc.returncode == RC_GREEN and "CHECK: ALL GREEN" in out,
            "all-passing tree exits 0 with 'CHECK: ALL GREEN'",
            detail=f"returncode={proc.returncode}\nFull output:\n{out}",
        )
    return failures


def check_persistent_failure_unchanged() -> list[str]:
    """Check 4: a genuine failure is still a genuine failure, and is not
    reclassified as flaky by any of this."""
    print("Check 4: a persistent failure still fails the gate as persistent …")
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp, {"test_persistent_synth.py": PERSISTENT_SOURCE})
        proc = _run_loop(root)
        out = proc.stdout + proc.stderr
        failures += _assert(
            proc.returncode == RC_PERSISTENT_FAILURE,
            f"persistent failure exits with the documented code {RC_PERSISTENT_FAILURE}",
            detail=f"returncode={proc.returncode}\nFull output:\n{out}",
        )
        failures += _assert(
            "CHECK: FAILURES:" in out and "test_persistent_synth.py" in out,
            "persistent failure is reported on the 'CHECK: FAILURES:' line",
            detail=f"Full output:\n{out}",
        )
        failures += _assert(
            "CHECK: ALL GREEN" not in out,
            "persistent failure does NOT print 'CHECK: ALL GREEN'",
            detail=f"Full output:\n{out}",
        )
    return failures


def check_check_sh_refuses_concurrent_runs() -> list[str]:
    """Check 5: scripts/check.sh serialises full (infra) runs behind a lock
    rather than letting two runs corrupt each other's shared `npx`/CDK state.

    Driven through CHECK_LOCK_DIR so the check never touches the real
    repo-wide lock, and CHECK_LOCK_WAIT=0 so it fails fast instead of waiting.

    NOTE ON WHAT THIS ASSERTS: it deliberately pins the exact `CHECK: LOCK
    BUSY` marker and asserts the run produced NO test-loop output at all.
    A looser assertion (non-zero exit + the substring "LOCK") passes for the
    wrong reason in a fresh worktree, where `python` is missing, every test
    file fails instantly, and "LOCK" matches the filename
    tests/test_document_cache_block.py. The point of the lock is that the run
    stops BEFORE the test loop, so that is what is asserted.
    """
    print("Check 5: scripts/check.sh refuses to run alongside another holder …")
    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        lock_dir = Path(tmp) / "held.lock"
        lock_dir.mkdir()
        # Claim the lock for THIS process, which is demonstrably alive, so the
        # stale-lock reaper cannot decide the lock is abandoned.
        (lock_dir / "pid").write_text(str(os.getpid()))
        (lock_dir / "owner").write_text("test_gate_flaky_honesty.py")

        env = dict(os.environ)
        env["CHECK_LOCK_DIR"] = str(lock_dir)
        env["CHECK_LOCK_WAIT"] = "0"
        env["PYTHON"] = sys.executable
        env.pop("SKIP_INFRA", None)
        env.pop("CHECK_NO_LOCK", None)

        proc = subprocess.run(
            ["bash", str(CHECK_SH)],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=str(REPO_ROOT),
        )
        out = proc.stdout + proc.stderr

        failures += _assert(
            proc.returncode == RC_LOCK_BUSY,
            f"check.sh exits with the documented lock code {RC_LOCK_BUSY} "
            "rather than running alongside another gate run",
            detail=f"returncode={proc.returncode}\nFull output:\n{out}",
        )
        failures += _assert(
            "CHECK: LOCK BUSY" in out,
            "the operator gets the exact 'CHECK: LOCK BUSY' marker",
            detail=f"Full output:\n{out}",
        )
        failures += _assert(
            "FAIL(" not in out and "CHECK: ALL GREEN" not in out,
            "a lock-blocked run stops BEFORE the test loop (no test output, "
            "no verdict line of any colour)",
            detail=f"Full output:\n{out}",
        )
        failures += _assert(
            lock_dir.exists() and (lock_dir / "pid").exists(),
            "a blocked run does NOT delete the lock it failed to acquire",
            detail="check.sh released a lock owned by another process",
        )
    return failures


def check_contract_is_documented() -> list[str]:
    """Check 6: the honesty contract is written down where an operator reads
    it, not just implemented."""
    print("Check 6: ALLOW_FLAKY and the exit codes are documented …")
    failures = []
    loop_text = SHARED_LOOP.read_text(encoding="utf-8")
    check_text = CHECK_SH.read_text(encoding="utf-8")

    failures += _assert(
        "ALLOW_FLAKY" in loop_text,
        "scripts/collect_test_failures.sh documents/implements ALLOW_FLAKY",
    )
    failures += _assert(
        "ALLOW_FLAKY" in check_text,
        "scripts/check.sh's header documents ALLOW_FLAKY",
    )
    header = loop_text.split("set -u", 1)[0]
    failures += _assert(
        re.search(r"^#\s*2\s+", header, re.M) is not None
        and "flak" in header.lower(),
        "scripts/collect_test_failures.sh's header spells out exit code 2 "
        "on its own line in the exit-code contract",
        detail="The header must document the 0/1/2 mapping explicitly.",
    )
    check_header = check_text.split("set -u", 1)[0]
    failures += _assert(
        re.search(r"^#\s*3\s+", check_header, re.M) is not None
        and "lock" in check_header.lower(),
        "scripts/check.sh's header spells out exit code 3 (lock busy)",
        detail="The header must document the 0/1/2/3 mapping explicitly.",
    )
    return failures


def main() -> int:
    checks = [
        ("1", "FLAKY fails the gate by default", check_flaky_fails_the_gate),
        ("2", "ALLOW_FLAKY=1 is the explicit opt-in", check_allow_flaky_opt_in),
        ("3", "All-passing tree still green", check_all_passing_still_green),
        ("4", "Persistent failure unchanged", check_persistent_failure_unchanged),
        ("5", "check.sh refuses concurrent full runs", check_check_sh_refuses_concurrent_runs),
        ("6", "Contract is documented", check_contract_is_documented),
    ]

    overall_pass = True
    for code, name, fn in checks:
        print(f"\n--- Check {code}: {name} ---")
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} … {status}")
        if failures:
            overall_pass = False

    print("\n" + "=" * 60)
    print("ALL GREEN" if overall_pass else "FAILURES ABOVE")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
