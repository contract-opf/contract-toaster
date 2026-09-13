/**
 * review-resume-on-reload-58.test.tsx — issue #58 (audit finding F8/A8 in
 * `docs/reports/2026-09-05-audit-hardening-diagnostic.md`).
 *
 * A reload during a ten-minute review used to drop the panel: `reviewId`
 * lived in React state alone. Issue #489 softened that with a
 * `GET /api/reviews?scope=mine` probe that attaches to the caller's NEWEST
 * non-terminal row; #58 puts the exact review THIS TAB submitted in front of
 * it, remembered for the life of the tab in `sessionStorage`
 * (`inflightReview.ts`, key `ct:inflight-review-id`).
 *
 * What is pinned here:
 *   1. The producer: a real submission through the console writes the key,
 *      and nothing writes it before one. The seeded ids below are the same
 *      uuid4 shape `backend/src/review_routes.py`'s `post_review` mints
 *      (`str(uuid.uuid4())`) and hands back on the 202 — the only shape that
 *      can ever reach this key in production.
 *   2. The resume: a seeded id whose detail is RUNNING attaches and polls
 *      THAT id, and the console shows the in-flight review rather than the
 *      idle form — including the working stage, which the console derives
 *      from `detail.progress_stage` (orbit-diner/projection.ts) and not from
 *      a submit event, so a resumed review animates exactly like a fresh one
 *      (issue #58 item 3, verify-only).
 *   3. The forget paths: terminal, 404 and a non-UUID stored value each drop
 *      the key and leave the idle form, falling through to #489's listing
 *      probe rather than replacing it.
 *
 * Fully offline: aws-amplify/auth and @aws-amplify/ui-react are mocked, fetch
 * is stubbed per test, jsdom supplies sessionStorage; no live network.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { INFLIGHT_REVIEW_STORAGE_KEY } from '../inflightReview';
import {
  DEFAULT_PLAYBOOKS,
  expectNoReviewAttached,
  findReviewResult,
  openReceipt,
  pressSubmit,
} from './support/consoleSurface';

vi.mock('aws-amplify/auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({
    user: { username: 'user-sub', signInDetails: { loginId: 'user@example.com' } },
    signOut: vi.fn(),
  }),
}));

/**
 * The id the server minted for the review this tab was watching. Canonical
 * `str(uuid.uuid4())` output — the ONLY thing `writeInflightReviewId` is ever
 * called with in production (ReviewSubmission.tsx, on the 202 from
 * `POST /api/reviews`).
 */
const RESUMED_ID = '3b1f5c78-6d2a-4f19-9c47-0ae8d5b31f62';
/** A second, unrelated review the account has running in ANOTHER tab — the
 *  newest row, and therefore the one issue #489's listing probe would pick.
 *  Present so "the stored id wins" is an assertion, not a coincidence. */
const OTHER_TAB_ID = 'd41a9e05-7c3b-4a80-b2e6-58f0c9d7a134';

/**
 * Routes by "METHOD path" (falling back to path-only for GETs), and lets a
 * route answer with an explicit status so a 404 can be expressed as the real
 * thing rather than as an absent route.
 */
// eslint-disable-next-line @typescript-eslint/no-redundant-type-constituents
type Route = unknown | { __status: number };

function stubFetch(routes: Record<string, Route>): ReturnType<typeof vi.fn> {
  // eslint-disable-next-line @typescript-eslint/require-await
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    // eslint-disable-next-line @typescript-eslint/no-base-to-string
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const body = routes[key];
    if (body === undefined) {
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    if (body && typeof body === 'object' && '__status' in (body as Record<string, unknown>)) {
      const status = (body as { __status: number }).__status;
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: false, status, json: async () => ({}) } as Response;
    }
    // eslint-disable-next-line @typescript-eslint/require-await
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

/** The stored id's own detail route — one status, one progress stage. */
function detailFor(reviewId: string, status: string, progressStage: string | null = null) {
  return {
    review_id: reviewId,
    status,
    decision: status === 'DONE' ? 'ACCEPT' : null,
    message: null,
    has_output: false,
    progress_stage: progressStage,
  };
}

/** Did anything poll this review id? */
function polled(fetchMock: ReturnType<typeof vi.fn>, reviewId: string): boolean {
  return fetchMock.mock.calls.some(([input]) => {
    const pathname = new URL(String(input), 'http://localhost').pathname;
    return pathname === `/api/reviews/${reviewId}`;
  });
}

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

// ---------------------------------------------------------------------------
// The producer. Nothing else in the app writes this key, so if a submission
// does not, everything below is asserting over a state production cannot
// reach.
// ---------------------------------------------------------------------------
describe('the in-flight id is written by a real submission, and only by one', () => {
  it('a successful submit stores the id the server returned; a bare mount stores nothing', async () => {
    stubFetch({
      'GET /api/playbooks': DEFAULT_PLAYBOOKS,
      'GET /api/reviews': { reviews: [] },
      'POST /api/reviews': { review_id: RESUMED_ID, resumed: false },
      [`GET /api/reviews/${RESUMED_ID}`]: detailFor(RESUMED_ID, 'RUNNING', 'primary_pass'),
    });

    render(<ReviewSubmission />);

    // Mounting alone must never write: the token-posture test in
    // security-posture.test.tsx renders the whole App with no submission and
    // asserts `sessionStorage.length === 0`.
    await screen.findByTestId('review-submission');
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull();

    const file = new File(['contents'], 'contract.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });
    fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [file] } });
    await pressSubmit();

    await waitFor(() =>
      expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBe(RESUMED_ID),
    );
    // One key, and only that one — the whole storage posture this ticket is
    // allowed to change.
    expect(window.sessionStorage.length).toBe(1);
  });

  it('the key is forgotten as soon as the polled review reaches a terminal status', async () => {
    // Two polls on the real cadence: RUNNING, then DONE. The second is what
    // must clear the key, so a reload from a finished review lands on a
    // fresh form and leaves the result to History.
    let pollCalls = 0;
    const fetchMock = stubFetch({
      'GET /api/playbooks': DEFAULT_PLAYBOOKS,
      'GET /api/reviews': { reviews: [] },
      'POST /api/reviews': { review_id: RESUMED_ID, resumed: false },
    });
    // eslint-disable-next-line @typescript-eslint/no-misused-promises, @typescript-eslint/require-await
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      // eslint-disable-next-line @typescript-eslint/no-base-to-string
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
      if (pathname === '/api/playbooks') {
        // eslint-disable-next-line @typescript-eslint/require-await
        return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
      }
      if (pathname === '/api/reviews' && method === 'POST') {
        return {
          ok: true,
          status: 202,
          // eslint-disable-next-line @typescript-eslint/require-await
          json: async () => ({ review_id: RESUMED_ID, resumed: false }),
        } as Response;
      }
      if (pathname === '/api/reviews' && method === 'GET') {
        // eslint-disable-next-line @typescript-eslint/require-await
        return { ok: true, status: 200, json: async () => ({ reviews: [] }) } as Response;
      }
      if (pathname === `/api/reviews/${RESUMED_ID}`) {
        pollCalls += 1;
        return {
          ok: true,
          status: 200,
          // eslint-disable-next-line @typescript-eslint/require-await
          json: async () =>
            pollCalls === 1
              ? detailFor(RESUMED_ID, 'RUNNING', 'redline')
              : detailFor(RESUMED_ID, 'DONE'),
        } as Response;
      }
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });

    render(<ReviewSubmission />);

    const file = new File(['contents'], 'contract.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });
    fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [file] } });
    await pressSubmit();

    await waitFor(() =>
      expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBe(RESUMED_ID),
    );

    // The next poll lands on the real POLL cadence (wall-clock, like the
    // sibling reattach tests in session-continuity-489.test.tsx).
    await waitFor(
      () => expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull(),
      { timeout: 8000 },
    );
    expect(window.sessionStorage.length).toBe(0);
  }, 12000);
});

// ---------------------------------------------------------------------------
// The resume itself — the reload half of the story.
// ---------------------------------------------------------------------------
describe('a stored in-flight id resumes the exact review after a reload (issue #58)', () => {
  it('a RUNNING stored review is picked back up, polled, and shown in flight', async () => {
    window.sessionStorage.setItem(INFLIGHT_REVIEW_STORAGE_KEY, RESUMED_ID);
    const fetchMock = stubFetch({
      'GET /api/playbooks': DEFAULT_PLAYBOOKS,
      // Another tab's newer review. Issue #489's listing probe would attach
      // to THIS one; the stored id must win.
      'GET /api/reviews': {
        reviews: [
          { review_id: OTHER_TAB_ID, status: 'RUNNING', playbook_id: 'nda' },
          { review_id: RESUMED_ID, status: 'RUNNING', playbook_id: 'nda' },
        ],
      },
      [`GET /api/reviews/${RESUMED_ID}`]: detailFor(RESUMED_ID, 'RUNNING', 'critic_pass'),
      [`GET /api/reviews/${OTHER_TAB_ID}`]: detailFor(OTHER_TAB_ID, 'RUNNING', 'primary_pass'),
    });

    render(<ReviewSubmission />);

    // The status block only renders once `reviewId` is set — i.e. once
    // something attached.
    await screen.findByTestId('review-status');
    await waitFor(() => expect(polled(fetchMock, RESUMED_ID)).toBe(true));

    // The EXACT review, not the newest one the account is running.
    expect(polled(fetchMock, OTHER_TAB_ID)).toBe(false);
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBe(RESUMED_ID);

    // The console shows a review in flight, not the idle form, and its stage
    // comes from `detail.progress_stage` (issue #58 item 3): a resumed
    // review animates on the same projection a freshly submitted one does.
    await waitFor(() =>
      expect(document.querySelector('.od-console')?.getAttribute('data-status')).toBe('running'),
    );
    expect(screen.getByTestId('toaster-state-progress')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '2'),
    );
  });

  it('a resumed review names no document while it runs — the slot is still empty', async () => {
    // `get_review_detail` (backend/src/reviews.py) does not project
    // `original_filename`, so the generic label is the only honest one — the
    // panel must not invent a document name for a review it did not submit.
    //
    // The surface that carries that claim WHILE the review is running is
    // `model.filename` (orbit-diner/projection.ts: `state.file instanceof File
    // ? state.file.name : (state.submittedFilename ?? detail?.original_filename
    // ?? undefined)`), which is NOT terminal-gated. It has two render sites —
    // the `.od-slice` button's "Selected document: <name>" label and
    // `.od-plain-file` — and its absence is what puts the empty slot's "Choose
    // or drop a .docx" on screen instead. Those are asserted here.
    //
    // `review-meta-filename` is deliberately NOT asserted in this state: it
    // hangs off `result.meta`, and `projectResult` returns undefined for any
    // non-terminal status, so it cannot exist yet no matter what the resume
    // path sets. The test below drives the review to a terminal status first,
    // which is what gives that assertion teeth.
    window.sessionStorage.setItem(INFLIGHT_REVIEW_STORAGE_KEY, RESUMED_ID);
    stubFetch({
      'GET /api/playbooks': DEFAULT_PLAYBOOKS,
      'GET /api/reviews': { reviews: [] },
      [`GET /api/reviews/${RESUMED_ID}`]: detailFor(RESUMED_ID, 'RUNNING', 'primary_pass'),
    });

    render(<ReviewSubmission />);

    await screen.findByTestId('review-status');
    await waitFor(() =>
      expect(document.querySelector('.od-console')?.getAttribute('data-status')).toBe('running'),
    );

    // No name anywhere the running console could print one.
    expect(screen.queryByRole('button', { name: /selected document:/i })).toBeNull();
    expect(document.querySelector('.od-plain-file')).toBeNull();
    // And the slot says so itself: it is still the empty one, offering a pick.
    expect(screen.getByRole('button', { name: /choose or drop a \.docx/i })).toBeInTheDocument();
  });

  it('a resumed review that finishes prints a receipt with no filename on it', async () => {
    // The terminal half of the same claim, and the only state in which
    // `review-meta-filename` can exist at all. Mirrors the shape of
    // session-continuity-489.test.tsx's reattach-to-DONE test: poll once
    // RUNNING, once DONE, then read the receipt.
    window.sessionStorage.setItem(INFLIGHT_REVIEW_STORAGE_KEY, RESUMED_ID);
    let pollCalls = 0;
    const fetchMock = stubFetch({});
    // eslint-disable-next-line @typescript-eslint/no-misused-promises, @typescript-eslint/require-await
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      // eslint-disable-next-line @typescript-eslint/no-base-to-string
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
      if (pathname === '/api/playbooks') {
        // eslint-disable-next-line @typescript-eslint/require-await
        return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
      }
      if (pathname === '/api/reviews' && method === 'GET') {
        // eslint-disable-next-line @typescript-eslint/require-await
        return { ok: true, status: 200, json: async () => ({ reviews: [] }) } as Response;
      }
      if (pathname === `/api/reviews/${RESUMED_ID}`) {
        pollCalls += 1;
        return {
          ok: true,
          status: 200,
          // eslint-disable-next-line @typescript-eslint/require-await
          json: async () =>
            pollCalls === 1
              ? detailFor(RESUMED_ID, 'RUNNING', 'critic_pass')
              : detailFor(RESUMED_ID, 'DONE'),
        } as Response;
      }
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });

    render(<ReviewSubmission />);

    await screen.findByTestId('review-status');
    // The second poll lands on the real POLL cadence; give it wall-clock time.
    await waitFor(
      async () => expect((await findReviewResult()).textContent).toContain('Accepted'),
      { timeout: 8000 },
    );
    expect(screen.getByTestId('toaster-state-done')).toBeInTheDocument();

    // The result surface exists now, so an absent filename is a real absence.
    expect(await openReceipt()).toBeInTheDocument();
    expect(screen.queryByTestId('review-meta-filename')).toBeNull();
  }, 12000);
});

// ---------------------------------------------------------------------------
// The forget paths. Each drops the key and leaves the #489 listing probe to
// answer for itself.
// ---------------------------------------------------------------------------
describe('a stored id that cannot be resumed is forgotten (issue #58)', () => {
  it('a terminal DONE stored review clears the key and leaves the idle form', async () => {
    window.sessionStorage.setItem(INFLIGHT_REVIEW_STORAGE_KEY, RESUMED_ID);
    const fetchMock = stubFetch({
      'GET /api/playbooks': DEFAULT_PLAYBOOKS,
      'GET /api/reviews': { reviews: [] },
      [`GET /api/reviews/${RESUMED_ID}`]: detailFor(RESUMED_ID, 'DONE'),
    });

    render(<ReviewSubmission />);

    await screen.findByTestId('review-submission');
    await waitFor(() =>
      expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull(),
    );
    // The probe ran (the key was checked against the server) but nothing
    // attached — a finished review belongs to History.
    expect(polled(fetchMock, RESUMED_ID)).toBe(true);
    expectNoReviewAttached();
  });

  it('a 404 stored review (purged, or not this caller’s) clears the key and leaves the idle form', async () => {
    window.sessionStorage.setItem(INFLIGHT_REVIEW_STORAGE_KEY, RESUMED_ID);
    stubFetch({
      'GET /api/playbooks': DEFAULT_PLAYBOOKS,
      'GET /api/reviews': { reviews: [] },
      [`GET /api/reviews/${RESUMED_ID}`]: { __status: 404 },
    });

    render(<ReviewSubmission />);

    await screen.findByTestId('review-submission');
    await waitFor(() =>
      expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull(),
    );
    expectNoReviewAttached();
  });

  it('a non-UUID stored value is never put in a request path — it is ignored and cleared', async () => {
    // Nothing in the app can write this; a hand-edited or third-party value
    // is the only way it exists. The read validates the shape, so it never
    // becomes `GET /api/reviews/<whatever>`.
    window.sessionStorage.setItem(INFLIGHT_REVIEW_STORAGE_KEY, '../../api/me');
    const fetchMock = stubFetch({
      'GET /api/playbooks': DEFAULT_PLAYBOOKS,
      'GET /api/reviews': { reviews: [] },
    });

    render(<ReviewSubmission />);

    await screen.findByTestId('review-submission');
    await waitFor(() =>
      expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull(),
    );
    expect(
      fetchMock.mock.calls.some(([input]) => String(input).includes('../../api/me')),
    ).toBe(false);
    expectNoReviewAttached();
  });

  it('with no id stored, issue #489’s "?scope=mine" probe still reattaches', async () => {
    // #58 puts the stored id in FRONT of the #489 listing; it does not
    // replace it. A review submitted in another tab — or before this tab's
    // sessionStorage existed — must still be picked up.
    const fetchMock = stubFetch({
      'GET /api/playbooks': DEFAULT_PLAYBOOKS,
      'GET /api/reviews': {
        reviews: [{ review_id: OTHER_TAB_ID, status: 'RUNNING', playbook_id: 'nda' }],
      },
      [`GET /api/reviews/${OTHER_TAB_ID}`]: detailFor(OTHER_TAB_ID, 'RUNNING', 'primary_pass'),
    });

    render(<ReviewSubmission />);

    await screen.findByTestId('review-status');
    await waitFor(() => expect(polled(fetchMock, OTHER_TAB_ID)).toBe(true));
    // Reattaching from the listing is not a submission, so it writes nothing.
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull();
  });
});
