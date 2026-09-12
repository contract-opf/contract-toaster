#!/usr/bin/env bash
#
# setup-hooks.sh — opt in to this repository's git hooks (issue #64).
#
# Points `core.hooksPath` at `.githooks/`, which currently holds one hook:
# a pre-push that refuses a direct push to `main` (branch protection is
# unavailable on this repository's GitHub plan — see .githooks/pre-push).
#
# THIS IS OPT-IN AND MUST STAY OPT-IN.
#   No gate, no scripts/check*.sh, and no npm lifecycle script may call this:
#   a clean checkout keeps git's default hooks, so nobody inherits a local
#   push policy they did not ask for. tests/test_landing_flow_64.py enforces
#   that invariant.
#
# Usage:
#   bash scripts/setup-hooks.sh            # install
#   bash scripts/setup-hooks.sh --uninstall  # back to git's default hooks
#
set -euo pipefail

cd "$(dirname "$0")/.."

if [ "${1:-}" = "--uninstall" ]; then
  git config --unset core.hooksPath || true
  echo "setup-hooks: core.hooksPath unset — git's default hooks are back."
  exit 0
fi

git config core.hooksPath .githooks
echo "setup-hooks: core.hooksPath = .githooks"
echo "setup-hooks: 'git push origin main' is now refused; override with"
echo "setup-hooks:     LAND_TO_MAIN=1 git push origin main"
echo "setup-hooks: undo with 'bash scripts/setup-hooks.sh --uninstall'."
