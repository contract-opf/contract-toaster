#!/usr/bin/env bash
#
# eslint-staged.sh — run ESLint over the frontend files pre-commit hands it.
#
# Issue #65. `.pre-commit-config.yaml` passes repo-relative paths
# (`frontend/src/App.tsx`), but `frontend/eslint.config.js` resolves its
# `files:`/`ignores:` globs against `frontend/`. This strips the prefix and
# runs ESLint from there, so the committed config applies unchanged.
#
# No `--fix`: #65's baseline is annotation-only, and CLAUDE.md's warning that
# no gate in this repo can see a stylesheet is the reason a hook must never
# rewrite code on its way into a commit.
#
# Exits 0 when pre-commit passes it nothing relevant (every path was filtered
# out) — an empty ESLint invocation would otherwise lint the whole tree.
set -euo pipefail

cd "$(dirname "$0")/.."

args=()
for path in "$@"; do
  args+=("${path#frontend/}")
done

if [ "${#args[@]}" -eq 0 ]; then
  exit 0
fi

exec npx --no-install eslint "${args[@]}"
