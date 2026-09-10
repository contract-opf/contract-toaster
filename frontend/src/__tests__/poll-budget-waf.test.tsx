/**
 * Issue #50 — the UI's poll cadence must stay inside the WAF's per-IP budget.
 *
 * The finding (audit F1): the WAF blocked an IP after 60 GETs on
 * /api/reviews/ in 5 minutes while the UI polled every 3 s (100 per 5 min),
 * so any review longer than ~3 minutes locked the reviewer out mid-review.
 *
 * Two layers, because the first alone proved nothing about the component:
 *
 *   1. The MODEL. Reads the poll constants from ReviewSubmission.tsx and the
 *      WAF limit by parsing waf-stack.ts, computes the worst-case request
 *      count over any 5-minute window and asserts it stays under half the
 *      budget (the other half is for History, downloads, cancel and a second
 *      concurrent review). If either side drifts, this fails.
 *
 *   2. The COMPONENT. Renders the real panel with fake timers and counts
 *      the GETs it actually issues: an old review polls at the slow cadence
 *      from the first response (the phase is anchored on the server's
 *      `created_at`, so neither "Check now" nor a reload can restart it), a
 *      fresh review polls fast, and a flapping endpoint deep in a review is
 *      polled no faster than the slow cadence.
 *
 * Fully offline: aws-amplify/auth and @aws-amplify/ui-react are mocked, fetch
 * is stubbed per test.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import ReviewSubmission, {
  POLL_BACKOFF_MAX_MS,
  POLL_FAST_PHASE_MS,
  POLL_INTERVAL_MS,
  POLL_INTERVAL_SLOW_MS,
  nextPollDelayMs,
  pollIntervalFor,
  reviewAgeMs,
} from '../ReviewSubmission';
import { DEFAULT_PLAYBOOKS } from './support/consoleSurface';

const WINDOW_MS = 5 * 60 * 1000;
// Same idiom as the sibling source-scanning tests: anchor on this file, not
// on the working directory. (Do NOT write `new URL('...', import.meta.url)`
// here — Vite's asset plugin rewrites that literal form to a dev-server URL.)
const WAF_SOURCE = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../infra/lib/nested/waf-stack.ts',
);

/** The polling rule's limit, read from the rule's own `name:` field forward. */
function wafPollingLimit(): number {
  const source = readFileSync(WAF_SOURCE, 'utf8');
  const start = source.indexOf("name: 'RateLimitPollingEndpoint'");
  expect(start, "name: 'RateLimitPollingEndpoint' present in waf-stack.ts").toBeGreaterThan(-1);
  const match = /\blimit:\s*(\d+)/.exec(source.slice(start));
  expect(match, 'a numeric limit follows the rule name').not.toBeNull();
  return Number(match![1]);
}

/** Polls in the 5-minute window starting `offsetMs` into a review's life. */
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

// ---------------------------------------------------------------------------
// 1. The model
// ---------------------------------------------------------------------------

describe('poll cadence functions (issue #50)', () => {
  it('polls fast for the first two minutes of a review, then slowly', () => {
    expect(pollIntervalFor(0)).toBe(POLL_INTERVAL_MS);
    expect(pollIntervalFor(POLL_FAST_PHASE_MS - 1)).toBe(POLL_INTERVAL_MS);
    expect(pollIntervalFor(POLL_FAST_PHASE_MS)).toBe(POLL_INTERVAL_SLOW_MS);
  });

  it('anchors the age on the server created_at, falling back to first sight', () => {
    const now = Date.UTC(2026, 8, 10, 12, 0, 0);
    const tenMinutesAgo = new Date(now - 600_000).toISOString();
    expect(reviewAgeMs(tenMinutesAgo, now - 1_000, now)).toBe(600_000);
    // Unparseable or absent: time since this client first saw the review.
    expect(reviewAgeMs('not a date', now - 5_000, now)).toBe(5_000);
    expect(reviewAgeMs(null, now - 5_000, now)).toBe(5_000);
    // A created_at in the future (skewed client clock) must not stretch the
    // fast phase; fall back rather than go negative.
    expect(reviewAgeMs(new Date(now + 60_000).toISOString(), now - 5_000, now)).toBe(5_000);
  });

  it('never lets the failure backoff drop below the phase cadence', () => {
    // Fast phase: the ladder starts at the fast interval.
    expect(nextPollDelayMs(0, 0)).toBe(POLL_INTERVAL_MS);
    expect(nextPollDelayMs(0, 1)).toBe(POLL_INTERVAL_MS);
    expect(nextPollDelayMs(0, 2)).toBe(POLL_INTERVAL_MS * 2);
    // Slow phase: a first failure waits the slow interval, not 3 s.
    expect(nextPollDelayMs(POLL_FAST_PHASE_MS, 0)).toBe(POLL_INTERVAL_SLOW_MS);
    expect(nextPollDelayMs(POLL_FAST_PHASE_MS, 1)).toBe(POLL_INTERVAL_SLOW_MS);
    expect(nextPollDelayMs(POLL_FAST_PHASE_MS, 2)).toBe(POLL_INTERVAL_SLOW_MS);
    expect(nextPollDelayMs(POLL_FAST_PHASE_MS, 3)).toBe(POLL_INTERVAL_MS * 4);
    // Both phases cap at the ceiling.
    expect(nextPollDelayMs(0, 20)).toBe(POLL_BACKOFF_MAX_MS);
    expect(nextPollDelayMs(POLL_FAST_PHASE_MS, 20)).toBe(POLL_BACKOFF_MAX_MS);
  });
});

describe('poll budget vs WAF polling rule (issue #50)', () => {
  it('the first window is the worst: 40 fast + 18 slow, then 30 steady', () => {
    expect(pollsInWindow(0)).toBe(58);
    expect(pollsInWindow(POLL_FAST_PHASE_MS)).toBe(30);
  });

  it('never uses more than half the WAF budget in any 5-minute window', () => {
    const limit = wafPollingLimit();
    let worst = 0;
    for (let offset = 0; offset <= WINDOW_MS; offset += 1000) {
      worst = Math.max(worst, pollsInWindow(offset));
    }
    expect(worst).toBe(pollsInWindow(0));
    expect(worst).toBeLessThanOrEqual(limit * 0.5);
  });
});

// ---------------------------------------------------------------------------
// 2. The component
// ---------------------------------------------------------------------------

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  }),
}));
vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({
    user: { username: 'reviewer-sub', signInDetails: { loginId: 'reviewer@example.com' } },
    signOut: vi.fn(),
  }),
}));

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

interface Harness {
  /** GETs of the review's status so far. */
  polls: () => number;
}

/**
 * Render the panel, submit a file, and answer every status poll RUNNING with
 * the given review age. `failEveryOther` makes odd-numbered polls reject.
 */
async function runningReview(ageMs: number, failEveryOther = false): Promise<Harness> {
  let polls = 0;
  const createdAt = new Date(Date.now() - ageMs).toISOString();
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (pathname === '/api/playbooks') {
      return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
    }
    if (method === 'POST' && pathname === '/api/reviews') {
      return {
        ok: true,
        status: 200,
        json: async () => ({ review_id: 'rev-cadence', resumed: false }),
      } as Response;
    }
    if (pathname === '/api/reviews/rev-cadence') {
      polls += 1;
      if (failEveryOther && polls % 2 === 0) {
        throw new TypeError('network down');
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          review_id: 'rev-cadence',
          status: 'RUNNING',
          decision: null,
          message: null,
          has_output: false,
          created_at: createdAt,
        }),
      } as Response;
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', fetchMock);

  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  // Land the playbook catalog (issue #733) before the submit — no poll timer
  // exists yet, so draining the timers here terminates.
  await vi.runAllTimersAsync();
  fireEvent.click(screen.getByTestId('review-submit-button'));
  // Let the POST and the first poll settle without advancing the clock.
  await vi.advanceTimersByTimeAsync(0);
  return { polls: () => polls };
}

describe('the rendered poll loop honours the cadence (issue #50)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('a review already past the fast phase is polled at the slow cadence from the start', async () => {
    const h = await runningReview(10 * 60_000);
    const first = h.polls();
    await vi.advanceTimersByTimeAsync(60_000);
    // 60 s at 10 s is 6 more polls; at 3 s it would be 20.
    expect(h.polls() - first).toBeGreaterThanOrEqual(5);
    expect(h.polls() - first).toBeLessThanOrEqual(7);
  });

  it('a fresh review is polled at the fast cadence', async () => {
    const h = await runningReview(0);
    const first = h.polls();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(h.polls() - first).toBeGreaterThanOrEqual(19);
    expect(h.polls() - first).toBeLessThanOrEqual(21);
  });

  it('a flapping endpoint deep in a review is never polled faster than the slow cadence', async () => {
    const h = await runningReview(10 * 60_000, true);
    const first = h.polls();
    await vi.advanceTimersByTimeAsync(60_000);
    // Success → 10 s, failure → max(10 s, 3 s) = 10 s: still ~6 per minute.
    expect(h.polls() - first).toBeLessThanOrEqual(7);
  });
});
