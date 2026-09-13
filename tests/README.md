# The test suite

Two styles live here on purpose. This file says which is which, when to use
which, and how to run one file without paying for the whole gate.

The landing signal is `scripts/check.sh` and nothing else — read its **exit
code**, not its last line. `pytest` is a convenience for the files that support
it, never the gate.

## The two styles

### 1. Script style — the existing ~300 files

Each file is a self-contained `__main__` script: a hand-rolled `main()`, a
`unittest` loader, or a `_run_tests()` that threads fixtures in as **positional
arguments** to its `test_*` functions. `scripts/collect_test_failures.sh` runs
each one as its own process and reads its exit status; nothing else about the
file reaches the gate.

```python
def test_something(failures: list[str]) -> None:   # positional fixture
    ...

if __name__ == "__main__":
    sys.exit(main())
```

That process boundary is load-bearing, not historical. The tree has real
cross-file `sys.modules` pollution — six files import a module called
`handler`, and they are not the same `handler` (see "The `handler` collision"
below) — and one process per file is what keeps them from serving each other's
modules out of the import cache.

**pytest cannot collect most of these.** A `test_*` function with a positional
argument reads to pytest as a request for a fixture that does not exist, so it
is reported as an error, not run. Do not "fix" that by adding fixtures; the
script runner is the correct runner for these files.

### 2. pytest style — every NEW test

Write `test_*` functions that take **no arguments** and raise `AssertionError`
on failure, plus an `if __name__ == "__main__":` runner that calls them all and
returns a non-zero exit status if any failed. That shape passes under both
runners:

```python
def test_the_thing() -> None:
    assert compute() == expected

TESTS = [test_the_thing]

def main() -> int:
    ...            # run each, print PASS/FAIL, return 0 or 1

if __name__ == "__main__":
    sys.exit(main())
```

`tests/test_parallel_runner_74.py` is the worked example.

**Convert an existing file only when you are already touching it for another
reason.** A conversion is a behaviour change to a test, and a test that changed
in the same commit as the code it guards has stopped guarding it. There is no
migration sprint.

`pytest.ini` and `tests/conftest.py` are what make style 2 work:

- `--import-mode=importlib` imports each **test** module under a name derived
  from its path instead of inserting the file's directory at the front of
  `sys.path` and registering it under its bare basename. That is what removes
  most of the cross-file pollution described above. It does nothing for the
  non-test modules a test imports.
- `-p no:cacheprovider` keeps `.pytest_cache/` out of the tree.
- `tests/conftest.py` puts the repo root, `tests/`, `backend/src` and
  `scripts/` at the front of `sys.path` and appends each
  `infra/lambda/<handler>/` to the tail — the same directories the script-style
  files add for themselves.

### The `handler` collision

`tests/conftest.py` defines no fixture, but do not read that as "it cannot
change what a test means". **It can.** A shared search path decides which file
a bare `import <name>` binds to, and that is a semantic change whether or not a
fixture is involved — under the two runners the same source line can import two
different files.

All six Lambda bundles — `infra/lambda/` `mark_running`, `mock_review`,
`orphan_reconciler`, `persist`, `purge_worker`, `redline` — contain a module
named `handler`. Because the conftest pre-seeds all six under pytest, two
things follow:

- A bare `import handler` resolves to whichever bundle is earliest on
  `sys.path`, not to the bundle the test means.
- The `if str(DIR) not in sys.path: sys.path.insert(0, str(DIR))` guard the
  script-style files use **silently does nothing** under pytest — the entry is
  already there, so the guard skips the insert that would have put the right
  bundle first.

So a test that imports one specific bundle's handler must either insert its own
bundle directory **unconditionally** at position 0
(`tests/test_retention_purge_worker.py`) or load the file by path with
`importlib` under a distinct module name
(`tests/test_spend_reservation_settlement.py`). Never guard that insert.
`tests/test_parallel_runner_74.py` pins this with a real `pytest` run of a
handler test, because the gate itself cannot see it: the gate runs these files
script-style, where `conftest.py` never loads and the mis-binding is invisible.

Before adding any directory to the conftest's search path, enumerate duplicate
module basenames across all the roots being added and re-run `pytest` on the
files that import them.

## How to run

```bash
bash scripts/check.sh                       # the landing signal. Full suite.
SKIP_INFRA=1 bash scripts/check.sh          # fast gate: skips the ~26 CDK-synth files
bash scripts/check.sh --only 'tests/test_review_api_*.py'   # one file or a glob
ALLOW_FLAKY=1 bash scripts/check.sh         # land despite a FLAKY verdict (a human decides)
```

Quote the glob — unquoted, the shell expands it before `check.sh` sees it. A
glob that matches nothing exits **4** and is never green.

One file, the two direct ways:

```bash
.venv/bin/python tests/test_review_api_84.py                  # both styles
.venv/bin/python -m pytest tests/test_parallel_runner_74.py   # pytest style only
```

`check.sh --only` is the better of the three for a real check: it is the same
loop, with the same retry pass, the same FLAKY rules and the same exit codes as
a full run. `python tests/<file>.py` skips all of that.

### Exit codes

| code | meaning |
| --- | --- |
| 0 | every discovered file passed — `CHECK: ALL GREEN` |
| 1 | at least one file failed its first run **and** its isolated re-run |
| 2 | a file failed then passed alone (FLAKY) and `ALLOW_FLAKY` was unset |
| 3 | another full gate run holds the repo-wide lock; nothing ran |
| 4 | nothing ran: a bad argument, or an `--only` glob that selected no file |

**FLAKY is red.** A pass-on-re-run is equally consistent with a genuine
intermittent regression, so a human decides, not the script. Read the log
directory the run prints before reaching for `ALLOW_FLAKY=1`.

## Speed

Files run in parallel through `xargs -P`, defaulting to `nproc` (Linux) or
`sysctl -n hw.ncpu` (macOS). `COLLECT_JOBS=N` overrides it and `COLLECT_JOBS=1`
is the serial baseline.

Discovery, reporting and the retry pass are all unaffected by the job count:
the `FAIL(rc=N):` blocks come out in discovery order, the `CHECK:` verdict
lines and exit codes are identical to a serial run, and the retry pass re-runs
each failure alone, one at a time — because "does this file pass on its own?"
is the whole question it exists to ask.

Two things are deliberately **not** parallel:

- The retry pass, per above.
- The files that shell out to the CDK synth command. They write the one shared
  `infra/cdk.out`, so two at once corrupt each other — the failure that
  produced two false greens on 2026-08-20 and the reason `check.sh` takes a
  repo-wide lock for full runs. The runner partitions them out by the same
  content match `SKIP_INFRA` uses and runs them one at a time. A
  `SKIP_INFRA=1` gate has none of them and is fully parallel.

## Writing a test that will not be skipped by accident

`SKIP_INFRA=1` decides a file is a slow infra test by grepping the file's
**entire contents** — comments and docstrings included — for the lowercase CDK
synth invocation. Writing that phrase in prose therefore exiles your file from
the fast gate, which is the gate agents actually run. Spell it "CDK synth",
capitalised, as `tests/test_gate_flaky_honesty.py`,
`tests/test_check_only_flag_68.py` and `tests/test_parallel_runner_74.py` all
do, and build the lowercase form with `.lower()` where a fixture genuinely
needs it.

Build fixtures **deterministically**. On this tree "flakes under load" has
twice meant test-side nondeterminism whose window load merely widens — a
`.docx` rebuilt per call, stamped by `zipfile.writestr` with `time.localtime()`
at two-second DOS resolution — never a race in the mocked AWS layer. See the
header of `scripts/collect_test_failures.sh` for both diagnoses.

No live network, no AWS: `moto` for AWS, and the CDK synth step is offline.
