/**
 * download-quota-429.test.tsx — issue #102.
 *
 * A download route answers HTTP 429 when the caller has spent the per-user
 * DAILY limit (`MAX_DAILY_REVIEWS`, backend/src/download.py). That refusal was
 * rendered through `friendlyDownloadError` as "an admin needs to check the
 * deployment's storage settings" — a true failure with an untrue
 * explanation. The reviewer must instead be told the daily limit is the cause.
 *
 * Also pinned here: the completion-time auto-save asks the server for the
 * uncharged path (`?autosave=1`) and a user's own click does not, and a 409
 * (the review's single auto-save already used) is not shown as a failure.
 *
 * Driven end to end through the real ReviewSubmission with a stubbed `fetch`.
 * Every wait is on awaited state (testing-library `waitFor` / `findBy*`,
 * which poll until a real-time deadline) — never a bounded iteration count.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

import ReviewSubmission from '../ReviewSubmission';
import { DOWNLOAD_ERROR_COPY, DOWNLOAD_QUOTA_COPY } from '../api';
import { allowConsoleErrorsInThisTest } from './support/consoleErrorGuard';
import { DEFAULT_PLAYBOOKS, findReviewResult, pressSubmit } from './support/consoleSurface';

vi.mock('aws-amplify/auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const PRESIGNED_URL = 'https://s3.example.test/outputs/rev-102/out.docx?sig=abc';
const QUOTA_DETAIL = 'Per-user limit exceeded: max 20 download requests per day.';

type OutputAnswer = (isAutosave: boolean) => { status: number; body: unknown };

function stubFetch(output: OutputAnswer): ReturnType<typeof vi.fn> {
  // eslint-disable-next-line @typescript-eslint/require-await
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    // eslint-disable-next-line @typescript-eslint/no-base-to-string
    const url = new URL(String(input), 'http://localhost');
    const method = (init?.method ?? 'GET').toUpperCase();
    let status = 200;
    let body: unknown;
    if (url.pathname === '/api/playbooks') {
      body = DEFAULT_PLAYBOOKS;
    } else if (method === 'POST' && url.pathname === '/api/reviews') {
      body = { review_id: 'rev-102', resumed: false };
    } else if (url.pathname === '/api/reviews/rev-102') {
      body = {
        review_id: 'rev-102',
        status: 'DONE',
        decision: 'REQUEST_CHANGE',
        message: null,
        has_output: true,
      };
    } else if (url.pathname === '/api/reviews/rev-102/output') {
      ({ status, body } = output(url.searchParams.get('autosave') === '1'));
    } else {
      status = 404;
      body = {};
    }
    return {
      ok: status >= 200 && status < 300,
      status,
      // eslint-disable-next-line @typescript-eslint/require-await
      json: async () => body,
    } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function outputCalls(fetchMock: ReturnType<typeof vi.fn>): URL[] {
  return fetchMock.mock.calls
    .map(([input]) => new URL(String(input), 'http://localhost'))
    .filter((u) => u.pathname === '/api/reviews/rev-102/output');
}

async function submitAndReachResult(): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: {
      files: [
        new File(['contents'], 'contract.docx', {
          type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        }),
      ],
    },
  });
  await pressSubmit();
  await findReviewResult();
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('download 429 — the daily limit is named, not a storage fault', () => {
  it('tells the reviewer the daily limit was reached when their Save click is refused', async () => {
    const fetchMock = stubFetch((isAutosave) =>
      isAutosave
        ? { status: 200, body: { url: PRESIGNED_URL, expires_in: 60 } }
        : { status: 429, body: { detail: QUOTA_DETAIL } },
    );

    await submitAndReachResult();
    // The auto-save is uncharged and has succeeded; only the click is refused.
    await waitFor(() => expect(outputCalls(fetchMock).some((u) => u.searchParams.has('autosave'))).toBe(true));

    fireEvent.click(await screen.findByTestId('review-download-button'));

    const banner = await screen.findByTestId('review-download-error');
    expect(banner).toHaveTextContent(DOWNLOAD_QUOTA_COPY);
    expect(banner.textContent ?? '').toMatch(/daily|today'?s download limit/i);
    expect(banner.textContent ?? '').not.toContain('storage settings');
    expect(banner).not.toHaveTextContent(DOWNLOAD_ERROR_COPY);
    // Escaped, fixed copy only: the server's raw detail never reaches the screen.
    expect(banner.textContent ?? '').not.toContain('Per-user limit exceeded');
  });

  it('still reports a genuine 503 as the storage fault, not as a quota', async () => {
    // friendlyDownloadError logs the raw detail to the console on purpose.
    allowConsoleErrorsInThisTest(/Unable to generate presigned URL/);
    stubFetch((isAutosave) =>
      isAutosave
        ? { status: 200, body: { url: PRESIGNED_URL, expires_in: 60 } }
        : { status: 503, body: { detail: 'Unable to generate presigned URL.' } },
    );

    await submitAndReachResult();
    fireEvent.click(await screen.findByTestId('review-download-button'));

    const banner = await screen.findByTestId('review-download-error');
    expect(banner).toHaveTextContent(DOWNLOAD_ERROR_COPY);
    expect(banner).not.toHaveTextContent(DOWNLOAD_QUOTA_COPY);
  });
});

describe('completion-time auto-save — the uncharged path (issue #102)', () => {
  it('asks for the uncharged path on completion and not on the reviewer\'s own click', async () => {
    const fetchMock = stubFetch(() => ({ status: 200, body: { url: PRESIGNED_URL, expires_in: 60 } }));

    await submitAndReachResult();
    await waitFor(() => expect(outputCalls(fetchMock)).toHaveLength(1));
    expect(outputCalls(fetchMock)[0].searchParams.get('autosave')).toBe('1');

    fireEvent.click(await screen.findByTestId('review-download-button'));
    await waitFor(() => expect(outputCalls(fetchMock)).toHaveLength(2));
    expect(outputCalls(fetchMock)[1].searchParams.has('autosave')).toBe(false);
  });

  it('does not show a failure when the single auto-save was already used (409)', async () => {
    const fetchMock = stubFetch((isAutosave) =>
      isAutosave
        ? { status: 409, body: { detail: 'The automatic save was already used for this review.' } }
        : { status: 200, body: { url: PRESIGNED_URL, expires_in: 60 } },
    );

    await submitAndReachResult();
    await waitFor(() => expect(outputCalls(fetchMock)).toHaveLength(1));
    // Positive signal before asserting an absence: the auto-save's own fetch
    // has resolved, and one real macrotask inside act() lets every promise
    // hop after it (the json read and friendly-error path the 409 branch
    // short-circuits, and the resulting state update) run and commit. This
    // waits on awaited state, not on a count of flushes, so a slow runner
    // cannot make it pass vacuously.
    const autosaveIndex = fetchMock.mock.calls.findIndex(([input]) =>
      new URL(String(input), 'http://localhost').searchParams.get('autosave') === '1',
    );
    await fetchMock.mock.results[autosaveIndex].value;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.queryByTestId('review-download-error')).toBeNull();
  });
});
