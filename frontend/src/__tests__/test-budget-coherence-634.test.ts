/**
 * test-budget-coherence-634.test.ts — issue #634 item 2.
 *
 * ## What went wrong
 *
 * Three test files intermittently died with "Test timed out in 5000ms",
 * migrating between files across runs. It looked like flakiness; it was a
 * budget that was never big enough.
 *
 * Every slow test in this suite has to span at least one real
 * `POLL_INTERVAL_MS` (3000 ms, `ReviewSubmission.tsx`), because that is how
 * the panel learns anything: the test changes what the stubbed
 * `GET /api/reviews/{id}` returns and waits for the NEXT poll. Measured on an
 * idle machine the worst such test took 4127 ms — against vitest's DEFAULT
 * 5000 ms `testTimeout`. Any co-scheduled load spends that 873 ms margin.
 *
 * The tell was already in the source: those files declare
 * `waitFor(…, { timeout: 6000 })` and `{ timeout: 8000 }` — windows LARGER
 * than the enclosing per-test budget, so vitest killed the test before the
 * author's own tolerance could elapse. A declared window bigger than the
 * budget that contains it is always a lie, and it fails as a confusing
 * harness timeout rather than as the assertion that actually didn't hold.
 *
 * ## What this file locks
 *
 * That inconsistency, permanently — statically AND at runtime. It is the
 * automatic signal for item 2, the same way the CI job is the automatic
 * signal for item 3 (`tests/test_frontend_gate_wired_634.py`).
 *
 * It asserts nothing about how long a test SHOULD take. It asserts only that
 * the harness can honour what the suite already asks for.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';
import { describe, expect, it } from 'vitest';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC_DIR = join(HERE, '..');
const FRONTEND_DIR = join(SRC_DIR, '..');
const CONFIG_PATH = join(FRONTEND_DIR, 'vitest.config.ts');
const REVIEW_SUBMISSION = join(SRC_DIR, 'ReviewSubmission.tsx');

/** vitest's built-in default. The number every one of these tests died on. */
const VITEST_DEFAULT_TEST_TIMEOUT_MS = 5000;

/** This file, excluded from the scan: its own prose quotes the numbers. */
const SELF = 'test-budget-coherence-634.test.ts';

function readNumber(source: string, pattern: RegExp, label: string): number {
  const match = source.match(pattern);
  if (!match) {
    throw new Error(`could not read ${label} — pattern ${pattern} did not match`);
  }
  return Number(match[1].replace(/_/g, ''));
}

function testFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules') continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      found.push(...testFiles(full));
    } else if (/\.test\.tsx?$/.test(entry) && entry !== SELF) {
      found.push(full);
    }
  }
  return found;
}

interface DeclaredWindow {
  file: string;
  ms: number;
}

/** Every `timeout: <number>` a test file hands to waitFor/findBy*. */
function declaredWindows(): DeclaredWindow[] {
  const windows: DeclaredWindow[] = [];
  for (const file of testFiles(SRC_DIR)) {
    const source = readFileSync(file, 'utf8');
    for (const match of source.matchAll(/\btimeout:\s*(\d[\d_]*)\b/g)) {
      windows.push({ file: relative(FRONTEND_DIR, file), ms: Number(match[1].replace(/_/g, '')) });
    }
  }
  return windows;
}

const configuredTimeoutMs = readNumber(
  readFileSync(CONFIG_PATH, 'utf8'),
  /^\s*testTimeout:\s*(\d[\d_]*)\s*,/m,
  'testTimeout in vitest.config.ts',
);

const pollIntervalMs = readNumber(
  readFileSync(REVIEW_SUBMISSION, 'utf8'),
  /^const POLL_INTERVAL_MS = (\d[\d_]*);/m,
  'POLL_INTERVAL_MS in ReviewSubmission.tsx',
);

describe('issue #634 — the per-test budget must honour what the suite declares', () => {
  it('the scan actually sees the declarations it is meant to police', () => {
    // Without this, a regex that stopped matching would leave every assertion
    // below vacuously green — the failure mode this whole file exists to
    // prevent, reproduced one level up.
    const windows = declaredWindows();
    expect(windows.length).toBeGreaterThanOrEqual(8);
    const overDefault = windows.filter((w) => w.ms > VITEST_DEFAULT_TEST_TIMEOUT_MS);
    expect(
      overDefault.length,
      'the poll-spanning files declare windows wider than vitest\'s 5000 ms default; ' +
        'if none are found the scanner has stopped seeing them',
    ).toBeGreaterThan(0);
  });

  it('vitest.config.ts pins the per-test budget instead of inheriting the 5000 ms default', () => {
    expect(configuredTimeoutMs).toBeGreaterThan(VITEST_DEFAULT_TEST_TIMEOUT_MS);
  });

  it('no test declares a wait window its enclosing budget cannot honour', () => {
    const unreachable = declaredWindows().filter((w) => w.ms > configuredTimeoutMs);
    expect(
      unreachable,
      'a waitFor/findBy window larger than testTimeout can never elapse — the test ' +
        'is killed by the harness first, and reports a timeout instead of the ' +
        'assertion that failed. Either shorten the window or raise testTimeout ' +
        'in vitest.config.ts (and say why there).',
    ).toEqual([]);
  });

  it('the budget still leaves a full poll interval beyond the widest declared window', () => {
    // The real dependency: these tests wait on ReviewSubmission's polling
    // loop, so the budget is only meaningful relative to POLL_INTERVAL_MS.
    // Raise the poll interval and this fires — which is the point.
    const widest = Math.max(...declaredWindows().map((w) => w.ms));
    expect(configuredTimeoutMs).toBeGreaterThanOrEqual(widest + pollIntervalMs);
  });

  it('the runner really applies that budget, not just the config file text', async () => {
    // The checks above read source. This one spends real wall clock: it
    // outlives vitest's 5000 ms default by 600 ms, so it CANNOT pass unless
    // the effective per-test budget is genuinely larger. Set `testTimeout`
    // back to 5_000 in vitest.config.ts and this is the test that dies with
    // "Test timed out in 5000ms" — the same failure the three poll-spanning
    // files were suffering. (The source-reading checks above go red too, but
    // as assertion failures; only this one reproduces the symptom.)
    const start = Date.now();
    await new Promise((resolve) => setTimeout(resolve, VITEST_DEFAULT_TEST_TIMEOUT_MS + 600));
    expect(Date.now() - start).toBeGreaterThan(VITEST_DEFAULT_TEST_TIMEOUT_MS);
  });

  it('the runner\'s parallelism is a stated decision, not the host core count', () => {
    // Bounded workers cannot move the 3000 ms poll floor, but oversubscribing
    // 6 physical cores with 11 jsdom workers is contention we inflict on
    // ourselves: measured over the whole suite, the worst poll-spanning test
    // drops 4127 ms -> 3657 ms at 50% for ~1 s of extra wall clock.
    expect(readFileSync(CONFIG_PATH, 'utf8')).toMatch(/^\s*maxWorkers:/m);
  });
});
