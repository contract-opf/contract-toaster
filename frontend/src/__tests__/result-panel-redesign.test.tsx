/**
 * result-panel-redesign.test.tsx — the finished/RUNNING result-panel rewrite
 * (issue #492): outcome-first, no UUIDs or DONE/RUNNING tokens, no focus
 * narration in visible copy, and no attorney-approval disclaimer anywhere in
 * frontend/src (review policy is the deploying organization's, entirely
 * outside this product).
 *
 * Every acceptance criterion in the ticket gets its own assertion here:
 *
 *   AC1 — the finished panel shows the outcome headline, the truthful
 *         save/download line, the quiet meta line and the download button;
 *         no UUID, no DONE/RUNNING token, no focus narration outside the
 *         (visually-hidden) announcement region.
 *   AC2 — "Copy review ID" puts the id on the clipboard in both RUNNING and
 *         finished states.
 *   AC3 — no attorney-approval disclaimer text anywhere the component
 *         renders (checked via the removed sentence's own lead-in,
 *         "Tool recommendation only", so this file doesn't itself become a
 *         hit for the sweep it verifies).
 *   AC4 — the critic-flagged variant gets the same treatment and still gates
 *         per criticDeltaHasContent.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
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

function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (pathname.endsWith('.mp3')) {
      return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) } as Response;
    }
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const body = routes[key];
    if (body === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

async function submitAndReachStatus(): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-status');
}

const REVIEW_ID = 'rev-8f2a1c9d';
const PRESIGNED_URL = `https://s3.example.test/outputs/${REVIEW_ID}/out.docx?sig=abc`;

let anchorClickSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.restoreAllMocks();
  // The automatic save (issue #448) fires a real anchor click on completion;
  // stub it so jsdom's "navigation to another Document" noise never fires
  // and the save can resolve without a real browser.
  anchorClickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AC1 — the finished panel', () => {
  it('shows the outcome headline, the truthful save line, the meta line and the download button — no UUID, no DONE token, no disclaimer', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: REVIEW_ID, resumed: false },
      [`GET /api/reviews/${REVIEW_ID}`]: {
        review_id: REVIEW_ID,
        status: 'DONE',
        decision: 'REQUEST_CHANGE',
        message: null,
        has_output: true,
        updated_at: '1000000192',
      },
      [`GET /api/reviews/${REVIEW_ID}/output`]: { url: PRESIGNED_URL, expires_in: 60 },
    });

    await submitAndReachStatus();
    await screen.findByTestId('review-result');

    // Outcome headline — the shared outcome map's label, promoted to the
    // biggest text in the panel. Never the raw DONE status token.
    expect(screen.getByTestId('review-outcome').textContent).toBe('Changes requested');

    // Truthful save line — only once the automatic save has actually
    // resolved (issue #466's discipline, extended to visible copy).
    await screen.findByTestId('review-saved-line');
    expect(screen.getByTestId('review-saved-line').textContent).toBe(
      'Redline saved to your downloads.',
    );

    // Quiet meta line — filename · finished-at time (no contract type was
    // submitted in this fixture, so that clause is simply absent — "never
    // invent precision").
    const meta = screen.getByTestId('review-meta-line').textContent ?? '';
    expect(meta).toContain('contract.docx');
    expect(meta).toMatch(/\d{4}-\d{2}-\d{2}/);

    // Download button, renamed per the ticket.
    expect(screen.getByTestId('review-download-button').textContent).toBe('Download redline');

    // Nothing anywhere in the finished panel ever names the raw review id,
    // the bare DONE token, or the attorney-approval disclaimer -- checked at
    // the review-status level, which wraps the id-row control, the
    // provenance receipt (issue #498) and review-result together, so this
    // assertion is red if ANY of those three paints the id, not just the
    // redesigned review-result piece on its own. The focus narration ("now
    // has focus") that used to leak into visible copy is gone from this
    // panel too — it lives ONLY in the sr-only announcement region, which
    // sits OUTSIDE review-status entirely.
    const statusText = screen.getByTestId('review-status').textContent ?? '';
    expect(statusText).not.toContain(REVIEW_ID);
    expect(statusText).not.toMatch(/\bDONE\b/);
    expect(statusText).not.toContain('Tool recommendation only');
    expect(statusText).not.toContain('now has focus');

    const resultText = screen.getByTestId('review-result').textContent ?? '';
    expect(resultText).not.toContain(REVIEW_ID);
    expect(resultText).not.toMatch(/\bDONE\b/);
    expect(resultText).not.toContain('Tool recommendation only');
    expect(resultText).not.toContain('now has focus');
    // The id-row control at the top of review-status is likewise clean.
    expect(screen.getByTestId('review-id-row').textContent).not.toContain(REVIEW_ID);

    // The behavior itself is NOT gone, only relocated: the announcement
    // region still carries it, for assistive tech, via a visually-hidden
    // class (jsdom doesn't apply app.css, so this checks the class itself
    // rather than computed visibility) — not as visible copy.
    const announcement = screen.getByTestId('review-ready-announcement');
    expect(announcement.textContent).toContain('focus');
    expect(announcement.className).toContain('ct-sr-only');
  });
});

describe('AC2 — Copy review ID, in both RUNNING and finished states', () => {
  it('copies the id to the clipboard while RUNNING, without ever printing it', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: REVIEW_ID, resumed: false },
      [`GET /api/reviews/${REVIEW_ID}`]: {
        review_id: REVIEW_ID,
        status: 'RUNNING',
        decision: null,
        message: null,
        has_output: false,
      },
    });

    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    await submitAndReachStatus();

    const statusText = screen.getByTestId('review-status').textContent ?? '';
    expect(statusText).not.toContain(REVIEW_ID);
    expect(statusText).not.toMatch(/\bRUNNING\b/);

    fireEvent.click(screen.getByTestId('review-copy-id-button'));
    expect(writeText).toHaveBeenCalledWith(REVIEW_ID);
    expect(await screen.findByText('Copied')).toBeInTheDocument();
  });

  it('copies the id to the clipboard once finished, without ever printing it', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: REVIEW_ID, resumed: false },
      [`GET /api/reviews/${REVIEW_ID}`]: {
        review_id: REVIEW_ID,
        status: 'DONE',
        decision: 'ACCEPT',
        message: null,
        has_output: false,
      },
    });

    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    await submitAndReachStatus();
    await screen.findByTestId('review-result');

    // The provenance receipt (issue #498) is not exempt from the ticket's
    // "no UUID in visible DOM" AC either -- checked at the review-status
    // level, which wraps the id-row control, the receipt and review-result
    // together.
    expect(screen.getByTestId('review-status').textContent ?? '').not.toContain(REVIEW_ID);
    expect(screen.getByTestId('review-result').textContent ?? '').not.toContain(REVIEW_ID);
    expect(screen.getByTestId('review-id-row').textContent).not.toContain(REVIEW_ID);

    fireEvent.click(screen.getByTestId('review-copy-id-button'));
    expect(writeText).toHaveBeenCalledWith(REVIEW_ID);
  });

  it('degrades to a no-op, not a throw, when navigator.clipboard is unavailable', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: REVIEW_ID, resumed: false },
      [`GET /api/reviews/${REVIEW_ID}`]: {
        review_id: REVIEW_ID,
        status: 'RUNNING',
        decision: null,
        message: null,
        has_output: false,
      },
    });
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined });

    await submitAndReachStatus();

    expect(() => fireEvent.click(screen.getByTestId('review-copy-id-button'))).not.toThrow();
  });
});

describe('AC3 — no attorney-approval framing anywhere this component renders', () => {
  it.each(['ERROR', 'MANUAL_REVIEW_REQUIRED', 'DONE'])(
    'never renders the disclaimer for a terminal %s review',
    async (status) => {
      stubFetch({
        'POST /api/reviews': { review_id: REVIEW_ID, resumed: false },
        [`GET /api/reviews/${REVIEW_ID}`]: {
          review_id: REVIEW_ID,
          status,
          decision: status === 'DONE' ? 'ACCEPT' : null,
          message: 'A legal admin will review it.',
          has_output: false,
        },
      });

      await submitAndReachStatus();
      await screen.findByTestId('review-result');

      // Checked via the removed disclaimer's own lead-in text — see the
      // module docstring's AC3 note for why this isn't the swept phrase
      // itself.
      expect(document.body.textContent ?? '').not.toContain('Tool recommendation only');
    },
  );
});

describe('AC4 — the critic-flagged variant keeps the same voice and the same gate', () => {
  it('flags before the download button, still names the outcome, and still saves nothing until a human clicks', async () => {
    const fetchMock = stubFetch({
      'POST /api/reviews': { review_id: REVIEW_ID, resumed: false },
      [`GET /api/reviews/${REVIEW_ID}`]: {
        review_id: REVIEW_ID,
        status: 'DONE',
        decision: 'REQUEST_CHANGE',
        message: null,
        has_output: true,
        critic_delta: {
          contested_replacements: [{ critic_objection: 'Drifts from the playbook position.' }],
          added_issues: [],
        },
      },
      [`GET /api/reviews/${REVIEW_ID}/output`]: { url: PRESIGNED_URL, expires_in: 60 },
    });

    await submitAndReachStatus();
    await screen.findByTestId('review-result');

    expect(screen.getByTestId('review-outcome').textContent).toBe('Changes requested');
    const indicator = await screen.findByTestId('review-critic-delta');
    const button = screen.getByTestId('review-download-button');
    const relation = indicator.compareDocumentPosition(button);
    expect(relation & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    // The gate is still in force: no automatic save, no truthful save line,
    // until a human passes the indicator and clicks.
    expect(screen.queryByTestId('review-saved-line')).toBeNull();
    expect(anchorClickSpy).not.toHaveBeenCalled();

    fireEvent.click(button);
    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/output'))).toBe(true);
  });
});
