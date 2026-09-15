#!/usr/bin/env python3
"""
The parallel per-file runner, `scripts/check.sh --only`, and the pytest
convergence bootstrap (issue #74).

WHAT CHANGED AND WHY IT NEEDS PINNING
-------------------------------------
`scripts/collect_test_failures.sh` is the ONE discovery + collect-all-failures
loop shared by `scripts/check.sh` and CI GATE A (issue #276). It used to run
its ~300 files strictly one at a time, and a `SKIP_INFRA=1` gate cost ~17
minutes of wall clock on a 12-core machine at ~1 core of load. It now runs the
selected files through `xargs -P`, keeping the per-file process as the
isolation boundary — that boundary is load-bearing, not incidental, because
the tree has real cross-file `sys.modules` pollution.

Speed bought with a changed answer is not a bargain, and this loop's answer is
the repo's landing signal. So what this file pins is that the answer did NOT
change:

  (a) For an all-green tree, a persistent-failure tree and a flaky tree, the
      parallel run's `CHECK:` summary lines and exit code are byte-identical
      to the serial (`COLLECT_JOBS=1`) run's.
  (b) `FAIL(rc=N):` blocks come out in DISCOVERY order, not completion order —
      pinned with stubs whose sleeps make completion order the reverse of
      discovery order, so a report that printed as workers finished would fail.
  (c) `scripts/check.sh --only <glob>` still runs only the matching files.
  (d) `pytest.ini` declares `--import-mode=importlib` and `-p no:cacheprovider`,
      and `tests/conftest.py` really puts `backend/src`, `scripts/` and each
      `infra/lambda/*` on `sys.path` (asserted by importing it, not by reading
      it).

and two properties that (a) would otherwise pass vacuously without:

  (e) The parallel batch genuinely OVERLAPS. "Identical output" is trivially
      true of a `-P` that silently ran serially, so the speedup itself is
      asserted.
  (f) The synth-running infra files are NEVER run concurrently with each other.
      They shell out to the synth command with no `--output`, so they all write
      the one shared `infra/cdk.out`; two at once is exactly the corruption
      that produced two false greens on 2026-08-20 and that `check.sh`'s
      repo-wide lock exists to prevent. The runner partitions them out by the
      same content match `SKIP_INFRA` uses and runs them one at a time.

NOTE FOR EDITORS: never write the lowercase synth invocation anywhere in this
file — always "CDK synth", capitalised, the way tests/test_gate_flaky_honesty.py
and tests/test_check_only_flag_68.py do. `SKIP_INFRA=1` decides which files to
skip by grepping test sources for that phrase with a CASE-SENSITIVE regex
(scripts/collect_test_failures.sh), so one lowercase mention here would exile
this pure-shell, no-CDK test from the fast gate — the gate agents actually run.
The one place the lowercase form is needed below builds it with `.lower()`.

HOW IT IS DRIVEN
----------------
Against synthetic roots, not this repo: a temporary directory holding a COPY of
the two shipped scripts and a `tests/` tree of stub files, exactly the pattern
`tests/test_gate_flaky_honesty.py` and `tests/test_check_only_flag_68.py` use.
Copied and never re-implemented, so a change to either script changes these
runs on the next invocation with no edit here.

The stubs are the same SHAPE the real corpus is — a `tests/test_*.py` file that
is a self-contained `__main__` script and exits 0 or non-zero — because that
shape is the loop's entire input contract; `scripts/collect_test_failures.sh`
runs `"$PY" "$t"` and reads the status, and nothing else about a test file
reaches it. The "infra" stub is a stub whose text contains the lowercase synth
phrase, which is exactly how the real infra files are recognised (see
`tests/test_infra_app_stack.py`, which shells out to that command).

DUAL MODE (the pytest-convergence half of issue #74)
----------------------------------------------------
Every check below is a no-argument `test_*` function that raises on failure, so
this file passes under BOTH runners:

    .venv/bin/python tests/test_parallel_runner_74.py
    .venv/bin/python -m pytest tests/test_parallel_runner_74.py

That is the convergence direction B9 asks for: NEW tests are written this way,
existing ones convert only when touched. See tests/README.md.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SH = REPO_ROOT / "scripts" / "check.sh"
SHARED_LOOP = REPO_ROOT / "scripts" / "collect_test_failures.sh"
PYTEST_INI = REPO_ROOT / "pytest.ini"
CONFTEST = REPO_ROOT / "tests" / "conftest.py"

# The documented exit codes of the shared loop.
RC_GREEN = 0
RC_PERSISTENT_FAILURE = 1
RC_FLAKY = 2

# The lowercase phrase SKIP_INFRA (and the serial partition) greps test sources
# for. Built with .lower() so this file never contains it — see NOTE FOR
# EDITORS in the docstring.
INFRA_SKIP_PHRASE = "CDK synth".lower()


# ---------------------------------------------------------------------------
# Stub sources — the shapes the loop actually consumes
# ---------------------------------------------------------------------------

def passing_stub(sleep_s: float = 0.0) -> str:
    return (
        "import sys, time\n"
        f"time.sleep({sleep_s!r})\n"
        "print('STUB_OK')\n"
        "sys.exit(0)\n"
    )


def failing_stub(rc: int, sleep_s: float = 0.0) -> str:
    return (
        "import sys, time\n"
        f"time.sleep({sleep_s!r})\n"
        f"print('STUB_FAILED_rc={rc}')\n"
        f"sys.exit({rc})\n"
    )


def flaky_stub() -> str:
    """Fails its first run, passes every run after it — the shape the retry
    pass classifies as FLAKY. Same mechanism as tests/test_gate_flaky_honesty.py.
    """
    return (
        "import os\n"
        "import sys\n"
        "SENTINEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),\n"
        "                        '.flaky_sentinel_74')\n"
        "if os.path.exists(SENTINEL):\n"
        "    print('FLAKY_SECOND_RUN_PASS')\n"
        "    sys.exit(0)\n"
        "open(SENTINEL, 'w').close()\n"
        "print('FLAKY_FIRST_RUN_FAILURE')\n"
        "sys.exit(1)\n"
    )


def interval_stub(ledger: Path, name: str, hold_s: float, infra: bool) -> str:
    """A stub that records the wall-clock interval it occupied the CPU for.

    One `name start end` line per run, which is what turns "were these ever
    running at the same time?" into an assertion rather than a guess. `infra`
    plants the lowercase synth phrase in a comment, which is exactly how the
    runner recognises a synth-running infra file (and how `SKIP_INFRA` skips
    one) — see tests/test_infra_app_stack.py for the real thing.
    """
    marker = f"# shells out to {INFRA_SKIP_PHRASE} like the real infra tests\n" if infra else ""
    return (
        f"{marker}"
        "import sys, time\n"
        "start = time.time()\n"
        f"time.sleep({hold_s!r})\n"
        "end = time.time()\n"
        f"with open({str(ledger)!r}, 'a') as fh:\n"
        f"    fh.write('{name} %.6f %.6f\\n' % (start, end))\n"
        "sys.exit(0)\n"
    )


def stdin_reading_stub(infra: bool = True) -> str:
    """A stub that READS STDIN, which the real corpus does by proxy.

    Every one of the 26 infra files shells out with `subprocess.run([...],
    capture_output=True)` and no `stdin=`, so the child `npx`/`npm` inherits
    whatever stdin the runner handed down — and this script's own header
    documents the worktree-without-node_modules case where `npx` then PROMPTS
    on it. The rest of this file's corpus never touches stdin, so the doubles
    accepted a strictly narrower input space than the dependency they stand in
    for, in exactly the dimension #74 changed. That is why every `_equivalence`
    assertion passed while the serial pass was silently eating its own work
    list (`run_one_file` inherited the `while read ... <"$SERIAL_LIST"` fd).

    Infra-shaped by default: the synth marker is what routes a file to the
    SERIAL pass, which is the only path that ever diverged.
    """
    marker = f"# shells out to {INFRA_SKIP_PHRASE} like the real infra tests\n" if infra else ""
    return (
        f"{marker}"
        "import select\n"
        "import sys\n"
        "# Drains whatever stdin ALREADY holds, exactly as an inherited-fd\n"
        "# child does -- but bounded, never a bare sys.stdin.read(). When the\n"
        "# runner hands down its own work-list fd there is data waiting and we\n"
        "# consume it, which is the defect this stub exists to expose. When\n"
        "# stdin is /dev/null (the fix) the read returns empty immediately, and\n"
        "# when it is an open pipe nobody writes to, select times out instead\n"
        "# of hanging the gate. A test that can hang is worse than no test.\n"
        "drained = 0\n"
        "while True:\n"
        "    ready, _, _ = select.select([sys.stdin], [], [], 0.25)\n"
        "    if not ready:\n"
        "        break\n"
        "    chunk = sys.stdin.readline()\n"
        "    if not chunk:\n"
        "        break\n"
        "    drained += len(chunk)\n"
        "print('STUB_READ_STDIN bytes=%d' % drained)\n"
        "sys.exit(0)\n"
    )


def ledger_stub(ledger: Path, name: str, rc: int = 0) -> str:
    """Records the fact that it ran, then exits `rc`. One line per RUN."""
    return (
        "import sys\n"
        f"with open({str(ledger)!r}, 'a') as fh:\n"
        f"    fh.write({name!r} + '\\n')\n"
        f"sys.exit({rc})\n"
    )


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def build_tree(root: Path, files: dict[str, str]) -> Path:
    """Write `files` (paths relative to `root`) and return `root`."""
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


def clean_env(**extra: str) -> dict[str, str]:
    """The environment every synthetic run gets.

    Scrubbed of the four variables that narrow or soften the loop, so an
    operator who exported one — or `scripts/check.sh --only`, which EXPORTS
    CHECK_ONLY into this process — cannot change what these runs measure.
    """
    env = dict(os.environ)
    for name in ("ALLOW_FLAKY", "SKIP_INFRA", "CHECK_ONLY", "COLLECT_JOBS"):
        env.pop(name, None)
    env["PYTHON"] = sys.executable
    env.update(extra)
    return env


def run_loop(root: Path, **extra: str) -> subprocess.CompletedProcess:
    """Run the SHIPPED shared loop against a synthetic root."""
    return subprocess.run(  # noqa: S603
        ["bash", str(SHARED_LOOP), str(root)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=300,
        env=clean_env(**extra),
    )


def check_lines(proc: subprocess.CompletedProcess) -> list[str]:
    """Every `CHECK:` summary line, in order — the lines CI parses."""
    out = proc.stdout + proc.stderr
    return [line for line in out.splitlines() if line.startswith("CHECK:")]


def fail_order(proc: subprocess.CompletedProcess) -> list[str]:
    """The files named by `FAIL(rc=N):` blocks, in the order they were printed."""
    return re.findall(r"^FAIL\(rc=\d+\): (\S+)$", proc.stdout + proc.stderr, re.M)


def _assert(condition: bool, label: str, detail: str = "") -> None:
    if not condition:
        raise AssertionError(label + (f"\n         {detail}" if detail else ""))


def _equivalence(files: dict[str, str], label: str, expected_rc: int) -> None:
    """Run `files` twice — parallel and COLLECT_JOBS=1 — in two pristine trees,
    and require the two runs to agree on every `CHECK:` line and on the code.

    Two trees, not one, because the flaky stub leaves a sentinel behind: reusing
    a tree would make the second run green for the wrong reason.
    """
    with tempfile.TemporaryDirectory() as tmp_par, tempfile.TemporaryDirectory() as tmp_ser:
        par = run_loop(build_tree(Path(tmp_par), files), COLLECT_JOBS="8")
        ser = run_loop(build_tree(Path(tmp_ser), files), COLLECT_JOBS="1")

    _assert(
        par.returncode == ser.returncode,
        f"{label}: parallel and serial exit codes agree",
        f"parallel={par.returncode} serial={ser.returncode}\n"
        f"parallel output:\n{par.stdout}{par.stderr}\n"
        f"serial output:\n{ser.stdout}{ser.stderr}",
    )
    _assert(
        par.returncode == expected_rc,
        f"{label}: exits with the documented code {expected_rc}",
        f"returncode={par.returncode}\n{par.stdout}{par.stderr}",
    )
    _assert(
        check_lines(par) == check_lines(ser),
        f"{label}: the CHECK: summary lines are byte-identical",
        f"parallel={check_lines(par)!r}\nserial={check_lines(ser)!r}",
    )
    _assert(
        check_lines(par) != [],
        f"{label}: the run printed a CHECK: verdict at all",
        f"{par.stdout}{par.stderr}",
    )


# ---------------------------------------------------------------------------
# (a) parallel == serial, on all three verdicts
# ---------------------------------------------------------------------------

def test_green_tree_parallel_matches_serial() -> None:
    _equivalence(
        {
            "tests/test_alpha_74.py": passing_stub(),
            "tests/test_beta_74.py": passing_stub(),
            "tests/sub/test_gamma_74.py": passing_stub(),
            "tests/lint-delta-74.py": passing_stub(),
        },
        "all-green tree",
        RC_GREEN,
    )


def test_persistent_failure_tree_parallel_matches_serial() -> None:
    _equivalence(
        {
            "tests/test_alpha_74.py": passing_stub(),
            "tests/test_red_74.py": failing_stub(1),
            "tests/lint-delta-74.py": passing_stub(),
        },
        "persistent-failure tree",
        RC_PERSISTENT_FAILURE,
    )


def test_stdin_reading_infra_tree_parallel_matches_serial() -> None:
    """Regression for the serial pass eating its own work list (#74 review).

    THREE infra-shaped files, so the serial loop has entries left to lose after
    the first child drains stdin. Before `</dev/null` this produced
    `FAIL(rc=125)` for files that never ran and a FLAKY-UNRESOLVED verdict
    (exit 2) from the serial path while the parallel path stayed green --
    xargs already gives its children /dev/null, so only the serial loop
    diverged. The `rc=125` assertion below is the sharp one: the runner
    synthesises that code for a file with no recorded `.rc`, which is precisely
    the signature of a file the loop skipped rather than ran.
    """
    files = {
        "tests/test_infra1_probe_74.py": stdin_reading_stub(),
        "tests/test_infra2_probe_74.py": stdin_reading_stub(),
        "tests/test_infra3_probe_74.py": stdin_reading_stub(),
        "tests/test_alpha_74.py": passing_stub(),
    }
    _equivalence(files, "stdin-reading infra tree", RC_GREEN)

    # And, specifically, that every file recorded a real exit status. A tree
    # that agreed on being RED in both paths would satisfy _equivalence; it
    # would not satisfy this.
    with tempfile.TemporaryDirectory() as tmp:
        ser = run_loop(build_tree(Path(tmp), files), COLLECT_JOBS="1")
    combined = f"{ser.stdout}{ser.stderr}"
    _assert(
        "125" not in combined,
        "stdin-reading infra tree: no file was skipped (no synthesised rc=125)",
        combined,
    )
    for name in ("test_infra1_probe_74", "test_infra2_probe_74", "test_infra3_probe_74"):
        _assert(
            f"FAIL(rc=125): tests/{name}.py" not in combined,
            f"stdin-reading infra tree: {name} actually ran",
            combined,
        )


def test_flaky_tree_parallel_matches_serial() -> None:
    _equivalence(
        {
            "tests/test_alpha_74.py": passing_stub(),
            "tests/test_flaky_74.py": flaky_stub(),
        },
        "flaky tree",
        RC_FLAKY,
    )


# ---------------------------------------------------------------------------
# (b) discovery order, not completion order
# ---------------------------------------------------------------------------

def test_failure_blocks_are_reported_in_discovery_order() -> None:
    """Five failing files whose sleeps make completion order the EXACT REVERSE
    of discovery order. A report that printed as workers finished would emit the
    reversed list and fail here.
    """
    files = {
        "tests/test_a_74.py": failing_stub(1, sleep_s=1.0),
        "tests/test_b_74.py": failing_stub(2, sleep_s=0.8),
        "tests/test_c_74.py": failing_stub(3, sleep_s=0.6),
        "tests/sub/test_d_74.py": failing_stub(4, sleep_s=0.4),
        "tests/lint-e-74.py": failing_stub(5, sleep_s=0.2),
    }
    # The loop's own glob order: tests/test_*.py, then tests/*/test_*.py, then
    # tests/lint-*.py — each expanded in sorted order by the shell.
    expected = [
        "tests/test_a_74.py",
        "tests/test_b_74.py",
        "tests/test_c_74.py",
        "tests/sub/test_d_74.py",
        "tests/lint-e-74.py",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        proc = run_loop(build_tree(Path(tmp), files), COLLECT_JOBS="8")

    _assert(
        fail_order(proc) == expected,
        "FAIL(rc=N): blocks come out in discovery order under -P",
        f"got={fail_order(proc)!r}\nexpected={expected!r}\n{proc.stdout}{proc.stderr}",
    )
    # The rc each block reports must be that file's OWN status, which is the
    # thing a shared per-basename status file would get wrong.
    pairs = re.findall(r"^FAIL\(rc=(\d+)\): (\S+)$", proc.stdout + proc.stderr, re.M)
    _assert(
        pairs == [("1", "tests/test_a_74.py"), ("2", "tests/test_b_74.py"),
                  ("3", "tests/test_c_74.py"), ("4", "tests/sub/test_d_74.py"),
                  ("5", "tests/lint-e-74.py")],
        "each FAIL block reports that file's own exit status",
        f"got={pairs!r}\n{proc.stdout}{proc.stderr}",
    )


def test_same_basename_in_two_directories_keeps_its_own_result() -> None:
    """`tests/test_dup_74.py` passes and `tests/sub/test_dup_74.py` fails.

    They share a basename, which is what the per-file log and status files used
    to be keyed on. Run concurrently, a basename key means one worker reports
    the other's result — so exactly one FAIL block, naming the sub/ file, is the
    assertion.
    """
    files = {
        "tests/test_dup_74.py": passing_stub(sleep_s=0.3),
        "tests/sub/test_dup_74.py": failing_stub(7, sleep_s=0.3),
    }
    with tempfile.TemporaryDirectory() as tmp:
        proc = run_loop(build_tree(Path(tmp), files), COLLECT_JOBS="8")

    _assert(
        fail_order(proc) == ["tests/sub/test_dup_74.py"],
        "colliding basenames do not swap results between workers",
        f"got={fail_order(proc)!r}\n{proc.stdout}{proc.stderr}",
    )
    _assert(
        proc.returncode == RC_PERSISTENT_FAILURE,
        f"the colliding-basename tree exits {RC_PERSISTENT_FAILURE}",
        f"returncode={proc.returncode}\n{proc.stdout}{proc.stderr}",
    )


# ---------------------------------------------------------------------------
# (e) the parallel batch really overlaps
# ---------------------------------------------------------------------------

def test_parallel_batch_actually_overlaps() -> None:
    """Without this, every equivalence check above passes for a `-P` that never
    parallelised anything.

    Eight sleep-bound stubs: serial is >= 8 * 0.5s, a genuinely concurrent run
    is a small multiple of 0.5s. Sleep-bound rather than CPU-bound so the
    assertion holds on a single-core runner too. The threshold is deliberately
    loose (half the serial time) so it measures "did these overlap at all",
    which is the invariant, not the machine's core count.
    """
    files = {f"tests/test_sleep{i}_74.py": passing_stub(sleep_s=0.5) for i in range(8)}
    with tempfile.TemporaryDirectory() as tmp_par, tempfile.TemporaryDirectory() as tmp_ser:
        par_root = build_tree(Path(tmp_par), files)
        ser_root = build_tree(Path(tmp_ser), files)

        t0 = time.monotonic()
        par = run_loop(par_root, COLLECT_JOBS="8")
        par_elapsed = time.monotonic() - t0

        t0 = time.monotonic()
        ser = run_loop(ser_root, COLLECT_JOBS="1")
        ser_elapsed = time.monotonic() - t0

    _assert(par.returncode == RC_GREEN and ser.returncode == RC_GREEN,
            "both timing runs were green",
            f"parallel={par.returncode} serial={ser.returncode}")
    _assert(
        par_elapsed < ser_elapsed / 2,
        "COLLECT_JOBS=8 overlaps the per-file processes (it is not a serial loop "
        "wearing an xargs hat)",
        f"parallel={par_elapsed:.2f}s serial={ser_elapsed:.2f}s",
    )


def test_collect_jobs_rejects_a_value_that_is_not_a_positive_integer() -> None:
    """`xargs -P 0` means UNLIMITED, so a junk or zero COLLECT_JOBS must fall
    back to 1 rather than forking one interpreter per discovered file. Both
    non-happy variants are seeded: an empty-ish junk string and an explicit 0.
    """
    files = {"tests/test_alpha_74.py": passing_stub()}
    for bad in ("nope", "0", "-4"):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_loop(build_tree(Path(tmp), files), COLLECT_JOBS=bad)
        out = proc.stdout + proc.stderr
        _assert(
            proc.returncode == RC_GREEN,
            f"COLLECT_JOBS={bad!r} still produces a correct verdict",
            f"returncode={proc.returncode}\n{out}",
        )
        _assert(
            "CHECK: ALL GREEN" in out,
            f"COLLECT_JOBS={bad!r} still runs the suite",
            out,
        )
        _assert(
            "COLLECT_JOBS" in out and "serially" in out,
            f"COLLECT_JOBS={bad!r} says out loud that it fell back to 1",
            out,
        )


# ---------------------------------------------------------------------------
# (f) the synth-running infra files are never concurrent
# ---------------------------------------------------------------------------

def _intervals(ledger: Path) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for line in ledger.read_text().splitlines():
        name, start, end = line.split()
        out[name] = (float(start), float(end))
    return out


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def test_synth_infra_files_are_never_run_concurrently() -> None:
    """The real infra files all write the one shared infra/cdk.out, so running
    two at once is the corruption check.sh's repo-wide lock exists to prevent.
    The runner must not manufacture it inside a single run.

    Asserted both ways in ONE run, so a harness that simply cannot observe
    overlap fails: the three plain stubs MUST overlap and the three infra-shaped
    stubs MUST NOT.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ledger = root / "intervals.ledger"
        files = {}
        for i in range(3):
            files[f"tests/test_plain{i}_74.py"] = interval_stub(
                ledger, f"plain{i}", hold_s=0.6, infra=False
            )
            files[f"tests/test_synthy{i}_74.py"] = interval_stub(
                ledger, f"synthy{i}", hold_s=0.6, infra=True
            )
        # SKIP_INFRA deliberately UNSET: this is the full-gate shape, the only
        # one in which the infra files run at all.
        proc = run_loop(build_tree(root, files), COLLECT_JOBS="8")
        _assert(
            proc.returncode == RC_GREEN,
            "the mixed plain/infra tree is green",
            f"returncode={proc.returncode}\n{proc.stdout}{proc.stderr}",
        )
        seen = _intervals(ledger)

    _assert(
        len(seen) == 6,
        "every stub in the mixed tree ran exactly once",
        f"ledger={seen!r}",
    )
    infra = [seen[f"synthy{i}"] for i in range(3)]
    plain = [seen[f"plain{i}"] for i in range(3)]

    infra_overlaps = [
        (i, j) for i in range(3) for j in range(i + 1, 3) if _overlaps(infra[i], infra[j])
    ]
    _assert(
        infra_overlaps == [],
        "no two synth-running infra files ever run at the same time",
        f"overlapping pairs={infra_overlaps!r} intervals={infra!r}",
    )
    plain_overlaps = [
        (i, j) for i in range(3) for j in range(i + 1, 3) if _overlaps(plain[i], plain[j])
    ]
    _assert(
        plain_overlaps != [],
        "the non-infra files DID overlap (otherwise the check above passes "
        "because nothing was parallel, not because infra was serialised)",
        f"intervals={plain!r}",
    )


def test_skip_infra_still_excludes_the_same_files() -> None:
    """The serial partition and SKIP_INFRA's exclusion must keep answering
    "which files are the infra ones?" the same way."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ledger = root / "ran.ledger"
        files = {
            "tests/test_plain_74.py": ledger_stub(ledger, "plain"),
            "tests/test_synthy_74.py": interval_stub(
                root / "unused.ledger", "synthy", hold_s=0.0, infra=True
            ),
        }
        proc = run_loop(build_tree(root, files), SKIP_INFRA="1", COLLECT_JOBS="8")
        ran = ledger.read_text().split() if ledger.exists() else []
        unused_exists = (root / "unused.ledger").exists()

    out = proc.stdout + proc.stderr
    _assert(proc.returncode == RC_GREEN, "SKIP_INFRA run is green", out)
    _assert(ran == ["plain"], "the non-infra file ran", f"ran={ran!r}\n{out}")
    _assert(not unused_exists, "the infra-shaped file did NOT run", out)
    _assert(
        "tests/test_synthy_74.py" in out and "SKIP_INFRA" in out,
        "the skipped file is named on the SKIP_INFRA note",
        out,
    )


def test_skip_infra_zero_does_not_exclude_files() -> None:
    """SKIP_INFRA=0 must behave like SKIP_INFRA unset, not like SKIP_INFRA=1
    (issue #109). Before the fix the loop tested `[ -n "${SKIP_INFRA:-}" ]`,
    which treats the STRING "0" as "set" and skips the infra file anyway --
    exactly backwards for a value a shell profile or CI config exports
    meaning "off". Same tree as test_skip_infra_still_excludes_the_same_files
    above, SKIP_INFRA="0" instead of "1", and the opposite assertions."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ledger = root / "ran.ledger"
        files = {
            "tests/test_plain_74.py": ledger_stub(ledger, "plain"),
            "tests/test_synthy_74.py": interval_stub(
                ledger, "synthy", hold_s=0.0, infra=True
            ),
        }
        proc = run_loop(build_tree(root, files), SKIP_INFRA="0", COLLECT_JOBS="8")
        ran = ledger.read_text().split() if ledger.exists() else []

    out = proc.stdout + proc.stderr
    _assert(proc.returncode == RC_GREEN, "SKIP_INFRA=0 run is green", out)
    _assert(
        "plain" in ran and "synthy" in ran,
        "SKIP_INFRA=0 ran BOTH the plain and the infra-shaped file "
        "(SKIP_INFRA=0 must not act like SKIP_INFRA=1)",
        f"ran={ran!r}\n{out}",
    )
    _assert(
        "NOTE: SKIP_INFRA set" not in out,
        "no file was skipped, so the SKIP_INFRA note is not printed",
        out,
    )


# ---------------------------------------------------------------------------
# (c) scripts/check.sh --only
# ---------------------------------------------------------------------------

def test_check_sh_only_runs_just_the_matching_files() -> None:
    """`--only 'tests/test_gate_*.py'` against a synthetic corpus whose expected
    selection is known exactly, driven through a COPY of the real check.sh.

    A ledger, not the log, is what proves the deselected file did not run: on an
    all-green run a file that DID run prints nothing, so "absent from the log"
    alone cannot tell selection from silence. Both are asserted.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        shutil.copy2(CHECK_SH, root / "scripts" / "check.sh")
        shutil.copy2(SHARED_LOOP, root / "scripts" / "collect_test_failures.sh")
        ledger = root / "ran.ledger"
        build_tree(root, {
            "tests/test_gate_alpha_74.py": ledger_stub(ledger, "test_gate_alpha_74.py"),
            "tests/test_gate_beta_74.py": ledger_stub(ledger, "test_gate_beta_74.py"),
            "tests/test_other_74.py": ledger_stub(ledger, "test_other_74.py"),
            "tests/lint-other-74.py": ledger_stub(ledger, "lint-other-74.py"),
        })
        env = clean_env(SKIP_INFRA="1", CHECK_NO_LOCK="1")
        env["CHECK_LOCK_DIR"] = str(root / "unused.lock")
        proc = subprocess.run(  # noqa: S603
            ["bash", str(root / "scripts" / "check.sh"),  # noqa: S607
             "--only", "tests/test_gate_*.py"],
            capture_output=True, text=True, timeout=300, env=env, cwd=str(root),
        )
        ran = sorted(ledger.read_text().split()) if ledger.exists() else []

    out = proc.stdout + proc.stderr
    _assert(proc.returncode == RC_GREEN, "--only run is green", out)
    _assert(
        ran == ["test_gate_alpha_74.py", "test_gate_beta_74.py"],
        "--only ran exactly the matching files",
        f"ran={ran!r}\n{out}",
    )
    for skipped in ("test_other_74.py", "lint-other-74.py"):
        _assert(skipped not in out, f"{skipped} is absent from the log", out)


# ---------------------------------------------------------------------------
# (d) pytest convergence: pytest.ini and tests/conftest.py
# ---------------------------------------------------------------------------

def test_pytest_ini_declares_the_isolating_options() -> None:
    _assert(PYTEST_INI.is_file(), f"{PYTEST_INI} exists")
    text = PYTEST_INI.read_text(encoding="utf-8")
    body = text.split("[pytest]", 1)
    _assert(len(body) == 2, "pytest.ini has a [pytest] section", text)
    config = body[1]
    _assert("--import-mode=importlib" in config,
            "pytest.ini's addopts declare --import-mode=importlib", config)
    _assert("-p no:cacheprovider" in config,
            "pytest.ini's addopts declare -p no:cacheprovider", config)
    _assert(re.search(r"^\s*python_files\s*=.*test_\*\.py", config, re.M) is not None,
            "pytest.ini declares python_files = test_*.py", config)


def test_conftest_puts_the_declared_roots_on_sys_path() -> None:
    """Asserted by IMPORTING tests/conftest.py in a clean interpreter, not by
    reading it: a path list that is written down but never installed is exactly
    the failure this is here to catch."""
    _assert(CONFTEST.is_file(), f"{CONFTEST} exists")
    probe = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'tests')!r})\n"
        "import conftest\n"
        "print(json.dumps(sys.path))\n"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        env=clean_env(), cwd=str(REPO_ROOT),
    )
    _assert(proc.returncode == 0, "tests/conftest.py imports cleanly",
            proc.stdout + proc.stderr)
    import json

    installed = set(json.loads(proc.stdout))

    required = [REPO_ROOT / "backend" / "src", REPO_ROOT / "scripts"]
    lambda_root = REPO_ROOT / "infra" / "lambda"
    handlers = sorted(c for c in lambda_root.iterdir() if c.is_dir())
    _assert(handlers != [], "infra/lambda/* has handler directories to add",
            f"{lambda_root}")
    required.extend(handlers)

    missing = [str(p) for p in required if str(p) not in installed]
    _assert(missing == [], "tests/conftest.py installs every declared root",
            f"missing={missing!r}")


def test_this_file_collects_and_passes_under_pytest() -> None:
    """The convergence bullet, proved on a real file rather than asserted.

    Runs pytest on THIS file — not a copy, whose REPO_ROOT would resolve to the
    temp directory — under a guard that makes the inner run return from this
    function immediately, so the recursion is exactly one level deep. `-k`
    narrows the inner run to the two cheap checks: the point here is that the
    file COLLECTS and PASSES under pytest, not to run the shell harness twice.
    """
    if os.environ.get("PARALLEL_RUNNER_74_INNER"):
        return
    env = clean_env(PARALLEL_RUNNER_74_INNER="1")
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "--no-header",
         "-p", "no:cacheprovider", "--import-mode=importlib",
         "-k", "pytest_ini or conftest_puts or collects_and_passes",
         str(Path(__file__).resolve())],
        capture_output=True, text=True, timeout=600,
        env=env, cwd=str(REPO_ROOT),
    )
    _assert(proc.returncode == 0,
            "this file's pytest-style functions collect and pass under pytest",
            proc.stdout + proc.stderr)
    _assert("3 passed" in proc.stdout,
            "the inner pytest run collected all three selected functions",
            proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# (d) regression: the conftest's search path must not re-point `import handler`
# ---------------------------------------------------------------------------
#
# All six infra/lambda/<bundle>/ directories contain a module named `handler`,
# and tests/conftest.py puts all six on sys.path. That defeats the
# `if str(DIR) not in sys.path: sys.path.insert(0, str(DIR))` guard the
# script-style tests use — the entry is already present, so the guard skips the
# insert that would have put the RIGHT bundle first, and a bare
# `import handler` binds to whichever bundle is earliest on the path instead.
#
# No gate here can see that: scripts/collect_test_failures.sh runs those files
# script-style, where conftest.py never loads. So the two checks below are the
# only thing standing between this repo and a silently mis-bound handler.

# Assignments of the form `NAME = REPO_ROOT / "infra" / "lambda" / ...`.
BUNDLE_ASSIGNMENT = re.compile(
    r'^(\w+)\s*=\s*REPO_ROOT\s*/\s*"infra"\s*/\s*"lambda"', re.M)

# The representative handler test: it imports infra/lambda/purge_worker's
# `handler`, which is NOT the alphabetically first bundle, so a mis-bound
# import is a failure rather than a coincidence.
HANDLER_TEST = REPO_ROOT / "tests" / "test_retention_purge_worker.py"


def test_no_test_guards_its_lambda_bundle_sys_path_insert() -> None:
    """Static half: no file may guard the insert of its own bundle directory.

    A guarded insert is correct under `python tests/<file>.py` and silently
    wrong under pytest, so the shape itself is the bug — catching it here means
    a new handler test cannot reintroduce the mis-binding unseen.
    """
    tests_dir = REPO_ROOT / "tests"
    checked = 0
    for path in sorted(tests_dir.glob("test_*.py")):
        src = path.read_text(encoding="utf-8")
        names = BUNDLE_ASSIGNMENT.findall(src)
        if not names:
            continue
        checked += 1
        for name in names:
            guard = re.search(
                r"if\s+(?:str\(\s*%s\s*\)|%s)\s+not in sys\.path" % (name, name),  # noqa: UP031
                src)
            _assert(guard is None,
                    f"{path.name} does not guard its insert of {name}",
                    "a guarded insert is a no-op under pytest; insert "
                    "unconditionally at sys.path[0] instead")
            looped = re.search(
                r"for\s+\w+\s+in\s+\([^)]*\b%s\b[^)]*\):\s*\n\s*"  # noqa: UP031
                r"if\s+\w+\s+not in sys\.path" % name,
                src)
            _assert(looped is None,
                    f"{path.name} does not guard {name} inside a loop",
                    "same no-op, spelled as a loop over several roots")
            if re.search(r"^\s*import handler\b", src, re.M):
                unguarded = re.search(
                    r"^sys\.path\.insert\(0,\s*str\(%s\)\)" % name, src, re.M)  # noqa: UP031
                _assert(unguarded is not None,
                        f"{path.name} inserts {name} unconditionally at "
                        f"sys.path[0]",
                        "a file doing a bare `import handler` must force its "
                        "own bundle to the front of the path")
    _assert(checked >= 6,
            "every infra/lambda bundle test was checked",
            f"checked={checked} (expected at least the six handler tests)")


def test_a_handler_test_still_passes_under_pytest() -> None:
    """Dynamic half: run a real handler test under real pytest.

    This is the check that would have caught the conftest pre-seed re-pointing
    `import handler` at infra/lambda/mark_running. It uses the shipped
    pytest.ini and conftest.py — no overrides — because those are what the bug
    lives in.
    """
    _assert(HANDLER_TEST.is_file(), f"{HANDLER_TEST} exists")
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "--no-header", str(HANDLER_TEST)],
        capture_output=True, text=True, timeout=600,
        env=clean_env(), cwd=str(REPO_ROOT),
    )
    _assert(proc.returncode == 0,
            f"{HANDLER_TEST.name} passes under pytest, not just script-style",
            proc.stdout + proc.stderr)


def test_the_handler_test_binds_the_bundle_it_names_under_pytest() -> None:
    """And the module it bound is the right FILE.

    The mis-binding was loud here (AttributeError) only by luck; two bundles
    exporting the same name would have made it silent. Assert the file.
    """
    probe = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'tests')!r})\n"
        "import conftest\n"
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('_probe', {str(HANDLER_TEST)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print(json.dumps(mod._handler_module.__file__))\n"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=300,
        env=clean_env(), cwd=str(REPO_ROOT),
    )
    _assert(proc.returncode == 0,
            "the handler test imports cleanly with conftest's paths installed",
            proc.stdout + proc.stderr)
    import json

    bound = Path(json.loads(proc.stdout.strip().splitlines()[-1])).resolve()
    expected = (REPO_ROOT / "infra" / "lambda" / "purge_worker" / "handler.py").resolve()
    _assert(bound == expected,
            "`import handler` bound the purge_worker bundle",
            f"bound={bound}\nexpected={expected}")


# ---------------------------------------------------------------------------
# Script-style runner (the other half of dual mode)
# ---------------------------------------------------------------------------

TESTS = [
    test_green_tree_parallel_matches_serial,
    test_persistent_failure_tree_parallel_matches_serial,
    test_flaky_tree_parallel_matches_serial,
    test_stdin_reading_infra_tree_parallel_matches_serial,
    test_failure_blocks_are_reported_in_discovery_order,
    test_same_basename_in_two_directories_keeps_its_own_result,
    test_parallel_batch_actually_overlaps,
    test_collect_jobs_rejects_a_value_that_is_not_a_positive_integer,
    test_synth_infra_files_are_never_run_concurrently,
    test_skip_infra_still_excludes_the_same_files,
    test_skip_infra_zero_does_not_exclude_files,
    test_check_sh_only_runs_just_the_matching_files,
    test_pytest_ini_declares_the_isolating_options,
    test_conftest_puts_the_declared_roots_on_sys_path,
    test_this_file_collects_and_passes_under_pytest,
    test_no_test_guards_its_lambda_bundle_sys_path_insert,
    test_a_handler_test_still_passes_under_pytest,
    test_the_handler_test_binds_the_bundle_it_names_under_pytest,
]


def main() -> int:
    failures = 0
    for fn in TESTS:
        name = fn.__name__
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"  [FAIL] {name}\n         {exc}")
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures += 1
            print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  [PASS] {name}")

    print("\n" + "=" * 60)
    print("ALL GREEN" if failures == 0 else f"FAILURES: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
