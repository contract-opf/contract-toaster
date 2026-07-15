#!/usr/bin/env bash
#
# check-frontend.sh — offline green gate for the frontend/ SPA.
#
# Runs the TypeScript typecheck + production Vite build (`build:ci` = `tsc &&
# vite build`), then the vitest component-test suite (`npm test`). This is
# the authoritative gate for changes under frontend/: it fails on any type
# error, build breakage, or failing component test, entirely offline (no
# AWS/Cognito, no network beyond the already-installed node_modules — the
# auth/amplify layer is mocked in the tests themselves; see
# src/__tests__/security-posture.test.tsx).
#
# USAGE: scripts/check-frontend.sh   (from anywhere; resolves its own paths)

set -euo pipefail
cd "$(dirname "$0")/../frontend"

if [ ! -d node_modules ]; then
  echo "Installing frontend deps (npm ci) …"
  npm ci
fi

echo "Typecheck + production build (build:ci) …"
npm run build:ci

echo "Component tests (vitest) …"
npm test

echo "CHECK-FRONTEND: ALL GREEN"
