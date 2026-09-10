/**
 * Issue #50 — the UI's poll cadence must stay inside the WAF's per-IP budget.
 *
 * The finding (audit F1): the WAF blocked an IP after 60 GETs on
 * /api/reviews/ in 5 minutes while the UI polled every 3 s (100 per 5 min),
 * so any review longer than ~3 minutes locked the reviewer out mid-review.
 *
 * This test reads BOTH sides from their sources — the poll constants from
 * ReviewSubmission.tsx and the WAF limit by parsing waf-stack.ts — and
 * computes the worst-case request count over any 5-minute window. Half the
 * WAF budget is reserved for History, cancel, downloads and a second
 * concurrent review. If either side drifts, this fails.
 */
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import {
  POLL_FAST_PHASE_MS,
  POLL_INTERVAL_MS,
  POLL_INTERVAL_SLOW_MS,
  pollIntervalFor,
} from '../ReviewSubmission';

const WINDOW_MS = 5 * 60 * 1000;
// Walk up from the working directory (frontend/ under the gate, or the repo
// root) to the checkout that holds infra/. jsdom gives import.meta.url an
// http: scheme, so a file URL cannot be derived from it here.
function repoRoot(): string {
  let dir = process.cwd();
  for (let i = 0; i < 5; i += 1) {
    if (existsSync(resolve(dir, 'infra/lib/nested/waf-stack.ts'))) return dir;
    dir = dirname(dir);
  }
  throw new Error('infra/lib/nested/waf-stack.ts not found above ' + process.cwd());
}
const WAF_SOURCE = resolve(repoRoot(), 'infra/lib/nested/waf-stack.ts');

function wafPollingLimit(): number {
  const source = readFileSync(WAF_SOURCE, 'utf8');
  const start = source.indexOf('RateLimitPollingEndpoint');
  expect(start, 'RateLimitPollingEndpoint rule present in waf-stack.ts').toBeGreaterThan(-1);
  const match = /limit:\s*(\d+)/.exec(source.slice(start));
  expect(match, 'a numeric limit follows the rule name').not.toBeNull();
  return Number(match![1]);
}

/** Worst-case polls in the 5-minute window starting `offsetMs` into the loop. */
function pollsInWindow(offsetMs: number): number {
  let count = 0;
  let t = offsetMs;
  const end = offsetMs + WINDOW_MS;
  while (t < end) {
    count += 1;
    t += pollIntervalFor(t);
  }
  return count;
}

describe('poll budget vs WAF polling rule (issue #50)', () => {
  it('polls fast first, then slows down', () => {
    expect(pollIntervalFor(0)).toBe(POLL_INTERVAL_MS);
    expect(pollIntervalFor(POLL_FAST_PHASE_MS - 1)).toBe(POLL_INTERVAL_MS);
    expect(pollIntervalFor(POLL_FAST_PHASE_MS)).toBe(POLL_INTERVAL_SLOW_MS);
    expect(POLL_INTERVAL_SLOW_MS).toBeGreaterThan(POLL_INTERVAL_MS);
  });

  it('the first 5-minute window is the worst and matches the design numbers', () => {
    const fast = Math.ceil(POLL_FAST_PHASE_MS / POLL_INTERVAL_MS);
    const slow = Math.ceil((WINDOW_MS - POLL_FAST_PHASE_MS) / POLL_INTERVAL_SLOW_MS);
    expect(pollsInWindow(0)).toBe(fast + slow);
    expect(pollsInWindow(0)).toBe(58);
    // Steady state, once the fast phase has passed.
    expect(pollsInWindow(POLL_FAST_PHASE_MS)).toBe(30);
  });

  it('never uses more than half the WAF budget in any 5-minute window', () => {
    const limit = wafPollingLimit();
    expect(limit).toBe(300);
    let worst = 0;
    for (let offset = 0; offset <= WINDOW_MS; offset += 1000) {
      worst = Math.max(worst, pollsInWindow(offset));
    }
    expect(worst).toBeLessThanOrEqual(limit * 0.5);
  });
});
