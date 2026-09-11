#!/usr/bin/env node
/**
 * bundle-budget.mjs — the shipped-JS budget (issue #56).
 *
 * Before #56, `vite build` emitted ONE 951 kB JS chunk (271 kB gzip): every
 * visitor, including a non-admin and every password-mode deployment where
 * `Amplify.configure` never runs, downloaded six admin panels and the whole
 * `aws-amplify` runtime to render the Review tab. The fix is App.tsx's
 * `React.lazy` boundaries plus vite.config.ts's `manualChunks`. This script
 * is what keeps it fixed: without a budget the split silently decays the
 * first time someone re-adds a static import of a panel at the top of
 * App.tsx, and no test in this repo would notice — the suite runs in jsdom
 * and never builds.
 *
 * Two numbers, both against `dist/` as `vite build` left it:
 *
 *   1. THE ENTRY CHUNK, under ENTRY_BUDGET_BYTES. "Entry" is not guessed
 *      from the filename: it is the module script `dist/index.html` actually
 *      references, resolved out of the HTML. That is the one file a visitor
 *      is guaranteed to download before anything renders, so it is the one
 *      the budget is about. A lazy chunk growing is a different (and much
 *      cheaper) problem.
 *   2. ALL of `dist/assets/*.js` together, under TOTAL_BUDGET_BYTES. The
 *      entry budget alone is gameable — moving weight into a chunk that is
 *      fetched immediately anyway is not a saving — so the total is the
 *      backstop that keeps the split honest rather than cosmetic.
 *
 * Both are MINIFIED byte counts (what Vite writes), not gzip: gzip ratios
 * drift with content and would make the threshold mean something slightly
 * different every run.
 *
 * Run after a build: `npm run build:ci && npm run audit:bundle`. Wired into
 * scripts/check-frontend.sh (un-piped, after build:ci) so both the local gate
 * and CI GATE E run it — see tests/test_frontend_gate_wired_634.py for why
 * the invocation must not be piped.
 *
 * Exits non-zero listing every budget that is blown, or if `dist/` does not
 * look like a completed build (a missing dist is a FAILURE, not a skip —
 * silently passing when there is nothing to measure is how a budget check
 * stops being one).
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_DIR = path.resolve(__dirname, '..');
const DIST_DIR = path.join(FRONTEND_DIR, 'dist');
const ASSETS_DIR = path.join(DIST_DIR, 'assets');
const INDEX_HTML = path.join(DIST_DIR, 'index.html');

/** The entry chunk a visitor downloads before anything renders. */
const ENTRY_BUDGET_BYTES = 300 * 1024;
/** Every emitted JS chunk together, lazy ones included. */
const TOTAL_BUDGET_BYTES = 1100 * 1024;

const failures = [];

function fail(message) {
  failures.push(message);
}

function kb(bytes) {
  return `${(bytes / 1024).toFixed(1)} kB`;
}

/**
 * The `src` of every module `<script>` in dist/index.html, as a dist-relative
 * path. Vite emits exactly one for a single-entry build; a build that emits
 * none means the HTML transform did not run and there is nothing to measure.
 */
function entryScriptsFromHtml(html) {
  const scripts = html.match(/<script\b[^>]*>/gi) ?? [];
  const srcs = [];
  for (const tag of scripts) {
    const src = tag.match(/\ssrc=["']([^"']+)["']/i);
    if (!src) {
      // An inline <script> would violate the deployed CSP (script-src 'self',
      // no 'unsafe-inline' — infra/lib/nested/frontend-stack.ts), so it is a
      // failure here rather than something to skip past.
      fail(`dist/index.html emits an inline <script> (no src=): ${tag}`);
      continue;
    }
    srcs.push(src[1].replace(/^\.?\//, ''));
  }
  return srcs;
}

if (!statSafe(DIST_DIR)?.isDirectory()) {
  fail(`no build to measure: ${path.relative(FRONTEND_DIR, DIST_DIR)}/ does not exist — run \`npm run build:ci\` first`);
} else if (!statSafe(INDEX_HTML)?.isFile()) {
  fail(`no build to measure: ${path.relative(FRONTEND_DIR, INDEX_HTML)} does not exist — run \`npm run build:ci\` first`);
} else if (!statSafe(ASSETS_DIR)?.isDirectory()) {
  fail(`no build to measure: ${path.relative(FRONTEND_DIR, ASSETS_DIR)}/ does not exist — run \`npm run build:ci\` first`);
} else {
  const jsFiles = readdirSync(ASSETS_DIR)
    .filter((name) => name.endsWith('.js'))
    .sort();

  if (jsFiles.length === 0) {
    fail('no .js chunk under dist/assets/ — nothing to measure');
  }

  const sizes = new Map(
    jsFiles.map((name) => [name, statSync(path.join(ASSETS_DIR, name)).size]),
  );

  // ---- 1. the entry chunk -------------------------------------------------
  const entrySrcs = entryScriptsFromHtml(readFileSync(INDEX_HTML, 'utf8'));
  const entryJs = entrySrcs.filter((src) => src.endsWith('.js'));
  if (entryJs.length === 0) {
    fail('dist/index.html references no .js module script — cannot identify the entry chunk');
  }
  for (const src of entryJs) {
    const name = path.basename(src);
    const size = sizes.get(name);
    if (size === undefined) {
      fail(`dist/index.html references ${src}, which is not under dist/assets/`);
      continue;
    }
    if (size >= ENTRY_BUDGET_BYTES) {
      fail(
        `entry chunk ${src} is ${kb(size)} minified, at or over the ${kb(ENTRY_BUDGET_BYTES)} budget. ` +
          'Something that used to be lazy is now in the entry graph — check for a static import of an ' +
          'Admin* panel, of ./SsoShell, or of aws-amplify in App.tsx / main.tsx / auth.ts (issue #56).',
      );
    } else {
      console.log(`  entry chunk ${src}: ${kb(size)} (budget ${kb(ENTRY_BUDGET_BYTES)})`);
    }
  }

  // ---- 2. every chunk together -------------------------------------------
  const total = [...sizes.values()].reduce((sum, size) => sum + size, 0);
  if (total >= TOTAL_BUDGET_BYTES) {
    const biggest = [...sizes.entries()].sort((a, b) => b[1] - a[1]).slice(0, 5);
    fail(
      `all ${sizes.size} JS chunk(s) total ${kb(total)}, at or over the ${kb(TOTAL_BUDGET_BYTES)} budget. ` +
        `Largest: ${biggest.map(([name, size]) => `${name} ${kb(size)}`).join(', ')}.`,
    );
  } else {
    console.log(`  all ${sizes.size} JS chunk(s): ${kb(total)} (budget ${kb(TOTAL_BUDGET_BYTES)})`);
  }
}

function statSafe(target) {
  try {
    return statSync(target);
  } catch {
    return null;
  }
}

if (failures.length > 0) {
  console.error('\nbundle-budget: FAILED');
  for (const message of failures) {
    console.error(`  - ${message}`);
  }
  process.exit(1);
}

console.log(
  `bundle-budget: OK — entry chunk under ${kb(ENTRY_BUDGET_BYTES)}, all chunks under ${kb(TOTAL_BUDGET_BYTES)} (issue #56)`,
);
