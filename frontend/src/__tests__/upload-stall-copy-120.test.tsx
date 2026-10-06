/**
 * upload-stall-copy-120.test.tsx — issue #120.
 *
 * The #53 size-scaled submit abort (`ReviewSubmission.tsx`'s `submitReview`)
 * fires on THIS CLIENT'S clock, whatever the server is doing. `post_review`
 * (`backend/src/review_routes.py`) runs the AV gauntlet, document stats, the
 * S3 put, the spend reservation, the row writes and `ensure_execution_started`
 * only AFTER the whole body has arrived — so a slow answer can still mean a
 * review was created, is RUNNING and is being charged for. Before this ticket
 * the reviewer was told "Nothing was submitted" unconditionally, and because
 * the abort path never wrote the in-flight key, a reload could not find the
 * review either.
 *
 * The fix has two halves, and this file pins both:
 *
 *   1. CHECK. When the budget runs out the panel asks the `?scope=mine`
 *      listing (`checkForStalledSubmission`) for a review created inside this
 *      attempt's window. Found → it attaches exactly as a successful POST
 *      would (review id, in-flight key, first poll) and no stall banner is
 *      ever shown. The listing names the caller, not the tab, so a row is
 *      taken only when it is the ONE row in the window and carries the
 *      playbook this submit named — another tab of the same caller on the
 *      same slow link must not be attached, labelled with this tab's file,
 *      or written into this tab's #58 in-flight key.
 *   2. HONEST BANNER. Not found — or the check itself stalls — the banner no
 *      longer asserts that nothing was submitted (the server may still be in
 *      the gauntlet and create the review a moment later). It says the
 *      outcome is unknown and points at History — and so does the REST of
 *      the rendered banner: the console's ordinary submit-error headline
 *      ("That didn't go through") and its "Try again" key would contradict
 *      that copy, so the banner is pinned whole, not by `toContain`.
 *
 * Every rule has a case that goes red when that rule alone is removed:
 * deleting the check reds the "picked up" case; deleting the `created_at`
 * window reds the "older row" case (it would attach to the earlier DONE
 * review — and "picked up" goes red with it, because the earlier row then
 * shares the window and the one-row rule refuses both); deleting the
 * one-row rule reds the "two rows" case; deleting the playbook rule reds the
 * "another playbook" case; deleting the check's own budget reds the "check
 * hangs" case (the console would sit in Submitting forever — the #53
 * defect); restoring the #53 wording reds the copy case; dropping the
 * unknown-outcome flag reds every banner case. Each rejecting fixture is
 * rejected by its own rule alone: the older row and both two-rows rows carry
 * the submitted playbook, and the other-playbook row is the only row in the
 * window.
 *
 * ## Fixture-rule notes
 *
 *   - Review ids are CANONICAL uuid4 strings: `post_review` mints
 *     `str(uuid.uuid4())`, and `readInflightReviewId` (inflightReview.ts)
 *     deletes any stored value that does not match `REVIEW_ID_RE`, so a
 *     placeholder like 'rev-120' is a shape production cannot produce.
 *   - Listing rows carry exactly the fields this code reads, in the shape
 *     `reviews.list_reviews` projects them (`_REVIEW_LIST_ITEM_FIELDS`):
 *     `review_id`, `status`, `playbook_id`, and `created_at` as a STRING OF
 *     EPOCH SECONDS — `_create_review_row` writes `now = str(int(time.time()))`
 *     (backend/src/reviews.py) — under the `reviews` key that `get_reviews`
 *     (backend/src/review_routes.py) answers with. Newest first, as
 *     `list_reviews` orders them. `playbook_id` is the POST's own form field,
 *     passed through `post_review` → `submit_review` to the row unchanged;
 *     the panel names the catalog's one active playbook (`nda`), and the
 *     harness records the posted value so each case can assert it did.
 *   - The row the "created" case finds only appears AFTER the POST was
 *     issued, as in production, so the mount-time reload probe (issue
 *     #489's `reattachFromListing`, the same listing) cannot pick it up
 *     early. The other tab's RUNNING row in the "two rows" and "another
 *     playbook" cases is the same: it lands during this tab's stall window,
 *     which is the scenario those cases exist for. The "older row" case's
 *     earlier review is DONE, which is what keeps that same mount probe —
 *     which only attaches to non-terminal rows — off it; an older RUNNING
 *     row would be attached at mount and the submit could never be pressed.
 *   - The POST stub honours its abort signal the way a real fetch does
 *     (rejects with an AbortError), AND schedules the server's own 202 for
 *     1 s after `submitTimeoutMs(file.size)` — the scenario the ticket's
 *     required verification names.
 *
 * Waits follow the #151 rule: every wait on rendered state is `vi.waitFor`
 * against a REAL-time 12_000 deadline with `interval: 0` and an async
 * callback that drains the fake clock by zero; nothing is bounded by a
 * count of flushes. The submit control is clicked only once its
 * `aria-disabled` is 'false' by awaited state (poll-gives-up-122.test.tsx).
 *
 * Fully offline: `../auth` is mocked and fetch is a hand-rolled stub.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';

import ReviewSubmission, {
  STALL_CHECK_TIMEOUT_MS,
  STALL_REATTACH_TOLERANCE_MS,
  UPLOAD_STALLED_COPY,
  submitTimeoutMs,
} from '../ReviewSubmission';
import { INFLIGHT_REVIEW_STORAGE_KEY } from '../inflightReview';
import { DEFAULT_PLAYBOOKS, submitArmed } from './support/consoleSurface';
import { allowConsoleErrorsInThisTest } from './support/consoleErrorGuard';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

/** The review the server created for the stalled attempt. */
const CREATED_ID = '3f2b8c1d-6e4a-4b7f-9c2d-8a1e5f6b7c90';
/** An earlier review of the same caller's, finished long before the attempt. */
const OLDER_ID = '9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d';
/** A review the same caller started from ANOTHER tab, landing inside this attempt's window. */
const OTHER_TAB_ID = '5c4d3e2f-1a0b-4c9d-8e7f-6a5b4c3d2e1f';
/** How long before the attempt the earlier review was created — well past the window. */
const OLDER_BY_MS = 10 * 60_000;
/** The catalog's one active playbook (DEFAULT_PLAYBOOKS) — what this panel's submit names. */
const SUBMITTED_PLAYBOOK = 'nda';
/** A playbook this submit did not name. */
const OTHER_PLAYBOOK = 'msa';

/**
 * The stalled banner, as the console must render it: the headline, the copy
 * and the one key — and nothing else. Pinned WHOLE because the ordinary
 * submit-error headline ("That didn't go through") and its "Try again" key
 * would each contradict the copy ("we can't tell", "Check History before you
 * submit it again") while a `toContain(UPLOAD_STALLED_COPY)` stayed green.
 */
const UNCONFIRMED_TITLE = 'Upload not confirmed';
const CHECK_HISTORY_KEY = 'Check History';

/** What the listing shows once the POST has been issued. */
type AfterPost = 'created' | 'empty' | 'older-only' | 'two-in-window' | 'other-playbook' | 'hangs';

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

function abortError(): Error {
  return Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' });
}

/** A promise that never settles on its own and rejects when `signal` aborts — a real fetch. */
function pendingUntilAborted(signal: AbortSignal | null | undefined): Promise<Response> {
  return new Promise<Response>((_, reject) => {
    signal?.addEventListener('abort', () => reject(abortError()), { once: true });
  });
}

function jsonResponse(status: number, body: unknown): Response {
  // eslint-disable-next-line @typescript-eslint/require-await
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

interface Harness {
  /** `METHOD pathname?search` of every request, in order. */
  calls: string[];
  /** Listing GETs issued after the POST — the stall check's own requests. */
  listingsAfterPost(): number;
  posted(): boolean;
  /** The `playbook_id` form field the POST carried, or null when it named none. */
  postedPlaybook(): string | null;
}

function stubStalledUpload(afterPost: AfterPost): Harness {
  const calls: string[] = [];
  let postIssued = false;
  let postedPlaybook: string | null = null;
  let listingsAfterPost = 0;
  // Fixed when the stub is built, under the fake clock, i.e. before the
  // attempt begins. Same playbook as the submit, so only the `created_at`
  // window can reject it.
  const olderRow = {
    review_id: OLDER_ID,
    status: 'DONE',
    playbook_id: SUBMITTED_PLAYBOOK,
    created_at: String(Math.floor((Date.now() - OLDER_BY_MS) / 1000)),
  };
  /** A RUNNING row stamped now — i.e. inside this attempt's window. */
  const runningNow = (reviewId: string, playbookId: string) => ({
    review_id: reviewId,
    status: 'RUNNING',
    playbook_id: playbookId,
    created_at: String(Math.floor(Date.now() / 1000)),
  });

  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    // eslint-disable-next-line @typescript-eslint/no-base-to-string
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const parsed = new URL(url, 'http://localhost');
    calls.push(`${method} ${parsed.pathname}${parsed.search}`);

    if (parsed.pathname === '/api/playbooks') {
      return Promise.resolve(jsonResponse(200, DEFAULT_PLAYBOOKS));
    }

    if (method === 'POST' && parsed.pathname === '/api/reviews') {
      postIssued = true;
      const field = init?.body instanceof FormData ? init.body.get('playbook_id') : null;
      postedPlaybook = typeof field === 'string' ? field : null;
      // The server's own answer, 1 s after the client's budget — always
      // outrun by the abort, which rejects this exactly as a real fetch
      // rejects on its signal.
      return new Promise<Response>((resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(abortError()), { once: true });
        setTimeout(() => {
          resolve(jsonResponse(202, { review_id: CREATED_ID, resumed: false }));
        }, submitTimeoutMs(docxFile().size) + 1_000);
      });
    }

    if (
      method === 'GET' &&
      parsed.pathname === '/api/reviews' &&
      parsed.searchParams.get('scope') === 'mine'
    ) {
      if (!postIssued) {
        // The mount-time reload probe: only what existed before the attempt.
        return Promise.resolve(
          jsonResponse(200, { reviews: afterPost === 'older-only' ? [olderRow] : [], next_token: null }),
        );
      }
      listingsAfterPost += 1;
      switch (afterPost) {
        case 'created':
          return Promise.resolve(
            jsonResponse(200, {
              // Stamped when `post_review` wrote the row: after the attempt
              // began.
              reviews: [runningNow(CREATED_ID, SUBMITTED_PLAYBOOK), olderRow],
              next_token: null,
            }),
          );
        case 'empty':
          return Promise.resolve(jsonResponse(200, { reviews: [], next_token: null }));
        case 'older-only':
          return Promise.resolve(jsonResponse(200, { reviews: [olderRow], next_token: null }));
        case 'two-in-window':
          // Another tab's review AND this attempt's, both under the submitted
          // playbook, both inside the window. Nothing in the listing says
          // which is this tab's — only the one-row rule rejects them.
          return Promise.resolve(
            jsonResponse(200, {
              reviews: [
                runningNow(OTHER_TAB_ID, SUBMITTED_PLAYBOOK),
                runningNow(CREATED_ID, SUBMITTED_PLAYBOOK),
                olderRow,
              ],
              next_token: null,
            }),
          );
        case 'other-playbook':
          // Another tab's review, the only row in the window, under a
          // playbook this submit did not name — only the playbook rule
          // rejects it. This attempt's own row has not been written yet.
          return Promise.resolve(
            jsonResponse(200, {
              reviews: [runningNow(OTHER_TAB_ID, OTHER_PLAYBOOK), olderRow],
              next_token: null,
            }),
          );
        case 'hangs':
          return pendingUntilAborted(init?.signal);
      }
    }

    const detailId = parsed.pathname.match(/^\/api\/reviews\/([0-9a-f-]{36})$/)?.[1];
    if (detailId === CREATED_ID || detailId === OTHER_TAB_ID || detailId === OLDER_ID) {
      // Every id a listing above can show answers its detail, so a wrong
      // attach would visibly attach and poll rather than 404.
      return Promise.resolve(
        jsonResponse(200, {
          review_id: detailId,
          status: detailId === OLDER_ID ? 'DONE' : 'RUNNING',
          decision: null,
          message: null,
          has_output: false,
          created_at: detailId === OLDER_ID ? olderRow.created_at : String(Math.floor(Date.now() / 1000)),
        }),
      );
    }

    return Promise.resolve(jsonResponse(404, {}));
  });
  vi.stubGlobal('fetch', fetchMock);

  return {
    calls,
    listingsAfterPost: () => listingsAfterPost,
    posted: () => postIssued,
    postedPlaybook: () => postedPlaybook,
  };
}

/**
 * The stalled banner is on screen and is exactly the unknown-outcome banner:
 * headline, copy and the History key, as one string — with nothing claiming
 * the upload failed and no one-press resubmit.
 */
function expectUnconfirmedBanner(): HTMLElement {
  const banner = screen.getByTestId('review-submit-error');
  expect(banner.textContent).toBe(`${UNCONFIRMED_TITLE}${UPLOAD_STALLED_COPY}${CHECK_HISTORY_KEY}`);
  expect(banner.textContent).not.toMatch(/didn't go through/i);
  expect(banner.textContent).not.toMatch(/try again/i);
  // Still interrupts a screen reader: it carries a key (OrbitDiner's
  // `messageRole`), and what it asks the reviewer to do matters.
  expect(banner.getAttribute('role')).toBe('alert');
  expect(within(banner).getAllByRole('button').map((key) => key.textContent)).toEqual([
    CHECK_HISTORY_KEY,
  ]);
  return banner;
}

/** Wait for `check` to hold by awaited state, against a REAL-time deadline (#151). */
async function waitUntil(check: () => void): Promise<void> {
  await vi.waitFor(
    async () => {
      await vi.advanceTimersByTimeAsync(0);
      check();
    },
    { interval: 0, timeout: 12_000 },
  );
}

/**
 * `review-cancel-button` present and enabled is the console's "attached to a
 * running review" reading (`OrbitDiner.tsx`'s lever: `working ?
 * 'review-cancel-button' : 'review-submit-button'`, disabled only while
 * SUBMITTING or a cancel is pending) — what a plain successful submit shows.
 */
function attachedToRunningReview(): boolean {
  const stop = screen.queryByTestId('review-cancel-button');
  // `aria-disabled` is ABSENT, not 'false', once attached: the lever passes
  // `m.status === "SUBMITTING" || m.cancelRequested`, and an unset
  // `cancelRequested` leaves the expression `undefined`, which React omits.
  return stop !== null && stop.getAttribute('aria-disabled') !== 'true';
}

/** Render, arm, press the lever, and run the client's own budget out. */
async function submitAndRunOutTheBudget(h: Harness): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  // Land the playbook catalog — no submit timer exists yet, so this ends.
  await vi.runAllTimersAsync();
  await waitUntil(() => {
    expect(screen.getByTestId('review-submit-button').getAttribute('aria-disabled')).toBe('false');
  });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await waitUntil(() => {
    expect(h.posted(), `requests so far: ${JSON.stringify(h.calls)}`).toBe(true);
  });
  // No banner while the upload is still inside its budget.
  expect(screen.queryByTestId('review-submit-error')).toBeNull();
  await vi.advanceTimersByTimeAsync(submitTimeoutMs(docxFile().size));
}

describe('UPLOAD_STALLED_COPY (issue #120)', () => {
  it('does not claim nothing was submitted, and points at History', () => {
    expect(UPLOAD_STALLED_COPY).not.toMatch(/nothing was submitted/i);
    expect(UPLOAD_STALLED_COPY).toMatch(/can't tell/);
    expect(UPLOAD_STALLED_COPY).toMatch(/\bHistory\b/);
    // Same posture as the #53 copy: no status, path or duration.
    expect(UPLOAD_STALLED_COPY).not.toMatch(/\d/);
    expect(UPLOAD_STALLED_COPY).not.toMatch(/\/api\//);
  });

  it('the window and the check budget are what the code says they are', () => {
    expect(STALL_REATTACH_TOLERANCE_MS).toBe(5_000);
    expect(STALL_CHECK_TIMEOUT_MS).toBe(15_000);
    // The older review in the fixtures sits well outside the window.
    expect(OLDER_BY_MS).toBeGreaterThan(STALL_REATTACH_TOLERANCE_MS * 10);
  });
});

describe('a submit whose budget runs out (issue #120)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    window.sessionStorage.clear();
    window.location.hash = '';
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
    window.location.hash = '';
  });

  it('picks up the review the server created 1 s late and never shows the stall copy', async () => {
    const h = stubStalledUpload('created');
    await submitAndRunOutTheBudget(h);

    // Picked up: attached and polling, exactly as a normal submit would be.
    await waitUntil(() => {
      expect(attachedToRunningReview(), `requests so far: ${JSON.stringify(h.calls)}`).toBe(true);
      expect(h.calls).toContain(`GET /api/reviews/${CREATED_ID}`);
    });
    // The submit named the playbook the matched row carries.
    expect(h.postedPlaybook()).toBe(SUBMITTED_PLAYBOOK);
    expect(h.listingsAfterPost()).toBeGreaterThanOrEqual(1);
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBe(CREATED_ID);
    expect(h.calls).not.toContain(`GET /api/reviews/${OLDER_ID}`);

    // Past the moment the server's own 202 would have arrived: still no
    // banner, and the stall copy is nowhere on the page.
    await vi.advanceTimersByTimeAsync(1_000);
    expect(screen.queryByTestId('review-submit-error')).toBeNull();
    expect(document.body.textContent ?? '').not.toContain(UPLOAD_STALLED_COPY);
  });

  it('nothing in the listing: says the outcome is unknown, points at History, re-arms', async () => {
    allowConsoleErrorsInThisTest(/POST \/api\/reviews abandoned after \d+ ms/);
    const h = stubStalledUpload('empty');
    await submitAndRunOutTheBudget(h);

    await waitUntil(() => {
      expect(screen.queryByTestId('review-submit-error')).not.toBeNull();
    });
    const banner = expectUnconfirmedBanner();
    // The listing WAS asked before the banner went up.
    expect(h.listingsAfterPost()).toBe(1);
    expect(attachedToRunningReview()).toBe(false);
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull();
    // The lever stays armed for a deliberate resubmit — but the banner's own
    // key goes where the copy says to look first, not round again.
    expect(submitArmed()).toBe(true);
    expect(window.location.hash).not.toBe('#/history');
    fireEvent.click(within(banner).getByRole('button', { name: CHECK_HISTORY_KEY }));
    expect(window.location.hash).toBe('#/history');
    await vi.advanceTimersByTimeAsync(0);
    expect(h.calls.filter((call) => call === 'POST /api/reviews')).toHaveLength(1);
  });

  it('an earlier review older than the attempt is not mistaken for this one', async () => {
    allowConsoleErrorsInThisTest(/POST \/api\/reviews abandoned after \d+ ms/);
    const h = stubStalledUpload('older-only');
    await submitAndRunOutTheBudget(h);

    await waitUntil(() => {
      expect(screen.queryByTestId('review-submit-error')).not.toBeNull();
    });
    expectUnconfirmedBanner();
    expect(h.listingsAfterPost()).toBe(1);
    expect(h.calls).not.toContain(`GET /api/reviews/${OLDER_ID}`);
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull();
    expect(submitArmed()).toBe(true);
  });

  it('two rows in the window: does not guess which is this tab\'s', async () => {
    allowConsoleErrorsInThisTest(/POST \/api\/reviews abandoned after \d+ ms/);
    const h = stubStalledUpload('two-in-window');
    await submitAndRunOutTheBudget(h);

    await waitUntil(() => {
      expect(screen.queryByTestId('review-submit-error')).not.toBeNull();
    });
    expectUnconfirmedBanner();
    expect(h.listingsAfterPost()).toBe(1);
    // Neither row is attached, polled, or written into this tab's #58 key.
    expect(attachedToRunningReview()).toBe(false);
    expect(h.calls).not.toContain(`GET /api/reviews/${OTHER_TAB_ID}`);
    expect(h.calls).not.toContain(`GET /api/reviews/${CREATED_ID}`);
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull();
    expect(submitArmed()).toBe(true);
  });

  it('another tab\'s review under another playbook is not taken for this one', async () => {
    allowConsoleErrorsInThisTest(/POST \/api\/reviews abandoned after \d+ ms/);
    const h = stubStalledUpload('other-playbook');
    await submitAndRunOutTheBudget(h);

    await waitUntil(() => {
      expect(screen.queryByTestId('review-submit-error')).not.toBeNull();
    });
    // This submit DID name a playbook, and it is not the row's.
    expect(h.postedPlaybook()).toBe(SUBMITTED_PLAYBOOK);
    expectUnconfirmedBanner();
    expect(h.listingsAfterPost()).toBe(1);
    expect(attachedToRunningReview()).toBe(false);
    expect(h.calls).not.toContain(`GET /api/reviews/${OTHER_TAB_ID}`);
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull();
    expect(submitArmed()).toBe(true);
  });

  it('a check that stalls too gives up on its own budget and re-arms the lever', async () => {
    allowConsoleErrorsInThisTest(/POST \/api\/reviews abandoned after \d+ ms/);
    const h = stubStalledUpload('hangs');
    await submitAndRunOutTheBudget(h);

    await waitUntil(() => {
      expect(h.listingsAfterPost()).toBe(1);
    });
    // The check is in flight: still submitting, no banner yet.
    expect(screen.queryByTestId('review-submit-error')).toBeNull();
    expect(screen.queryByTestId('review-submit-button')).toBeNull();

    await vi.advanceTimersByTimeAsync(STALL_CHECK_TIMEOUT_MS);
    await waitUntil(() => {
      expect(screen.queryByTestId('review-submit-error')).not.toBeNull();
    });
    expectUnconfirmedBanner();
    expect(window.sessionStorage.getItem(INFLIGHT_REVIEW_STORAGE_KEY)).toBeNull();
    expect(submitArmed()).toBe(true);
  });
});
