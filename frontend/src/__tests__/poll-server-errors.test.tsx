/**
 * poll-server-errors.test.tsx — a run of HTTP 500s on the status poll says
 * what is known, keeps retrying, and recovers.
 *
 * ## The production report (2026-10-28, build c35b5e1)
 *
 * Every `GET /api/reviews/{id}` of the first real OPF 0.6 review answered
 * HTTP 500 from its first second to well past DONE (the detail route could
 * not serialize a DynamoDB `Decimal` — tests/test_review_detail_decimal.py).
 * The console sat on "Stage not yet reported" under copy promising it was
 * "reconnecting…", while the review finished fine and History, a different
 * route, listed it as DONE.
 *
 * Owner rule #122 keeps a 5xx transient: the poller never gives up on it,
 * because a redeploy can clear it. What this pins is the COPY: after
 * `POLL_SERVER_ERROR_STREAK` consecutive 5xx the message stops saying
 * "reconnecting" and points at History; the retries carry on; and the first
 * good answer lands the review normally.
 *
 * Fully offline: auth is mocked and fetch is stubbed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import ReviewSubmission, {
  POLL_SERVER_ERROR_STREAK,
  SERVER_ERROR_COPY,
  nextPollDelayMs,
} from '../ReviewSubmission';
import { DEFAULT_PLAYBOOKS } from './support/consoleSurface';
import { allowConsoleErrorsInThisTest } from './support/consoleErrorGuard';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const REVIEW_ID = '5d2c8e17-0a4b-4f63-9b1e-7c3a9d0e2f84';
const DETAIL_PATH = `/api/reviews/${REVIEW_ID}`;
const STILL_CHECKING = /reconnecting/;

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

/** Status GETs answer 500 until `healed.current`, then DONE. */
function stubServerErrors(healed: { current: boolean }): { polls: () => number } {
  let polls = 0;
  const createdAt = String(Math.floor(Date.now() / 1000));
  // eslint-disable-next-line @typescript-eslint/require-await
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    // eslint-disable-next-line @typescript-eslint/no-base-to-string
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (pathname === '/api/playbooks') {
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
    }
    if (method === 'POST' && pathname === '/api/reviews') {
      return {
        ok: true,
        status: 202,
        // eslint-disable-next-line @typescript-eslint/require-await
        json: async () => ({ review_id: REVIEW_ID, resumed: false }),
      } as Response;
    }
    if (pathname === DETAIL_PATH) {
      polls += 1;
      if (!healed.current) {
        // eslint-disable-next-line @typescript-eslint/require-await
        return { ok: false, status: 500, json: async () => ({}) } as Response;
      }
      return {
        ok: true,
        status: 200,
        // eslint-disable-next-line @typescript-eslint/require-await
        json: async () => ({
          review_id: REVIEW_ID,
          status: 'DONE',
          decision: 'REQUEST_CHANGE',
          message: null,
          has_output: true,
          progress_stage: 'redline',
          created_at: createdAt,
        }),
      } as Response;
    }
    // eslint-disable-next-line @typescript-eslint/require-await
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return { polls: () => polls };
}

async function advance(ms: number): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
  for (let i = 0; i < 3; i += 1) {
    await vi.advanceTimersByTimeAsync(0);
  }
}

/** Advance past exactly the backoff step that follows failure `attempt`. */
async function nextAttempt(attempt: number): Promise<void> {
  await advance(nextPollDelayMs(0, attempt));
}

function consoleStatus(): string | null | undefined {
  return document.querySelector('.od-console')?.getAttribute('data-status');
}

describe('a run of server errors on the status poll (2026-10-28 production report)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    window.sessionStorage.clear();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  it('stops promising a reconnect, keeps retrying, and lands DONE once the route heals', async () => {
    allowConsoleErrorsInThisTest(/returned HTTP 500/);
    const healed = { current: false };
    const harness = stubServerErrors(healed);
    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await vi.runAllTimersAsync();
    await vi.waitFor(
      async () => {
        await vi.advanceTimersByTimeAsync(0);
        expect(screen.getByTestId('review-submit-button').getAttribute('aria-disabled')).toBe('false');
      },
      { interval: 0, timeout: 12_000 },
    );
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await vi.waitFor(
      async () => {
        await vi.advanceTimersByTimeAsync(0);
        expect(harness.polls()).toBe(1);
      },
      { interval: 0, timeout: 12_000 },
    );

    // Below the streak it is still an ordinary blip.
    await vi.waitFor(
      async () => {
        await vi.advanceTimersByTimeAsync(0);
        expect(screen.getByText(STILL_CHECKING)).toBeInTheDocument();
      },
      { interval: 0, timeout: 12_000 },
    );
    expect(screen.queryByText(SERVER_ERROR_COPY)).toBeNull();

    for (let attempt = 1; attempt < POLL_SERVER_ERROR_STREAK; attempt += 1) {
      await nextAttempt(attempt);
    }
    expect(harness.polls()).toBe(POLL_SERVER_ERROR_STREAK);
    expect(screen.getByText(SERVER_ERROR_COPY)).toBeInTheDocument();
    expect(screen.queryByText(STILL_CHECKING)).toBeNull();

    // Still transient (owner rule #122): the poller keeps going.
    await nextAttempt(POLL_SERVER_ERROR_STREAK);
    expect(harness.polls()).toBe(POLL_SERVER_ERROR_STREAK + 1);

    healed.current = true;
    await nextAttempt(POLL_SERVER_ERROR_STREAK + 1);
    await vi.waitFor(
      async () => {
        await vi.advanceTimersByTimeAsync(0);
        expect(consoleStatus()).toBe('done');
      },
      { interval: 0, timeout: 12_000 },
    );
    expect(screen.queryByText(SERVER_ERROR_COPY)).toBeNull();
  });
});
