#!/usr/bin/env bash
#
# check-frontend.sh — offline green gate for the frontend/ SPA.
#
# Runs the TypeScript typecheck + production Vite build (`build:ci` = `tsc &&
# vite build`), the shipped-JS bundle budget over the dist/ that build just
# wrote (`audit:bundle`, issue #56 — nothing in the vitest suite builds, so
# this is the only thing that can see the code split decay), the vitest
# component-test suite (`npm test`), then the CTDS
# design-system audits (contrast + focus/reduced-motion, issue #396; narrow-
# viewport layout containment, issue #457). This is
# the authoritative gate for changes under frontend/: it fails on any type
# error, build breakage, failing component test, or a11y/contrast
# regression, entirely offline (no AWS/Cognito, no network beyond the
# already-installed node_modules — the auth/amplify layer is mocked in the
# tests themselves; see src/__tests__/security-posture.test.tsx).
#
# USAGE: scripts/check-frontend.sh   (from anywhere; resolves its own paths)

set -euo pipefail
cd "$(dirname "$0")/../frontend"

if [ ! -d node_modules ]; then
  echo "Installing frontend deps (npm ci) …"
  npm ci
fi

# Lint (issue #65). FIRST because it is the cheapest step here — seconds,
# against the minutes the build and the vitest suite below cost.
#
# Green means "no NEW violations": the ~865 violations that existed when #65
# landed carry `eslint-disable-next-line` comments written by
# scripts/eslint-baseline.mjs, and eslint.config.js is the committed config.
# `npm run lint` is the plain `eslint .` — it fails on ERRORS, not warnings;
# the remaining warnings are the exhaustive-deps backlog and pre-existing
# `no-console` directives, both deliberately visible rather than silenced.
# `prettier --check` is NOT run: a whole-tree reformat is deferred to #90,
# for the reason CLAUDE.md gives — no gate here can see a stylesheet.
echo "Lint (eslint) …"
npm run lint

echo "Typecheck + production build (build:ci) …"
npm run build:ci

# Shipped-JS budget (issue #56) — must come after build:ci, it measures the
# dist/ that build just wrote. Deliberately NOT piped: a pipeline reports the
# exit status of its last command (see this script's own fail-closed
# `set -euo pipefail` and tests/test_frontend_gate_wired_634.py check 4).
echo "Bundle budget (audit:bundle) …"
npm run audit:bundle

echo "Component tests (vitest) …"
npm test

echo "Design-system audits (contrast + focus/reduced-motion + layout) …"
npm run audit:contrast
npm run audit:focus
npm run audit:layout

echo "CHECK-FRONTEND: ALL GREEN"
