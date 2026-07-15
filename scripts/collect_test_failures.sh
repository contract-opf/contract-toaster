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
#
# Exit: 0 if all discovered test files pass; 1 if any failed (a final
# "CHECK: FAILURES:<space-separated list>" line names every failing file).
#
# FLAKE HANDLING: a file that fails is re-run once, alone, before it counts.
# Pass-on-re-run is reported as FLAKY (a "CHECK: FLAKY:" line) but does not
# fail the gate; fail-both-runs is a persistent failure and fails the gate.

set -u

ROOT_DIR="${1:-.}"
cd "$ROOT_DIR" || exit 1

PY="${PYTHON:-python}"
first_pass_failed=""
skipped=""

for t in tests/test_*.py tests/*/test_*.py tests/lint-*.py; do
  [ -e "$t" ] || continue
  if [ -n "${SKIP_INFRA:-}" ] && grep -qlE 'cdk[^A-Za-z]*synth|npx cdk' "$t"; then
    skipped="$skipped $t"
    continue
  fi
  "$PY" "$t" >/tmp/check_"$(basename "$t")".log 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then
    first_pass_failed="$first_pass_failed $t"
    echo "FAIL(rc=$rc): $t"
    tail -15 /tmp/check_"$(basename "$t")".log
    echo "----------------------------------------"
  fi
done

# Retry pass: re-run each first-pass failure once, one at a time. A file that
# passes on the isolated re-run is reported FLAKY but does not fail the gate —
# a handful of moto-backed tests (e.g. the S3 upload end-to-end test) flake
# nondeterministically under load and pass reliably when re-run alone, and a
# gate that goes red on those trains everyone to ignore red. Persistent
# failures (fail both runs) still fail the gate exactly as before.
fail=0
failed=""
flaky=""
for t in $first_pass_failed; do
  if "$PY" "$t" >/tmp/check_retry_"$(basename "$t")".log 2>&1; then
    flaky="$flaky $t"
    echo "FLAKY (failed, then passed on isolated re-run): $t"
  else
    fail=1
    failed="$failed $t"
    echo "FAIL(persistent, failed re-run too): $t"
  fi
done

if [ -n "$flaky" ]; then
  echo "CHECK: FLAKY (passed on isolated re-run, not failing the gate):$flaky"
fi

if [ -n "$skipped" ]; then
  echo "NOTE: SKIP_INFRA set — skipped cdk-synth infra tests:$skipped"
  echo "      (run the full gate with SKIP_INFRA unset before landing infra changes)"
fi

if [ "$fail" -eq 0 ]; then
  echo "CHECK: ALL GREEN"
  exit 0
else
  echo "CHECK: FAILURES:$failed"
  exit 1
fi
