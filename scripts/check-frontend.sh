#!/usr/bin/env bash
#
# check-frontend.sh — green gate for the frontend/ SPA.
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
# regression. No AWS/Cognito network calls anywhere in it — the auth/amplify
# layer is mocked in the tests themselves; see
# src/__tests__/security-posture.test.tsx. It is NOT fully offline, though:
# the lockfile-validation step below (issue #112) fetches `npm@10.8.2`
# through `npx` the first time it runs on a given machine, the same npm
# registry reachability `npm ci` itself already requires — after that first
# fetch it is served from npx's own cache.
#
# USAGE: scripts/check-frontend.sh   (from anywhere; resolves its own paths)

set -euo pipefail
cd "$(dirname "$0")/../frontend"

# Lockfile validation (issue #112) — UNCONDITIONAL, before anything else in
# this script touches node_modules. The Docker build that ships this SPA
# (deploy/dts/frontend.Dockerfile: node:20-slim / npm 10.8.2) runs a bare
# `npm ci`, which hard-fails on a lockfile a newer local npm rewrote (see
# docs/frontend-design-system.md §3.3, "Local npm is 11.x"). Because the
# install step below only runs `npm ci` when node_modules is ABSENT — true on
# CI's first run, false on every dev machine after that — a dev machine that
# already has node_modules never asks npm whether package-lock.json still
# matches package.json. That is exactly the September incident (ef1a0fc): the
# frontend job died at install in CI and never reached a single test.
# `--dry-run` makes npm resolve and validate without writing node_modules or
# spending real install time (seconds, using npx's own cache); pinning to
# npm@10.8.2 reproduces the Docker build's npm VERSION, not whatever npm is
# on $PATH on this machine. It does NOT reproduce the Docker build's
# PLATFORM: this check validates package-lock.json for the OS/CPU it runs
# on, so an entry missing for another platform (the September incident's
# `@esbuild/linux-x64`) still passes here on macOS — CI's own `npm ci` on
# linux is still the real cross-platform backstop.
echo "Lockfile validation (npm ci --dry-run, pinned to npm 10.8.2) …"
npx --yes npm@10.8.2 ci --ignore-scripts --no-audit --no-fund --dry-run

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
