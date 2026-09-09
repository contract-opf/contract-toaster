/**
 * toaster-states.test.tsx — per-ReviewStatus visual states for the toaster
 * illustration (issue #280).
 *
 * ReviewSubmission.tsx reuses the existing ReviewStatus values (no new
 * states) and maps them onto three illustrated treatments, all decorative
 * (aria-hidden) and layered around the existing, already-tested markup:
 *
 *   - PENDING / RUNNING -> "doneness" progress treatment.
 *   - DONE               -> toast-up treatment; the #255/#271 download gate
 *                            (confidence band, critic delta, download
 *                            button, watermark) renders exactly as before.
 *   - ERROR /
 *     MANUAL_REVIEW_REQUIRED /
 *     ERROR_MANUAL_REVIEW_REQUIRED -> a distinct, sober (non-cute)
 *                            treatment; the existing designed copy is
 *                            untouched.
 *
 * Also locks in: the outcome headline renders on every terminal state (issue
 * #492 — no attorney-approval disclaimer any more, see
 * review-terminal-distinctness.test.tsx for the removal itself), and the
 * illustration's stylesheet honors `prefers-reduced-motion: reduce`.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import {
  DEFAULT_PLAYBOOKS,
  findReviewResult,
  pressSubmit,
  stateBadge,
} from './support/consoleSurface';

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

async function submit(): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  await pressSubmit();
  await screen.findByTestId('review-status');
}

describe('toaster illustration — ReviewStatus visual states', () => {
  it('PENDING/RUNNING render the doneness-progress treatment, not the toast-up or sober one', async () => {
    stubFetch({
      '/api/playbooks': DEFAULT_PLAYBOOKS,
      'POST /api/reviews': { review_id: 'rev-running', resumed: false },
      'GET /api/reviews/rev-running': {
        review_id: 'rev-running',
        status: 'RUNNING',
        decision: null,
        message: null,
        has_output: false,
      },
    });

    await submit();

    // Issue #733: `-progress` is the working progressbar on both surfaces;
    // `-sober` resolves to the console's status-named terminal badge.
    expect(await screen.findByTestId(stateBadge('progress'))).toBeInTheDocument();
    expect(screen.queryByTestId(stateBadge('done'))).toBeNull();
    expect(screen.queryByTestId(stateBadge('sober'))).toBeNull();
  });

  it('DONE renders the toast-up treatment alongside the unchanged download gate', async () => {
    stubFetch({
      '/api/playbooks': DEFAULT_PLAYBOOKS,
      'POST /api/reviews': { review_id: 'rev-done', resumed: false },
      'GET /api/reviews/rev-done': {
        review_id: 'rev-done',
        status: 'DONE',
        decision: 'REQUEST_CHANGE',
        message: null,
        has_output: true,
        confidence_band: 'HIGH_CONFIDENCE',
      },
    });

    await submit();
    await findReviewResult();

    expect(screen.getByTestId(stateBadge('done'))).toBeInTheDocument();
    expect(screen.queryByTestId(stateBadge('progress'))).toBeNull();
    expect(screen.queryByTestId(stateBadge('sober'))).toBeNull();

    // The #255/#271 download gate is untouched: confidence band and download
    // button still render. Issue #492 removed the attorney-approval
    // disclaimer that used to render alongside them — asserted absent here,
    // never present.
    expect(screen.getByTestId('review-confidence-band').textContent).toContain('HIGH_CONFIDENCE');
    expect(screen.getByTestId('review-download-button')).toBeInTheDocument();
    // "Tool recommendation only" is the removed disclaimer's own lead-in —
    // checked verbatim (not the phrase this file's own sweep target greps
    // for) so this assertion doesn't itself show up as a hit.
    expect(screen.getByTestId('review-result').textContent).not.toContain(
      'Tool recommendation only',
    );
    // The outcome headline (issue #492) renders instead — the same friendly
    // label describeOutcome resolves everywhere else.
    expect(screen.getByTestId('review-outcome').textContent).toBe('Changes requested');
  });

  // The ILLUSTRATION is deliberately shared by all three (see this file's
  // header) — the sober treatment, never the popped toast. The HEADLINE is
  // not: issue #666 split ERROR_MANUAL_REVIEW_REQUIRED's label off
  // MANUAL_REVIEW_REQUIRED's, because a run that failed and produced nothing
  // was reading in exactly the words of one that finished and is waiting on
  // a human. The three expected labels below are therefore three DIFFERENT
  // strings, and failed-review-outcome-666.test.tsx is what pins them apart
  // across every surface rather than only this one.
  it.each([
    ['ERROR', 'Failed'],
    ['MANUAL_REVIEW_REQUIRED', 'Needs manual review'],
    ['ERROR_MANUAL_REVIEW_REQUIRED', 'Failed — needs manual review'],
  ])(
    '%s renders the sober treatment (outcome headline %s), never the toast-up one, and no attorney-approval disclaimer',
    async (status, outcomeLabel) => {
      stubFetch({
        '/api/playbooks': DEFAULT_PLAYBOOKS,
        'POST /api/reviews': { review_id: `rev-${status}`, resumed: false },
        [`GET /api/reviews/rev-${status}`]: {
          review_id: `rev-${status}`,
          status,
          decision: status === 'MANUAL_REVIEW_REQUIRED' ? 'MANUAL_REVIEW_REQUIRED' : null,
          message: 'A legal admin will review it.',
          has_output: false,
        },
      });

      await submit();
      await findReviewResult();

      expect(screen.getByTestId(stateBadge('sober', status))).toBeInTheDocument();
      expect(screen.queryByTestId(stateBadge('done'))).toBeNull();
      expect(screen.queryByTestId(stateBadge('progress'))).toBeNull();
      // Issue #492: the outcome headline, never the removed disclaimer.
      expect(screen.getByTestId('review-outcome').textContent).toBe(outcomeLabel);
      // "Tool recommendation only" is the removed disclaimer's own lead-in —
      // checked verbatim (not the phrase this file's own sweep target greps
      // for) so this assertion doesn't itself show up as a hit.
      expect(screen.getByTestId('review-result').textContent).not.toContain(
        'Tool recommendation only',
      );
      // No download button when there is no output.
      expect(screen.queryByTestId('review-download-button')).toBeNull();
    },
  );

  it('never renders an tenant-brand string across illustrated states', async () => {
    stubFetch({
      '/api/playbooks': DEFAULT_PLAYBOOKS,
      'POST /api/reviews': { review_id: 'rev-brand', resumed: false },
      'GET /api/reviews/rev-brand': {
        review_id: 'rev-brand',
        status: 'DONE',
        decision: 'ACCEPT',
        message: null,
        has_output: false,
      },
    });

    await submit();
    await findReviewResult();
    expect(document.body.textContent ?? '').not.toMatch(/exos/i);
  });

  it("the illustration's stylesheet honors prefers-reduced-motion: reduce", async () => {
    stubFetch({});
    render(<ReviewSubmission />);
    await screen.findByTestId('review-submission');

    const styleText = Array.from(document.querySelectorAll('style'))
      .map((el) => el.textContent ?? '')
      .join('\n');
    expect(styleText).toMatch(/prefers-reduced-motion:\s*reduce/);
  });
});
