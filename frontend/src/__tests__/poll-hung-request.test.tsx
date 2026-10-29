/**
 * poll-hung-request.test.tsx — one poll that never answers must not freeze
 * the Review screen.
 *
 * ## Why (hardening from the 2026-10-28 production report)
 *
 * That report — "Stage not yet reported" for a whole 153 s review and
 * "Toasting…" past DONE — turned out to be a run of HTTP 500s
 * (poll-server-errors.test.tsx, tests/test_review_detail_decimal.py). Reading
 * the loop for it found a second way to the same frozen screen, with no error
 * shown at all, which this file closes.
 *
 * The poll loop keeps exactly ONE attempt in flight and schedules the next
 * only when that attempt settles (`ReviewSubmission.tsx`'s poll effect). The
 * attempt had no deadline, so an attempt that never settled — a stalled
 * connection at the edge, a proxy holding the request, a backend that stopped
 * answering — stopped polling for good with no error to show, and the screen
 * stayed on whatever the last good poll said. If the very first poll is the
 * one that hangs, that is "no stage, ever".
 *
 * The fake below hangs the first status GET forever and IGNORES its abort
 * signal, so the test proves the loop recovers by its own deadline
 * (`POLL_REQUEST_TIMEOUT_MS`) rather than by the platform honouring an abort.
 * It also pins `cache: 'no-store'` on the poll, the client half of the
 * "a cached poll is a frozen poll" rule (the server half is
 * tests/test_review_api_no_store.py).
 *
 * Fully offline: auth is mocked and fetch is stubbed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import ReviewSubmission, {
  POLL_INTERVAL_MS,
  POLL_REQUEST_TIMEOUT_MS,
} from '../ReviewSubmission';
import { DEFAULT_PLAYBOOKS } from './support/consoleSurface';
import { allowConsoleErrorsInThisTest } from './support/consoleErrorGuard';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const REVIEW_ID = '3b0f7a52-6c1e-4d9a-8f27-5e4c2a1d9b60';
const DETAIL_PATH = `/api/reviews/${REVIEW_ID}`;

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

/** Status GETs: the first never settles; every later one is DONE. */
function stubHungFirstPoll(): { polls: () => number; pollInits: () => Array<RequestInit | undefined> } {
  let polls = 0;
  const pollInits: Array<RequestInit | undefined> = [];
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
      pollInits.push(init);
      if (polls === 1) {
        return new Promise<Response>(() => undefined);
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
  return { polls: () => polls, pollInits: () => pollInits };
}

async function advance(ms: number): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
  for (let i = 0; i < 3; i += 1) {
    await vi.advanceTimersByTimeAsync(0);
  }
}

function consoleStatus(): string | null | undefined {
  return document.querySelector('.od-console')?.getAttribute('data-status');
}

describe('a poll that never answers', () => {
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

  it('is abandoned at its deadline and the next poll lands the DONE review', async () => {
    // The abandoned attempt is logged as the technical detail behind the
    // transient "Still checking…" copy; that log is the point, not a fault.
    allowConsoleErrorsInThisTest(/timed out/);
    const harness = stubHungFirstPoll();
    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await vi.runAllTimersAsync();
    // Same "wait for the armed lever by awaited state" rule as
    // poll-gives-up-122.test.tsx — the click is a no-op until it holds.
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

    // The hung attempt: the screen is still waiting, and nothing has been
    // re-sent yet — the deadline, not a second loop, is what recovers it.
    await advance(POLL_REQUEST_TIMEOUT_MS - 1);
    expect(harness.polls()).toBe(1);
    // No poll has answered, so the console shows the submitted-but-unreported
    // state — the "Stage not yet reported" the report describes.
    expect(consoleStatus()).toBe('pending');

    // Deadline passes -> transient failure -> one backoff step -> poll 2.
    await advance(1 + POLL_INTERVAL_MS);
    expect(harness.polls()).toBe(2);
    await vi.waitFor(
      async () => {
        await vi.advanceTimersByTimeAsync(0);
        expect(consoleStatus()).toBe('done');
      },
      { interval: 0, timeout: 12_000 },
    );

    // Terminal: the loop stops rather than polling a finished review.
    await advance(10 * POLL_INTERVAL_MS);
    expect(harness.polls()).toBe(2);
  });

  it('asks for every status poll uncached', async () => {
    // The abandoned attempt is logged as the technical detail behind the
    // transient "Still checking…" copy; that log is the point, not a fault.
    allowConsoleErrorsInThisTest(/timed out/);
    const harness = stubHungFirstPoll();
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
    await advance(POLL_REQUEST_TIMEOUT_MS + POLL_INTERVAL_MS);
    expect(harness.polls()).toBe(2);
    for (const init of harness.pollInits()) {
      expect(init?.cache).toBe('no-store');
    }
  });
});
