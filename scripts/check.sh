#!/usr/bin/env bash
#
# check.sh — the reproducible local GREEN gate for contract-toaster.
#
# WHY THIS SHAPE (and not `pytest`):
#   Every module under tests/ is a self-contained *script* with an
#   `if __name__ == "__main__"` runner that executes a comprehensive suite
#   (a custom main(), a unittest loader, or a hand-rolled _run_tests()).
#   Many `test_*` functions take positional arguments (fixtures are threaded
#   in by the script runner, not by pytest), so a whole-suite `pytest` run
#   MIS-COLLECTS them as broken fixtures and also suffers cross-file
#   sys.modules pollution. Running each file as its own process is therefore
#   BOTH the authoritative gate and the correct isolation boundary.
#
# RELATION TO CI GATE A:
#   CI GATE A (.github/workflows/ci-pipeline.yml) and this script both
#   delegate the actual test discovery + collect-all-failures loop to
#   scripts/collect_test_failures.sh — the one authoritative implementation
#   (issue #276; previously GATE A ran a private `|| exit 1` copy of this
#   loop that stopped at the first failing file instead of collecting all
#   of them, so the two gates could disagree on multi-failure trees).
#   This script is a superset: same shared loop, run from a pinned venv with
#   the declared dev deps (requirements-dev.txt) so it is reproducible
#   offline, plus the SKIP_INFRA fast-path and the concurrency lock below.
#
# USAGE:
#   scripts/check.sh
#   (Activate the venv first, or let this script auto-activate ./.venv.)
#
# EXIT CODES (authoritative — this is the landing signal):
#   0  every discovered test file passed ("CHECK: ALL GREEN").
#   1  at least one file failed both its first run and its isolated re-run
#      ("CHECK: FAILURES:<list>").
#   2  no persistent failure, but at least one file landed in the FLAKY
#      bucket and ALLOW_FLAKY was not set ("CHECK: FLAKY-UNRESOLVED:<list>").
#   3  another gate run holds the repo-wide lock ("CHECK: LOCK BUSY"); no
#      tests ran at all.
#
#   READ THE EXIT CODE, NOT THE LAST LINE OF OUTPUT. This script's exit code
#   is authoritative, but a *pipeline* reports the exit code of its LAST
#   command, so `bash scripts/check.sh | tee gate.log` and
#   `bash scripts/check.sh | tail` both exit 0 no matter what the gate found.
#   Use `set -o pipefail`, or capture the code directly:
#       bash scripts/check.sh; rc=$?
#   Grepping the output for "CHECK: ALL GREEN" is a safe cross-check, because
#   that line is printed on exit 0 and never on 1, 2, or 3.
#
# FLAKY IS RED (ALLOW_FLAKY):
#   A file that fails and then passes on its isolated re-run FAILS this gate
#   (exit 2). It used to be reported as FLAKY and then ignored, so a green
#   gate could be hiding a real intermittent regression. See
#   scripts/collect_test_failures.sh's header for the full story. To land
#   anyway, after reading the logs and deciding it was noise:
#       ALLOW_FLAKY=1 bash scripts/check.sh
#
# CONCURRENCY LOCK:
#   Full (infra) runs shell out to `npx cdk synth`. Git worktrees have no
#   local infra/node_modules, so `npx` falls back to the `~/.npm/_npx` cache
#   — which is SHARED across every worktree on the machine. Two concurrent
#   full gate runs (one per worktree: routine when agents work in parallel)
#   therefore corrupt each other's synth. On 2026-08-20 that produced two
#   false greens: the losing run's infra tests failed, passed on the isolated
#   re-run once the other run had finished, and the gate reported ALL GREEN.
#
#   So a full run now takes a repo-wide lock, anchored at the shared
#   `git rev-parse --git-common-dir` so every worktree of this repo contends
#   for the same one. A blocked run WAITS (see CHECK_LOCK_WAIT) and then
#   exits 3 — it never runs the suite alongside another holder and never
#   reports green. A lock whose owning PID is gone is reaped automatically.
#
#   SKIP_INFRA=1 runs take no lock: they run no `cdk synth`, and each
#   worktree's infra/cdk.out is its own, so they share nothing. The fast gate
#   stays fully parallel across worktrees.
#
#   Env knobs:
#     CHECK_NO_LOCK=1     skip the lock entirely (you are asserting nothing
#                         else is running; a false green is then on you).
#     CHECK_LOCK_WAIT=N   seconds to wait for the lock before exiting 3
#                         (default 1800; 0 = fail immediately).
#     CHECK_LOCK_DIR=P    override the lock path (used by the gate's own
#                         tests so they never touch the real lock).
#
# CLOCK PARITY WITH CI (CHECK_TZ):
#   The runner is UTC and a machine here is UTC-4, so a date-boundary test can
#   pass locally on the 31st and fail in CI just after midnight on the 1st.
#   This gate therefore runs under TZ=UTC. Override with CHECK_TZ=<zone>.
#   See the export near the top of this script, and issue #639.
#
# DETERMINISTIC / OFFLINE:
#   Infra tests shell out to `cdk synth` (offline; no AWS calls). AWS-touching
#   tests use moto. No live network or Bedrock is required.

set -u
cd "$(dirname "$0")/.."

# ---------------------------------------------------------------------------
# CLOCK PARITY WITH CI (issue #639).
#   GitHub runners are UTC; a developer machine here is UTC-4. On 2026-09-01
#   tests/test_audit_query_api_93.py failed in CI at 01:02:39Z while the same
#   commit's local gate had passed minutes earlier on a machine still reading
#   08-31 — a date-boundary divergence this gate could not see, fixed in
#   bed1fa9 after it had already turned main red.
#
#   Pinning the gate's timezone to the runner's makes that class of divergence
#   surface HERE. CHECK_TZ overrides it (e.g. CHECK_TZ=America/New_York) for
#   the rare case of deliberately reproducing a local-time-specific failure.
#   tests/test_ci_env_parity_639.py Check 4 reads this line's RESOLVED default:
#   it fails if the line is removed, if the default is anything but UTC, if it
#   is indented (conditionally reached), or if it is moved below the
#   collect_test_failures.sh invocation, where it could not take effect.
# ---------------------------------------------------------------------------
export TZ="${CHECK_TZ:-UTC}"

# ---------------------------------------------------------------------------
# Repo-wide gate lock (see CONCURRENCY LOCK above). Acquired BEFORE anything
# else touches shared state — including the `rm -rf infra/cdk.out` below.
# ---------------------------------------------------------------------------
LOCK_HELD=0

resolve_lock_dir() {
  if [ -n "${CHECK_LOCK_DIR:-}" ]; then
    echo "$CHECK_LOCK_DIR"
    return
  fi
  local common
  common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  [ -n "$common" ] || common="$PWD/.git"
  echo "$common/contract-toaster-gate.lock"
}

release_lock() {
  if [ "$LOCK_HELD" -eq 1 ]; then
    LOCK_HELD=0
    # Only drop the lock if it is still OURS. If a peer wrongly reaped it as
    # stale and took it, blindly rm -rf'ing would hand a third run the lock
    # while that peer is mid-synth — the exact corruption this prevents.
    if [ "$(cat "$LOCK_DIR/pid" 2>/dev/null || true)" = "$$" ]; then
      rm -rf "$LOCK_DIR"
    fi
  fi
}

acquire_lock() {
  local wait_left="${CHECK_LOCK_WAIT:-1800}"
  local announced=0
  local anonymous_for=0
  local owner_pid owner_where
  # A non-numeric CHECK_LOCK_WAIT must not silently become an infinite wait.
  case "$wait_left" in
    ''|*[!0-9]*)
      echo "CHECK: CHECK_LOCK_WAIT='$wait_left' is not a non-negative integer; using 1800." >&2
      wait_left=1800
      ;;
  esac
  # The lock lives beside the shared git dir, which normally exists; create the
  # parent anyway so a non-git or unusual CHECK_LOCK_DIR fails fast and loudly
  # rather than by spinning until the timeout.
  if ! mkdir -p "$(dirname "$LOCK_DIR")" 2>/dev/null; then
    echo "CHECK: cannot create the lock's parent directory: $(dirname "$LOCK_DIR")" >&2
    return 3
  fi
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    owner_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    owner_where="$(cat "$LOCK_DIR/owner" 2>/dev/null || echo 'unknown')"

    # Reap a lock whose owner is gone (crashed run, killed agent, reboot).
    if [ -n "$owner_pid" ] && ! kill -0 "$owner_pid" 2>/dev/null; then
      echo "CHECK: reaping stale gate lock (owner pid $owner_pid is gone): $LOCK_DIR"
      rm -rf "$LOCK_DIR"
      anonymous_for=0
      continue
    fi

    # A lock with no pid file names no owner, so the check above can never
    # reap it — it would deadlock every later run forever. The holder writes
    # its pid microseconds after mkdir, so the only way to see one for long is
    # a run killed inside that window. Give it a wide grace period, then reap.
    if [ -z "$owner_pid" ]; then
      anonymous_for=$((anonymous_for + 5))
      if [ "$anonymous_for" -ge 30 ]; then
        echo "CHECK: reaping ownerless gate lock (no pid file after ${anonymous_for}s): $LOCK_DIR"
        rm -rf "$LOCK_DIR"
        anonymous_for=0
        continue
      fi
    else
      anonymous_for=0
    fi

    if [ "$wait_left" -le 0 ]; then
      echo "CHECK: LOCK BUSY — another full gate run holds $LOCK_DIR" >&2
      echo "       owner: pid ${owner_pid:-?} in $owner_where" >&2
      echo "       No tests were run. Two concurrent full runs corrupt each" >&2
      echo "       other's shared npx/CDK state and produce false greens, so" >&2
      echo "       this run refuses rather than reporting a colour it cannot" >&2
      echo "       stand behind. Wait for that run, or use" >&2
      echo "       SKIP_INFRA=1 (no lock needed) for a Python-only gate." >&2
      return 3
    fi

    if [ "$announced" -eq 0 ] || [ $((wait_left % 60)) -eq 0 ]; then
      echo "CHECK: waiting for the gate lock held by pid ${owner_pid:-?} in $owner_where (${wait_left}s left) …"
      announced=1
    fi
    sleep 5
    wait_left=$((wait_left - 5))
  done

  # Stamp ownership FIRST, so the window in which this lock exists without
  # naming an owner is as short as possible (see the ownerless reaper above),
  # then arm the release traps.
  echo "$$" >"$LOCK_DIR/pid"
  echo "$PWD" >"$LOCK_DIR/owner"
  LOCK_HELD=1
  trap 'release_lock' EXIT
  trap 'release_lock; exit 130' INT
  trap 'release_lock; exit 143' TERM
}

LOCK_DIR="$(resolve_lock_dir)"
if [ -z "${SKIP_INFRA:-}" ] && [ -z "${CHECK_NO_LOCK:-}" ]; then
  acquire_lock || exit 3
fi

# Auto-activate the local venv if one exists and none is active.
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# ---------------------------------------------------------------------------
# INTERPRETER PARITY (issue #63). The repo declares ONE Python — `.python-version`
# at the root, matched by pyproject.toml's requires-python, the workflows'
# `python-version:` pins and deploy/dts/backend.Dockerfile's FROM line
# (tests/test_python_version_parity_63.py fails if those four DECLARATIONS
# drift apart). What no gate here can check is the interpreter this run is
# ACTUALLY using: a venv built on another minor version is invisible to a
# declared-pins test and passes locally while CI runs something else.
#
# So print it, and WARN — never fail — on a major.minor mismatch. Warn because
# the venv on the machine this was written for is 3.11 and rebuilding it is not
# this script's business; a hard failure here would only teach people to skip
# the gate. Deliberately after the activation block above so it reports the
# interpreter the rest of the run will use, and deliberately clear of the
# `export TZ=` line, which test_ci_env_parity_639.py Check 4 reads in place.
# ---------------------------------------------------------------------------
CHECK_PY_ACTUAL="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo unknown)"
echo "CHECK: python3 is ${CHECK_PY_ACTUAL} ($(command -v python3 || echo 'not found'))"
if [ -f ".python-version" ]; then
  CHECK_PY_DECLARED="$(tr -d '[:space:]' < .python-version)"
  if [ "${CHECK_PY_ACTUAL}" != "${CHECK_PY_DECLARED}" ]; then
    echo "CHECK: WARNING - interpreter ${CHECK_PY_ACTUAL} does not match .python-version (${CHECK_PY_DECLARED}); CI runs ${CHECK_PY_DECLARED}, so a green run here is not proof of a green run there." >&2
  fi
fi

# Clear stale CDK synth output. cdk.out is gitignored and cdk synth does NOT
# prune templates for stacks that no longer exist, so pre-rename artifacts
# (e.g. eiaareviewdev*.nested.template.json from before PR #184) linger and
# poison glob-count assertions in the infra tests (they expect exactly one
# Pipeline / Observability nested template). CI is a fresh checkout with no
# cache, so it never sees this. Start every local run from a clean cdk.out.
rm -rf infra/cdk.out

# SKIP_INFRA=1 skips the tests that shell out to `cdk synth` (~14 files, each a
# full ~15s synth → ~5-6 min total). Use it as a FAST gate for changes that do
# NOT touch infra/ (e.g. pure-Python backend/scripts fixes): those changes
# cannot affect the synthesized CDK templates, so the infra assertions are
# irrelevant to them, and the full ~6 min gate otherwise exceeds an automated
# agent's per-turn wall-clock budget. Always run the FULL gate (unset) before
# landing anything that touches infra/, and as a final pre-merge confirmation.
#
# The actual discovery + collect-all-failures loop lives in
# scripts/collect_test_failures.sh (issue #276) so this script and CI GATE A
# share one authoritative implementation.
"$(dirname "$0")/collect_test_failures.sh" .
rc=$?

# Exit explicitly with the loop's code: the EXIT trap that releases the lock
# must not be what determines this script's status.
exit "$rc"
