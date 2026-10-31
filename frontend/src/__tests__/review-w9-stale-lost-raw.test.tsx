/**
 * review-w9-stale-lost-raw.test.tsx — QA 2026-10-31, workstream W9.
 *
 * The Review tab's state owner (`ReviewSubmission.tsx`) against the frontend
 * logic findings:
 *
 *   1. a cover note, disposition or cancel answered for review A after B is
 *      on screen is dropped (top item #15);
 *   2. a poll that gave up for good leaves a `lost` review: no Stop, the
 *      intake back, Run again and the lever both work, a fixed explanation;
 *   3. no raw error text — a rejected fetch, an unparseable body and a 5xx
 *      `detail` all render fixed copy;
 *   4. a reset clears the cancel, disposition and poll channels;
 *   5. races: Run again re-checks after its read, preference PUTs are
 *      sequenced, reload-resume reads only the newest row;
 *   6. a redline answer with no `url` is a friendly failure, never a download
 *      of "/undefined".
 *
 * Fully offline: auth is mocked and `fetch` is a per-test router whose
 * handlers can hold a response back (`deferred`) to stage the race.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { DOWNLOAD_ERROR_COPY } from '../api';
import { requestRunAgain, resetRunAgainRequestForTests } from '../runAgainRequest';
import {
  DEFAULT_PLAYBOOKS,
  butterIt,
  findReviewResult,
  openDisposition,
  pressSubmit,
} from './support/consoleSurface';
import { allowConsoleErrorsInThisTest } from './support/consoleErrorGuard';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

type Handler = (init: RequestInit | undefined, call: number) => Response | Promise<Response>;

function json(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    // eslint-disable-next-line @typescript-eslint/require-await
    json: async () => body,
  } as Response;
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

interface Router {
  calls: string[];
  countOf: (key: string) => number;
}

/** `routes` keys are `METHOD /path` (or a bare path for any method). */
function stubRoutes(routes: Record<string, Handler>): Router {
  const calls: string[] = [];
  const counts = new Map<string, number>();
  const all: Record<string, Handler> = {
    'GET /api/playbooks': () => json(DEFAULT_PLAYBOOKS),
    'GET /api/reviews': () => json({ reviews: [] }),
    ...routes,
  };
  const impl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    // eslint-disable-next-line @typescript-eslint/no-base-to-string
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}`;
    calls.push(key);
    counts.set(key, (counts.get(key) ?? 0) + 1);
    if (pathname.endsWith('.mp3')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        // eslint-disable-next-line @typescript-eslint/require-await
        arrayBuffer: async () => new ArrayBuffer(8),
      } as Response);
    }
    const handler = all[key] ?? all[pathname];
    if (!handler) {
      return Promise.resolve(json({}, 404));
    }
    return Promise.resolve(handler(init, counts.get(key) ?? 1));
  });
  vi.stubGlobal('fetch', impl);
  return { calls, countOf: (key) => counts.get(key) ?? 0 };
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

const REVIEW_A = '11111111-1111-4111-8111-111111111111';
const REVIEW_B = '22222222-2222-4222-8222-222222222222';
const SOURCE = '33333333-3333-4333-8333-333333333333';

function doneDetail(reviewId: string): Record<string, unknown> {
  return {
    review_id: reviewId,
    status: 'DONE',
    decision: 'REQUEST_CHANGE',
    message: null,
    has_output: true,
    // Holds the automatic save back (the output-contract download gate), so
    // these cases see only the fetches they stage themselves.
    critic_delta: { content_dings: 1, dings: [{ kind: 'content' }] },
  };
}

function runningDetail(reviewId: string): Record<string, unknown> {
  return { review_id: reviewId, status: 'RUNNING', decision: null, message: null, has_output: false };
}

function chooseFile(): void {
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
}

function consoleStatus(): string | null | undefined {
  return document.querySelector('.od-console')?.getAttribute('data-status');
}

/** Cmd/Ctrl+Enter — the panel's own submit chord, which works from a finished review. */
function resubmit(): void {
  fireEvent.keyDown(window, { key: 'Enter', metaKey: true });
}

// Web Storage (the reload-resume key among it) is cleared between tests by
// setupTests.ts; this file touches none of it itself.
beforeEach(() => {
  resetRunAgainRequestForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 1. Answers for a review that is no longer on screen
// ---------------------------------------------------------------------------

describe('W9-1: an answer for review A never lands on review B', () => {
  it('a cover note started on A and resolved after B is on screen is dropped, and B butters its own', async () => {
    const coverForA = deferred<Response>();
    let submits = 0;
    const router = stubRoutes({
      'POST /api/reviews': () => {
        submits += 1;
        return json({ review_id: submits === 1 ? REVIEW_A : REVIEW_B, resumed: false });
      },
      [`GET /api/reviews/${REVIEW_A}`]: () => json(doneDetail(REVIEW_A)),
      [`GET /api/reviews/${REVIEW_B}`]: () => json(doneDetail(REVIEW_B)),
      [`POST /api/reviews/${REVIEW_A}/cover-note`]: () => coverForA.promise,
      [`POST /api/reviews/${REVIEW_B}/cover-note`]: () =>
        json({ review_id: REVIEW_B, draft: 'Draft for B.', cost_usd_cents: 1, cached: false }),
    });

    render(<ReviewSubmission />);
    chooseFile();
    await pressSubmit();
    await findReviewResult();

    // Butter A, and hold its answer back.
    await butterIt();
    await waitFor(() =>
      expect(router.countOf(`POST /api/reviews/${REVIEW_A}/cover-note`)).toBe(1),
    );

    // B replaces A on screen while A's draft is still being written.
    resubmit();
    await waitFor(() => expect(router.countOf(`GET /api/reviews/${REVIEW_B}`)).toBeGreaterThan(0));
    await waitFor(() => expect(consoleStatus()).toBe('done'));

    // A's draft arrives late.
    await act(async () => {
      coverForA.resolve(
        json({ review_id: REVIEW_A, draft: 'Draft for A.', cost_usd_cents: 2, cached: false }),
      );
      await coverForA.promise;
    });

    // Never shown, never copyable on B.
    expect(screen.queryByText('Draft for A.')).toBeNull();
    expect(screen.queryByTestId('review-cover-note-text')).toBeNull();

    // And B's own key is live and asks for B's note, not A's.
    await butterIt();
    await waitFor(() =>
      expect(router.countOf(`POST /api/reviews/${REVIEW_B}/cover-note`)).toBe(1),
    );
    expect(router.countOf(`POST /api/reviews/${REVIEW_A}/cover-note`)).toBe(1);
    expect((await screen.findByTestId('review-cover-note-text')).textContent).toBe('Draft for B.');
  });

  it('a disposition failure for A that lands after B is on screen raises no alert on B', async () => {
    allowConsoleErrorsInThisTest(/returned HTTP 500/, /boom/);
    const dispositionForA = deferred<Response>();
    let submits = 0;
    stubRoutes({
      'POST /api/reviews': () => {
        submits += 1;
        return json({ review_id: submits === 1 ? REVIEW_A : REVIEW_B, resumed: false });
      },
      [`GET /api/reviews/${REVIEW_A}`]: () => json(doneDetail(REVIEW_A)),
      [`GET /api/reviews/${REVIEW_B}`]: () => json(doneDetail(REVIEW_B)),
      [`POST /api/reviews/${REVIEW_A}/disposition`]: () => dispositionForA.promise,
    });

    render(<ReviewSubmission />);
    chooseFile();
    await pressSubmit();
    await findReviewResult();

    const region = await openDisposition();
    fireEvent.click(region.querySelector('button') as HTMLButtonElement);

    resubmit();
    await waitFor(() => expect(consoleStatus()).toBe('done'));

    await act(async () => {
      dispositionForA.resolve(json({ detail: 'boom' }, 500));
      await dispositionForA.promise;
    });

    expect(screen.queryByTestId('review-disposition-error')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 2. The lost state
// ---------------------------------------------------------------------------

describe('W9-2: a poll that gave up for good leaves a lost review, not a running one', () => {
  it('410: no Stop, the intake is back, the explanation is fixed, and Run again and the lever both work', async () => {
    allowConsoleErrorsInThisTest(/returned HTTP 410/);
    const router = stubRoutes({
      'POST /api/reviews': () => json({ review_id: REVIEW_A, resumed: false }),
      [`GET /api/reviews/${REVIEW_A}`]: () => json({ detail: 'gone' }, 410),
      [`GET /api/reviews/${SOURCE}`]: () =>
        json({
          review_id: SOURCE,
          status: 'ERROR',
          playbook_id: 'nda',
          markup_intensity: 'heavy',
          notes_mode: 'external',
          toaster_guidance: 'Keep it short.',
        }),
    });

    render(<ReviewSubmission />);
    chooseFile();
    await pressSubmit();

    const stopped = await screen.findByTestId('review-poll-stopped');
    expect(stopped.textContent).toContain('no longer being followed');
    expect(stopped.textContent).not.toMatch(/HTTP|410|gone/);

    // Not working: the lever is Start again, there is no Stop, the intake is
    // back, and the tab chrome has stopped telling a running story.
    expect(screen.queryByTestId('review-cancel-button')).toBeNull();
    expect(screen.getByTestId('review-submit-button')).toBeInTheDocument();
    expect(screen.getByTestId('review-intake-slot').hidden).toBe(false);
    expect(consoleStatus()).toBe('loaded');
    expect(document.title).not.toMatch(/Toasting/);

    // Run again is not refused as "still running".
    act(() => requestRunAgain(SOURCE));
    expect(await screen.findByTestId('review-prefill-note')).toBeInTheDocument();
    expect(screen.queryByTestId('review-prefill-error')).toBeNull();
    // The reset Run again performs also drops the give-up message.
    expect(screen.queryByTestId('review-poll-stopped')).toBeNull();

    // And the lever starts a new review.
    chooseFile();
    await pressSubmit();
    await waitFor(() => expect(router.countOf('POST /api/reviews')).toBe(2));
  });
});

// ---------------------------------------------------------------------------
// 3. No raw error text
// ---------------------------------------------------------------------------

describe('W9-3: a failure renders fixed copy, never its own text', () => {
  it('a rejected submit fetch shows the fixed sentence, not the browser\'s "Failed to fetch"', async () => {
    allowConsoleErrorsInThisTest(/Failed to fetch/);
    stubRoutes({
      'POST /api/reviews': () => {
        throw new TypeError('Failed to fetch');
      },
    });
    render(<ReviewSubmission />);
    chooseFile();
    await pressSubmit();
    const banner = await screen.findByTestId('review-submit-error');
    expect(banner.textContent).toContain("We couldn't submit your file for review.");
    expect(banner.textContent).not.toContain('Failed to fetch');
  });

  it('a 503 submit detail naming server configuration is not shown', async () => {
    allowConsoleErrorsInThisTest(/UPLOADS_BUCKET/);
    stubRoutes({
      'POST /api/reviews': () => json({ detail: 'UPLOADS_BUCKET not configured.' }, 503),
    });
    render(<ReviewSubmission />);
    chooseFile();
    await pressSubmit();
    const banner = await screen.findByTestId('review-submit-error');
    expect(banner.textContent).toContain("We couldn't submit your file for review.");
    expect(banner.textContent).not.toContain('UPLOADS_BUCKET');
  });

  it('a cover-note answer that is not JSON shows the fixed sentence, not the parser\'s message', async () => {
    allowConsoleErrorsInThisTest(/Unexpected token/);
    stubRoutes({
      'POST /api/reviews': () => json({ review_id: REVIEW_A, resumed: false }),
      [`GET /api/reviews/${REVIEW_A}`]: () => json(doneDetail(REVIEW_A)),
      [`POST /api/reviews/${REVIEW_A}/cover-note`]: () =>
        ({
          ok: true,
          status: 200,
          // eslint-disable-next-line @typescript-eslint/require-await
          json: async () => {
            throw new SyntaxError('Unexpected token < in JSON at position 0: <html>proxy</html>');
          },
        }) as unknown as Response,
    });
    render(<ReviewSubmission />);
    chooseFile();
    await pressSubmit();
    await findReviewResult();
    await butterIt();
    const error = await screen.findByTestId('review-cover-note-real-error');
    expect(error.textContent).not.toMatch(/Unexpected token|<html>|proxy/);
    expect(error.textContent).toContain("We couldn't load that.");
  });
});

// ---------------------------------------------------------------------------
// 4. Reset clears every channel
// ---------------------------------------------------------------------------

describe('W9-4: a reset clears the cancel, disposition and poll channels', () => {
  it('a disposition error does not survive the reset Run again performs', async () => {
    allowConsoleErrorsInThisTest(/returned HTTP 500/, /boom/);
    stubRoutes({
      'POST /api/reviews': () => json({ review_id: REVIEW_A, resumed: false }),
      [`GET /api/reviews/${REVIEW_A}`]: () => json(doneDetail(REVIEW_A)),
      [`POST /api/reviews/${REVIEW_A}/disposition`]: () => json({ detail: 'boom' }, 500),
      [`GET /api/reviews/${SOURCE}`]: () =>
        json({ review_id: SOURCE, status: 'DONE', playbook_id: 'nda' }),
    });

    render(<ReviewSubmission />);
    chooseFile();
    await pressSubmit();
    await findReviewResult();
    const region = await openDisposition();
    fireEvent.click(region.querySelector('button') as HTMLButtonElement);
    expect(await screen.findByTestId('review-disposition-error')).toBeInTheDocument();

    act(() => requestRunAgain(SOURCE));
    expect(await screen.findByTestId('review-prefill-note')).toBeInTheDocument();
    expect(screen.queryByTestId('review-disposition-error')).toBeNull();
  });

  it('a cancel error does not survive "Toast another slice"', async () => {
    allowConsoleErrorsInThisTest(/returned HTTP 500/, /boom/);
    stubRoutes({
      'POST /api/reviews': () => json({ review_id: REVIEW_A, resumed: false }),
      [`GET /api/reviews/${REVIEW_A}`]: (_init, call) =>
        json(
          call === 1
            ? runningDetail(REVIEW_A)
            : { ...runningDetail(REVIEW_A), status: 'ERROR', failure_reason: 'unhandled_exception' },
        ),
      [`POST /api/reviews/${REVIEW_A}/cancel`]: () => json({}, 500),
    });

    render(<ReviewSubmission />);
    chooseFile();
    await pressSubmit();
    fireEvent.click(await screen.findByTestId('review-cancel-button'));
    expect(await screen.findByTestId('review-cancel-error')).toBeInTheDocument();

    // The next poll burns it.
    fireEvent.click(await screen.findByTestId('review-retry-button', {}, { timeout: 8000 }));
    await waitFor(() => expect(screen.queryByTestId('review-cancel-error')).toBeNull());
  }, 15_000);
});

// ---------------------------------------------------------------------------
// 5. Races
// ---------------------------------------------------------------------------

describe('W9-5: races', () => {
  it('Run again does not detach a review that started while its settings read was out', async () => {
    const sourceRead = deferred<Response>();
    stubRoutes({
      [`GET /api/reviews/${SOURCE}`]: () => sourceRead.promise,
      'POST /api/reviews': () => json({ review_id: REVIEW_A, resumed: false }),
      [`GET /api/reviews/${REVIEW_A}`]: () => json(runningDetail(REVIEW_A)),
    });

    render(<ReviewSubmission />);
    chooseFile();
    // Idle when pressed, so the first check lets it through…
    act(() => requestRunAgain(SOURCE));
    // …and a review starts while the read is out.
    await pressSubmit();
    await waitFor(() => expect(consoleStatus()).toBe('running'));

    await act(async () => {
      sourceRead.resolve(json({ review_id: SOURCE, status: 'DONE', playbook_id: 'nda' }));
      await sourceRead.promise;
    });

    // Still attached, still running, and the reviewer is told why nothing changed.
    expect(consoleStatus()).toBe('running');
    expect(screen.getByTestId('review-cancel-button')).toBeInTheDocument();
    expect((await screen.findByTestId('review-prefill-error')).textContent).toContain(
      'still running',
    );
  });

  it('preference PUTs go out one at a time, and a superseded failure raises no alert', async () => {
    allowConsoleErrorsInThisTest(/returned HTTP 500/, /boom/);
    const first = deferred<Response>();
    const bodies: string[] = [];
    let puts = 0;
    stubRoutes({
      'PUT /api/me/preferences': (init) => {
        puts += 1;
        bodies.push(typeof init?.body === 'string' ? init.body : '');
        return puts === 1 ? first.promise : json({});
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-notes-mode-control');
    fireEvent.keyDown(window, { key: '4' }); // none
    fireEvent.keyDown(window, { key: '2' }); // external

    // The second choice waits for the first to be answered.
    await waitFor(() => expect(puts).toBe(1));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(puts).toBe(1);

    await act(async () => {
      first.resolve(json({}, 500));
      await first.promise;
    });
    await waitFor(() => expect(puts).toBe(2));
    expect(bodies[0]).toContain('"none"');
    expect(bodies[1]).toContain('"external"');
    // The failed PUT was for a value the reviewer has already moved off.
    expect(screen.queryByTestId('review-notes-mode-save-error')).toBeNull();
  });

  it('reload-resume attaches only when the NEWEST row is still running', async () => {
    const router = stubRoutes({
      'GET /api/reviews': () =>
        json({
          reviews: [
            { review_id: REVIEW_B, status: 'DONE' },
            { review_id: REVIEW_A, status: 'RUNNING' },
          ],
        }),
      [`GET /api/reviews/${REVIEW_A}`]: () => json(runningDetail(REVIEW_A)),
    });

    render(<ReviewSubmission />);
    await waitFor(() => expect(router.countOf('GET /api/reviews')).toBe(1));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(router.countOf(`GET /api/reviews/${REVIEW_A}`)).toBe(0);
    expect(['empty', 'loaded']).toContain(consoleStatus());
  });
});

// ---------------------------------------------------------------------------
// 6. Redline without a url
// ---------------------------------------------------------------------------

describe('W9-6: a redline answer with no url', () => {
  it('shows the friendly download error and never points an anchor at "undefined"', async () => {
    allowConsoleErrorsInThisTest(/returned no url/);
    const clicks: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicks.push(this.getAttribute('href') ?? '');
    });
    stubRoutes({
      'POST /api/reviews': () => json({ review_id: REVIEW_A, resumed: false }),
      [`GET /api/reviews/${REVIEW_A}`]: () => json(doneDetail(REVIEW_A)),
      [`GET /api/reviews/${REVIEW_A}/output`]: () => json({}),
    });

    render(<ReviewSubmission />);
    chooseFile();
    await pressSubmit();
    await findReviewResult();
    fireEvent.click(screen.getAllByTestId('review-download-button')[0]);

    expect((await screen.findByTestId('review-download-error')).textContent).toContain(
      DOWNLOAD_ERROR_COPY,
    );
    expect(clicks).toEqual([]);
  });
});
