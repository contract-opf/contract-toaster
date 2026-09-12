#!/usr/bin/env python3
"""
`scripts/check.sh --only <glob>` runs the matching test files and ONLY those
(issue #68).

WHY THIS EXISTS
---------------
The gate is 300+ self-contained scripts run one process at a time, so a full
run costs minutes and there was no way to ask it for one file. The cost of
that is not patience: a developer who cannot run the gate narrowly runs it
rarely, or runs the single file by hand with `python3 tests/test_x.py` and
thereby skips the retry pass, the FLAKY rules and the exit-code contract that
make the gate's answer mean something.

`--only` closes that gap without forking the loop: `scripts/check.sh` parses
the flag and exports `CHECK_ONLY`, and the selection itself happens inside
`scripts/collect_test_failures.sh` — the ONE discovery loop shared with CI
GATE A (issue #276). This file is what stops those two halves drifting apart,
and what pins the property that matters most about a subset runner:

  A RUN THAT EXECUTED NOTHING IS NOT GREEN. A mistyped glob that printed
  `CHECK: ALL GREEN` after running zero files would be a worse failure than
  anything this gate reports, because it reads exactly like success.

NOTE FOR EDITORS: never write the lowercase CDK synth invocation anywhere in
this file — always "CDK synth", capitalised, the way the sibling
tests/test_gate_flaky_honesty.py does. SKIP_INFRA=1 decides which files to
skip by grepping test sources for that phrase with a CASE-SENSITIVE regex
(scripts/collect_test_failures.sh), so one lowercase mention here would exile
this pure-shell test from the fast gate — the gate agents actually run. The
one place the lowercase form is needed below builds it with `.lower()`.

HOW IT IS DRIVEN
----------------
Against a synthetic repo, not this one: a temporary root holding a copy of
BOTH shipped scripts and a `tests/` tree of stub files that record the fact
they ran. `scripts/check.sh` resolves its own root with
`cd "$(dirname "$0")/.."`, so a copy in `<tmp>/scripts/` gates `<tmp>` — the
real script, end to end, with a test corpus whose expected result is known
exactly. Running `--only` against the real tree could only assert "some files
ran", which is the assertion that cannot tell selection from luck.

The stub corpus covers every shape the loop discovers — `tests/test_*.py`,
`tests/*/test_*.py` and `tests/lint-*.py` — because a selector tested against
one glob is a selector half-tested, and both exit-4 branches, because the one
that is never seeded is the one that rots.

CHECKS (exit 0 = all pass, 1 = one or more fail)
  1. A path glob runs exactly the files it names, and nothing else.
  2. A bare-filename glob selects the same file as its full path.
  3. `--only=<glob>` and `--only <glob>` behave identically; no flag at all
     still runs the whole corpus (the selector NARROWS, it is not the loop) —
     including when `CHECK_ONLY` is EXPORTED into the environment, because
     scripts/land.sh measures a landing by a flagless `bash scripts/check.sh`
     and an inherited variable must not shrink that to one file.
  4. A glob that matches nothing exits 4, runs nothing, and never prints
     `CHECK: ALL GREEN`. Same for an unusable argument.
  5. A glob whose every match is excluded by SKIP_INFRA also exits 4 — with
     its own marker, because the fix is different.
  6. Selection does not launder failures: a selected failing file still fails
     the gate, and is still re-run once by the retry pass.
  7. Both scripts document the flag, the env var and exit code 4.

Run with: python3 tests/test_check_only_flag_68.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SH = REPO_ROOT / "scripts" / "check.sh"
SHARED_LOOP = REPO_ROOT / "scripts" / "collect_test_failures.sh"

# The documented exit codes this file asserts against.
RC_GREEN = 0
RC_PERSISTENT_FAILURE = 1
RC_NOTHING_RAN = 4

# The lowercase phrase SKIP_INFRA greps test sources for. Built with .lower()
# so this file never contains it — see NOTE FOR EDITORS in the docstring.
INFRA_SKIP_PHRASE = "CDK synth".lower()


def stub_source(name: str, ledger: Path, exit_code: int = 0, extra: str = "") -> str:
    """A stub test file that records the fact it ran, then exits `exit_code`.

    The ledger is append-only and one line per RUN, not per file, so the retry
    pass — which re-runs a failure once, alone — is visible as a second line
    rather than hidden behind a set.
    """
    return (
        f"{extra}"
        "import sys\n"
        f"with open({str(ledger)!r}, 'a') as fh:\n"
        f"    fh.write({name!r} + '\\n')\n"
        f"sys.exit({exit_code})\n"
    )


# The corpus every check below is selected out of. Keys are paths relative to
# the synthetic root, and they exercise all three of the loop's globs.
def corpus(ledger: Path) -> dict[str, str]:
    return {
        "tests/test_alpha_68.py": stub_source("test_alpha_68.py", ledger),
        "tests/test_beta_68.py": stub_source("test_beta_68.py", ledger),
        "tests/lint-gamma-68.py": stub_source("lint-gamma-68.py", ledger),
        "tests/sub/test_delta_68.py": stub_source("test_delta_68.py", ledger),
        # Looks like an infra test to SKIP_INFRA's content grep, exactly the
        # way the real infra files do — they shell out to the synth command
        # and the loop recognises them by that text.
        "tests/test_infra_epsilon_68.py": stub_source(
            "test_infra_epsilon_68.py",
            ledger,
            extra=f"# shells out to {INFRA_SKIP_PHRASE} like the real infra tests\n",
        ),
        # Fails every run, first and retry.
        "tests/test_red_68.py": stub_source("test_red_68.py", ledger, exit_code=1),
    }


ALL_STUBS = {
    "test_alpha_68.py",
    "test_beta_68.py",
    "lint-gamma-68.py",
    "test_delta_68.py",
    "test_infra_epsilon_68.py",
    "test_red_68.py",
}


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


class SyntheticRepo:
    """A temporary root gated by a COPY of the two shipped scripts.

    Copied, never imported or re-implemented: if check.sh stops exporting
    CHECK_ONLY, or the loop stops reading it, these runs change behaviour on
    the next invocation with no edit here.
    """

    def __init__(self, tmp: str) -> None:
        self.root = Path(tmp)
        (self.root / "scripts").mkdir()
        shutil.copy2(CHECK_SH, self.root / "scripts" / "check.sh")
        shutil.copy2(SHARED_LOOP, self.root / "scripts" / "collect_test_failures.sh")
        self.ledger = self.root / "ran.ledger"
        for rel, source in corpus(self.ledger).items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)

    def run(self, *args: str, skip_infra: bool = True, inherited_only: str = ""):
        """Run the copied check.sh with `args`, returning (proc, ran-list).

        `inherited_only` EXPORTS CHECK_ONLY into the child's environment — the
        hostile case, not the normal one. Every other run scrubs the variable
        so an operator's exported value cannot silently narrow these runs.
        """
        if self.ledger.exists():
            self.ledger.unlink()
        env = dict(os.environ)
        env["PYTHON"] = sys.executable
        env["CHECK_NO_LOCK"] = "1"
        env["CHECK_LOCK_DIR"] = str(self.root / "unused.lock")
        env.pop("ALLOW_FLAKY", None)
        env.pop("CHECK_ONLY", None)
        if inherited_only:
            env["CHECK_ONLY"] = inherited_only
        if skip_infra:
            env["SKIP_INFRA"] = "1"
        else:
            env.pop("SKIP_INFRA", None)
        proc = subprocess.run(
            ["bash", str(self.root / "scripts" / "check.sh"), *args],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            cwd=str(self.root),
        )
        ran = (
            self.ledger.read_text().split() if self.ledger.exists() else []
        )
        return proc, ran


def check_path_glob_selects_exactly() -> list[str]:
    print("Check 1: a path glob runs the files it names and nothing else …")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        repo = SyntheticRepo(tmp)

        proc, ran = repo.run("--only", "tests/test_alpha_68.py")
        out = proc.stdout + proc.stderr
        failures += _assert(
            proc.returncode == RC_GREEN,
            "an --only run over a passing file exits 0",
            detail=f"returncode={proc.returncode}\n{out}",
        )
        failures += _assert(
            ran == ["test_alpha_68.py"],
            "exactly the named file ran",
            detail=f"ran={ran}\n{out}",
        )
        failures += _assert(
            "CHECK: ALL GREEN" in out,
            "the subset run still reports the gate's green marker",
            detail=out,
        )
        failures += _assert(
            "SUBSET" in out,
            "the run says out loud that it was a subset, not a landing signal",
            detail=out,
        )

        # A wildcard selects the two it matches — and, just as load-bearing,
        # leaves the sibling that does NOT match unrun.
        proc, ran = repo.run("--only", "tests/test_*a_68.py")
        failures += _assert(
            sorted(ran) == ["test_alpha_68.py", "test_beta_68.py"],
            "a wildcard path glob selects every match and only the matches",
            detail=f"ran={ran}\n{proc.stdout + proc.stderr}",
        )

        # The nested glob (tests/*/test_*.py) is a separate discovery pattern,
        # so it gets its own case rather than being assumed to follow.
        proc, ran = repo.run("--only", "tests/sub/test_delta_68.py")
        failures += _assert(
            ran == ["test_delta_68.py"],
            "a file discovered by the nested glob is selectable by path",
            detail=f"ran={ran}\n{proc.stdout + proc.stderr}",
        )

        # …and so is the lint-*.py pattern.
        proc, ran = repo.run("--only", "tests/lint-*.py")
        failures += _assert(
            ran == ["lint-gamma-68.py"],
            "a file discovered by the lint glob is selectable by path",
            detail=f"ran={ran}\n{proc.stdout + proc.stderr}",
        )
    return failures


def check_bare_filename_glob() -> list[str]:
    print("Check 2: a bare filename selects the same file as its full path …")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        repo = SyntheticRepo(tmp)

        proc, ran = repo.run("--only", "test_delta_68.py")
        failures += _assert(
            ran == ["test_delta_68.py"],
            "a bare filename reaches a file nested under tests/sub/",
            detail=f"ran={ran}\n{proc.stdout + proc.stderr}",
        )

        proc, ran = repo.run("--only", "lint-gamma-68.py")
        failures += _assert(
            ran == ["lint-gamma-68.py"],
            "a bare filename reaches a lint-*.py file",
            detail=f"ran={ran}\n{proc.stdout + proc.stderr}",
        )
    return failures


def check_flag_forms_and_no_flag() -> list[str]:
    print("Check 3: --only=<glob> == --only <glob>, and no flag runs it all …")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        repo = SyntheticRepo(tmp)

        _, spaced = repo.run("--only", "tests/test_alpha_68.py")
        _, equals = repo.run("--only=tests/test_alpha_68.py")
        failures += _assert(
            spaced == equals == ["test_alpha_68.py"],
            "both spellings of the flag select the same single file",
            detail=f"spaced={spaced} equals={equals}",
        )

        # The control. Without the flag the corpus runs in full (minus the
        # SKIP_INFRA exclusion), which is what makes every narrowing above a
        # statement about the SELECTOR rather than about a loop that happens
        # to run one file.
        proc, ran = repo.run()
        expected = ALL_STUBS - {"test_infra_epsilon_68.py"}
        control_rc = proc.returncode
        failures += _assert(
            set(ran) == expected,
            "with no --only, every discovered file runs",
            detail=f"ran={sorted(set(ran))} expected={sorted(expected)}\n"
            f"{proc.stdout + proc.stderr}",
        )
        failures += _assert(
            "SUBSET" not in (proc.stdout + proc.stderr),
            "a full run does not claim to be a subset",
            detail=proc.stdout + proc.stderr,
        )

        # THE FLAG IS THE ONLY WAY IN. An inherited CHECK_ONLY must not narrow
        # a flagless run: scripts/land.sh invokes `bash scripts/check.sh` with
        # no arguments and gates on its exit code, so a subset that still
        # printed ALL GREEN would measure a landing against one file. land.sh
        # neutralises SKIP_INFRA at that boundary but not this variable, so
        # check.sh clears it itself before parsing arguments.
        proc, ran = repo.run(inherited_only="tests/test_alpha_68.py")
        out = proc.stdout + proc.stderr
        failures += _assert(
            set(ran) == expected,
            "an exported CHECK_ONLY does not narrow a flagless run",
            detail=f"ran={sorted(set(ran))} expected={sorted(expected)}\n{out}",
        )
        failures += _assert(
            proc.returncode == control_rc and "CHECK_ONLY=" not in out,
            "that run is indistinguishable from the flagless control",
            detail=f"rc={proc.returncode} control_rc={control_rc}\n{out}",
        )

        # And the flag still wins over the environment when it IS passed, so
        # clearing the variable did not break the selector itself.
        _, ran_flagged = repo.run(
            "--only", "tests/test_alpha_68.py", inherited_only="tests/lint-*.py"
        )
        failures += _assert(
            ran_flagged == ["test_alpha_68.py"],
            "--only selects its own glob, not the inherited one",
            detail=f"ran={ran_flagged}",
        )
    return failures


def check_nothing_ran_is_not_green() -> list[str]:
    print("Check 4: a glob that matches nothing is exit 4, never green …")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        repo = SyntheticRepo(tmp)

        proc, ran = repo.run("--only", "tests/test_typo_*.py")
        out = proc.stdout + proc.stderr
        failures += _assert(
            proc.returncode == RC_NOTHING_RAN,
            f"a glob matching nothing exits {RC_NOTHING_RAN}",
            detail=f"returncode={proc.returncode}\n{out}",
        )
        failures += _assert(ran == [], "no file ran", detail=f"ran={ran}\n{out}")
        failures += _assert(
            "CHECK: ALL GREEN" not in out,
            "a run that executed nothing does NOT print the green marker",
            detail=out,
        )
        failures += _assert(
            "ONLY-MATCHED-NOTHING" in out,
            "the operator gets a marker naming what went wrong",
            detail=out,
        )

        # An unusable argument is the same class of answer: nothing ran, and
        # it must not be mistaken for a gate result.
        for args in (("--only",), ("--only=",), ("--wat",)):
            proc, ran = repo.run(*args)
            out = proc.stdout + proc.stderr
            failures += _assert(
                proc.returncode == RC_NOTHING_RAN and ran == [],
                f"{args!r} exits {RC_NOTHING_RAN} and runs nothing",
                detail=f"returncode={proc.returncode} ran={ran}\n{out}",
            )
            failures += _assert(
                "CHECK: ALL GREEN" not in out,
                f"{args!r} does not print the green marker",
                detail=out,
            )
    return failures


def check_all_matches_skipped_is_not_green() -> list[str]:
    print("Check 5: a glob whose every match SKIP_INFRA excludes is exit 4 …")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        repo = SyntheticRepo(tmp)

        proc, ran = repo.run("--only", "tests/test_infra_epsilon_68.py", skip_infra=True)
        out = proc.stdout + proc.stderr
        failures += _assert(
            proc.returncode == RC_NOTHING_RAN,
            f"selecting only a SKIP_INFRA-excluded file exits {RC_NOTHING_RAN}",
            detail=f"returncode={proc.returncode}\n{out}",
        )
        failures += _assert(ran == [], "no file ran", detail=f"ran={ran}\n{out}")
        failures += _assert(
            "ONLY-ALL-SKIPPED" in out and "CHECK: ALL GREEN" not in out,
            "its own marker, and no green marker — the fix is dropping "
            "SKIP_INFRA, not correcting the glob",
            detail=out,
        )

        # The same selection with SKIP_INFRA unset runs it: proof the previous
        # result is SKIP_INFRA's exclusion and not an unselectable file.
        proc, ran = repo.run(
            "--only", "tests/test_infra_epsilon_68.py", skip_infra=False
        )
        failures += _assert(
            proc.returncode == RC_GREEN and ran == ["test_infra_epsilon_68.py"],
            "the same file runs, and is green, with SKIP_INFRA unset",
            detail=f"returncode={proc.returncode} ran={ran}\n"
            f"{proc.stdout + proc.stderr}",
        )
    return failures


def check_selection_does_not_launder_failures() -> list[str]:
    print("Check 6: a selected failing file still fails, and is still retried …")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        repo = SyntheticRepo(tmp)

        proc, ran = repo.run("--only", "tests/test_red_68.py")
        out = proc.stdout + proc.stderr
        failures += _assert(
            proc.returncode == RC_PERSISTENT_FAILURE,
            f"a failing selected file exits {RC_PERSISTENT_FAILURE}",
            detail=f"returncode={proc.returncode}\n{out}",
        )
        failures += _assert(
            "CHECK: ALL GREEN" not in out and "CHECK: FAILURES" in out,
            "the failure is reported, and no green marker is printed",
            detail=out,
        )
        failures += _assert(
            ran == ["test_red_68.py", "test_red_68.py"],
            "the retry pass still re-runs the selected failure exactly once",
            detail=f"ran={ran}\n{out}",
        )
    return failures


def check_contract_is_documented() -> list[str]:
    print("Check 7: both scripts document the flag, the env var and exit 4 …")
    failures: list[str] = []
    check_text = CHECK_SH.read_text(encoding="utf-8")
    loop_text = SHARED_LOOP.read_text(encoding="utf-8")

    failures += _assert(
        "--only" in check_text.split("set -u")[0],
        "scripts/check.sh's header documents --only",
        detail="the flag is undocumented in the USAGE block",
    )
    failures += _assert(
        "   4 " in check_text or "#   4  " in check_text,
        "scripts/check.sh's header documents exit code 4",
    )
    failures += _assert(
        "CHECK_ONLY" in loop_text.split("set -u")[0],
        "scripts/collect_test_failures.sh's header documents CHECK_ONLY",
    )
    failures += _assert(
        "CHECK_ONLY" in check_text,
        "scripts/check.sh actually sets CHECK_ONLY rather than filtering itself",
    )
    return failures


def main() -> int:
    checks = [
        ("1", "Path globs select exactly", check_path_glob_selects_exactly),
        ("2", "Bare filenames select too", check_bare_filename_glob),
        ("3", "Flag forms, and the no-flag control", check_flag_forms_and_no_flag),
        ("4", "Nothing ran is not green", check_nothing_ran_is_not_green),
        ("5", "All matches skipped is not green", check_all_matches_skipped_is_not_green),
        ("6", "Selection launders nothing", check_selection_does_not_launder_failures),
        ("7", "Contract is documented", check_contract_is_documented),
    ]

    overall_pass = True
    for code, name, fn in checks:
        print(f"\n--- Check {code}: {name} ---")
        found = fn()
        print(f"Check {code}: {name} … {'PASS' if not found else 'FAIL'}")
        if found:
            overall_pass = False

    print("\n" + "=" * 60)
    print("ALL GREEN" if overall_pass else "FAILURES ABOVE")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
