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
#
# EXIT CODES (authoritative — this is the landing signal):
#   0  every discovered test file passed. Prints "CHECK: ALL GREEN".
#   1  at least one file failed BOTH its first run and its isolated re-run.
#      A final "CHECK: FAILURES:<space-separated list>" line names them.
#   2  no persistent failure, but at least one file failed and then passed on
#      its isolated re-run (the FLAKY bucket) and ALLOW_FLAKY was not set.
#      A final "CHECK: FLAKY-UNRESOLVED:<list>" line names them.
#      "CHECK: ALL GREEN" is NOT printed.
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
#   The lenient behaviour existed for a real reason: a handful of moto-backed
#   tests (e.g. the S3 upload end-to-end test in
#   tests/test_review_routes_mounted_186.py) flake under heavy CPU load and
#   pass reliably alone, and a gate that is red every other run trains
#   everyone to ignore red. That reason still holds — but the answer is to fix
#   or quarantine the specific offender, not to leave the FLAKY bucket silent
#   for everything. If you find yourself typing ALLOW_FLAKY=1 by reflex, the
#   flaky test has become the problem: file it. (Most of that load came from
#   concurrent full gate runs, which check.sh's lock now prevents.)
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

for t in tests/test_*.py tests/*/test_*.py tests/lint-*.py; do
  [ -e "$t" ] || continue
  if [ -n "${SKIP_INFRA:-}" ] && grep -qlE 'cdk[^A-Za-z]*synth|npx cdk' "$t"; then
    skipped="$skipped $t"
    continue
  fi
  "$PY" "$t" >"$LOG_DIR/$(basename "$t").log" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then
    first_pass_failed="$first_pass_failed $t"
    echo "FAIL(rc=$rc): $t"
    tail -15 "$LOG_DIR/$(basename "$t").log"
    echo "----------------------------------------"
  fi
done

# Retry pass: re-run each first-pass failure once, one at a time. A file that
# passes on the isolated re-run is classified FLAKY. FLAKY is a RED result
# unless ALLOW_FLAKY is set — see the header for why. Persistent failures
# (fail both runs) fail the gate exactly as before.
fail=0
failed=""
flaky=""
for t in $first_pass_failed; do
  if "$PY" "$t" >"$LOG_DIR/retry_$(basename "$t").log" 2>&1; then
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
  if [ -n "${ALLOW_FLAKY:-}" ]; then
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
