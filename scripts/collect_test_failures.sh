#!/usr/bin/env bash
#
# collect_test_failures.sh — the ONE authoritative collect-all-failures test
# loop, shared by scripts/check.sh (local dev gate) and CI GATE A
# (.github/workflows/ci-pipeline.yml). Extracted for issue #276: the two
# gates used to maintain separate copies of this loop and had drifted apart
# — GATE A ran `python3 "$t" || exit 1` and stopped at the first failure,
# while check.sh collected every failure before reporting. Both callers now
# invoke this single script so they cannot diverge again.
#
# Runs every tests/test_*.py, tests/*/test_*.py, tests/lint-*.py file found
# under ROOT_DIR (default: current directory), continuing past failures so
# the caller sees every failing file in one run, not just the first. Honors
# SKIP_INFRA=1 to skip cdk-synth infra tests (same semantics as check.sh's
# local fast gate).
#
# USAGE:
#   scripts/collect_test_failures.sh [root_dir]
#   PYTHON=python3 scripts/collect_test_failures.sh [root_dir]
#   ALLOW_FLAKY=1 scripts/collect_test_failures.sh [root_dir]
#   CHECK_ONLY='tests/test_foo_*.py' scripts/collect_test_failures.sh [root_dir]
#   COLLECT_JOBS=1 scripts/collect_test_failures.sh [root_dir]   # serial baseline
#
# PARALLELISM (issue #74) — COLLECT_JOBS:
#   The per-file process is the isolation boundary and stays exactly that; what
#   changed is that the boundaries are now crossed concurrently. Discovery still
#   walks the globs below in one pass, in order; the selected files are then run
#   through `xargs -P <jobs>`, and their results are REPORTED afterwards by a
#   second walk of the same ordered list. So the `FAIL(rc=N):` blocks, the
#   `CHECK:` verdict lines and the exit codes are identical to a serial run —
#   only the wall clock differs. The retry pass stays SERIAL and one file at a
#   time, because "does this file pass alone?" is the question it exists to ask.
#
#   COLLECT_JOBS overrides the job count; unset it defaults to `nproc` (Linux)
#   or `sysctl -n hw.ncpu` (macOS), falling back to 1. COLLECT_JOBS=1 is the
#   serial baseline, and it runs through the SAME xargs path as a parallel run
#   rather than a second copy of the loop, so "serial and parallel agree" is a
#   structural property here and not a coincidence two branches have to keep.
#
#   WHY THE INFRA FILES ARE NOT RUN CONCURRENTLY. The files SKIP_INFRA excludes
#   shell out to the synth command with no `--output`, so they all write the one
#   shared `infra/cdk.out` in this tree. Running two of them at once is exactly
#   the corruption scripts/check.sh's repo-wide lock exists to prevent — it
#   produced two false greens on 2026-08-20 — so this script would be
#   manufacturing that hazard inside a single run. They are therefore partitioned
#   out by the SAME content match SKIP_INFRA uses and run one at a time, after
#   the parallel batch. A fast gate (SKIP_INFRA=1) has none of them and is fully
#   parallel; a full gate parallelises everything else and serialises those.
#
#   Per-file log and exit-status files are keyed on the file's FULL relative
#   path (`/` -> `_`), not its basename: `tests/test_x.py` and
#   `tests/sub/test_x.py` collide as basenames, and two concurrent workers
#   writing one status file would report one file's result for the other.
#
# CHECK_ONLY — RUN A SUBSET (issue #68):
#   When set to a glob, only the discovered files whose path (`tests/lint-x.py`)
#   or bare filename (`lint-x.py`) matches it are run; everything else is
#   counted and reported as deselected. This is what backs
#   `scripts/check.sh --only <glob>`, and it lives HERE rather than in that
#   script so the local gate and CI GATE A keep one shared answer to "which
#   files are the suite" (issue #276).
#
#   It narrows the loop's own discovery — it can never reach a path the globs
#   below do not expand to, and it does not override SKIP_INFRA's exclusions.
#   A glob matching nothing is exit 4, deliberately NOT a green run: a typo
#   that ran zero tests and printed ALL GREEN would be worse than any failure
#   this script reports.
#
# EXIT CODES (authoritative — this is the landing signal):
#   0  every discovered test file passed. Prints "CHECK: ALL GREEN".
#   1  at least one file failed BOTH its first run and its isolated re-run.
#      A final "CHECK: FAILURES:<space-separated list>" line names them.
#   2  no persistent failure, but at least one file failed and then passed on
#      its isolated re-run (the FLAKY bucket) and ALLOW_FLAKY was not set.
#      A final "CHECK: FLAKY-UNRESOLVED:<list>" line names them.
#      "CHECK: ALL GREEN" is NOT printed.
#   4  nothing ran: CHECK_ONLY was set and either matched no discovered file
#      ("CHECK: ONLY-MATCHED-NOTHING:<glob>") or matched only files SKIP_INFRA
#      excluded ("CHECK: ONLY-ALL-SKIPPED:<glob>"). Never "CHECK: ALL GREEN".
#
# FLAKE HANDLING — WHY FLAKY IS RED BY DEFAULT:
#   A file that fails is re-run once, alone, before it counts. Pass-on-re-run
#   is classified FLAKY. That bucket used to be reported and then IGNORED: the
#   run still printed "CHECK: ALL GREEN" and exited 0.
#
#   That laundered real failures into a green gate. Observed twice on
#   2026-08-20: two concurrent `scripts/check.sh` runs in different git
#   worktrees corrupted each other's `npx cdk synth` (the `~/.npm/_npx` cache
#   is shared across every worktree, and worktrees have no local
#   infra/node_modules so `npx` falls back to it), the losing run's infra
#   tests failed, passed when re-run alone after the other run had finished,
#   and the gate reported ALL GREEN / exit 0 with tests/test_ci_pipeline.py
#   quietly marked FLAKY. The same path would equally launder a GENUINE
#   intermittent infra regression, and this gate is the repo's only landing
#   signal.
#
#   So: anything in the FLAKY bucket now fails the gate (exit 2). A HUMAN
#   decides it was noise, not the script. To land anyway after looking:
#
#       ALLOW_FLAKY=1 bash scripts/check.sh
#
#   which restores the old lenient behaviour (exit 0 / ALL GREEN) but says
#   loudly, on its own line, which files were waved through and that they
#   were waved through. Read the re-run logs first — the log directory is
#   printed whenever anything fails.
#
#   scripts/check.sh additionally serialises full (infra) runs behind a
#   repo-wide lock so the concurrency that produced the original false green
#   cannot arise in the first place; see its header.
#
#   The lenient behaviour existed for a real reason: a gate that is red every
#   other run trains everyone to ignore red. That reason still holds — but the
#   answer is to fix or quarantine the specific offender, not to leave the
#   FLAKY bucket silent for everything. If you find yourself typing
#   ALLOW_FLAKY=1 by reflex, the flaky test has become the problem: file it.
#   (Most of the load that provoked flakes came from concurrent full gate runs,
#   which check.sh's lock now prevents.)
#
#   WHAT "FLAKES UNDER LOAD" ACTUALLY MEANT HERE — read this before blaming
#   moto. This comment used to name tests/test_review_routes_mounted_186.py as
#   the standing example of "a moto-backed test that flakes under heavy CPU
#   load and passes reliably alone", and issue #583 was filed off that
#   description. Diagnosed 2026-08-22: moto was never involved, and there was
#   no race. The test built a .docx TWICE — once to upload, once to compare the
#   stored object against — and asserted the two archives were byte-identical.
#   `zipfile.writestr` stamps `time.localtime()` into every local-file header
#   at 2-SECOND DOS resolution, so two independent builds differ whenever they
#   straddle a tick. Load did not create a race; it widened the gap between the
#   two builds (75ms idle -> ~700ms at load average ~100), and that gap over
#   2s IS the failure probability. Measured on the pre-fix tree: 11/60 parallel
#   runs failed under load, and the failing runs were EXACTLY the 11 whose two
#   builds straddled a tick — no false positives, no false negatives. 8cb2beb
#   (2026-08-19) replaced the byte comparison with a semantic one; 60/60 pass
#   under the same load. The test is not quarantined and needs no allowlist.
#
#   THE SAME ROOT CAUSE BIT A SECOND FILE, found by the very gate run that was
#   verifying the above: tests/test_review_api_84.py, whose `_submit` also
#   rebuilt its .docx on every call. There the non-deterministic bytes fed
#   sha256(file) -> the DERIVED idempotency key, so
#   test_derived_key_same_bucket_collides submitted two files it believed were
#   identical, got two hashes, two keys, and two review_ids. Measured: 31/60
#   parallel runs failed at load average ~240; 0/60 after the builder was
#   pinned to a fixed ZipInfo date_time (verified at load average ~380).
#   Note what was NOT wrong: the 10-minute bucket and the previous-bucket
#   probe in find_existing_submission are both correct and already handle a
#   genuine bucket straddle. Pin the timestamp; don't widen the bucket.
#
#   The transferable lesson: on this tree "flakes under load" has meant
#   test-side nondeterminism whose window load merely WIDENS, not a race in the
#   mocked AWS layer. Look for a wall-clock-stamped artifact first. Note the
#   two instances presented completely differently — one as a byte comparison,
#   one as an idempotency-key mismatch — so grep for the SOURCE (a helper
#   rebuilt per call via `zipfile.writestr` with a str name), not the symptom.
#   Build fixtures deterministically and neither symptom can arise.
#
# Per-run log files land in their own mktemp directory, NOT at a fixed
# /tmp/check_<basename>.log path — that fixed path was itself shared across
# concurrent runs in different worktrees, so one run's log could be
# overwritten by another's while a human was reading it.

set -u

ROOT_DIR="${1:-.}"
cd "$ROOT_DIR" || exit 1

PY="${PYTHON:-python}"
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/check_logs.XXXXXXXX")"
first_pass_failed=""
skipped=""
deselected=0
matched=0
selected=0

# The ordered discovery list the parallel batch consumes and the report walks
# again. One path per line; the globs below cannot produce a path containing a
# newline, and nothing else ever writes this file.
# DISCOVERY_LIST is every selected file in discovery order — it is what the
# report walk reads, so the printed order never depends on execution order.
# PARALLEL_LIST and SERIAL_LIST partition it for execution only.
DISCOVERY_LIST="$LOG_DIR/.discovered"
PARALLEL_LIST="$LOG_DIR/.run_parallel"
SERIAL_LIST="$LOG_DIR/.run_serial"
: >"$DISCOVERY_LIST"
: >"$PARALLEL_LIST"
: >"$SERIAL_LIST"

# The content match that names a synth-running infra file. ONE definition, used
# both by SKIP_INFRA's exclusion and by the serial partition (see PARALLELISM in
# the header) so the fast gate and the full gate cannot disagree about which
# files those are.
INFRA_MATCH='cdk[^A-Za-z]*synth|npx cdk'

is_infra_file() {
  grep -qlE "$INFRA_MATCH" "$1"
}

# ALLOW_FLAKY / SKIP_INFRA are opt-in flags, not presence flags: `[ -n ... ]`
# treats ALLOW_FLAKY=0 or SKIP_INFRA=0 as "set" and waves the flaky file
# through / skips the infra tests anyway, which is backwards for a value a
# shell profile or CI config can export as "0" meaning "off" (issue #109).
# 1/true/yes/on, case-insensitive; everything else (including "0" and unset)
# is false. Same convention as backend/src/purge_scheduler.py's
# PURGE_SWEEP_ENABLED parsing, applied here as a whitelist (these two default
# to off) rather than that helper's blocklist (it defaults to on).
is_truthy() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

# Log/status key: the full relative path with '/' collapsed to '_'. Basenames
# are not unique across the globs and two workers must never share a file.
slug_for() {
  printf '%s' "$1" | tr '/' '_'
}

# Run one discovered file, recording its output and its exit status where the
# report pass can find them. Exported so the xargs workers can call it.
run_one_file() {
  local target="$1"
  local slug
  slug="$(printf '%s' "$target" | tr '/' '_')"
  # `</dev/null` is load-bearing, not hygiene. The serial pass drives this
  # from `while IFS= read -r t; do run_one_file "$t"; done <"$SERIAL_LIST"`,
  # so without it the child INHERITS the work list on stdin: any test whose
  # process reads stdin swallows the remaining entries, the loop ends early,
  # and report_pass synthesises rc=125 for files that never ran. That is not
  # hypothetical here -- all 26 infra files shell out to `npx cdk synth`
  # with no `stdin=`, and this script's own header documents the
  # worktree-without-node_modules case where `npx` prompts on stdin. The
  # damage lands in the FLAKY bucket, the one bucket this script exists to
  # stop laundering, where ALLOW_FLAKY=1 would then turn it green.
  #
  # It goes HERE rather than on the serial loop so both paths are fixed by
  # one line: xargs already gives its children /dev/null, so only the serial
  # path diverged -- and "serial and parallel are structurally identical",
  # which this file claims at the top, is only true with this redirect.
  "$PY" "$target" >"$LOG_DIR/$slug.log" 2>&1 </dev/null
  printf '%s\n' "$?" >"$LOG_DIR/$slug.rc"
}
export -f run_one_file
export PY LOG_DIR

# How many files to run at once. `nproc` on Linux, `sysctl -n hw.ncpu` on macOS,
# 1 if neither answers. A COLLECT_JOBS that is not a positive integer must not
# quietly become "unlimited" — xargs -P 0 means no limit, which on a 300-file
# tree forks 300 interpreters at once.
detect_cpus() {
  local n
  n="$(nproc 2>/dev/null || true)"
  if [ -z "$n" ]; then
    n="$(sysctl -n hw.ncpu 2>/dev/null || true)"
  fi
  case "$n" in
    ''|*[!0-9]*|0) n=1 ;;
  esac
  printf '%s' "$n"
}

JOBS="${COLLECT_JOBS:-}"
if [ -n "$JOBS" ]; then
  case "$JOBS" in
    ''|*[!0-9]*|0)
      echo "CHECK: COLLECT_JOBS='$JOBS' is not a positive integer; running serially (1)." >&2
      JOBS=1
      ;;
  esac
else
  JOBS="$(detect_cpus)"
fi

# CHECK_ONLY's glob, matched against the discovered path AND its bare filename
# so both `tests/lint-*.py` and `lint-*.py` select the same files (issue #68).
# `case` is the match: its patterns are shell globs, which is exactly what the
# caller typed, and unlike `[[ =~ ]]` it is POSIX and needs no regex
# translation. The pattern is deliberately UNQUOTED — quoting it would compare
# the glob literally and match nothing at all.
only_selects() {
  case "$1" in
    $CHECK_ONLY) return 0 ;;
  esac
  case "${1##*/}" in
    $CHECK_ONLY) return 0 ;;
  esac
  return 1
}

for t in tests/test_*.py tests/*/test_*.py tests/lint-*.py; do
  [ -e "$t" ] || continue
  if [ -n "${CHECK_ONLY:-}" ]; then
    if ! only_selects "$t"; then
      deselected=$((deselected + 1))
      continue
    fi
    matched=$((matched + 1))
  fi
  if is_truthy "${SKIP_INFRA:-}" && is_infra_file "$t"; then
    skipped="$skipped $t"
    continue
  fi
  selected=$((selected + 1))
  printf '%s\n' "$t" >>"$DISCOVERY_LIST"
  # Partition for EXECUTION only, do not run: the batch below runs these. A
  # synth-running infra file goes to the serial list (see PARALLELISM in the
  # header); the report still walks DISCOVERY_LIST, so this does not reorder
  # anything the operator sees.
  if is_infra_file "$t"; then
    printf '%s\n' "$t" >>"$SERIAL_LIST"
  else
    printf '%s\n' "$t" >>"$PARALLEL_LIST"
  fi
done

# A CHECK_ONLY run that executed nothing is exit 4, before any verdict is
# printed. It cannot be folded in with the greens below: every later branch
# ends in a colour, and "zero tests ran" has no honest colour to report. The
# two ways to get here are reported separately because the fix differs — one
# is a glob to correct, the other a SKIP_INFRA to drop.
if [ -n "${CHECK_ONLY:-}" ] && [ "$matched" -eq 0 ]; then
  echo "CHECK: ONLY-MATCHED-NOTHING:$CHECK_ONLY" >&2
  echo "      No test file the gate discovers matches that glob, so NOTHING ran." >&2
  echo "      $deselected discovered file(s) were deselected by it. Quote the glob," >&2
  echo "      and match a path the loop globs (tests/test_*.py, tests/*/test_*.py," >&2
  echo "      tests/lint-*.py) or a bare filename." >&2
  exit 4
fi
if [ -n "${CHECK_ONLY:-}" ] && [ "$selected" -eq 0 ]; then
  echo "CHECK: ONLY-ALL-SKIPPED:$CHECK_ONLY" >&2
  echo "      $matched file(s) matched the glob and SKIP_INFRA excluded every one" >&2
  echo "      of them, so NOTHING ran. Re-run with SKIP_INFRA unset." >&2
  exit 4
fi

# ---------------------------------------------------------------------------
# FIRST PASS (issue #74). Everything that is safe to run concurrently goes
# through one `xargs -P` batch; the synth-running infra files follow one at a
# time. Both halves go through run_one_file, which writes each file's output and
# exit status under $LOG_DIR keyed on its full path, and NEITHER prints: the
# report walk below prints, in discovery order, so the operator-visible output
# does not depend on which worker finished first.
#
# run_one_file deliberately SWALLOWS the test's exit status: its last command is
# the `printf` that records that status, so the function — and therefore each
# `bash -c 'run_one_file "$0"'` worker — always exits 0, and xargs always sees a
# successful child. That is the point, not an accident: a child exiting 255 makes
# xargs abandon the whole batch immediately, and every file it never got to would
# then be reported as rc=125. Do not "fix" run_one_file to propagate the child's
# status. The per-file `.rc` files are the only record of what each test
# returned; a missing one is reported as rc=125 rather than silently passing.
# (The `|| true` below is belt-and-braces for the ways xargs itself can fail.)
# ---------------------------------------------------------------------------
if [ -s "$PARALLEL_LIST" ]; then
  tr '\n' '\0' <"$PARALLEL_LIST" \
    | xargs -0 -n 1 -P "$JOBS" "${BASH:-bash}" -c 'run_one_file "$0"' || true
fi
if [ -s "$SERIAL_LIST" ]; then
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    run_one_file "$t"
  done <"$SERIAL_LIST"
fi

# Report pass: DISCOVERY order, the single ordered list the `for t in …` walk
# above wrote — not the order the workers finished in, and not the
# parallel/serial partition. Nothing here runs a test; it only reads what the
# workers recorded.
report_pass() {
  local list="$1"
  local slug rc
  [ -s "$list" ] || return 0
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    slug="$(slug_for "$t")"
    rc="$(cat "$LOG_DIR/$slug.rc" 2>/dev/null || true)"
    case "$rc" in
      ''|*[!0-9]*)
        # The worker never recorded a status (killed, or out of file
        # descriptors). That is a failure, not a pass.
        rc=125
        : >>"$LOG_DIR/$slug.log"
        ;;
    esac
    if [ "$rc" -ne 0 ]; then
      first_pass_failed="$first_pass_failed $t"
      echo "FAIL(rc=$rc): $t"
      tail -15 "$LOG_DIR/$slug.log"
      echo "----------------------------------------"
    fi
  done <"$list"
}

report_pass "$DISCOVERY_LIST"

if [ -n "${CHECK_ONLY:-}" ]; then
  echo "NOTE: CHECK_ONLY='$CHECK_ONLY' — ran $selected file(s), deselected $deselected."
  echo "      This is a SUBSET. Only a full run is the landing signal."
fi

# Retry pass: re-run each first-pass failure once, one at a time. A file that
# passes on the isolated re-run is classified FLAKY. FLAKY is a RED result
# unless ALLOW_FLAKY is set — see the header for why. Persistent failures
# (fail both runs) fail the gate exactly as before.
fail=0
failed=""
flaky=""
for t in $first_pass_failed; do
  if "$PY" "$t" >"$LOG_DIR/retry_$(slug_for "$t").log" 2>&1; then
    flaky="$flaky $t"
    echo "FLAKY (failed, then passed on isolated re-run): $t"
  else
    fail=1
    failed="$failed $t"
    echo "FAIL(persistent, failed re-run too): $t"
  fi
done

if [ -n "$first_pass_failed" ]; then
  echo "NOTE: full logs for this run: $LOG_DIR"
fi

if [ -n "$skipped" ]; then
  echo "NOTE: SKIP_INFRA set — skipped cdk-synth infra tests:$skipped"
  echo "      (run the full gate with SKIP_INFRA unset before landing infra changes)"
fi

if [ -n "$flaky" ]; then
  if is_truthy "${ALLOW_FLAKY:-}"; then
    echo "CHECK: FLAKY-ALLOWED (ALLOW_FLAKY set — waved through by the operator, NOT by the gate):$flaky"
  else
    echo "CHECK: FLAKY-UNRESOLVED (failed, then passed alone — a human must decide):$flaky"
    echo "      A pass-on-re-run is NOT proof the code is good: it is equally"
    echo "      consistent with a genuine intermittent regression, or with"
    echo "      cross-run interference (see this script's header). Read"
    echo "      $LOG_DIR, then re-run, then either fix it or land with:"
    echo "        ALLOW_FLAKY=1 bash scripts/check.sh"
    [ "$fail" -eq 0 ] && fail=2
  fi
fi

if [ "$fail" -eq 0 ]; then
  echo "CHECK: ALL GREEN"
  exit 0
elif [ "$fail" -eq 2 ]; then
  echo "CHECK: FLAKY-UNRESOLVED:$flaky"
  exit 2
else
  echo "CHECK: FAILURES:$failed"
  exit 1
fi
