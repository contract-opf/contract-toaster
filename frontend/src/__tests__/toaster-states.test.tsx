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
 * Also locks in: the attorney-approval watermark renders on every terminal
 * state, and the illustration's stylesheet honors
 * `prefers-reduced-motion: reduce`.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi } from 'vitest';
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
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-status');
}

describe('toaster illustration — ReviewStatus visual states', () => {
  it('PENDING/RUNNING render the doneness-progress treatment, not the toast-up or sober one', async () => {
    stubFetch({
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

    expect(await screen.findByTestId('toaster-state-progress')).toBeInTheDocument();
    expect(screen.queryByTestId('toaster-state-done')).toBeNull();
    expect(screen.queryByTestId('toaster-state-sober')).toBeNull();
  });

  it('DONE renders the toast-up treatment alongside the unchanged download gate', async () => {
    stubFetch({
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
    await screen.findByTestId('review-result');

    expect(screen.getByTestId('toaster-state-done')).toBeInTheDocument();
    expect(screen.queryByTestId('toaster-state-progress')).toBeNull();
    expect(screen.queryByTestId('toaster-state-sober')).toBeNull();

    // The #255/#271 download gate is untouched: confidence band, watermark,
    // and download button all still render.
    expect(screen.getByTestId('review-confidence-band').textContent).toContain('HIGH_CONFIDENCE');
    expect(screen.getByTestId('review-download-button')).toBeInTheDocument();
    expect(screen.getByTestId('review-result').textContent).toContain(
      'Tool recommendation only — attorney approval required.',
    );
  });

  it.each(['ERROR', 'MANUAL_REVIEW_REQUIRED', 'ERROR_MANUAL_REVIEW_REQUIRED'])(
    '%s renders the sober treatment, never the toast-up one, and still carries the watermark',
    async (status) => {
      stubFetch({
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
      await screen.findByTestId('review-result');

      expect(screen.getByTestId('toaster-state-sober')).toBeInTheDocument();
      expect(screen.queryByTestId('toaster-state-done')).toBeNull();
      expect(screen.queryByTestId('toaster-state-progress')).toBeNull();
      expect(screen.getByTestId('review-result').textContent).toContain(
        'Tool recommendation only — attorney approval required.',
      );
      // No download button when there is no output.
      expect(screen.queryByTestId('review-download-button')).toBeNull();
    },
  );

  it('never renders an Exos/EXOS string across illustrated states', async () => {
    stubFetch({
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
    await screen.findByTestId('review-result');
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
