#!/usr/bin/env bash
#
# land.sh — branch-per-change landing helper (issue #64, diagnostic G8/B8).
#
# WHY THIS EXISTS
#   Work used to land straight on `main`: zero pull-request merges in the last
#   40 commits before 2026-09-05, and three red-`main` runs on that day alone,
#   each fixed by a follow-up commit. Branch protection and rulesets are not
#   available on this repository's GitHub plan (the API answers 403), so the
#   required-status-check gate cannot be turned on yet. Until it can, the
#   interim control (owner decision Q6) is local: this script runs a FAST
#   SUBSET of CI's gates, refuses to push anything red, and opens a pull
#   request so CI gets its own say before the change reaches `main`.
#
#   THE SUBSET IS NOT PARITY, AND DELIBERATELY SO
#     The frontend gate here is `cd frontend && npm test` — vitest only. CI's
#     `frontend-gate` job runs `npm ci` and then `bash scripts/check-frontend.sh`,
#     which is strictly broader: tsc, the production vite build, and the three
#     CTDS audits — among them `npm run audit:layout`, the enforcer of the 14px
#     type floor (#600) and the `minmax(0, …fr)` rule (#457).
#     So a branch this script calls green can still go red on CI's frontend
#     gate. If your change touches `frontend/`, run
#     `bash scripts/check-frontend.sh` yourself before landing it; the
#     pull-request template asks for exactly that.
#
# USAGE
#   git checkout -b phase-N/short-description
#   ... commit ...
#   bash scripts/land.sh
#
# WHAT IT DOES
#   1. Refuses to run on `main` (and on a detached HEAD) — exit 1, no push.
#   2. Runs, in order, stopping at the first red gate:
#        frontend   cd frontend && npm test   (vitest only — see the note above)
#        check      bash scripts/check.sh   (SKIP_INFRA=1 unless this branch
#                                            touches infra/ — see below)
#        brand-free python3 tests/lint-brand-free.py
#      A red gate prints the gate's NAME and its exit code, and exits 1
#      WITHOUT pushing.
#   3. All green: pushes the branch and runs `gh pr create --fill`.
#
# check.sh's EXIT CODE IS AUTHORITATIVE
#   It is read directly here — never piped through `tee`/`tail`, which would
#   report the pipeline's last command instead. 0 = green, 1 = failures,
#   2 = FLAKY-UNRESOLVED (red: pass-on-re-run is not evidence of health),
#   3 = another gate run holds the repo-wide lock (nothing ran at all).
#
# SKIP_INFRA
#   The infra tests shell out to `cdk synth` and are slow, so they are skipped
#   unless the branch actually touches `infra/` (`git diff --name-only main...`).
#
# TEST SEAMS (tests/test_landing_flow_64.py drives this script for real)
#   LAND_GATE_FRONTEND_CMD / LAND_GATE_CHECK_CMD / LAND_GATE_BRAND_CMD each
#   override one gate's command line. They exist so the gate calls are
#   fakeable in a throwaway repository; a developer run leaves them unset and
#   gets the real gates above.
#
# RELATED
#   .githooks/pre-push  blocks a direct push to `main` (opt in with
#                       scripts/setup-hooks.sh).
#
set -uo pipefail

cd "$(dirname "$0")/.."

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"

if [ "$BRANCH" = "main" ]; then
  echo "land.sh: refusing to run on 'main'." >&2
  echo "land.sh: this repository lands through pull requests — branch first:" >&2
  echo "land.sh:     git checkout -b phase-N/short-description" >&2
  exit 1
fi

if [ "$BRANCH" = "HEAD" ]; then
  echo "land.sh: refusing to run on a detached HEAD — check out a branch first." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Does this branch touch infra/? If so the full gate runs (cdk synth included).
# When the base cannot be resolved at all (no `main`, no `origin/main`) the
# answer is unknown, and unknown resolves to the FULL gate — a fast gate chosen
# by accident would skip the 24 CDK-synth tests without saying so.
# ---------------------------------------------------------------------------
DIFF_BASE=""
for ref in main origin/main; do
  if git rev-parse --verify --quiet "$ref" >/dev/null 2>&1; then
    DIFF_BASE="$ref"
    break
  fi
done

if [ -z "$DIFF_BASE" ]; then
  CHECK_SKIP_INFRA=""
  echo "land.sh: no 'main' or 'origin/main' to diff against — running the FULL gate."
else
  # Capture the file list FIRST, then match on the captured text.
  #
  # Do NOT branch on `git diff --name-only ... | grep -q '^infra/'` here. Under
  # the `set -o pipefail` above that pipeline LIES on exactly the branch that
  # matters: `grep -q` exits at its first match, `git diff` is then killed by
  # SIGPIPE (141) while still writing the rest of the list, and pipefail reports
  # 141 — non-zero — for a branch that DOES touch `infra/`. It only shows up
  # once the file names following the match outgrow the pipe buffer, i.e. on a
  # tree-wide branch, and it fails in the one direction this block must never
  # fail in: silently choosing the fast gate (see the note above).
  #
  # `case` on the captured text runs no pipeline at all. The leading newline
  # lets one pattern match `infra/` on the first line as well as on any later
  # one.
  CHANGED_FILES="$(git diff --name-only "$DIFF_BASE"...)"
  NL=$'\n'
  case "$NL$CHANGED_FILES" in
    *"${NL}infra/"*)
      CHECK_SKIP_INFRA=""
      echo "land.sh: branch touches infra/ — running the FULL gate (SKIP_INFRA unset)."
      ;;
    *)
      CHECK_SKIP_INFRA="1"
      echo "land.sh: branch does not touch infra/ — running the fast gate (SKIP_INFRA=1)."
      ;;
  esac
fi

FRONTEND_CMD="${LAND_GATE_FRONTEND_CMD:-cd frontend && npm test}"
CHECK_CMD="${LAND_GATE_CHECK_CMD:-bash scripts/check.sh}"
BRAND_CMD="${LAND_GATE_BRAND_CMD:-python3 tests/lint-brand-free.py}"

fail_gate() {
  # $1 = gate name, $2 = exit code
  echo "" >&2
  echo "land.sh: FAILING GATE: $1 (exit $2)" >&2
  echo "land.sh: nothing was pushed. Fix the gate above and run land.sh again." >&2
  exit 1
}

run_gate() {
  # $1 = gate name, $2 = command line
  echo ""
  echo "land.sh: gate '$1' — $2"
  bash -c "$2"
  return $?
}

# --- gate: frontend --------------------------------------------------------
run_gate frontend "$FRONTEND_CMD"
rc=$?
[ "$rc" -eq 0 ] || fail_gate frontend "$rc"

# --- gate: check -----------------------------------------------------------
if [ -n "$CHECK_SKIP_INFRA" ]; then
  export SKIP_INFRA="$CHECK_SKIP_INFRA"
else
  unset SKIP_INFRA
fi
run_gate check "$CHECK_CMD"
rc=$?
unset SKIP_INFRA
case "$rc" in
  0) ;;
  2)
    echo "land.sh: check.sh reported FLAKY-UNRESOLVED — a file failed and then" >&2
    echo "land.sh: passed alone. That is red here: read the logs and decide." >&2
    fail_gate check "$rc"
    ;;
  3)
    echo "land.sh: check.sh could not run — another gate run holds the repo-wide" >&2
    echo "land.sh: lock. No tests ran; wait for it to finish and try again." >&2
    fail_gate check "$rc"
    ;;
  *) fail_gate check "$rc" ;;
esac

# --- gate: brand-free ------------------------------------------------------
run_gate brand-free "$BRAND_CMD"
rc=$?
[ "$rc" -eq 0 ] || fail_gate brand-free "$rc"

# ---------------------------------------------------------------------------
# Green. Push the branch and open the pull request.
# ---------------------------------------------------------------------------
echo ""
echo "land.sh: all gates green — pushing '$BRANCH' and opening a pull request."

git push -u origin "$BRANCH"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "land.sh: git push failed (exit $rc); no pull request was opened." >&2
  exit 1
fi

gh pr create --fill
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "land.sh: 'gh pr create --fill' failed (exit $rc). The branch IS pushed;" >&2
  echo "land.sh: open the pull request by hand." >&2
  exit 1
fi

echo "land.sh: done. CI now runs its OWN, broader gates on the pull request —"
echo "land.sh: its frontend job is 'scripts/check-frontend.sh' (tsc + vite build"
echo "land.sh: + CTDS audits), not the vitest run above. Watch the PR checks."
