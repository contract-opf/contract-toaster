#!/usr/bin/env bash
#
# prettier-staged.sh — `prettier --check` over the frontend files pre-commit hands it.
#
# Issue #90, the formatter half of what eslint-staged.sh does for lint.
# `.pre-commit-config.yaml` passes repo-relative paths (`frontend/src/App.tsx`),
# but `.prettierrc.json` and `.prettierignore` resolve against `frontend/`. This
# strips the prefix and runs Prettier from there, so the committed config and
# ignore list apply unchanged (a path on `.prettierignore` is skipped, not
# reported).
#
# `--check`, never `--write`: a hook must not rewrite a file on its way into a
# commit, and CLAUDE.md's warning that no gate in this repo can see a
# stylesheet is the reason. The fix for a red run is
# `npm run format -- <the paths it names>`.
#
# Exits 0 when pre-commit passes it nothing relevant (every path was filtered
# out) — an empty Prettier invocation would otherwise check the whole tree.
set -euo pipefail

cd "$(dirname "$0")/.."

args=()
for path in "$@"; do
  args+=("${path#frontend/}")
done

if [ "${#args[@]}" -eq 0 ]; then
  exit 0
fi

exec npx --no-install prettier --check "${args[@]}"
