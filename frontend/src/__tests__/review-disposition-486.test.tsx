/**
 * review-disposition-486.test.tsx — the disposition capture UI (issue #486).
 *
 * Wires the previously-unreachable `backend/src/disposition.py` behind two
 * frontend surfaces sharing `disposition.ts`:
 *
 *   - ReviewSubmission.tsx's finished-review panel: one click, no modal,
 *     optional note.
 *   - ReviewHistory.tsx's Disposition column: settable from the row.
 *
 * THIS IS NOT AN APPROVAL GATE (owner correction on issue #486,
 * 2026-08-02): the capture is optional and never nags. The tests below
 * pin the corrected copy directly (neutral prompt, no "awaiting
 * disposition" language, "Not recorded" rather than "Awaiting review" as
 * the empty state) and assert the retired disclaimer never reappears
 * (issue #513).
 *
 * Fully offline: auth is mocked, fetch is stubbed per test.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import ReviewHistory, { HistoryRow } from '../ReviewHistory';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// ReviewSubmission — the finished-review panel's capture control.
// ---------------------------------------------------------------------------

function stubReviewSubmissionFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
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
    // `__httpStatus` (not `status`) so a stubbed REVIEW body — which has its
    // OWN `status` field ("DONE"/"CANCELLED"/…) — is never mistaken for this
    // wrapper.
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

async function submitAndReachResult(
  fetchMock: ReturnType<typeof vi.fn>,
): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-result');
  void fetchMock;
}

const DONE_DETAIL = {
  review_id: 'rev-1',
  status: 'DONE',
  decision: 'ACCEPT',
  message: null,
  has_output: true,
};

describe('ReviewSubmission — disposition capture (issue #486)', () => {
  it('offers the three neutral, optional choices after a review finishes', async () => {
    const fetchMock = stubReviewSubmissionFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': DONE_DETAIL,
    });
    await submitAndReachResult(fetchMock);

    const block = screen.getByTestId('review-disposition');
    expect(block.textContent).toContain('Want to note how this one landed? (optional)');
    expect(screen.getByTestId('review-disposition-accepted')).toBeInTheDocument();
    expect(screen.getByTestId('review-disposition-edited')).toBeInTheDocument();
    expect(screen.getByTestId('review-disposition-rejected')).toBeInTheDocument();
    expect(screen.getByTestId('review-disposition-note')).toBeInTheDocument();

    // Owner correction (2026-08-02): no nag/nag-adjacent wording, and the
    // retired attorney-approval disclaimer (issue #513) never reappears
    // beside this capture.
    expect(block.textContent).not.toMatch(/awaiting/i);
    expect(block.textContent?.toLowerCase()).not.toContain('attorney approval');
  });

  it('records a click as a single POST, and shows the recorded outcome', async () => {
    const fetchMock = stubReviewSubmissionFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': DONE_DETAIL,
      'POST /api/reviews/rev-1/disposition': {
        review_id: 'rev-1',
        attorney_disposition: 'ACCEPTED',
        attorney_disposition_recorded_at: '1700000000',
        legal_triage_status: null,
      },
    });
    await submitAndReachResult(fetchMock);

    fireEvent.click(screen.getByTestId('review-disposition-accepted'));

    await screen.findByTestId('review-disposition-recorded');
    expect(screen.getByTestId('review-disposition-recorded').textContent).toContain('Accepted');

    const call = fetchMock.mock.calls.find(([input, init]) => {
      const pathname = new URL(String(input), 'http://localhost').pathname;
      return (
        pathname === '/api/reviews/rev-1/disposition' &&
        (init as RequestInit | undefined)?.method === 'POST'
      );
    });
    expect(call, 'expected a POST to the disposition route').toBeTruthy();
    const body = JSON.parse((call![1] as RequestInit).body as string);
    expect(body).toEqual({ disposition: 'ACCEPTED' });
  });

  it('sends the optional note only when it carries content', async () => {
    const fetchMock = stubReviewSubmissionFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': DONE_DETAIL,
      'POST /api/reviews/rev-1/disposition': {
        review_id: 'rev-1',
        attorney_disposition: 'EDITED',
        legal_triage_status: 'PENDING_TRIAGE',
      },
    });
    await submitAndReachResult(fetchMock);

    fireEvent.change(screen.getByTestId('review-disposition-note'), {
      target: { value: 'Narrowed the indemnification carve-out further.' },
    });
    fireEvent.click(screen.getByTestId('review-disposition-edited'));
    await screen.findByTestId('review-disposition-recorded');

    const call = fetchMock.mock.calls.find(([input, init]) => {
      const pathname = new URL(String(input), 'http://localhost').pathname;
      return (
        pathname === '/api/reviews/rev-1/disposition' &&
        (init as RequestInit | undefined)?.method === 'POST'
      );
    });
    const body = JSON.parse((call![1] as RequestInit).body as string);
    expect(body).toEqual({
      disposition: 'EDITED',
      note: 'Narrowed the indemnification carve-out further.',
    });
  });

  it('shows a friendly error and never a raw HTTP detail when the write fails', async () => {
    const fetchMock = stubReviewSubmissionFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': DONE_DETAIL,
      'POST /api/reviews/rev-1/disposition': {
        __httpStatus: 500,
        body: { detail: 'DYNAMODB_TABLE_NAME not configured.' },
      },
    });
    await submitAndReachResult(fetchMock);

    fireEvent.click(screen.getByTestId('review-disposition-rejected'));

    const error = await screen.findByTestId('review-disposition-error');
    expect(error.textContent).not.toContain('DYNAMODB_TABLE_NAME');
    expect(error.textContent).not.toMatch(/\b5\d\d\b/);
    expect(screen.queryByTestId('review-disposition-recorded')).toBeNull();
  });

  it('does not render the capture control for a status with nothing to disposition', async () => {
    const fetchMock = stubReviewSubmissionFetch({
      'POST /api/reviews': { review_id: 'rev-1', resumed: false },
      'GET /api/reviews/rev-1': {
        review_id: 'rev-1',
        status: 'CANCELLED',
        decision: null,
        message: null,
        has_output: false,
      },
    });
    await submitAndReachResult(fetchMock);

    expect(screen.queryByTestId('review-disposition')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// ReviewHistory — the Disposition column.
// ---------------------------------------------------------------------------

// Fix-round-1 (issue #486, finding #1): matches on METHOD + EXACT pathname,
// same convention as `stubReviewSubmissionFetch` above (falls back to a
// bare-pathname key when no `"METHOD /path"` key is present). The previous
// version matched by `url.includes(candidate)` in `Object.keys()` insertion
// order, so a `'/api/reviews'` route entry silently swallowed the POST to
// `/api/reviews/rev-h1/disposition` too (a substring of it) and served that
// request the LISTING body instead of the disposition response — the write
// path below was therefore never actually exercised by these tests.
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
    decision: 'ACCEPT',
    created_at: '1800000000',
    has_output: false,
    has_input: false,
    ...overrides,
  };
}

describe('ReviewHistory — the Disposition column (issue #486)', () => {
  it('renders "Not recorded" — never the pre-correction "Awaiting review" nag copy', async () => {
    stubHistoryFetch({
      'GET /api/reviews': { status: 200, body: { reviews: [historyRow({ attorney_disposition: null })] } },
    });
    render(<ReviewHistory />);

    // The dedicated value testid, not the whole cell: the cell ALSO holds
    // the "Rejected" action button's light-DOM label, and an assertion
    // against the cell as a whole cannot tell the two apart.
    const value = await screen.findByTestId('history-disposition-value-rev-h1');
    expect(value.textContent).toContain('Not recorded');
    expect(value.textContent?.toLowerCase()).not.toContain('awaiting');
  });

  it('shows the recorded outcome using shared display labels', async () => {
    stubHistoryFetch({
      'GET /api/reviews': {
        status: 200,
        body: { reviews: [historyRow({ attorney_disposition: 'EDITED' })] },
      },
    });
    render(<ReviewHistory />);

    const value = await screen.findByTestId('history-disposition-value-rev-h1');
    expect(value.textContent).toContain('Accepted with changes');
  });

  it('is settable from the row, and updates in place without re-fetching the list', async () => {
    const fetchMock = stubHistoryFetch({
      'GET /api/reviews': {
        status: 200,
        body: { reviews: [historyRow({ attorney_disposition: null })] },
      },
      'POST /api/reviews/rev-h1/disposition': {
        status: 200,
        body: { review_id: 'rev-h1', attorney_disposition: 'REJECTED', legal_triage_status: 'PENDING_TRIAGE' },
      },
    });
    render(<ReviewHistory />);

    await screen.findByTestId('history-row-rev-h1');
    // Sanity check on the value BEFORE the click — the assertion after the
    // click is only meaningful if this one is different.
    expect(screen.getByTestId('history-disposition-value-rev-h1').textContent).toBe(
      'Not recorded',
    );

    fireEvent.click(screen.getByTestId('history-disposition-rejected-rev-h1'));

    // Fix-round-1 (finding #1): asserted on the VALUE span alone, which
    // renders only `describeDisposition(row.attorney_disposition)` — the
    // "Rejected" action BUTTON sitting in the same `<td>` can never satisfy
    // this the way it could satisfy a whole-cell `toContain('Rejected')`.
    await waitFor(() =>
      expect(screen.getByTestId('history-disposition-value-rev-h1').textContent).toBe('Rejected'),
    );
    expect(screen.getByTestId('history-disposition-value-rev-h1').textContent).not.toContain(
      'Not recorded',
    );

    // The POST actually reached the disposition route with the right body —
    // not just "some fetch resolved and the UI happened to look right".
    const postCall = fetchMock.mock.calls.find(([input, init]) => {
      const pathname = new URL(String(input), 'http://localhost').pathname;
      return (
        pathname === '/api/reviews/rev-h1/disposition' &&
        (init as RequestInit | undefined)?.method === 'POST'
      );
    });
    expect(postCall, 'expected a POST to the disposition route').toBeTruthy();
    const body = JSON.parse((postCall![1] as RequestInit).body as string);
    expect(body).toEqual({ disposition: 'REJECTED' });

    // Exactly one listing fetch — the row was patched in place, not refetched.
    const listingCalls = fetchMock.mock.calls.filter(([input]) => {
      const pathname = new URL(String(input), 'http://localhost').pathname;
      return pathname === '/api/reviews';
    });
    expect(listingCalls).toHaveLength(1);
  });

  it('offers no action buttons for a review with nothing to disposition yet', async () => {
    stubHistoryFetch({
      'GET /api/reviews': {
        status: 200,
        body: { reviews: [historyRow({ status: 'RUNNING', attorney_disposition: null })] },
      },
    });
    render(<ReviewHistory />);

    await screen.findByTestId('history-row-rev-h1');
    expect(screen.queryByTestId('history-disposition-accepted-rev-h1')).toBeNull();
    expect(screen.getByTestId('history-disposition-value-rev-h1').textContent).toContain(
      'Not recorded',
    );
  });
});
