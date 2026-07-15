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
#   offline, plus the SKIP_INFRA fast-path below.
#   There are no pytest-only files (every file has a __main__ runner), so this
#   loop has no coverage blind spot.
#
# USAGE:
#   scripts/check.sh
#   (Activate the venv first, or let this script auto-activate ./.venv.)
#
# DETERMINISTIC / OFFLINE:
#   Infra tests shell out to `cdk synth` (offline; no AWS calls). AWS-touching
#   tests use moto. No live network or Bedrock is required.

set -u
cd "$(dirname "$0")/.."

# Auto-activate the local venv if one exists and none is active.
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
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
