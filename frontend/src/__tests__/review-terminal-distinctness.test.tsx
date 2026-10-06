/**
 * review-terminal-distinctness.test.tsx — A9's open question, answered
 * against rendered output instead of a code reading (issue #450 item 4).
 *
 * THE QUESTION (audit §A9): can an attorney mistake a review that did not
 * complete for a `DONE` one? It stayed open because answering it appeared to
 * need a real review reaching that state on the live deployment — which
 * spends real money and writes production data (§E6).
 *
 * It does not: the whole question is "what does the Review tab render for
 * that response body", reachable by serving the response the backend would
 * have served. The question was first asked about `MANUAL_REVIEW_REQUIRED`;
 * issue #133 (owner decision 2026-09-16) retired that status — a review never
 * concludes as "manual review required", a run that does not complete is
 * `ERROR` with its `reason` — so it is now asked about the `ERROR` row the
 * same oversized document produces today (`reason: document_too_large`). What
 * serving the body gets us is every structural differentiator: which hero
 * state renders, which copy renders, whether a download affordance exists.
 * What it does NOT get us is pixels — jsdom runs with `css: false`, so
 * "distinct enough at a glance" in the colour/contrast sense still belongs to
 * a browser pass.
 *
 * The three differentiators pinned below are the ones an attorney would
 * actually read, and each is independently sufficient:
 *
 *   1. the hero: `toaster-state-done` (toast pops out) vs
 *      `toaster-state-error` (the muted, unplugged X mark);
 *   2. the copy: the reason's own explanation (`REASON_EXPLANATIONS`) vs
 *      "No requested changes identified by tool.";
 *   3. the download affordance, present on one and absent on the other.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ReviewSubmission, { REASON_EXPLANATIONS } from '../ReviewSubmission';
import {
  DEFAULT_PLAYBOOKS,
  findReviewResult,
  pressSubmit,
  stateBadge,
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

function stubFetch(routes: Record<string, unknown>): void {
  // Issue #733: the catalog is a fixture every scenario needs — the console
  // will not arm its lever without an active playbook.
  routes = { '/api/playbooks': DEFAULT_PLAYBOOKS, ...routes };
  vi.stubGlobal(
    'fetch',
    // eslint-disable-next-line @typescript-eslint/require-await
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // eslint-disable-next-line @typescript-eslint/no-base-to-string
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
      if (pathname.endsWith('.mp3')) {
        // eslint-disable-next-line @typescript-eslint/require-await
        return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) } as Response;
      }
      const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
      const entry = routes[key];
      if (entry === undefined) {
        // eslint-disable-next-line @typescript-eslint/require-await
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }
      // eslint-disable-next-line @typescript-eslint/require-await
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
  await pressSubmit();
  await findReviewResult();
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
 * A review that did not complete, exactly as `get_review_detail` reports it
 * since issue #133: status `ERROR`, the classified `reason` that explains it,
 * no decision, no status-keyed `message`, and no output object (nothing was
 * produced to download).
 */
const FAILED_TOO_LARGE = {
  review_id: 'rev-1',
  status: 'ERROR',
  decision: null,
  message: null,
  reason: 'document_too_large',
  has_output: false,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('A9 — a failed review vs DONE, as rendered', () => {
  it('pops the toast out of the toaster on a clean DONE', async () => {
    await submitAndSettle(DONE_ACCEPT);

    // Issue #733: `-sober` was one appearance for every bad ending; the
    // console names the STATUS, so the badge to look for is resolved.
    expect(screen.getByTestId(stateBadge('done'))).toBeInTheDocument();
    expect(screen.queryByTestId(stateBadge('sober', 'ERROR'))).toBeNull();
    expect(screen.getByTestId('review-result').textContent).toContain(
      'No requested changes identified by tool.',
    );
    expect(screen.getByTestId('review-download-button')).toBeInTheDocument();
  });

  it('renders the sober hero, not the popped toast, on a failed review', async () => {
    await submitAndSettle(FAILED_TOO_LARGE);

    // Differentiator 1 — the hero. These two are mutually exclusive states of
    // the same illustration, so the completed-review picture cannot appear on
    // a review that was NOT completed.
    expect(
      screen.getByTestId(stateBadge('sober', 'ERROR')),
    ).toBeInTheDocument();
    expect(screen.queryByTestId(stateBadge('done'))).toBeNull();
  });

  it('tells the attorney what happened and what to do, in words DONE never uses', async () => {
    await submitAndSettle(FAILED_TOO_LARGE);

    // Differentiator 2 — the copy. Reading the screen and reading it as
    // "finished, nothing to change" must not be possible.
    const result = screen.getByTestId('review-result').textContent ?? '';
    expect(result).toContain(REASON_EXPLANATIONS.document_too_large.cause);
    expect(result).toContain(REASON_EXPLANATIONS.document_too_large.fix);
    expect(result).not.toContain('No requested changes identified by tool.');
    // Issue #492 removed the attorney-approval disclaimer that used to sit
    // on every terminal state — asserted absent here, not present, now that
    // attorney/legal review is a policy that lives entirely outside this
    // product. Checked via the disclaimer's own lead-in text rather than
    // repeating the swept phrase verbatim in this file.
    expect(result).not.toContain('Tool recommendation only');
  });

  it('offers nothing to download, because nothing was produced', async () => {
    await submitAndSettle(FAILED_TOO_LARGE);

    // Differentiator 3. A download button on this state would be the single
    // most misleading thing the screen could do: it would imply a finished
    // work product exists.
    expect(screen.queryByTestId('review-download-button')).toBeNull();
  });

  it('never surfaces the internal reason token as the user-facing message', async () => {
    await submitAndSettle(FAILED_TOO_LARGE);

    // `reason` is system metadata (backend/src/reviews.py: "carried separately
    // as system metadata, not rendered as its own message"). It may appear in
    // the small technical trailer, but must not stand in for the explanation.
    // Issue #733: on the console the trailer lives INSIDE the result panel, so
    // "not in the result" would forbid the trailer itself. The claim is the
    // same either way — the token is trailer material, never the explanation —
    // so it is asserted as "only there".
    const result = screen.getByTestId('review-result').textContent ?? '';
    const trailer = screen.queryByTestId('review-failure-reason')?.textContent ?? '';
    expect(result.replace(trailer, '')).not.toContain('document_too_large');
    expect(screen.getByTestId('review-outcome').textContent).not.toContain('document_too_large');
  });
});
