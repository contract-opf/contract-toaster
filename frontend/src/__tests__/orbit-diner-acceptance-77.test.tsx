/**
 * orbit-diner-acceptance-77.test.tsx — the nine acceptance routes of issue
 * #77, walked end to end through the real `ReviewSubmission` → `OrbitDiner`
 * tree with a scripted fetch stub.
 *
 * ## What this file is, and what it deliberately is not
 *
 * The ticket asked for browser automation against a running deployment. The
 * issue's own "Automation" section reformulates that, and this file is the
 * reformulation: the repo carries no browser driver (devDependencies are
 * vitest, jsdom and Testing Library only) and the one runnable deployment
 * pins a paid model provider with no mock fallback, so "one real review" is
 * not reproducible offline. Routes 2–9, however, are every one of them a
 * CLASSIFIED HTTP RESPONSE rendered by this assembled tree, and that is
 * exactly what a scripted `fetch` stub can drive faithfully.
 *
 * So: one `describe` per numbered route, each asserting on the rendered DOM
 * rather than on a screenshot. What stays human — a genuinely real review
 * with real model spend against the DTS deployment — is folded into the
 * attended session for the follow-up issue and is NOT claimed here.
 *
 * `vitest.config.ts` runs jsdom with `css: false`, so nothing in this file
 * can see a stylesheet and nothing here claims visual acceptance. Where a
 * route's outcome is expressed through appearance, the assertion lands on
 * the status-named attribute or the testid the stylesheet keys off, never on
 * a computed style.
 *
 * ## Relationship to the per-ticket tests
 *
 * #717–#735 each prove one piece in isolation. This file proves the
 * SEQUENCE through the one live surface: that a review submitted through the
 * console's own lever reaches PENDING, RUNNING and DONE; that a refusal at
 * any of the five classified submit failures leaves nothing burnt; that a
 * degraded poll channel does not take the review with it. Nothing here
 * deletes or weakens an existing file — the shared placement resolvers in
 * `support/consoleSurface.ts` are imported rather than re-derived.
 *
 * ## Escaped text
 *
 * Every failure route asserts the negative as well as the positive: the
 * server's own technical `detail` (a storage env var, an authorization
 * sentence, an HTTP status line) must NOT reach the status window, the
 * receipt or the result panel. Those assertions are the reason several
 * fixtures below carry a deliberately ugly `detail` string.
 *
 * Fully offline: `../auth` is mocked, `fetch` is stubbed per test, and the
 * clipboard is never touched.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import ReviewSubmission, { POLL_INTERVAL_MS } from '../ReviewSubmission';
import { DOWNLOAD_ERROR_COPY } from '../api';
import { COVER_NOTE_FAILURE_COPY } from '../coverNote';
import { OUTCOME_CHIPS } from '../outcome';
import { allowConsoleErrorsInThisTest } from './support/consoleErrorGuard';
import {
  DEFAULT_PLAYBOOKS,
  findReviewResult,
  openDisposition,
  pressSubmit,
  progressStep,
} from './support/consoleSurface';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

// ---------------------------------------------------------------------------
// The scripted server
// ---------------------------------------------------------------------------

/**
 * One HTTP answer. `status` is the real status code, because every route in
 * this file turns on one: the console tells a 409 from a 202, and
 * `readErrorDetail` only ever reads a JSON body's `detail` string.
 */
interface Reply {
  status: number;
  body?: unknown;
  /**
   * The body is not JSON at all — the shape a proxy or load balancer returns
   * when it answers instead of the app (an HTML error page). `readErrorDetail`
   * catches the parse failure and yields no `detail`; a double that always
   * resolved `json()` would never exercise that arm.
   */
  notJson?: boolean;
}

/**
 * A route's answer, or a function of how many times it has been called — the
 * only honest way to script a POLLING endpoint, whose whole job is to say
 * something different on the next tick.
 */
type Handler = Reply | ((callNumber: number) => Reply);

const REVIEW_ID = 'rev-77';
const DETAIL_PATH = `/api/reviews/${REVIEW_ID}`;
const OUTPUT_PATH = `${DETAIL_PATH}/output`;
const CANCEL_PATH = `${DETAIL_PATH}/cancel`;
const COVER_PATH = `${DETAIL_PATH}/cover-note`;
const DISPOSITION_PATH = `${DETAIL_PATH}/disposition`;

const PRESIGNED_URL = 'https://storage.example.test/outputs/rev-77/redline.docx?sig=abc';

function ok(body: unknown): Reply {
  return { status: 200, body };
}

/** A classified refusal, carrying the backend's own `detail` sentence. */
function refuse(status: number, detail?: string): Reply {
  return { status, body: detail === undefined ? {} : { detail } };
}

/** A refusal from in front of the app, with an HTML body and no `detail`. */
function gatewayError(status: number): Reply {
  return { status, notJson: true };
}

interface Server {
  /** Every `METHOD /path` this render has requested, in order. */
  calls: string[];
  /** How many times one `METHOD /path` has been requested. */
  countOf: (key: string) => number;
}

/**
 * Stub `fetch` with a routing table keyed `METHOD /pathname`.
 *
 * `GET /api/playbooks` is seeded by default and is a FIXTURE, not a scenario:
 * `canSubmit` (orbit-diner/state.ts) refuses to arm the lever until the
 * selection names an ACTIVE catalog entry, so a table without it describes an
 * app state that never exists rather than a route worth testing.
 *
 * Anything unrouted answers 404 with the backend's own "Review not found."
 * body — the same shape the real API returns — so an accidental call is
 * visible rather than silently benign. The panel's optional loaders
 * (preferences, cost estimate, duration estimate, preflight, the in-flight
 * reattach) are all documented fail-soft reads that render nothing on a
 * failure, which is why they are left unrouted here.
 */
function stubServer(routes: Record<string, Handler>): Server {
  const calls: string[] = [];
  const table: Record<string, Handler> = {
    'GET /api/playbooks': ok(DEFAULT_PLAYBOOKS),
    ...routes,
  };
  // eslint-disable-next-line @typescript-eslint/require-await
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    // eslint-disable-next-line @typescript-eslint/no-base-to-string
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    // The appliance's sound files are fetched as ArrayBuffers, never JSON.
    if (pathname.endsWith('.mp3')) {
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) } as Response;
    }
    const key = `${method} ${pathname}`;
    calls.push(key);
    const handler = table[key];
    if (handler === undefined) {
      return {
        ok: false,
        status: 404,
        // eslint-disable-next-line @typescript-eslint/require-await
        json: async () => ({ detail: 'Review not found.' }),
      } as Response;
    }
    const callNumber = calls.filter((entry) => entry === key).length;
    const reply = typeof handler === 'function' ? handler(callNumber) : handler;
    return {
      ok: reply.status >= 200 && reply.status < 300,
      status: reply.status,
      // eslint-disable-next-line @typescript-eslint/require-await
      json: async () => {
        if (reply.notJson) {
          throw new SyntaxError('Unexpected token < in JSON at position 0');
        }
        return reply.body ?? {};
      },
    } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return {
    calls,
    countOf: (key) => calls.filter((entry) => entry === key).length,
  };
}

/** The `.docx` a reviewer drops into the slot. */
function docxFile(name = 'mutual-nda.docx'): File {
  return new File(['contract bytes'], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

/**
 * A `GET /api/reviews/{id}` body, in the shape
 * `backend/src/reviews.py::get_review_detail` actually returns. Only the
 * fields that route wrote are spelled out; everything a real row omits is
 * omitted here too, because a fixture that carries a field no writer produces
 * is a fixture asserting over a state the system cannot reach.
 */
function detail(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    review_id: REVIEW_ID,
    status: 'RUNNING',
    decision: null,
    message: null,
    has_output: false,
    has_input: true,
    progress_stage: null,
    failing_stage: null,
    reason: null,
    normalization_notes: null,
    critic_delta: null,
    issues: null,
    attorney_disposition: null,
    cancel_requested: false,
    // Epoch-seconds strings, the shape `reviews.py::create_review` writes
    // (`str(int(time.time()))`) and `get_review_detail` projects verbatim.
    created_at: String(Math.floor(Date.now() / 1000)),
    updated_at: String(Math.floor(Date.now() / 1000)),
    ...overrides,
  };
}

/** The console root, which carries the status the whole surface turns on. */
function consoleRoot(): HTMLElement {
  const node = document.querySelector<HTMLElement>('.od-console');
  if (!node) throw new Error('the Orbit Diner console did not render');
  return node;
}

function consoleStatus(): string | null {
  return consoleRoot().getAttribute('data-status');
}

/** The one dialog the console opens, and which of its five faces is up. */
function openModal(): string | null {
  return document.querySelector('dialog.od-dialog')?.getAttribute('data-modal') ?? null;
}

/** Everything the reviewer can read, as one string. */
function screenText(): string {
  return document.body.textContent ?? '';
}

/** Render, drop a `.docx` in the slot, and pull the lever. */
async function submitADocument(name?: string): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(await screen.findByTestId('review-file-input'), {
    target: { files: [docxFile(name)] },
  });
  await pressSubmit();
}

/** One poll interval's worth of tolerance (`POLL_INTERVAL_MS` is 3000 ms). */
const NEXT_POLL = { timeout: 6000 };

// jsdom's `window.location.assign` is non-configurable, so it is replaced
// wholesale for the download routes — the component must never call it (issue
// #271 item 5), and the anchor it uses instead is what these tests count.
const realLocation = window.location;
let assignMock: ReturnType<typeof vi.fn>;
let anchorClickSpy: ReturnType<typeof vi.spyOn>;
let createElementSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  assignMock = vi.fn();
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: {
      assign: assignMock,
      replace: vi.fn(),
      reload: vi.fn(),
      href: realLocation.href,
      origin: realLocation.origin,
    },
  });
  anchorClickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  createElementSpy = vi.spyOn(document, 'createElement');
});

afterEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
  vi.unstubAllGlobals();
});

/** The href of the last temporary anchor the app handed to the browser. */
function lastDownloadHref(): string | null {
  // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment, @typescript-eslint/no-unsafe-call, @typescript-eslint/no-unsafe-member-access
  const anchors = createElementSpy.mock.calls
    .map((call: unknown[], index: number) => ({ tag: call[0], index }))
    // eslint-disable-next-line @typescript-eslint/no-unsafe-member-access
    .filter((entry: { tag: unknown }) => entry.tag === 'a');
  // eslint-disable-next-line @typescript-eslint/no-unsafe-member-access
  if (anchors.length === 0) return null;
  // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment, @typescript-eslint/no-unsafe-member-access
  const last = anchors[anchors.length - 1]!.index;
  // eslint-disable-next-line @typescript-eslint/no-unsafe-member-access
  return (createElementSpy.mock.results[last]!.value as HTMLAnchorElement).href;
}

// ===========================================================================
// Route 1 — one end-to-end review, upload to redline
// ===========================================================================

describe('#77 route 1 — one review from upload to redline', () => {
  it('walks PENDING → RUNNING → DONE and hands the redline to the browser', async () => {
    const server = stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: (call) =>
        call === 1
          ? ok(detail({ status: 'PENDING' }))
          : call === 2
            ? ok(detail({ status: 'RUNNING', progress_stage: 'primary_pass' }))
            : ok(
                detail({
                  status: 'DONE',
                  decision: 'REQUEST_CHANGE',
                  has_output: true,
                  issues: [{ id: 'i-1' }, { id: 'i-2' }],
                }),
              ),
      [`GET ${OUTPUT_PATH}`]: ok({ url: PRESIGNED_URL, expires_in: 60 }),
    });

    await submitADocument();

    // The pipeline's own first status, before anything has been polled back.
    await waitFor(() => expect(consoleStatus()).toBe('pending'));
    expect(server.countOf('POST /api/reviews')).toBe(1);

    // …then a stage the pipeline actually reported. The step is never a guess.
    await waitFor(() => expect(consoleStatus()).toBe('running'), NEXT_POLL);
    expect(progressStep()).toBe(1);

    await waitFor(() => expect(consoleStatus()).toBe('done'), NEXT_POLL);
    expect(consoleRoot()).toHaveAttribute('data-terminal', 'true');

    // The completion handoff saves the redline once, through a temporary
    // anchor carrying the presigned URL — never `location.assign`, which
    // would navigate the SPA away and lose its in-memory state.
    await waitFor(() => expect(server.countOf(`GET ${OUTPUT_PATH}`)).toBe(1));
    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalledTimes(1));
    expect(lastDownloadHref()).toBe(PRESIGNED_URL);
    expect(assignMock).not.toHaveBeenCalled();

    // The visible control is a real affordance too, not decoration left over
    // from the automatic save.
    fireEvent.click(screen.getByTestId('review-download-button'));
    await waitFor(() => expect(server.countOf(`GET ${OUTPUT_PATH}`)).toBe(2));
    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalledTimes(2));

    // And the finished review's own facts are on screen, named by the
    // shared outcome map rather than by a raw status token.
    const result = await findReviewResult();
    expect(result).toHaveTextContent(OUTCOME_CHIPS.REQUEST_CHANGE.label);
    expect(result.textContent ?? '').not.toContain('REQUEST_CHANGE');
  });

  it('stops polling once the review is terminal', async () => {
    const server = stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(
        detail({ status: 'DONE', decision: 'ACCEPT', has_output: true, issues: [] }),
      ),
      [`GET ${OUTPUT_PATH}`]: ok({ url: PRESIGNED_URL, expires_in: 60 }),
    });

    await submitADocument();
    await waitFor(() => expect(consoleStatus()).toBe('done'));
    const settled = server.countOf(`GET ${DETAIL_PATH}`);

    // A FULL poll interval of quiet, read from the component's own constant:
    // anything shorter would pass even if the loop were still scheduling, and
    // would prove nothing about the endpoint the WAF rate-limits (#50).
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS + 400));
    expect(server.countOf(`GET ${DETAIL_PATH}`)).toBe(settled);
  });
});

// ===========================================================================
// Route 2 — classified submit rejections
// ===========================================================================

describe('#77 route 2 — every classified submit rejection, with nothing burnt', () => {
  it('refuses a non-.docx before any request leaves the browser', async () => {
    const server = stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
    });

    render(<ReviewSubmission />);
    fireEvent.change(await screen.findByTestId('review-file-input'), {
      target: { files: [new File(['%PDF-1.7'], 'contract.pdf', { type: 'application/pdf' })] },
    });

    expect(await screen.findByText('Choose a Word .docx document.')).toBeInTheDocument();
    // The refusal is local: nothing was uploaded, and no document was taken.
    expect(server.countOf('POST /api/reviews')).toBe(0);
    expect(consoleStatus()).toBe('empty');
  });

  it.each([
    [
      'oversize',
      413,
      // The sentence's ONLY producer interpolates
      // `upload_validation.MAX_UPLOAD_SIZE_BYTES` (`_check_size`), which is
      // `25 * 1024 * 1024` — so the number here is the cap the deployment
      // actually enforces, not a rounder one.
      `Upload exceeds the maximum allowed size of ${25 * 1024 * 1024} bytes.`,
    ],
    [
      'the OOXML gauntlet',
      400,
      'File could not be opened as a valid ZIP/OOXML container.',
    ],
    ['no active playbook', 503, 'no active playbook'],
    [
      'the daily cap',
      429,
      'Daily spend limit reached. Try again after the cap resets (UTC midnight).',
    ],
  ])('renders the classified refusal for %s and burns nothing', async (_label, status, detailText) => {
    const server = stubServer({ 'POST /api/reviews': refuse(status, detailText) });

    await submitADocument();

    const banner = await screen.findByTestId('review-submit-error');
    expect(banner).toHaveTextContent(detailText);
    // Each refusal is its OWN copy: a cap is not a corrupt file.
    expect(banner.textContent ?? '').not.toContain("We couldn't submit");

    // Nothing was started, so nothing can have burnt: no review is attached,
    // the appliance is back to holding a document, and the burnt-toast
    // treatment is nowhere on screen.
    await waitFor(() => expect(consoleStatus()).toBe('loaded'));
    expect(screen.queryByTestId('review-id-row')).toBeNull();
    expect(screen.queryByTestId('toaster-state-error')).toBeNull();
    expect(screen.queryByTestId('review-retry-button')).toBeNull();
    expect(server.countOf(`GET ${DETAIL_PATH}`)).toBe(0);
  });

  it('shows fixed copy — never the raw response — when the server classified nothing', async () => {
    // An HTML error page from in front of the app: `readErrorDetail`'s parse
    // catches, so there is no classified sentence to render. The status line
    // goes to the console for an operator; the reviewer gets one honest
    // sentence, and never the response body.
    stubServer({ 'POST /api/reviews': gatewayError(500) });

    await submitADocument();

    const banner = await screen.findByTestId('review-submit-error');
    expect(banner).toHaveTextContent(/couldn[’']t submit your file for review/i);
    expect(screenText()).not.toMatch(/HTTP 500/);
    expect(screenText()).not.toMatch(/Traceback/i);
  });

  it('lets the reviewer try again from the refusal itself', async () => {
    const server = stubServer({
      'POST /api/reviews': (call) =>
        call === 1
          ? refuse(503, 'no active playbook')
          : ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(detail({ status: 'RUNNING' })),
    });

    await submitADocument();
    await screen.findByTestId('review-submit-error');

    fireEvent.click(screen.getByRole('button', { name: /try again/i }));

    await waitFor(() => expect(server.countOf('POST /api/reviews')).toBe(2));
    await waitFor(() => expect(consoleStatus()).toBe('running'));
    expect(screen.queryByTestId('review-submit-error')).toBeNull();
  });
});

// ===========================================================================
// Route 3 — a poll failure
// ===========================================================================

describe('#77 route 3 — a poll failure keeps the last real state and burns nothing', () => {
  it('keeps the running review on screen and says only that it is still checking', async () => {
    stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: (call) =>
        call === 1
          ? ok(detail({ status: 'RUNNING', progress_stage: 'critic_pass' }))
          : refuse(500, 'DYNAMODB_TABLE_NAME not configured.'),
    });

    await submitADocument();
    await waitFor(() => expect(progressStep()).toBe(2));

    const notice = await screen.findByTestId('review-poll-error', {}, NEXT_POLL);

    // The CHANNEL is degraded; the REVIEW is not.
    expect(consoleStatus()).toBe('running');
    expect(progressStep()).toBe(2);
    expect(screen.queryByTestId('toaster-state-error')).toBeNull();
    expect(screen.queryByTestId('review-result')).toBeNull();

    // A hiccup that heals itself on the next tick is a status, not an alert
    // worth interrupting a screen reader for.
    expect(notice.getAttribute('role')).toBe('status');
    // …and the server's own configuration detail is not the reviewer's to read.
    expect(screenText()).not.toContain('DYNAMODB_TABLE_NAME');
  });

  it('clears the notice and renders the real outcome once the channel recovers', async () => {
    stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: (call) =>
        call === 1
          ? ok(detail({ status: 'RUNNING', progress_stage: 'primary_pass' }))
          : call === 2
            ? refuse(502)
            : ok(detail({ status: 'DONE', decision: 'ACCEPT', has_output: false, issues: [] })),
      [`GET ${OUTPUT_PATH}`]: refuse(404, 'This review has finished with no output to download.'),
    });

    await submitADocument();
    await screen.findByTestId('review-poll-error', {}, NEXT_POLL);
    await waitFor(() => expect(consoleStatus()).toBe('done'), NEXT_POLL);
    expect(screen.queryByTestId('review-poll-error')).toBeNull();
  });
});

// ===========================================================================
// Route 4 — the cancellation / completion race
// ===========================================================================

describe('#77 route 4 — the cancel/completion race', () => {
  it('202 acknowledges the request and keeps polling until the pipeline stops', async () => {
    const server = stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`POST ${CANCEL_PATH}`]: {
        status: 202,
        body: { review_id: REVIEW_ID, status: 'RUNNING', cancel_requested: true },
      },
      [`GET ${DETAIL_PATH}`]: (call) =>
        call < 3
          ? ok(detail({ status: 'RUNNING', progress_stage: 'primary_pass' }))
          : ok(detail({ status: 'CANCELLED' })),
    });

    await submitADocument();
    const lever = await screen.findByTestId('review-cancel-button');
    const polledBefore = server.countOf(`GET ${DETAIL_PATH}`);
    fireEvent.click(lever.querySelector('button') ?? lever);

    await waitFor(() => expect(server.countOf(`POST ${CANCEL_PATH}`)).toBe(1));
    // 202 is "we asked", not "it stopped" — the control must say so, and the
    // poller must keep going, because the pipeline stops at its own next
    // checkpoint and only the poll can report that.
    await waitFor(() =>
      expect(screen.getByTestId('review-cancel-button')).toHaveAttribute('aria-disabled', 'true'),
    );
    expect(screen.getByTestId('review-cancel-button').textContent).toMatch(/stopping/i);
    await waitFor(
      () => expect(server.countOf(`GET ${DETAIL_PATH}`)).toBeGreaterThan(polledBefore),
      NEXT_POLL,
    );

    await waitFor(() => expect(consoleStatus()).toBe('cancelled'), NEXT_POLL);
    // A review the reviewer stopped rests; it does not burn: the appliance
    // reads "Cancelled" rather than wearing the burnt-toast treatment, and
    // the record names the outcome by the shared map's own label.
    expect(screen.queryByTestId('toaster-state-error')).toBeNull();
    expect(screenText()).toContain('Cancelled');
    expect(await findReviewResult()).toHaveTextContent(OUTCOME_CHIPS.CANCELLED.label);
  });

  it('409 says the review finished first, and completion wins the race', async () => {
    stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      // The cancel route's 409 `detail` is an OBJECT, not a sentence
      // (`review_routes.post_review_cancel`), which is exactly why the panel
      // must not try to render it.
      [`POST ${CANCEL_PATH}`]: {
        status: 409,
        body: { detail: { message: 'Review is already terminal.', status: 'DONE' } },
      },
      [`GET ${DETAIL_PATH}`]: (call) =>
        call === 1
          ? ok(detail({ status: 'RUNNING', progress_stage: 'redline' }))
          : ok(
              detail({
                status: 'DONE',
                decision: 'REQUEST_CHANGE',
                has_output: true,
                issues: [{ id: 'i-1' }],
              }),
            ),
      [`GET ${OUTPUT_PATH}`]: ok({ url: PRESIGNED_URL, expires_in: 60 }),
    });

    await submitADocument();
    const lever = await screen.findByTestId('review-cancel-button');
    fireEvent.click(lever.querySelector('button') ?? lever);

    const notice = await screen.findByTestId('review-cancel-error');
    expect(notice).toHaveTextContent(/finished before it could be stopped/i);
    // The server's raw 409 sentence is not what the reviewer reads.
    expect(screenText()).not.toContain('Review is already terminal.');

    // The real terminal outcome arrives from the poll, and it is COMPLETION.
    await waitFor(() => expect(consoleStatus()).toBe('done'), NEXT_POLL);
    expect(screenText()).not.toContain(OUTCOME_CHIPS.CANCELLED.label);
    expect(await findReviewResult()).toHaveTextContent(OUTCOME_CHIPS.REQUEST_CHANGE.label);
  });
});

// ===========================================================================
// Route 5 — the manual-review handoff, both spellings
// ===========================================================================

describe('#77 route 5 — a manual-review handoff gets the calm treatment', () => {
  // Each spelling is seeded with a reason `reviews.py`'s
  // STAGE_FAILURE_REASON_STATUS maps to THAT status. The table lists every
  // token explicitly so a row's (status, reason) pair can be read straight
  // off it, and a pair it does not list is a row no writer produces.
  it.each([
    [
      'MANUAL_REVIEW_REQUIRED',
      OUTCOME_CHIPS.MANUAL_REVIEW_REQUIRED.label,
      // A provider-side length rejection — the document's own problem,
      // measured inside the review call, so `run_review` is its stage.
      'model_context_length_exceeded',
    ],
    [
      'ERROR_MANUAL_REVIEW_REQUIRED',
      OUTCOME_CHIPS.ERROR_MANUAL_REVIEW_REQUIRED.label,
      // `primary_review_pass` returns this as a terminal status DICT rather
      // than raising, and `pipeline_runner`'s `result["status"] != "OK"`
      // branch is what stamps `failing_stage = "run_review"` on it — so the
      // whole triple below is written together by one call site.
      'structured_output_retry_exhausted',
    ],
  ])('%s reads as a next step, not as a malfunction', async (status, label, reason) => {
    stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(detail({ status, reason, failing_stage: 'run_review' })),
    });

    await submitADocument();

    await waitFor(() => expect(consoleStatus()).toBe(status.toLowerCase()));
    expect(await screen.findByTestId(`toaster-state-${status.toLowerCase()}`)).toBeInTheDocument();

    // The result stays ON THE PAGE — nobody should have to open something to
    // read that a human is needed — and it is headed as the next step.
    const result = await screen.findByTestId('review-result');
    expect(result).toHaveTextContent(label);
    expect(result).toHaveTextContent('Next step');
    expect(result.textContent ?? '').not.toContain('What happened');

    // Calm: not the burnt-toast treatment, and not the burnt review's reset.
    expect(document.querySelector('.od-failure')).toBeNull();
    expect(screen.queryByTestId('toaster-state-error')).toBeNull();
    expect(screen.queryByTestId('review-retry-button')).toBeNull();
    expect(screenText()).toContain('Needs a human');
    // The raw status token is never what the reviewer reads.
    expect(screenText()).not.toContain(status);
  });

  it('offers the record, and the disposition capture, on a manual handoff', async () => {
    stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(detail({ status: 'MANUAL_REVIEW_REQUIRED' })),
    });

    await submitADocument();
    await waitFor(() => expect(consoleStatus()).toBe('manual_review_required'));

    expect(await openDisposition()).toBeInTheDocument();
  });
});

// ===========================================================================
// Route 6 — a download failure
// ===========================================================================

describe('#77 route 6 — a download failure keeps the review, and retries nothing', () => {
  it('renders the fixed storage copy, keeps DONE, and saves exactly once', async () => {
    // The server's `detail` here is deployment configuration — the exact
    // shape of the bug #465 fixed — and must never reach the screen.
    allowConsoleErrorsInThisTest(/OUTPUTS_BUCKET not configured/);
    const server = stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(
        detail({
          status: 'DONE',
          decision: 'REQUEST_CHANGE',
          has_output: true,
          issues: [{ id: 'i-1' }],
        }),
      ),
      [`GET ${OUTPUT_PATH}`]: refuse(503, 'OUTPUTS_BUCKET not configured.'),
    });

    await submitADocument();
    await waitFor(() => expect(consoleStatus()).toBe('done'));

    const banner = await screen.findByTestId('review-download-error');
    expect(banner).toHaveTextContent(DOWNLOAD_ERROR_COPY);
    // No false promise that pressing again fixes a mis-configured bucket.
    expect(banner.textContent ?? '').not.toMatch(/please try again/i);
    expect(screenText()).not.toContain('OUTPUTS_BUCKET');

    // The review itself finished and stays finished.
    expect(consoleStatus()).toBe('done');
    expect(consoleRoot()).toHaveAttribute('data-terminal', 'true');

    // Exactly ONE automatic attempt: the handoff is keyed on the review id,
    // so a failure does not re-arm itself on the re-renders the failure
    // banner itself causes, and nothing was handed to the browser.
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(server.countOf(`GET ${OUTPUT_PATH}`)).toBe(1);
    expect(anchorClickSpy).not.toHaveBeenCalled();
    expect(assignMock).not.toHaveBeenCalled();
  });

  it('leaves the save control working as the reviewer’s own retry path', async () => {
    allowConsoleErrorsInThisTest(/OUTPUTS_BUCKET not configured/);
    const server = stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(
        detail({
          status: 'DONE',
          decision: 'REQUEST_CHANGE',
          has_output: true,
          issues: [{ id: 'i-1' }],
        }),
      ),
      [`GET ${OUTPUT_PATH}`]: (call) =>
        call === 1
          ? refuse(503, 'OUTPUTS_BUCKET not configured.')
          : ok({ url: PRESIGNED_URL, expires_in: 60 }),
    });

    await submitADocument();
    await screen.findByTestId('review-download-error');

    fireEvent.click(screen.getByTestId('review-download-button'));

    await waitFor(() => expect(server.countOf(`GET ${OUTPUT_PATH}`)).toBe(2));
    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalledTimes(1));
    expect(lastDownloadHref()).toBe(PRESIGNED_URL);
    await waitFor(() => expect(screen.queryByTestId('review-download-error')).toBeNull());
  });
});

// ===========================================================================
// Route 7 — the cover note
// ===========================================================================

/** A finished REQUEST_CHANGE review, the only shape "Butter it" is offered on. */
const BUTTERABLE = detail({
  status: 'DONE',
  decision: 'REQUEST_CHANGE',
  has_output: true,
  issues: [{ id: 'i-1' }],
});

describe('#77 route 7 — the cover note retries what is retryable, and nothing else', () => {
  it('offers a retry after a 502 and succeeds on the second press', async () => {
    // `coverNote.ts` logs the 502's own `detail` for support correlation and
    // renders the quiet copy instead; the log is expected here and nowhere else.
    allowConsoleErrorsInThisTest(/butter this one/);
    const server = stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(BUTTERABLE),
      [`GET ${OUTPUT_PATH}`]: ok({ url: PRESIGNED_URL, expires_in: 60 }),
      [`POST ${COVER_PATH}`]: (call) =>
        call === 1
          ? refuse(502, COVER_NOTE_FAILURE_COPY)
          : ok({
              review_id: REVIEW_ID,
              draft: 'Attached is our markup. We restored the liability cap.',
              cost_usd_cents: 3,
              cached: false,
              generated_at: '1789257600',
              served_model_id: 'anthropic/claude-sonnet-5',
              last_generation_cost_usd_cents: null,
            }),
    });

    await submitADocument();
    await waitFor(() => expect(consoleStatus()).toBe('done'));
    fireEvent.click(await screen.findByTestId('review-cover-note-butter'));

    // Quiet, not alarming: the redline is unaffected, and another press is
    // an honest offer for this failure.
    const quiet = await screen.findByTestId('review-cover-note-error');
    expect(quiet).toHaveTextContent(COVER_NOTE_FAILURE_COPY);
    expect(screen.queryByTestId('review-cover-note-card')).toBeNull();

    fireEvent.click(screen.getByTestId('review-cover-note-retry'));

    const card = await screen.findByTestId('review-cover-note-card');
    expect(card).toBeInTheDocument();
    expect(screen.getByTestId('review-cover-note-text').textContent).toContain(
      'restored the liability cap',
    );
    expect(server.countOf(`POST ${COVER_PATH}`)).toBe(2);
    expect(screen.queryByTestId('review-cover-note-error')).toBeNull();
  });

  it.each([
    [404, 'Review not found.'],
    [403, 'You are not the owner of this review and do not have admin privileges.'],
    [409, 'This review has no requested changes to describe.'],
  ])('treats %i as final: a banner, no retry, no second request', async (status, detailText) => {
    allowConsoleErrorsInThisTest(new RegExp(detailText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
    const server = stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(BUTTERABLE),
      [`GET ${OUTPUT_PATH}`]: ok({ url: PRESIGNED_URL, expires_in: 60 }),
      [`POST ${COVER_PATH}`]: refuse(status, detailText),
    });

    await submitADocument();
    await waitFor(() => expect(consoleStatus()).toBe('done'));
    fireEvent.click(await screen.findByTestId('review-cover-note-butter'));

    const banner = await screen.findByTestId('review-cover-note-real-error');
    expect(banner).toHaveTextContent(/couldn[’']t butter this one/i);
    // A retry key here would be a lie the button tells: this never changes.
    expect(screen.queryByTestId('review-cover-note-retry')).toBeNull();
    expect(screen.queryByTestId('review-cover-note-error')).toBeNull();
    // The server's own sentence — which names ownership, or the row's state —
    // stays in the console for support, never on screen.
    expect(screenText()).not.toContain(detailText);
    expect(server.countOf(`POST ${COVER_PATH}`)).toBe(1);

    // And the redline the note was only ever a courtesy beside is untouched.
    expect(consoleStatus()).toBe('done');
    expect(screen.getByTestId('review-download-button')).toBeInTheDocument();
  });
});

// ===========================================================================
// Route 8 — the disposition capture
// ===========================================================================

describe('#77 route 8 — a disposition save can fail and then succeed', () => {
  it('says it was not recorded, then records it on the retry', async () => {
    const server = stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(BUTTERABLE),
      [`GET ${OUTPUT_PATH}`]: ok({ url: PRESIGNED_URL, expires_in: 60 }),
      [`POST ${DISPOSITION_PATH}`]: (call) =>
        call === 1
          ? // A 502 from in front of the app (nginx reverse-proxies /api/ —
            // see api.ts's `authorizedFetch` note), which is the transient
            // failure a second press can actually get past. The app installs
            // no exception handler of its own, so a failure INSIDE it comes
            // back as Starlette's plain-text default rather than as JSON with
            // an empty body: `gatewayError`'s unparseable body is the honest
            // shape for either, and it exercises `readErrorDetail`'s catch.
            gatewayError(502)
          : ok({
              review_id: REVIEW_ID,
              attorney_disposition: 'EDITED',
              attorney_disposition_recorded_at: '1789257600',
              // An EDITED outcome always enqueues for triage
              // (`disposition.py::record_disposition`); only ACCEPTED leaves this unset.
              legal_triage_status: 'PENDING_TRIAGE',
            }),
    });

    await submitADocument();
    await waitFor(() => expect(consoleStatus()).toBe('done'));

    await openDisposition();
    fireEvent.change(screen.getByTestId('review-disposition-note'), {
      target: { value: 'Tightened the cap before sending.' },
    });
    fireEvent.click(screen.getByTestId('review-disposition-edited'));

    const failure = await screen.findByTestId('review-disposition-error');
    expect(failure).toHaveTextContent(/couldn[’']t record that/i);
    // Nothing was stamped, so the controls are still offered.
    expect(screen.queryByTestId('review-disposition-recorded')).toBeNull();
    expect(screen.getByTestId('review-disposition-edited')).toBeEnabled();
    // The failure did not eat the note the reviewer typed.
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    expect((screen.getByTestId('review-disposition-note') as HTMLTextAreaElement).value).toBe(
      'Tightened the cap before sending.',
    );

    fireEvent.click(screen.getByTestId('review-disposition-edited'));

    const stamp = await screen.findByTestId('review-disposition-recorded');
    // The console keeps its own display names (`dispositionNames`), so this
    // asserts the SENSE — an acceptance that was modified — rather than
    // pinning a wording two modules already spell differently.
    expect(stamp).toHaveTextContent(/recorded:\s*accepted with/i);
    expect(server.countOf(`POST ${DISPOSITION_PATH}`)).toBe(2);
    expect(screen.queryByTestId('review-disposition-error')).toBeNull();
    // The raw wire token is never what a reviewer reads.
    expect(stamp.textContent ?? '').not.toContain('EDITED');
  });

  it('never lets the route’s own refusal sentence reach the screen', async () => {
    // The disposition banner is the one failure surface in this file that
    // renders a VARIABLE string — `handleRecordDisposition` shows
    // `err.message`. What keeps that safe is one link further down:
    // `disposition.ts` builds the thrown message through
    // `friendlyErrorMessage`, which logs the technical detail and returns the
    // FIXED copy. So this drives the route's own authorization refusal (the
    // 403 `post_review_disposition` raises before anything about the review
    // is disclosed) and pins both halves: the sentence in the console, the
    // fixed copy on screen.
    const REFUSAL = 'You are not the owner of this review and do not have admin privileges.';
    allowConsoleErrorsInThisTest(/not the owner of this review/);
    const server = stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(BUTTERABLE),
      [`GET ${OUTPUT_PATH}`]: ok({ url: PRESIGNED_URL, expires_in: 60 }),
      [`POST ${DISPOSITION_PATH}`]: refuse(403, REFUSAL),
    });

    await submitADocument();
    await waitFor(() => expect(consoleStatus()).toBe('done'));

    await openDisposition();
    fireEvent.click(screen.getByTestId('review-disposition-accepted'));

    const failure = await screen.findByTestId('review-disposition-error');
    expect(failure).toHaveTextContent(/couldn[’']t record that/i);
    // Not the sentence, and not any fragment of it that would disclose the
    // same thing on its own.
    expect(screenText()).not.toContain(REFUSAL);
    expect(screenText()).not.toContain('admin privileges');
    expect(screenText()).not.toMatch(/HTTP 403/);

    // Nothing was stamped, and a permanent refusal is not retried for them.
    expect(screen.queryByTestId('review-disposition-recorded')).toBeNull();
    expect(server.countOf(`POST ${DISPOSITION_PATH}`)).toBe(1);
    // The review itself is untouched by a failed optional capture.
    expect(consoleStatus()).toBe('done');
  });
});

// ===========================================================================
// Route 9 — the host's global shortcuts, with each dialog open
// ===========================================================================

/**
 * The console's keyboard guard, measured from INSIDE an open dialog.
 *
 * Both layers have to hold and both are exercised by firing here: the kit
 * stops the event while a modal is up, and `ReviewSubmission`'s own window
 * dispatcher refuses any target inside `dialog[open]`. Every branch of that
 * dispatcher reaches a control BEHIND the scrim — it would focus a dial the
 * reader cannot see, eject their document out from under an open receipt, or
 * swap the cheat sheet in for whatever they were reading.
 */
function fireHostVocabularyInside(target: HTMLElement): void {
  fireEvent.keyDown(target, { key: '?' });
  fireEvent.keyDown(target, { key: '/', metaKey: true });
  fireEvent.keyDown(target, { key: 'p', metaKey: true, shiftKey: true });
  fireEvent.keyDown(target, { key: 'g', ctrlKey: true, shiftKey: true });
  fireEvent.keyDown(target, { key: 'm', metaKey: true, shiftKey: true });
  fireEvent.keyDown(target, { key: 'u', metaKey: true });
  fireEvent.keyDown(target, { key: 'Enter', metaKey: true });
}

/** The sound key's accessible name, which says what the toggle is set to. */
function soundLabel(): string | null {
  return document.querySelector('[aria-label^="App sounds"]')?.getAttribute('aria-label') ?? null;
}

/**
 * A `<dialog open>` outside the React container, with something focusable in
 * it — removed by the afterEach below.
 *
 * Why this is here and not a shortcut: with the console mounted, the kit's own
 * handler calls `stopPropagation` while a modal is up, so an event fired
 * inside a console dialog never reaches the window listener at all. That is
 * the right composed behaviour and the tests above assert it — but it also
 * means those tests would stay green if `ReviewSubmission`'s own
 * `dialog[open]` guard were deleted, which would leave the host dispatcher
 * one moved listener away from reaching a dial behind a scrim again. This is
 * the one way to hand that dispatcher a target inside an open dialog and see
 * what IT does. Same technique, and the same reason, as
 * `orbit-diner-shortcuts-720.test.tsx`.
 */
function handBuiltDialog(): HTMLButtonElement {
  const dialog = document.createElement('dialog');
  dialog.setAttribute('open', '');
  dialog.setAttribute('data-testid', 'acceptance-77-hand-built-dialog');
  const inner = document.createElement('button');
  dialog.appendChild(inner);
  document.body.appendChild(dialog);
  inner.focus();
  return inner;
}

afterEach(() => {
  document.querySelector('[data-testid="acceptance-77-hand-built-dialog"]')?.remove();
});

describe('#77 route 9 — the host global shortcuts, with each console dialog open', () => {
  it.each([
    // eslint-disable-next-line @typescript-eslint/require-await
    ['shortcuts', async () => fireEvent.click(screen.getByTestId('review-shortcuts-key'))],
    [
      'playbooks',
      // eslint-disable-next-line @typescript-eslint/require-await
      async () => fireEvent.click(screen.getByRole('button', { name: /browse playbooks/i })),
    ],
    [
      'receipt',
      // eslint-disable-next-line @typescript-eslint/require-await
      async () => fireEvent.click(screen.getByRole('button', { name: /view review receipt/i })),
    ],
    // eslint-disable-next-line @typescript-eslint/require-await
    ['cover', async () => fireEvent.click(screen.getByTestId('review-cover-note-butter'))],
    [
      'disposition',
      // eslint-disable-next-line @typescript-eslint/require-await
      async () => fireEvent.click(screen.getByRole('button', { name: /record outcome/i })),
    ],
  ])('reaches nothing behind the %s dialog, and Escape closes it', async (modal, openIt) => {
    stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(BUTTERABLE),
      [`GET ${OUTPUT_PATH}`]: ok({ url: PRESIGNED_URL, expires_in: 60 }),
      [`POST ${COVER_PATH}`]: ok({
        review_id: REVIEW_ID,
        draft: 'Attached is our markup.',
        cost_usd_cents: 3,
        cached: false,
        generated_at: '1789257600',
        served_model_id: 'anthropic/claude-sonnet-5',
        last_generation_cost_usd_cents: null,
      }),
    });

    await submitADocument();
    await waitFor(() => expect(consoleStatus()).toBe('done'));

    const soundBefore = soundLabel();
    expect(soundBefore).toBeTruthy();

    await openIt();
    await waitFor(() => expect(openModal()).toBe(modal));
    const paper = document.querySelector<HTMLElement>('.od-dialog-paper');
    expect(paper).not.toBeNull();
    const focusedBefore = document.activeElement;

    fireHostVocabularyInside(paper!);

    // Whatever the reader opened is still what is open — the cheat sheet did
    // not swap itself in — and no chord moved focus to a control behind the
    // scrim: the dial and the instructions pad are exactly what Cmd/Ctrl +
    // Shift + P and + G would have grabbed.
    expect(openModal()).toBe(modal);
    expect(document.activeElement).toBe(focusedBefore);
    expect(document.activeElement).not.toBe(screen.queryByTestId('review-playbook-dial'));
    expect(document.activeElement).not.toBe(screen.queryByTestId('review-guidance-input'));
    expect(soundLabel()).toBe(soundBefore);
    // The finished review is untouched: nothing was ejected or resubmitted.
    expect(consoleStatus()).toBe('done');

    // Escape belongs to the dialog, and closes exactly it.
    fireEvent.keyDown(paper!, { key: 'Escape' });
    await waitFor(() => expect(openModal()).toBeNull());
    expect(consoleStatus()).toBe('done');
  });

  it('reaches nothing behind the record dialog either', async () => {
    // The record overlay is offered where there is no redline to print, which
    // is exactly the manual handoff of route 5.
    stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(detail({ status: 'MANUAL_REVIEW_REQUIRED' })),
    });

    await submitADocument();
    await waitFor(() => expect(consoleStatus()).toBe('manual_review_required'));

    const soundBefore = soundLabel();
    fireEvent.click(await screen.findByTestId('review-details-button'));
    await waitFor(() => expect(openModal()).toBe('record'));

    const paper = document.querySelector<HTMLElement>('.od-dialog-paper')!;
    const focusedBefore = document.activeElement;
    fireHostVocabularyInside(paper);

    expect(openModal()).toBe('record');
    expect(document.activeElement).toBe(focusedBefore);
    expect(soundLabel()).toBe(soundBefore);

    fireEvent.keyDown(paper, { key: 'Escape' });
    await waitFor(() => expect(openModal()).toBeNull());
  });

  it('refuses its own vocabulary for any open dialog, not only the console’s', async () => {
    // The host dispatcher, on its own terms: a live document, a dial and an
    // instructions pad to steal, and a target inside `dialog[open]`.
    stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(detail({ status: 'RUNNING' })),
    });

    render(<ReviewSubmission />);
    fireEvent.change(await screen.findByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await waitFor(() => expect(consoleStatus()).toBe('loaded'));
    fireEvent.click(screen.getByTestId('review-shortcuts-key'));
    await waitFor(() => expect(openModal()).toBe('shortcuts'));

    const inner = handBuiltDialog();
    const soundBefore = soundLabel();

    // Every branch releases the keystroke rather than claiming it…
    expect(fireEvent.keyDown(inner, { key: 'p', metaKey: true, shiftKey: true })).toBe(true);
    expect(fireEvent.keyDown(inner, { key: 'g', ctrlKey: true, shiftKey: true })).toBe(true);
    expect(fireEvent.keyDown(inner, { key: 'm', metaKey: true, shiftKey: true })).toBe(true);
    expect(fireEvent.keyDown(inner, { key: '?' })).toBe(true);
    expect(fireEvent.keyDown(inner, { key: 'Escape' })).toBe(true);

    // …and nothing behind the scrim moved: focus stayed in the dialog, the
    // appliance's sound setting is untouched, and the reviewer's document was
    // not ejected out from under them.
    expect(document.activeElement).toBe(inner);
    expect(soundLabel()).toBe(soundBefore);
    expect(consoleStatus()).toBe('loaded');
    expect(screen.getByTestId('review-playbook-dial')).not.toBe(document.activeElement);
  });

  it('still answers the vocabulary once no dialog is open', async () => {
    // The guard is a guard, not a kill switch: with the scrim gone the same
    // keystroke reaches the same control it always did.
    stubServer({
      'POST /api/reviews': ok({ review_id: REVIEW_ID, resumed: false }),
      [`GET ${DETAIL_PATH}`]: ok(detail({ status: 'RUNNING' })),
    });

    render(<ReviewSubmission />);
    fireEvent.change(await screen.findByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await waitFor(() => expect(consoleStatus()).toBe('loaded'));

    expect(fireEvent.keyDown(window, { key: '?' })).toBe(false);
    await waitFor(() => expect(openModal()).toBe('shortcuts'));

    fireEvent.keyDown(document.querySelector('.od-dialog-paper')!, { key: 'Escape' });
    await waitFor(() => expect(openModal()).toBeNull());

    expect(fireEvent.keyDown(window, { key: 'p', metaKey: true, shiftKey: true })).toBe(false);
    expect(document.activeElement).toBe(screen.getByTestId('review-playbook-dial'));
  });
});
