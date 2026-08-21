/**
 * review-terminal-distinctness.test.tsx — A9's open question, answered
 * against rendered output instead of a code reading (issue #450 item 4).
 *
 * THE QUESTION (audit §A9): can an attorney mistake a
 * `MANUAL_REVIEW_REQUIRED` review for a `DONE` one? It stayed open because
 * answering it appeared to need a real review reaching that state on the live
 * deployment — which spends real money and writes production data (§E6).
 *
 * It does not. `MANUAL_REVIEW_REQUIRED` is a terminal review STATUS that the
 * detail endpoint reports (`backend/src/reviews.py`'s `TERMINAL_STATUSES` /
 * `STATUS_USER_MESSAGES`), so the whole question is "what does the Review tab
 * render for that response body" — reachable by serving the response the
 * backend would have served. What that gets us is every structural
 * differentiator: which hero state renders, which copy renders, whether a
 * download affordance exists. What it does NOT get us is pixels — jsdom runs
 * with `css: false`, so "distinct enough at a glance" in the colour/contrast
 * sense still belongs to a browser pass.
 *
 * The three differentiators pinned below are the ones an attorney would
 * actually read, and each is independently sufficient:
 *
 *   1. the hero: `toaster-state-done` (toast pops out) vs
 *      `toaster-state-sober` (the muted, unplugged X mark);
 *   2. the copy: `STATUS_USER_MESSAGES`' "could not be automatically
 *      reviewed — a legal admin will review it" vs "No requested changes
 *      identified by tool.";
 *   3. the download affordance, present on one and absent on the other.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

function stubFetch(routes: Record<string, unknown>): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
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
      return { ok: true, status: 200, json: async () => entry } as Response;
    }),
  );
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

/**
 * Drive the REAL component through submit → poll until it renders `detail`
 * verbatim as the terminal response.
 */
async function submitAndSettle(detail: Record<string, unknown>): Promise<void> {
  stubFetch({
    'POST /api/reviews': { review_id: 'rev-1', resumed: false },
    'GET /api/reviews/rev-1': detail,
    'GET /api/reviews/rev-1/output': { url: 'https://s3.example.test/o.docx', expires_in: 60 },
  });
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-result');
}

/** A clean, successful review: nothing to change, output ready to download. */
const DONE_ACCEPT = {
  review_id: 'rev-1',
  status: 'DONE',
  decision: 'ACCEPT',
  message: null,
  has_output: true,
};

/**
 * The manual-review outcome, exactly as `get_review_detail` reports it: the
 * terminal status, its fixed `STATUS_USER_MESSAGES` sentence as `message`,
 * a classified `reason` carried as system metadata, and no output object
 * (nothing was produced to download).
 */
const MANUAL_REVIEW = {
  review_id: 'rev-1',
  status: 'MANUAL_REVIEW_REQUIRED',
  decision: 'MANUAL_REVIEW_REQUIRED',
  message:
    'Your document could not be automatically reviewed — a legal admin will ' +
    'review it and follow up with you. No action is needed on your part right now.',
  reason: 'document_too_large',
  has_output: false,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('A9 — MANUAL_REVIEW_REQUIRED vs DONE, as rendered', () => {
  it('pops the toast out of the toaster on a clean DONE', async () => {
    await submitAndSettle(DONE_ACCEPT);

    expect(screen.getByTestId('toaster-state-done')).toBeInTheDocument();
    expect(screen.queryByTestId('toaster-state-sober')).toBeNull();
    expect(screen.getByTestId('review-result').textContent).toContain(
      'No requested changes identified by tool.',
    );
    expect(screen.getByTestId('review-download-button')).toBeInTheDocument();
  });

  it('renders the sober hero, not the popped toast, on MANUAL_REVIEW_REQUIRED', async () => {
    await submitAndSettle(MANUAL_REVIEW);

    // Differentiator 1 — the hero. These two are mutually exclusive states of
    // the same illustration, so the completed-review picture cannot appear on
    // a review that was NOT completed.
    expect(screen.getByTestId('toaster-state-sober')).toBeInTheDocument();
    expect(screen.queryByTestId('toaster-state-done')).toBeNull();
  });

  it('tells the attorney a human is taking it over, in words DONE never uses', async () => {
    await submitAndSettle(MANUAL_REVIEW);

    // Differentiator 2 — the copy. Reading the screen and reading it as
    // "finished, nothing to change" must not be possible.
    const result = screen.getByTestId('review-result').textContent ?? '';
    expect(result).toContain('could not be automatically reviewed');
    expect(result).toContain('a legal admin will review it');
    expect(result).not.toContain('No requested changes identified by tool.');
    // Issue #492 removed the attorney-approval disclaimer that used to sit
    // on every terminal state — asserted absent here, not present, now that
    // attorney/legal review is a policy that lives entirely outside this
    // product. Checked via the disclaimer's own lead-in text rather than
    // repeating the swept phrase verbatim in this file.
    expect(result).not.toContain('Tool recommendation only');
  });

  it('offers nothing to download, because nothing was produced', async () => {
    await submitAndSettle(MANUAL_REVIEW);

    // Differentiator 3. A download button on this state would be the single
    // most misleading thing the screen could do: it would imply a finished
    // work product exists.
    expect(screen.queryByTestId('review-download-button')).toBeNull();
  });

  it('never surfaces the internal reason token as the user-facing message', async () => {
    await submitAndSettle(MANUAL_REVIEW);

    // `reason` is system metadata (backend/src/reviews.py: "carried separately
    // as system metadata, not rendered as its own message"). It may appear in
    // the small technical trailer, but must not stand in for the explanation.
    const result = screen.getByTestId('review-result').textContent ?? '';
    expect(result).not.toContain('document_too_large');
  });
});
