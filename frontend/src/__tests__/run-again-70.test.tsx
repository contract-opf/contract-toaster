/**
 * run-again-70.test.tsx — "Run again", from the burnt-toast panel and from a
 * History row (issue #70; 2026-09-05 diagnostic finding G11, action B11).
 *
 * ## The rule this file exists to hold
 *
 * **Run again restores SETTINGS. It never fetches the document.**
 *
 * That is an owner ruling (2026-09-13), not an implementation shortcut, and it
 * is the reason this ticket was parked once already. `GET /api/reviews/{id}/
 * input` answers with a presigned URL on a DIFFERENT origin; `connect-src`
 * forbids the SPA from reading it on both deployment targets
 * (`infra/lib/nested/frontend-stack.ts`, `deploy/dts/nginx.conf`), there is no
 * bucket CORS anywhere in `infra/`, and every presign spends a slot of the
 * caller's daily download quota and writes a `review_input_downloaded` audit
 * row. Today's input download works only because `triggerBrowserDownload`
 * navigates an anchor, which that directive does not govern.
 *
 * jsdom can no more see a Content-Security-Policy than it can see a
 * stylesheet, so a `fetch()` for those bytes would pass every test in this
 * repo and fail in production — the same shape as the two defects CLAUDE.md
 * already warns about (#727, #739). A green suite cannot prove the policy; it
 * CAN prove the request is never made, and that is the negative asserted here,
 * on every path, for `has_input: true` AND `has_input: false`.
 *
 * ## What else is pinned
 *
 *   - both entry points: a History row action (which navigates via App.tsx's
 *     own `#/review` hash, not a second routing mechanism) and a key on the
 *     burnt panel BESIDE `review-retry-button` — that one clears, this one
 *     clears and restores;
 *   - the picker is always empty afterwards, and says why, in fixed copy that
 *     names no file;
 *   - nothing is submitted until the reviewer presses the lever, and the
 *     `FormData` that then goes out is byte-for-byte what a manual selection
 *     of the same four settings sends;
 *   - a settings read that fails says so in FIXED copy and leaves the burnt
 *     review, and the dials, exactly where they were;
 *   - and a press made while a review is STILL RUNNING is refused outright.
 *     The prefill's first act is the same `resetForRetry()` the burnt panel's
 *     other key performs, which stops the poller and takes the review off the
 *     screen WITHOUT cancelling it server-side. The Review tab hides the
 *     intake behind `working` so no manual gesture can reach that reset
 *     mid-flight; the History row is a programmatic caller of the same
 *     handler and needs the gate written down, and seeded, here.
 *
 * Asserts on rendered testids, control state and recorded fetch calls only —
 * never computed styles (`vitest.config.ts` runs jsdom with `css: false`).
 * Fully offline: Amplify auth and `../auth` are mocked, fetch is stubbed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import App from '../App';
import ReviewSubmission from '../ReviewSubmission';
import { fromMarkupIntensity } from '../toaster/browning';
import { resetRunAgainRequestForTests } from '../runAgainRequest';
import { pressSubmit } from './support/consoleSurface';

vi.mock('aws-amplify/auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

// `../auth` is deliberately NOT mocked. This file renders the whole `App` for
// the History entry point, and a mocked `isPasswordMode: () => true` would put
// the password login screen in front of every tab — the signed-in shell only
// appears on the Amplify path these two mocks describe.
vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({
    user: { username: 'user-sub', signInDetails: { loginId: 'user@example.com' } },
    signOut: vi.fn(),
  }),
}));

/**
 * Review ids as the product actually mints them: `backend/src/review_routes.py`
 * `post_review` writes `str(uuid.uuid4())`. Not the `rev-xyz` stand-ins older
 * fixtures use — `inflightReview.ts` validates this shape before it will put
 * an id back into a request path, so a synthetic id would quietly take the
 * submit path down a branch a real session never takes.
 */
const BURNT_ID = '8f14e45f-ceea-467a-9c0b-7bba5f2b1e3d';
const HISTORY_ID = '2b1c9d77-4e6a-4c11-8f2a-0d5e7a9c3b41';

/**
 * Two ACTIVE playbooks, because the settings under test include WHICH one.
 * With a single-entry catalog the prefill could set the playbook to the only
 * value the dial can hold and prove nothing; `msa` is deliberately not the
 * first entry, which is where the catalog reconciliation (#464) parks a
 * selection it cannot honour.
 */
const CATALOG = {
  playbooks: [
    { playbook_id: 'nda', display_name: 'NDA', status: 'active' },
    { playbook_id: 'msa', display_name: 'MSA', status: 'active' },
  ],
};

const STORED_GUIDANCE = 'Cap liability at 12 months of fees; leave the IP clause alone.';

/**
 * The stored review's settings, as `get_review_detail` (backend/src/reviews.py)
 * actually projects them: `playbook_id`, `toaster_guidance`, `notes_mode`,
 * `markup_intensity` and `has_input` are all on that projection today.
 *
 * `original_filename` is deliberately ABSENT, because the projection does not
 * carry it — #58 left it off and the #70 ruling keeps it off. A fixture that
 * invented the field would let a prefill quietly start depending on a value
 * production never sends.
 *
 * Every one of the four settings differs from what this panel defaults to
 * (first active playbook, medium, external, no instructions), so a prefill
 * that did nothing at all cannot pass these assertions.
 */
function storedDetail(
  reviewId: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    review_id: reviewId,
    status: 'ERROR',
    decision: null,
    message: null,
    has_output: false,
    has_input: true,
    reason: 'model_context_length_exceeded',
    failing_stage: 'run_review',
    playbook_id: 'msa',
    toaster_guidance: STORED_GUIDANCE,
    notes_mode: 'none',
    markup_intensity: 'heavy',
    ...overrides,
  };
}

/** The `_review_list_item` shape the History table reads. */
function historyRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    review_id: HISTORY_ID,
    playbook_id: 'msa',
    status: 'DONE',
    decision: 'REQUEST_CHANGE',
    created_at: '1800000200',
    updated_at: '1800000300',
    has_output: true,
    has_input: true,
    markup_intensity: 'heavy',
    ...overrides,
  };
}

type Routes = Record<string, unknown>;

/** The request URL, whichever of `fetch`'s three argument shapes arrived. */
function urlOf(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input;
  return input instanceof URL ? input.href : input.url;
}

/**
 * The stub every test here drives. Routes are keyed `METHOD /path` or bare
 * `/path`; a route whose value is a `{ __status }` marker answers not-ok,
 * which is how the failure branch is exercised without teaching the stub a
 * second shape.
 */
function stubFetch(routes: Routes): ReturnType<typeof vi.fn> {
  // eslint-disable-next-line @typescript-eslint/require-await
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(urlOf(input), 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const body = routes[key];
    if (body === undefined) {
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    if (body !== null && typeof body === 'object' && '__status' in body) {
      const status = (body as { __status: number }).__status;
      return {
        ok: false,
        status,
        // eslint-disable-next-line @typescript-eslint/require-await
        json: async () => ({ detail: 'Reviews table unreachable: ConnectionError(...)' }),
        // eslint-disable-next-line @typescript-eslint/require-await
        text: async () => 'Reviews table unreachable: ConnectionError(...)',
      } as unknown as Response;
    }
    // eslint-disable-next-line @typescript-eslint/require-await
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

const VERSION_OK = {
  version: '0.0.1',
  commit: 'abcdef1234567890',
  image_digest: 'sha256:x',
  uptime_seconds: 1,
};

/**
 * Every call this test's fetch stub recorded, as `[METHOD /path, init]`.
 * `mock.calls` is `any[][]`, so the two arguments are named back into their
 * real types once, here, rather than at each use.
 */
function recorded(
  fetchMock: ReturnType<typeof vi.fn>,
): { path: string; init: RequestInit | undefined }[] {
  return (fetchMock.mock.calls as unknown as [RequestInfo | URL, RequestInit | undefined][]).map(
    ([input, init]) => {
      const method = (init?.method ?? 'GET').toUpperCase();
      return { path: `${method} ${new URL(urlOf(input), 'http://localhost').pathname}`, init };
    },
  );
}

/** Every path this test's fetch stub was asked for, as `METHOD /path`. */
function calls(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return recorded(fetchMock).map((call) => call.path);
}

/**
 * THE assertion this file is built around. Not "no call to this one id's
 * input route" — ANY review's input route, by suffix, so a prefill that
 * reached for a different review's bytes is caught too.
 */
function expectNoInputRequest(fetchMock: ReturnType<typeof vi.fn>): void {
  const inputCalls = calls(fetchMock).filter((call) => call.endsWith('/input'));
  expect(inputCalls, 'Run again must never request the input document').toEqual([]);
}

function postCount(fetchMock: ReturnType<typeof vi.fn>): number {
  return calls(fetchMock).filter((call) => call === 'POST /api/reviews').length;
}

function chooseFile(name = 'contract.docx'): void {
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: {
      files: [
        new File(['contract bytes'], name, {
          type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        }),
      ],
    },
  });
}

function radioChecked(testId: string): boolean {
  return screen.getByTestId<HTMLInputElement>(testId).checked;
}

/** The four settings, read back off the controls the reviewer would use. */
function readControls(): {
  playbookId: string;
  browning: string | null;
  notesMode: string | null;
  guidance: string;
} {
  const browning = ['light', 'medium', 'dark'].find((level) =>
    radioChecked(`review-browning-option-${level}`),
  );
  const notesMode = ['none', 'external', 'internal', 'both'].find((mode) =>
    radioChecked(`review-notes-mode-option-${mode}`),
  );
  return {
    playbookId: screen.getByTestId<HTMLSelectElement>('review-playbook-dial').value,
    browning: browning ?? null,
    notesMode: notesMode ?? null,
    guidance: screen.getByTestId<HTMLTextAreaElement>('review-guidance-input').value,
  };
}

/** Assert the four settings landed, whichever entry point put them there. */
async function expectPrefilled(): Promise<void> {
  await waitFor(() => expect(readControls().playbookId).toBe('msa'));
  await waitFor(() => expect(readControls().browning).toBe('dark'));
  expect(readControls().notesMode).toBe('none');
  expect(readControls().guidance).toBe(STORED_GUIDANCE);
}

/**
 * Assert the picker is empty and says why. The sentence is fixed text that
 * names no document — the whole point is that the file did NOT come with the
 * settings, and echoing a filename would suggest otherwise.
 */
async function expectPickerAsksForTheDocumentAgain(): Promise<void> {
  const note = await screen.findByTestId('review-prefill-note');
  expect(note.textContent?.replace(/\s+/g, ' ').trim()).toBe(
    'Choose the document again — Run again restores your settings, not the file.',
  );
  expect(screen.getByTestId<HTMLInputElement>('review-file-input').value).toBe('');
  expect(note.textContent).not.toContain('.docx');
}

/** Drive a review to its burnt terminal state, as `burnt-toast.test.tsx` does. */
async function submitAndBurn(
  detail: Record<string, unknown>,
): Promise<ReturnType<typeof vi.fn>> {
  const fetchMock = stubFetch({
    '/api/playbooks': CATALOG,
    '/api/me/preferences': { preferences: {}, notes_mode_available: false },
    'PUT /api/me/preferences': {},
    'POST /api/reviews': { review_id: BURNT_ID, resumed: false },
    [`GET /api/reviews/${BURNT_ID}`]: detail,
  });
  render(<ReviewSubmission />);
  chooseFile();
  await pressSubmit();
  await screen.findByTestId('review-failure');
  return fetchMock;
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage?.clear();
  window.sessionStorage?.clear();
  resetRunAgainRequestForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.location.hash = '';
});

// ---------------------------------------------------------------------------
// The wire-vocabulary inverse
// ---------------------------------------------------------------------------

describe('fromMarkupIntensity — the dial a stored review ran under', () => {
  it('translates the wire enum back to the control vocabulary', () => {
    expect(fromMarkupIntensity('light')).toBe('light');
    expect(fromMarkupIntensity('medium')).toBe('medium');
    expect(fromMarkupIntensity('heavy')).toBe('dark');
  });

  it('reads an absent value as Medium — the setting that sends no field', () => {
    // Not a guess: Medium appends no `markup_intensity` and injects no
    // intensity block, so a row carrying nothing ran exactly the request
    // Medium produces, whether it was submitted at Medium or predates #54.
    expect(fromMarkupIntensity(null)).toBe('medium');
    expect(fromMarkupIntensity(undefined)).toBe('medium');
    expect(fromMarkupIntensity('')).toBe('medium');
  });

  it('refuses a value this build does not know, rather than downgrading it', () => {
    // A newer server's vocabulary. Silently calling it Medium would rerun the
    // document under an intensity nobody chose; null leaves the dial alone.
    expect(fromMarkupIntensity('scorched')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Entry point 1 — a History row
// ---------------------------------------------------------------------------

describe('Run again — from a History row (issue #70)', () => {
  // Both variants of the branch the ticket names. The prefill must behave
  // IDENTICALLY for a review whose input pointer is gone, because what it
  // restores lives on the row and never depended on the document surviving.
  for (const hasInput of [true, false]) {
    it(`hands the review to the Review tab and prefills its settings (has_input: ${String(hasInput)})`, async () => {
      const fetchMock = stubFetch({
        '/version': VERSION_OK,
        '/api/me': { is_admin: false },
        '/api/playbooks': CATALOG,
        '/api/me/preferences': { preferences: {}, notes_mode_available: false },
        'PUT /api/me/preferences': {},
        '/api/reviews': { reviews: [historyRow({ has_input: hasInput })] },
        [`GET /api/reviews/${HISTORY_ID}`]: storedDetail(HISTORY_ID, {
          status: 'DONE',
          decision: 'REQUEST_CHANGE',
          has_output: true,
          has_input: hasInput,
          reason: null,
          failing_stage: null,
        }),
      });

      render(<App />);
      await screen.findByTestId('version-display');
      fireEvent.click(await screen.findByRole('tab', { name: 'History' }));

      const action = await screen.findByTestId(`history-run-again-${HISTORY_ID}`);
      // The glyph alone is unreadable; the accessible name is the control
      // (#668's bar, held here too).
      expect(action).toHaveAccessibleName("Run again with this review's settings");
      fireEvent.click(action);

      // App.tsx's own hash routing — not a second navigation mechanism.
      await waitFor(() => expect(window.location.hash).toBe('#/review'));
      await waitFor(() =>
        expect(screen.getByRole('tab', { name: 'Review' })).toHaveAttribute(
          'aria-selected',
          'true',
        ),
      );

      await expectPrefilled();
      await expectPickerAsksForTheDocumentAgain();
      expectNoInputRequest(fetchMock);
      // And the settings came from the app's own detail route, once.
      expect(calls(fetchMock).filter((c) => c === `GET /api/reviews/${HISTORY_ID}`)).toHaveLength(
        1,
      );
    });
  }

  it('is refused while a review is still running, and detaches nothing', async () => {
    // FIXTURE REALITY: the Review tab is seeded NON-TERMINAL here. Every other
    // press in this file starts from a fresh panel or a terminal `ERROR`, so
    // the variant the prefill actually branches on was never exercised — and a
    // prefill that reset an in-flight review would have passed all of them.
    const fetchMock = stubFetch({
      '/version': VERSION_OK,
      '/api/me': { is_admin: false },
      '/api/playbooks': CATALOG,
      '/api/me/preferences': { preferences: {}, notes_mode_available: false },
      'PUT /api/me/preferences': {},
      '/api/reviews': { reviews: [historyRow()] },
      'POST /api/reviews': { review_id: BURNT_ID, resumed: false },
      [`GET /api/reviews/${BURNT_ID}`]: storedDetail(BURNT_ID, {
        status: 'RUNNING',
        decision: null,
        has_output: false,
        reason: null,
        failing_stage: null,
      }),
      [`GET /api/reviews/${HISTORY_ID}`]: storedDetail(HISTORY_ID, {
        status: 'DONE',
        decision: 'REQUEST_CHANGE',
        has_output: true,
        reason: null,
        failing_stage: null,
      }),
    });

    render(<App />);
    await screen.findByTestId('version-display');
    chooseFile();
    await pressSubmit();
    await waitFor(() =>
      expect(document.querySelector('.od-console')?.getAttribute('data-status')).toBe('running'),
    );
    const runningStatus = screen.getByTestId('review-status').textContent;
    // Not vacuous: the panel really is mid-flight, which is exactly the state
    // that hides the intake from a manual gesture.
    expect(screen.getByTestId('review-intake-slot')).toHaveAttribute('hidden');

    fireEvent.click(await screen.findByRole('tab', { name: 'History' }));
    fireEvent.click(await screen.findByTestId(`history-run-again-${HISTORY_ID}`));
    await waitFor(() => expect(window.location.hash).toBe('#/review'));

    // Refused, in the app's own fixed words — no status code, no server text.
    const banner = await screen.findByTestId('review-prefill-error');
    const text = banner.textContent ?? '';
    expect(text).toContain('That review is still running.');
    expect(text).not.toContain('ConnectionError');

    // And the running review is exactly where it was: still attached, still
    // reporting the same status, still hiding the intake that would invite a
    // second submission of work the server is already doing.
    expect(document.querySelector('.od-console')?.getAttribute('data-status')).toBe('running');
    expect(screen.getByTestId('review-status').textContent).toBe(runningStatus);
    expect(screen.getByTestId('review-intake-slot')).toHaveAttribute('hidden');
    expect(screen.queryByTestId('review-prefill-note')).toBeNull();

    // Nothing was read, so nothing could have been restored...
    expect(calls(fetchMock)).not.toContain(`GET /api/reviews/${HISTORY_ID}`);
    expectNoInputRequest(fetchMock);
    // ...and the refusal submitted nothing of its own.
    expect(postCount(fetchMock)).toBe(1);
  });

  it('offers the action on every row, including one whose document is gone', async () => {
    stubFetch({
      '/version': VERSION_OK,
      '/api/me': { is_admin: false },
      '/api/playbooks': CATALOG,
      '/api/reviews': {
        reviews: [historyRow({ has_input: false, has_output: false })],
      },
    });

    render(<App />);
    await screen.findByTestId('version-display');
    fireEvent.click(await screen.findByRole('tab', { name: 'History' }));

    const cell = await screen.findByTestId(`history-actions-${HISTORY_ID}`);
    // The row genuinely has no documents — so this is not a vacuous pass.
    expect(within(cell).getByTestId(`history-no-input-${HISTORY_ID}`)).toBeTruthy();
    expect(within(cell).getByTestId(`history-no-output-${HISTORY_ID}`)).toBeTruthy();
    expect(within(cell).getByTestId(`history-run-again-${HISTORY_ID}`)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Entry point 2 — the burnt panel
// ---------------------------------------------------------------------------

describe('Run again — from the burnt-toast panel (issue #70)', () => {
  it('sits beside the retry key and prefills instead of merely clearing', async () => {
    const fetchMock = await submitAndBurn(storedDetail(BURNT_ID));

    // Two keys, two meanings — both present, so neither replaced the other.
    expect(screen.getByTestId('review-retry-button')).toBeTruthy();
    const runAgain = screen.getByTestId('review-run-again-button');
    expect(runAgain.textContent).toContain('Run again');

    // Before: the submit that burnt ran on this panel's DEFAULTS, none of
    // which is the stored review's setting. Without this the assertions
    // below could pass on a prefill that never happened.
    expect(readControls()).toEqual({
      playbookId: 'nda',
      browning: 'medium',
      notesMode: 'external',
      guidance: '',
    });

    const postsBefore = postCount(fetchMock);
    expect(postsBefore).toBe(1);

    fireEvent.click(runAgain);

    // The burnt review is off the screen — this key clears, like the retry
    // key beside it...
    await waitFor(() => expect(screen.queryByTestId('review-failure')).toBeNull());
    // ...and unlike that key, the dials came back.
    await expectPrefilled();
    await expectPickerAsksForTheDocumentAgain();

    // Nothing was resubmitted, and the document was never asked for.
    expect(postCount(fetchMock)).toBe(postsBefore);
    expectNoInputRequest(fetchMock);
  });

  it('offers no Run again when the burn happened before a review existed', async () => {
    // A submit the server refused outright: there is no review id, so there
    // are no stored settings to restore and the key must not be offered.
    stubFetch({
      '/api/playbooks': CATALOG,
      '/api/me/preferences': { preferences: {}, notes_mode_available: false },
      'POST /api/reviews': { __status: 503 },
    });
    render(<ReviewSubmission />);
    chooseFile();
    await pressSubmit();

    await screen.findByTestId('review-submit-error');
    expect(screen.queryByTestId('review-run-again-button')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// The prefill submits nothing by itself
// ---------------------------------------------------------------------------

describe('a prefill is not a resubmit (issue #70)', () => {
  it('sends the same FormData a manual selection of those values sends', async () => {
    // --- the prefilled run -------------------------------------------------
    const prefilledFetch = await submitAndBurn(storedDetail(BURNT_ID));
    fireEvent.click(screen.getByTestId('review-run-again-button'));
    await expectPrefilled();

    // One POST so far — the one that burnt. The prefill added none.
    expect(postCount(prefilledFetch)).toBe(1);

    // Even with the settings back, a submit needs a document: the lever is
    // not armed until the reviewer chooses one.
    const lever = screen.getByTestId<HTMLButtonElement>('review-submit-button');
    expect(lever.getAttribute('aria-disabled') === 'true' || lever.disabled).toBe(true);

    chooseFile();
    expect(postCount(prefilledFetch)).toBe(1);

    await pressSubmit();
    await waitFor(() => expect(postCount(prefilledFetch)).toBe(2));
    const prefilled = formDataOf(prefilledFetch, 1);

    // --- the manual run ----------------------------------------------------
    vi.unstubAllGlobals();
    screen.getByTestId('review-file-input'); // still mounted; now replace it
    document.body.innerHTML = '';
    window.localStorage?.clear();
    window.sessionStorage?.clear();

    const manualFetch = stubFetch({
      '/api/playbooks': CATALOG,
      '/api/me/preferences': { preferences: {}, notes_mode_available: false },
      'PUT /api/me/preferences': {},
      'POST /api/reviews': { review_id: BURNT_ID, resumed: false },
      [`GET /api/reviews/${BURNT_ID}`]: storedDetail(BURNT_ID),
    });
    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');
    fireEvent.change(screen.getByTestId('review-playbook-dial'), {
      target: { value: 'msa' },
    });
    fireEvent.click(screen.getByTestId('review-browning-option-dark'));
    fireEvent.click(screen.getByTestId('review-notes-mode-option-none'));
    fireEvent.change(screen.getByTestId('review-guidance-input'), {
      target: { value: STORED_GUIDANCE },
    });
    await waitFor(() => expect(readControls().playbookId).toBe('msa'));
    chooseFile();
    await pressSubmit();
    await waitFor(() => expect(postCount(manualFetch)).toBe(1));
    const manual = formDataOf(manualFetch, 0);

    // --- and they are the same request -------------------------------------
    expect(prefilled).toEqual(manual);
    // Not vacuous: these are the values the ticket is about, and they are all
    // non-default, so an empty-vs-empty comparison cannot produce this.
    expect(prefilled).toEqual({
      playbook_id: 'msa',
      markup_intensity: 'heavy',
      notes_mode: 'none',
      toaster_guidance: STORED_GUIDANCE,
      file: 'contract.docx',
    });
  });
});

/**
 * The n-th `POST /api/reviews` body, flattened to comparable values. The File
 * is compared by name rather than by identity: two separate renders cannot
 * share one File object, and what the request carries is the document's name
 * and bytes, not the reference.
 */
function formDataOf(fetchMock: ReturnType<typeof vi.fn>, index: number): Record<string, string> {
  const posts = recorded(fetchMock).filter((call) => call.path === 'POST /api/reviews');
  expect(posts.length, `expected at least ${index + 1} POST /api/reviews`).toBeGreaterThan(index);
  const body = posts[index].init?.body as FormData;
  const flat: Record<string, string> = {};
  for (const [key, value] of body.entries()) {
    flat[key] = value instanceof File ? value.name : String(value);
  }
  return flat;
}

// ---------------------------------------------------------------------------
// The failure branch
// ---------------------------------------------------------------------------

describe('a settings read that fails (issue #70)', () => {
  it('says so in fixed copy, keeps the burnt review, and leaks no server text', async () => {
    const fetchMock = stubFetch({
      '/api/playbooks': CATALOG,
      '/api/me/preferences': { preferences: {}, notes_mode_available: false },
      'POST /api/reviews': { review_id: BURNT_ID, resumed: false },
      [`GET /api/reviews/${BURNT_ID}`]: storedDetail(BURNT_ID),
    });
    render(<ReviewSubmission />);
    chooseFile();
    await pressSubmit();
    await screen.findByTestId('review-failure');

    // The detail route starts failing between the poll and the press — the
    // ordinary shape of a transient backend problem.
    stubFetch({
      '/api/playbooks': CATALOG,
      '/api/me/preferences': { preferences: {}, notes_mode_available: false },
      [`GET /api/reviews/${BURNT_ID}`]: { __status: 500 },
    });

    fireEvent.click(screen.getByTestId('review-run-again-button'));

    const banner = await screen.findByTestId('review-prefill-error');
    const text = banner.textContent ?? '';
    expect(text).toContain("We couldn't read that review's settings just now.");
    // Escaped text only: the server's own words never reach the screen.
    expect(text).not.toContain('ConnectionError');
    expect(text).not.toContain('500');

    // Nothing was thrown away to find that out: the burnt review and its
    // diagnosis are still there, and the dials never moved.
    expect(screen.getByTestId('review-failure')).toBeTruthy();
    expect(readControls()).toEqual({
      playbookId: 'nda',
      browning: 'medium',
      notesMode: 'external',
      guidance: '',
    });
    expect(screen.queryByTestId('review-prefill-note')).toBeNull();
    expectNoInputRequest(fetchMock);
  });
});
