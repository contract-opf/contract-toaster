/**
 * cover-note-499.test.tsx — "Butter it" (issue #499): the finished-review
 * panel's cover-note draft control.
 *
 * Fully offline: auth is mocked, fetch is stubbed per test, and
 * `navigator.clipboard` is stubbed to assert the Copy button never sends
 * anything anywhere — it only writes to the clipboard.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import ReviewHistory, { type HistoryRow } from '../ReviewHistory';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (pathname.endsWith('.mp3')) {
      return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) } as Response;
    }
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const entry = routes[key];
    if (entry === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    if (entry && typeof entry === 'object' && '__httpStatus' in (entry as Record<string, unknown>)) {
      const { __httpStatus, body } = entry as { __httpStatus: number; body: unknown };
      return {
        ok: __httpStatus >= 200 && __httpStatus < 300,
        status: __httpStatus,
        json: async () => body,
      } as Response;
    }
    return { ok: true, status: 200, json: async () => entry } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

async function submitAndReachResult(): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-result');
}

const REQUEST_CHANGE_DETAIL = {
  review_id: 'rev-1',
  status: 'DONE',
  decision: 'REQUEST_CHANGE',
  message: null,
  has_output: true,
};

const ACCEPT_DETAIL = {
  review_id: 'rev-1',
  status: 'DONE',
  decision: 'ACCEPT',
  message: null,
  has_output: true,
};

const DRAFT_TEXT =
  'Attached is our markup of the agreement. We restored the liability cap. ' +
  'Happy to discuss.';

describe('ReviewSubmission — "Butter it" cover-note draft (issue #499)', () => {
  it('does not offer the control for an ACCEPT review (nothing to describe)', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': ACCEPT_DETAIL,
    });
    await submitAndReachResult();

    expect(screen.queryByTestId('review-cover-note')).toBeNull();
  });

  it('drafts a note on click, showing the draft text and its real cost', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': REQUEST_CHANGE_DETAIL,
      'POST /api/reviews/rev-1/cover-note': {
        review_id: 'rev-1',
        draft: DRAFT_TEXT,
        cost_usd_cents: 2,
        cached: false,
        generated_at: '1700000000',
        served_model_id: 'anthropic/claude-sonnet-5',
      },
    });
    await submitAndReachResult();

    expect(screen.getByTestId('review-cover-note-butter')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('review-cover-note-butter'));

    const card = await screen.findByTestId('review-cover-note-card');
    expect(card).toBeInTheDocument();
    expect(screen.getByTestId('review-cover-note-text').textContent).toBe(DRAFT_TEXT);
    expect(screen.getByTestId('review-cover-note-cost').textContent).toContain('$0.02');
    expect(screen.queryByTestId('review-cover-note-butter')).toBeNull();

    const call = fetchCallFor(fetch as unknown as ReturnType<typeof vi.fn>, 'POST', '/api/reviews/rev-1/cover-note');
    expect(JSON.parse((call![1] as RequestInit).body as string)).toEqual({ regenerate: false });
  });

  it('copy puts the draft on the clipboard — never a send, never a mailto', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': REQUEST_CHANGE_DETAIL,
      'POST /api/reviews/rev-1/cover-note': {
        review_id: 'rev-1',
        draft: DRAFT_TEXT,
        cost_usd_cents: 2,
        cached: false,
        generated_at: '1700000000',
        served_model_id: 'anthropic/claude-sonnet-5',
      },
    });
    await submitAndReachResult();
    fireEvent.click(screen.getByTestId('review-cover-note-butter'));
    await screen.findByTestId('review-cover-note-card');

    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    fireEvent.click(screen.getByTestId('review-cover-note-copy'));
    expect(writeText).toHaveBeenCalledWith(DRAFT_TEXT);
    expect(await screen.findByText('Copied!')).toBeInTheDocument();

    // No mailto/href anywhere on this card.
    const card = screen.getByTestId('review-cover-note-card');
    expect(card.querySelector('a[href^="mailto:"]')).toBeNull();
  });

  it('a revisit without regenerate serves the cached draft for free', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': REQUEST_CHANGE_DETAIL,
      'POST /api/reviews/rev-1/cover-note': {
        review_id: 'rev-1',
        draft: DRAFT_TEXT,
        cost_usd_cents: 0,
        cached: true,
        generated_at: '1700000000',
        served_model_id: 'anthropic/claude-sonnet-5',
        last_generation_cost_usd_cents: 2,
      },
    });
    await submitAndReachResult();
    fireEvent.click(screen.getByTestId('review-cover-note-butter'));

    await screen.findByTestId('review-cover-note-card');
    expect(screen.getByTestId('review-cover-note-cost').textContent).toMatch(/no charge/i);

    // Issue #499 fix round 1: the cached path must still seed the
    // Regenerate button's cost hint from the backend's stored
    // `last_generation_cost_usd_cents` -- without it, this is the common
    // real path (reload / History revisit → "View cover note draft") and
    // the button would render bare "Regenerate" with no cents.
    expect(screen.getByTestId('review-cover-note-regenerate').textContent).toContain('$0.02');
  });

  it('regenerate posts regenerate:true and replaces the shown draft', async () => {
    const secondDraft = 'Attached is an updated markup. Same substantive point. Happy to discuss.';
    let call = 0;
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': REQUEST_CHANGE_DETAIL,
    });
    // Override the cover-note route with a sequencing fake so the two POSTs
    // return different bodies.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const pathname = new URL(String(input), 'http://localhost').pathname;
        const method = (init?.method ?? 'GET').toUpperCase();
        if (pathname === '/api/reviews' && method === 'POST') {
          return { ok: true, status: 200, json: async () => ({ review_id: 'rev-1', resumed: false }) } as Response;
        }
        if (pathname === '/api/reviews/rev-1' && method === 'GET') {
          return { ok: true, status: 200, json: async () => REQUEST_CHANGE_DETAIL } as Response;
        }
        if (pathname === '/api/reviews/rev-1/cover-note' && method === 'POST') {
          call += 1;
          const draft = call === 1 ? DRAFT_TEXT : secondDraft;
          return {
            ok: true,
            status: 200,
            json: async () => ({
              review_id: 'rev-1',
              draft,
              cost_usd_cents: 3,
              cached: false,
              generated_at: '1700000000',
              served_model_id: 'anthropic/claude-sonnet-5',
            }),
          } as Response;
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    await submitAndReachResult();
    fireEvent.click(screen.getByTestId('review-cover-note-butter'));
    await screen.findByTestId('review-cover-note-card');
    expect(screen.getByTestId('review-cover-note-text').textContent).toBe(DRAFT_TEXT);

    fireEvent.click(screen.getByTestId('review-cover-note-regenerate'));
    await waitFor(() =>
      expect(screen.getByTestId('review-cover-note-text').textContent).toBe(secondDraft),
    );
    expect(call).toBe(2);
  });

  it('a failed generation shows the quiet copy and a retry, never a scary banner', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': REQUEST_CHANGE_DETAIL,
      'POST /api/reviews/rev-1/cover-note': {
        __httpStatus: 502,
        body: { detail: "Couldn't butter this one — the redline is unaffected." },
      },
    });
    await submitAndReachResult();
    fireEvent.click(screen.getByTestId('review-cover-note-butter'));

    const error = await screen.findByTestId('review-cover-note-error');
    expect(error.textContent).toContain("Couldn't butter this one");
    expect(screen.queryByTestId('review-cover-note-card')).toBeNull();
    expect(screen.getByTestId('review-cover-note-retry')).toBeInTheDocument();
  });

  // Issue #499 fix round 3 (review finding): a 409 (e.g. the review fell
  // out of its retention window between page-load and click) is a REAL,
  // non-retryable problem per coverNote.ts's own module docstring -- it must
  // NOT render the same quiet "try again" copy a transient 502 gets, since
  // retrying a 409 will never succeed.
  it('a 409 shows a real error banner, not the quiet retry copy', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': REQUEST_CHANGE_DETAIL,
      'POST /api/reviews/rev-1/cover-note': {
        __httpStatus: 409,
        body: { detail: 'This review is past its retention window.' },
      },
    });
    await submitAndReachResult();
    fireEvent.click(screen.getByTestId('review-cover-note-butter'));

    const banner = await screen.findByTestId('review-cover-note-real-error');
    expect(banner).toBeInTheDocument();
    expect(screen.queryByTestId('review-cover-note-error')).toBeNull();
    expect(screen.queryByTestId('review-cover-note-retry')).toBeNull();
    expect(screen.queryByTestId('review-cover-note-card')).toBeNull();
  });

  // Issue #499 fix round 3 (review finding): the resubmit-reset block reset
  // every other cover-note state field but not coverNoteErrorMessage, so a
  // stale real-error banner from a PREVIOUS review survived onto a newly
  // submitted one before the reviewer had even clicked "Butter it" for it.
  it('a resubmit clears a previous review\'s real-error banner', async () => {
    let coverNoteCall = 0;
    let submitCall = 0;
    const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const pathname = new URL(String(input), 'http://localhost').pathname;
      const method = (init?.method ?? 'GET').toUpperCase();
      if (pathname === '/api/reviews' && method === 'POST') {
        submitCall += 1;
        const reviewId = submitCall === 1 ? 'rev-1' : 'rev-2';
        return {
          ok: true,
          status: 200,
          json: async () => ({ review_id: reviewId, resumed: false }),
        } as Response;
      }
      if (pathname === '/api/reviews/rev-1' && method === 'GET') {
        return { ok: true, status: 200, json: async () => REQUEST_CHANGE_DETAIL } as Response;
      }
      if (pathname === '/api/reviews/rev-2' && method === 'GET') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ ...REQUEST_CHANGE_DETAIL, review_id: 'rev-2' }),
        } as Response;
      }
      if (pathname === '/api/reviews/rev-1/cover-note' && method === 'POST') {
        coverNoteCall += 1;
        return {
          ok: false,
          status: 409,
          json: async () => ({ detail: 'This review is past its retention window.' }),
        } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', impl);

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-result');

    fireEvent.click(screen.getByTestId('review-cover-note-butter'));
    await screen.findByTestId('review-cover-note-real-error');
    expect(coverNoteCall).toBe(1);

    // Resubmit a new document within the SAME mounted component -- rev-2.
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await waitFor(() => expect(submitCall).toBe(2));
    await screen.findByTestId('review-result');

    expect(screen.queryByTestId('review-cover-note-real-error')).toBeNull();
  });
});

function fetchCallFor(
  fetchMock: ReturnType<typeof vi.fn>,
  method: string,
  pathname: string,
): unknown[] | undefined {
  return fetchMock.mock.calls.find((call: unknown[]) => {
    const [input, init] = call as [RequestInfo | URL, RequestInit | undefined];
    const url = new URL(String(input), 'http://localhost').pathname;
    return url === pathname && (init?.method ?? 'GET').toUpperCase() === method;
  });
}

// ---------------------------------------------------------------------------
// ReviewHistory — the same control in a past row's expanded detail.
// ---------------------------------------------------------------------------

function stubHistoryFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const entry = routes[key] as { status: number; body: unknown } | undefined;
    if (!entry) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return {
      ok: entry.status >= 200 && entry.status < 300,
      status: entry.status,
      json: async () => entry.body,
    } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function historyRow(overrides: Partial<HistoryRow>): HistoryRow {
  return {
    review_id: 'rev-h1',
    playbook_id: 'synthetic-nda-sample',
    status: 'DONE',
    decision: 'REQUEST_CHANGE',
    created_at: '1800000000',
    has_output: true,
    has_input: false,
    ...overrides,
  };
}

describe('ReviewHistory — "Butter it" in the expanded row (issue #499)', () => {
  it('offers the control only inside the expanded row, and only for a REQUEST_CHANGE row', async () => {
    stubHistoryFetch({
      'GET /api/reviews': { status: 200, body: { reviews: [historyRow({})] } },
    });
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-rev-h1');

    // Not visible before the row is expanded.
    expect(screen.queryByTestId('history-cover-note-butter-rev-h1')).toBeNull();

    fireEvent.click(screen.getByTestId('history-guidance-toggle-rev-h1'));
    expect(await screen.findByTestId('history-cover-note-butter-rev-h1')).toBeInTheDocument();
  });

  it('does not offer the control for an ACCEPT row (nothing to describe)', async () => {
    stubHistoryFetch({
      'GET /api/reviews': {
        status: 200,
        body: { reviews: [historyRow({ decision: 'ACCEPT' })] },
      },
    });
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-rev-h1');
    fireEvent.click(screen.getByTestId('history-guidance-toggle-rev-h1'));
    await screen.findByTestId(`history-guidance-rev-h1`);

    expect(screen.queryByTestId('history-cover-note-rev-h1')).toBeNull();
  });

  it('drafts, copies, and regenerates from the expanded row', async () => {
    const secondDraft = 'Attached is an updated markup. Happy to discuss further.';
    let coverNoteCalls = 0;
    const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const pathname = new URL(String(input), 'http://localhost').pathname;
      const method = (init?.method ?? 'GET').toUpperCase();
      if (pathname === '/api/reviews' && method === 'GET') {
        return { ok: true, status: 200, json: async () => ({ reviews: [historyRow({})] }) } as Response;
      }
      if (pathname === '/api/reviews/rev-h1/cover-note' && method === 'POST') {
        coverNoteCalls += 1;
        const body = init?.body ? JSON.parse(init.body as string) : {};
        const draft = coverNoteCalls === 1 ? DRAFT_TEXT : secondDraft;
        return {
          ok: true,
          status: 200,
          json: async () => ({
            review_id: 'rev-h1',
            draft,
            cost_usd_cents: body.regenerate ? 4 : 2,
            cached: false,
            generated_at: '1800000000',
            served_model_id: 'anthropic/claude-sonnet-5',
          }),
        } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', impl);

    render(<ReviewHistory />);
    await screen.findByTestId('history-row-rev-h1');
    fireEvent.click(screen.getByTestId('history-guidance-toggle-rev-h1'));
    await screen.findByTestId('history-cover-note-butter-rev-h1');

    fireEvent.click(screen.getByTestId('history-cover-note-butter-rev-h1'));
    const card = await screen.findByTestId('history-cover-note-card-rev-h1');
    expect(card).toBeInTheDocument();
    expect(screen.getByTestId('history-cover-note-text-rev-h1').textContent).toBe(DRAFT_TEXT);

    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    fireEvent.click(screen.getByTestId('history-cover-note-copy-rev-h1'));
    expect(writeText).toHaveBeenCalledWith(DRAFT_TEXT);

    fireEvent.click(screen.getByTestId('history-cover-note-regenerate-rev-h1'));
    await waitFor(() =>
      expect(screen.getByTestId('history-cover-note-text-rev-h1').textContent).toBe(secondDraft),
    );
    expect(coverNoteCalls).toBe(2);
  });

  it('a cached revisit seeds the Regenerate cost hint from the stored generation cost', async () => {
    stubHistoryFetch({
      'GET /api/reviews': { status: 200, body: { reviews: [historyRow({})] } },
      'POST /api/reviews/rev-h1/cover-note': {
        status: 200,
        body: {
          review_id: 'rev-h1',
          draft: DRAFT_TEXT,
          cost_usd_cents: 0,
          cached: true,
          generated_at: '1800000000',
          served_model_id: 'anthropic/claude-sonnet-5',
          last_generation_cost_usd_cents: 3,
        },
      },
    });

    render(<ReviewHistory />);
    await screen.findByTestId('history-row-rev-h1');
    fireEvent.click(screen.getByTestId('history-guidance-toggle-rev-h1'));
    await screen.findByTestId('history-cover-note-butter-rev-h1');

    fireEvent.click(screen.getByTestId('history-cover-note-butter-rev-h1'));
    await screen.findByTestId('history-cover-note-card-rev-h1');

    expect(screen.getByTestId('history-cover-note-cost-rev-h1').textContent).toMatch(
      /no charge/i,
    );
    // Issue #499 fix round 1: a cached revisit (reload / History revisit,
    // never regenerated this session) must still show the real cost on
    // Regenerate, sourced from the backend's stored
    // `last_generation_cost_usd_cents` -- not left at "Regenerate" bare.
    expect(
      screen.getByTestId('history-cover-note-regenerate-rev-h1').textContent,
    ).toContain('$0.03');
  });

  // Issue #499 fix round 3 (review finding): same real-vs-quiet distinction
  // as ReviewSubmission's equivalent test above, per-row here.
  it('a 409 shows a real error banner for that row, not the quiet retry copy', async () => {
    stubHistoryFetch({
      'GET /api/reviews': { status: 200, body: { reviews: [historyRow({})] } },
      'POST /api/reviews/rev-h1/cover-note': {
        status: 409,
        body: { detail: 'This review is past its retention window.' },
      },
    });

    render(<ReviewHistory />);
    await screen.findByTestId('history-row-rev-h1');
    fireEvent.click(screen.getByTestId('history-guidance-toggle-rev-h1'));
    await screen.findByTestId('history-cover-note-butter-rev-h1');

    fireEvent.click(screen.getByTestId('history-cover-note-butter-rev-h1'));

    const banner = await screen.findByTestId('history-cover-note-real-error-rev-h1');
    expect(banner).toBeInTheDocument();
    expect(screen.queryByTestId('history-cover-note-error-rev-h1')).toBeNull();
    expect(screen.queryByTestId('history-cover-note-retry-rev-h1')).toBeNull();
    expect(screen.queryByTestId('history-cover-note-card-rev-h1')).toBeNull();
  });
});
