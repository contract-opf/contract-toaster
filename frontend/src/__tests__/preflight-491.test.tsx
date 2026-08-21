/**
 * preflight-491.test.tsx — the upload-time preflight card (issue #491).
 *
 * A cheap, fast, ADVISORY check fired the moment a file is chosen (before
 * "Upload for review"): deterministic document stats plus a cheap-model
 * agreement-type/paper-side guess and a server-computed match verdict.
 * Never blocks a submission.
 *
 * Covers:
 *   1. Stats render as soon as `POST /api/reviews/preflight` resolves, even
 *      when `classification: "unavailable"` -- no apology banner, just the
 *      deterministic line.
 *   2. `match: "likely"` renders an affirming banner; `"unlikely"` renders
 *      an amber mismatch note naming the SELECTED playbook; `"unclear"`
 *      renders neither banner, only the neutral type+side line (or nothing,
 *      if there is no type guess at all).
 *   3. The Upload button is NEVER disabled or gated by preflight -- pending,
 *      resolved, or failed, `!file` is the only thing that can disable it.
 *   4. A preflight failure (network error, non-2xx) renders no card and no
 *      error banner -- upload is unaffected.
 *   5. `one_line_summary` renders as an inert TEXT node only: markup-like
 *      text in the field never becomes a real `<a>`/`<script>` element.
 *   6. A stale response for a file the reviewer already replaced is
 *      dropped on arrival, never rendered over the current selection.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

// Same routing convention as playbook-selector.test.tsx / browning-control.test.tsx.
function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const entry = routes[key];
    if (entry === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    if (typeof entry === 'function') {
      return (entry as () => Promise<Response>)();
    }
    if (
      entry &&
      typeof entry === 'object' &&
      'status' in (entry as Record<string, unknown>) &&
      'body' in (entry as Record<string, unknown>)
    ) {
      const { status: statusCode, body } = entry as { status: number; body: unknown };
      return { ok: statusCode < 400, status: statusCode, json: async () => body } as Response;
    }
    return { ok: true, status: 200, json: async () => entry } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function docxFile(name = 'contract.docx'): File {
  return new File(['contents'], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

const CATALOG = [{ playbook_id: 'nda-sample', display_name: 'Sample NDA', status: 'active' }];

const BASE_STATS = {
  word_count: 2400,
  page_estimate: 8,
  paragraph_count: 12,
  title: 'Mutual Non-Disclosure Agreement',
};

function selectFile(file: File): void {
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [file] } });
}

describe('preflight card — ReviewSubmission.tsx', () => {
  it('renders deterministic stats even when the cheap model is unavailable, with no apology banner', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': {
        ...BASE_STATS,
        classification: 'unavailable',
        agreement_type_guess: null,
        paper_side: 'unclear',
        confidence: null,
        one_line_summary: null,
        match: null,
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    const card = await screen.findByTestId('review-preflight-card');
    expect(card).toHaveTextContent('2,400 words');
    expect(card).toHaveTextContent('8 pages');
    expect(card).toHaveTextContent('Mutual Non-Disclosure Agreement');
    expect(screen.queryByTestId('review-preflight-match-likely')).toBeNull();
    expect(screen.queryByTestId('review-preflight-match-unlikely')).toBeNull();
    expect(screen.queryByTestId('review-preflight-summary')).toBeNull();
    expect(card).not.toHaveTextContent(/sorry|unavailable|error/i);
  });

  it('shows an affirming banner on match: likely, regardless of paper side', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': {
        ...BASE_STATS,
        classification: 'ok',
        agreement_type_guess: 'Non-Disclosure Agreement',
        paper_side: 'counterparty',
        confidence: 0.9,
        one_line_summary: 'A mutual NDA between two parties.',
        match: 'likely',
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    const banner = await screen.findByTestId('review-preflight-match-likely');
    expect(banner).toHaveTextContent(/Non-Disclosure Agreement/);
    expect(banner).toHaveTextContent(/counterparty/i);
    expect(await screen.findByTestId('review-preflight-summary')).toHaveTextContent(
      'A mutual NDA between two parties.',
    );
  });

  it('shows an amber mismatch note naming the selected playbook on match: unlikely', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': {
        ...BASE_STATS,
        classification: 'ok',
        agreement_type_guess: 'Master Services Agreement',
        paper_side: 'ours',
        confidence: 0.85,
        one_line_summary: 'A master services agreement.',
        match: 'unlikely',
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    const banner = await screen.findByTestId('review-preflight-match-unlikely');
    expect(banner).toHaveTextContent(/Master Services Agreement/);
    expect(banner).toHaveTextContent(/Sample NDA/);
    expect(banner).toHaveTextContent(/toast it anyway/i);
    // Never a blocking word.
    expect(banner).not.toHaveTextContent(/blocked|cannot|forbidden/i);
  });

  it('shows neither banner on match: unclear, only the neutral type+side line', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': {
        ...BASE_STATS,
        classification: 'ok',
        agreement_type_guess: 'Statement of Work',
        paper_side: 'unclear',
        confidence: 0.4,
        one_line_summary: 'Hard to tell what this is.',
        match: 'unclear',
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    await screen.findByTestId('review-preflight-card');
    expect(screen.queryByTestId('review-preflight-match-likely')).toBeNull();
    expect(screen.queryByTestId('review-preflight-match-unlikely')).toBeNull();
    expect(screen.getByTestId('review-preflight-type-side')).toHaveTextContent(
      /Statement of Work/,
    );
  });

  it('never disables or delays the Upload button, even while preflight is still pending', async () => {
    const pending: { resolve: ((value: Response) => void) | null } = { resolve: null };
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': () =>
        new Promise<Response>((resolve) => {
          pending.resolve = resolve;
        }),
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    // Preflight is still in flight (never resolved) -- Upload is enabled the
    // instant a file is chosen, unconditionally on preflight's state.
    await waitFor(() => {
      expect(screen.getByTestId('review-submit-button')).not.toBeDisabled();
    });
    expect(screen.queryByTestId('review-preflight-card')).toBeNull();

    // Clean up the pending promise so the test doesn't leak a hung request.
    pending.resolve?.({ ok: true, status: 200, json: async () => ({}) } as Response);
  });

  it('renders no card and no error banner when the preflight request fails outright', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': { status: 500, body: { detail: 'boom' } },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    // Give the failed fetch a tick to resolve, then assert steady state:
    // no card, no error surface, and Upload still usable.
    await waitFor(() => expect(screen.getByTestId('review-submit-button')).not.toBeDisabled());
    expect(screen.queryByTestId('review-preflight-card')).toBeNull();
    expect(screen.queryByTestId('review-submit-error')).toBeNull();
  });

  it('renders one_line_summary as inert text, never as markup or a link', async () => {
    const summaryWithMarkup = '<a href="https://evil.example/collect">click me</a>';
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': {
        ...BASE_STATS,
        classification: 'ok',
        agreement_type_guess: 'Non-Disclosure Agreement',
        paper_side: 'ours',
        confidence: 0.7,
        one_line_summary: summaryWithMarkup,
        match: 'likely',
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    const summary = await screen.findByTestId('review-preflight-summary');
    // The literal string -- including its angle brackets -- is the visible
    // text, not parsed markup.
    expect(summary.textContent).toBe(summaryWithMarkup);
    expect(summary.querySelector('a')).toBeNull();
    expect(summary.querySelector('script')).toBeNull();
    expect(summary.innerHTML).not.toContain('<a ');
  });

  it('renders the #506 injection-scan flag inside the SAME preflight card (rider item 4)', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': {
        ...BASE_STATS,
        classification: 'unavailable',
        agreement_type_guess: null,
        paper_side: 'unclear',
        confidence: null,
        one_line_summary: null,
        match: null,
        injection_scan: {
          injection_scan_rule_ids: ['instruction-override'],
          injection_scan_finding_count: 1,
        },
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    // One flag, not two: no separate endpoint/card -- it's inside the same
    // "What we're looking at" card the deterministic stats render in.
    const card = await screen.findByTestId('review-preflight-card');
    const flag = await screen.findByTestId('review-preflight-injection-flag');
    expect(card).toContainElement(flag);
    expect(flag).toHaveTextContent('instruction-override');
    expect(flag).toHaveTextContent('1 item');
  });

  it('renders no injection-scan flag for a clean document', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': {
        ...BASE_STATS,
        classification: 'unavailable',
        agreement_type_guess: null,
        paper_side: 'unclear',
        confidence: null,
        one_line_summary: null,
        match: null,
        injection_scan: {},
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    await screen.findByTestId('review-preflight-card');
    expect(screen.queryByTestId('review-preflight-injection-flag')).toBeNull();
  });

  it('fix round 1: a dial change recomputes only the match verdict, without re-uploading the file', async () => {
    const catalog = [
      { playbook_id: 'nda-sample', display_name: 'Sample NDA', status: 'active' },
      { playbook_id: 'msa-sample', display_name: 'Sample MSA', status: 'active' },
    ];
    let preflightCalls = 0;
    let matchCalls = 0;
    stubFetch({
      'GET /api/playbooks': { playbooks: catalog },
      'POST /api/reviews/preflight': () => {
        preflightCalls += 1;
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({
            ...BASE_STATS,
            classification: 'ok',
            agreement_type_guess: 'Non-Disclosure Agreement',
            paper_side: 'ours',
            confidence: 0.9,
            one_line_summary: 'An NDA.',
            match: 'likely',
          }),
        } as Response);
      },
      'POST /api/reviews/preflight/match': () => {
        matchCalls += 1;
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ match: 'unlikely' }),
        } as Response);
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');
    selectFile(docxFile());

    await screen.findByTestId('review-preflight-match-likely');
    expect(preflightCalls).toBe(1);
    expect(matchCalls).toBe(0);

    fireEvent.click(screen.getByTestId('review-playbook-option-msa-sample'));

    await screen.findByTestId('review-preflight-match-unlikely');
    // The full, file-uploading request never re-fired -- only the cheap
    // verdict-only route did.
    expect(preflightCalls).toBe(1);
    expect(matchCalls).toBe(1);
  });

  it('drops a stale preflight response for a file the reviewer already replaced', async () => {
    const pending: { resolveFirst: ((value: Response) => void) | null } = { resolveFirst: null };
    let callCount = 0;
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': () => {
        callCount += 1;
        if (callCount === 1) {
          return new Promise<Response>((resolve) => {
            pending.resolveFirst = resolve;
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({
            ...BASE_STATS,
            title: 'Second File Title',
            classification: 'ok',
            agreement_type_guess: 'Non-Disclosure Agreement',
            paper_side: 'ours',
            confidence: 0.8,
            one_line_summary: 'The second file.',
            match: 'likely',
          }),
        } as Response);
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');

    // First file's preflight never resolves yet...
    selectFile(docxFile('first.docx'));
    // ...the reviewer replaces it with a second file before it does.
    selectFile(docxFile('second.docx'));

    const card = await screen.findByTestId('review-preflight-card');
    expect(card).toHaveTextContent('Second File Title');

    // Now let the FIRST (stale) response resolve -- it must not overwrite
    // the second file's card.
    pending.resolveFirst?.({
      ok: true,
      status: 200,
      json: async () => ({
        ...BASE_STATS,
        title: 'First File Title',
        classification: 'unavailable',
        agreement_type_guess: null,
        paper_side: 'unclear',
        confidence: null,
        one_line_summary: null,
        match: null,
      }),
    } as Response);

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByTestId('review-preflight-card')).toHaveTextContent('Second File Title');
  });
});
