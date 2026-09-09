#!/usr/bin/env node
/**
 * Run vitest, applying `--no-experimental-webstorage` ONLY on a Node that
 * accepts it.
 *
 * Why this wrapper exists (issue #654):
 *
 * The test script used to be
 *
 *     NODE_OPTIONS=--no-experimental-webstorage vitest run
 *
 * which passes on a developer machine (Node 26, where the flag is permitted in
 * NODE_OPTIONS) and fails hard on CI, which pins `node-version: '20'`:
 *
 *     node: --no-experimental-webstorage is not allowed in NODE_OPTIONS
 *     exit code 9
 *
 * The flag exists to stop Node's experimental Web Storage globals
 * (`localStorage`/`sessionStorage`, added in Node 22) from shadowing the jsdom
 * ones the component tests assert against. On Node 20 those globals do not
 * exist, so the flag is simultaneously **rejected** and **unnecessary** —
 * hardcoding it makes the suite unrunnable on the very version CI uses.
 *
 * `process.allowedNodeEnvironmentFlags` is the runtime's own answer to "will
 * you accept this flag?", so it is asked rather than guessed from a version
 * number. That keeps this correct on Node versions that do not exist yet, and
 * on any future release that removes the flag again.
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const FLAG = "--no-experimental-webstorage";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));

// vitest's `exports` map does not expose `./vitest.mjs`, so resolving that
// subpath directly throws even though the file is right there. Resolve the
// package's main entry (which IS exported) and take its sibling, then fall
// back to the installed bin. Checked with existsSync so a layout change fails
// loudly here instead of as a confusing spawn error.
const candidates = [];
try {
  candidates.push(join(dirname(require.resolve("vitest")), "vitest.mjs"));
} catch {
  /* not installed; the bin fallback below will also miss and we report once */
}
candidates.push(join(here, "..", "node_modules", "vitest", "vitest.mjs"));

const vitestEntry = candidates.find((c) => existsSync(c));
if (!vitestEntry) {
  console.error(
    "run-vitest: could not locate vitest's entry point. Run `npm install` in frontend/ first."
  );
  process.exit(1);
}

// Ask the runtime, do not infer from process.version: the set is authoritative
// and changes between releases.
const supported = process.allowedNodeEnvironmentFlags.has(FLAG);

// The flag goes in NODE_OPTIONS, not argv, and that distinction is the whole
// point: vitest runs the suite in WORKER processes, and NODE_OPTIONS is
// inherited by children while an argv flag applies only to this one process.
// Passing it as argv silently leaves the workers with Node's experimental Web
// Storage still shadowing jsdom's, which surfaces as `window.localStorage`
// failures inside tests rather than as an obvious startup error.
const env = { ...process.env };
if (supported) {
  env.NODE_OPTIONS = [env.NODE_OPTIONS, FLAG].filter(Boolean).join(" ");
}

const result = spawnSync(
  process.execPath,
  [vitestEntry, "run", ...process.argv.slice(2)],
  { stdio: "inherit", env }
);

if (result.error) {
  console.error(`run-vitest: failed to start vitest: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
