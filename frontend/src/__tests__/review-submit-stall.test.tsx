/**
 * Issue #53 (audit finding F4) — a stalled upload must not pin the console
 * in "Submitting" forever.
 *
 * Before this change the multipart POST ran with no AbortController: a
 * captive portal or a connection that dropped mid-body left `submitting`
 * true with no way out but a reload. Now the submit runs under a
 * size-scaled budget (`submitTimeoutMs`), is abandoned when it runs out,
 * and the reviewer reads one fixed sentence (`UPLOAD_STALLED_COPY`) with
 * the lever armed again.
 *
 * Two layers:
 *
 *   1. The BUDGET. `submitTimeoutMs` at the issue's three points (0 B,
 *      10 MiB, 50 MiB) and the constants it is built from.
 *
 *   2. The COMPONENT. The real panel with fake timers and a POST stub that
 *      never settles on its own — the stall itself — advanced to one tick
 *      short of the budget (still submitting, no banner) and then past it
 *      (banner, lever re-armed, the signal handed to fetch is aborted). And
 *      the other branch: a POST that lands in time must NOT be reported as
 *      stalled when the clock later passes the budget — the timer is
 *      cleared, not merely outrun.
 *
 * Fully offline: aws-amplify/auth is mocked, fetch is stubbed per test.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import ReviewSubmission, {
  SUBMIT_TIMEOUT_BASE_MS,
  SUBMIT_TIMEOUT_PER_MIB_MS,
  UPLOAD_STALLED_COPY,
  submitTimeoutMs,
} from '../ReviewSubmission';
import { DEFAULT_PLAYBOOKS, submitArmed } from './support/consoleSurface';
import { allowConsoleErrorsInThisTest } from './support/consoleErrorGuard';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  }),
}));

const MIB = 1024 * 1024;

// ---------------------------------------------------------------------------
// 1. The budget
// ---------------------------------------------------------------------------

describe('submitTimeoutMs (issue #53)', () => {
  it('is 60 s plus 1 s per MiB of the file', () => {
    expect(SUBMIT_TIMEOUT_BASE_MS).toBe(60_000);
    expect(SUBMIT_TIMEOUT_PER_MIB_MS).toBe(1_000);
    expect(submitTimeoutMs(0)).toBe(60_000);
    expect(submitTimeoutMs(10 * MIB)).toBe(70_000);
    expect(submitTimeoutMs(50 * MIB)).toBe(110_000);
  });

  it('rounds a started MiB in the reviewer’s favour and never below the floor', () => {
    expect(submitTimeoutMs(1)).toBe(61_000);
    expect(submitTimeoutMs(10 * MIB + 1)).toBe(71_000);
    // A nonsensical size cannot shrink the budget under the floor.
    expect(submitTimeoutMs(-5)).toBe(60_000);
  });

  it('the copy names no status, path or duration', () => {
    expect(UPLOAD_STALLED_COPY).toBe(
      'The upload stalled before it finished. Nothing was submitted — check your connection and try again.',
    );
    expect(UPLOAD_STALLED_COPY).not.toMatch(/\d/);
    expect(UPLOAD_STALLED_COPY).not.toMatch(/\/api\//);
  });
});

// ---------------------------------------------------------------------------
// 2. The component
// ---------------------------------------------------------------------------

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

interface Harness {
  /** The `init` of each POST /api/reviews the panel issued. */
  posts: RequestInit[];
}

/**
 * `submitting === true` as the console shows it: while the upload is in
 * flight the lever is the STOP control (`review-cancel-button`), held
 * disabled until a review id exists — there is nothing to cancel yet — and
 * the start control is not on the page at all (`OrbitDiner.tsx`, the
 * lever's `working` branch).
 */
function stillSubmitting(): boolean {
  const stop = screen.queryByTestId('review-cancel-button');
  return (
    stop !== null &&
    stop.getAttribute('aria-disabled') === 'true' &&
    screen.queryByTestId('review-submit-button') === null
  );
}

/**
 * Render the panel with a catalog and a POST stub: `respond` is what the
 * stub does with the upload — `'stall'` returns a promise that never settles
 * on its own (a real fetch would reject it on abort; this one deliberately
 * ignores the signal, so the timeout must end the wait by itself), and
 * `'accept'` answers 202 straight away.
 */
async function panelWithUpload(respond: 'stall' | 'accept'): Promise<Harness> {
  const posts: RequestInit[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (pathname === '/api/playbooks') {
      return Promise.resolve({ ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response);
    }
    if (method === 'POST' && pathname === '/api/reviews') {
      posts.push(init ?? {});
      if (respond === 'stall') {
        return new Promise<Response>(() => {});
      }
      return Promise.resolve({
        ok: true,
        status: 202,
        json: async () => ({ review_id: 'rev-53', resumed: false }),
      } as Response);
    }
    if (pathname === '/api/reviews/rev-53') {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          review_id: 'rev-53',
          status: 'RUNNING',
          decision: null,
          message: null,
          has_output: false,
        }),
      } as Response);
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) } as Response);
  });
  vi.stubGlobal('fetch', fetchMock);

  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  // Land the playbook catalog before the submit — no submit timer exists
  // yet, so draining the timers here terminates.
  await vi.runAllTimersAsync();
  expect(submitArmed()).toBe(true);
  fireEvent.click(screen.getByTestId('review-submit-button'));
  // Let the click's synchronous state land without advancing the clock.
  await vi.advanceTimersByTimeAsync(0);
  return { posts };
}

describe('the rendered submit abandons a stalled upload (issue #53)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('shows UPLOAD_STALLED_COPY and re-arms the lever once the budget runs out', async () => {
    // Abandoning the stalled upload is what this test provokes, and the
    // abort is logged with its budget and file size (issue #68).
    allowConsoleErrorsInThisTest(/POST \/api\/reviews abandoned after \d+ ms/);
    const h = await panelWithUpload('stall');
    const budget = submitTimeoutMs(docxFile().size);
    expect(h.posts).toHaveLength(1);
    // The signal actually reached the fetch — a real fetch would drop the
    // connection on abort, not just stop being awaited.
    const signal = h.posts[0].signal;
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal!.aborted).toBe(false);

    // Mid-upload: submitting, no banner, no start control to press.
    expect(stillSubmitting()).toBe(true);
    expect(screen.queryByTestId('review-submit-error')).toBeNull();

    // One tick short of the budget is still a live upload.
    await vi.advanceTimersByTimeAsync(budget - 1);
    expect(screen.queryByTestId('review-submit-error')).toBeNull();
    expect(stillSubmitting()).toBe(true);
    expect(signal!.aborted).toBe(false);

    // The budget runs out. (Synchronous queries only from here: under
    // vitest's fake timers testing-library's `findBy*`/`waitFor` poll on the
    // faked clock and never wake — the advance above already flushed the
    // abort, the rejection and the resulting render.)
    await vi.advanceTimersByTimeAsync(1);
    expect(signal!.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(0);
    const banner = screen.getByTestId('review-submit-error');
    expect(banner.textContent).toContain(UPLOAD_STALLED_COPY);
    // `submitting` is false again: the start control is back and armed for
    // a retry, and the stop control that stood in for it is gone.
    expect(stillSubmitting()).toBe(false);
    expect(submitArmed()).toBe(true);
    // Nothing was submitted, so nothing is polled: one POST, no review id.
    expect(h.posts).toHaveLength(1);
  });

  it('a POST that lands in time is never reported as stalled afterwards', async () => {
    const h = await panelWithUpload('accept');
    expect(h.posts).toHaveLength(1);
    expect(h.posts[0].signal!.aborted).toBe(false);
    // Well past the budget: the timer was cleared, not merely outrun.
    await vi.advanceTimersByTimeAsync(submitTimeoutMs(docxFile().size) + 5_000);
    expect(h.posts[0].signal!.aborted).toBe(false);
    expect(screen.queryByTestId('review-submit-error')).toBeNull();
  });
});
