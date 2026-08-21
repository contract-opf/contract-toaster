/**
 * review-cancel.test.tsx — the reviewer can stop a running review, and a
 * stopped review does not render as a failure.
 *
 * Reported 2026-08-04: a review wedged on step 1 "kept ticking" for many
 * minutes and there was no cancel button anywhere in the UI. There was no
 * cancel route either — POST /api/reviews was the only review-mutating
 * endpoint — so the only exits were the pipeline finishing or a restart.
 *
 * Two properties matter and are pinned here:
 *
 *   1. The control exists exactly when it is needed — for the whole working
 *      phase, including before the first progress stage lands, which is the
 *      state the reported review was stuck in.
 *   2. The honesty of the states. Cancellation is cooperative: the pipeline
 *      stops at its next checkpoint, which can be on the far side of an
 *      in-flight model call. So "stop requested" and "stopped" are different
 *      things and must look different — telling someone it had stopped while
 *      it was still spending their money is the failure mode to avoid.
 *
 * Fully offline — aws-amplify/auth and @aws-amplify/ui-react are mocked,
 * fetch is stubbed. No live AWS/Cognito/network.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: { idToken: { toString: () => 'mock-id-token' } },
  })),
}));

// sounds.ts is deliberately NOT mocked: it already no-ops without a real
// AudioContext, and the other ReviewSubmission tests in this suite render it
// unmocked too. A partial mock here would only risk drifting from its exports.

const WAIT = { timeout: 5000 };

interface Routes {
  detail: Record<string, unknown>;
  cancelStatus?: number;
}

function stubFetch(routes: Routes): { cancelCalls: string[] } {
  const cancelCalls: string[] = [];
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;

    if (pathname.endsWith('/cancel')) {
      cancelCalls.push(`${init?.method ?? 'GET'} ${pathname}`);
      const statusCode = routes.cancelStatus ?? 202;
      return {
        ok: statusCode < 400,
        status: statusCode,
        json: async () => ({}),
      } as Response;
    }
    if (pathname === '/api/playbooks') {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          playbooks: [{ playbook_id: 'pb', display_name: 'Contract', status: 'active' }],
        }),
      } as Response;
    }
    if (pathname === '/api/reviews' && (init?.method ?? 'GET') === 'POST') {
      return { ok: true, status: 202, json: async () => ({ review_id: 'r-1' }) } as Response;
    }
    if (pathname.startsWith('/api/reviews/')) {
      return { ok: true, status: 200, json: async () => routes.detail } as Response;
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return { cancelCalls };
}

const RUNNING = {
  review_id: 'r-1',
  status: 'RUNNING',
  decision: null,
  message: null,
  has_output: false,
};

/**
 * Drive the component into the working phase the way a reviewer does: pick a
 * file and submit. The submission response supplies the review_id that starts
 * polling.
 */
function submitAReview(): void {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(['x'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
  // fireEvent rather than user-event: this repo does not carry
  // @testing-library/user-event, and a file input needs its `files` set
  // directly anyway.
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  fireEvent.change(input);
  fireEvent.submit(document.querySelector('form') as HTMLFormElement);
}

describe('stopping a running review', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('offers no stop control before a review is submitted', async () => {
    stubFetch({ detail: RUNNING });
    render(<ReviewSubmission catalogVersion={0} />);
    await waitFor(() => expect(screen.queryByTestId('review-progress')).toBeNull(), WAIT);
    expect(screen.queryByTestId('review-cancel-button')).toBeNull();
  });

  it('offers a stop control while the review is running with no stage yet', async () => {
    // No `progress_stage` on the detail: this is the indeterminate state the
    // reported review was wedged in, and precisely when a stop is most needed.
    stubFetch({ detail: RUNNING });
    render(<ReviewSubmission catalogVersion={0} />);
    await waitFor(() => expect(document.querySelector('form')).toBeTruthy(), WAIT);
    submitAReview();
    await screen.findByTestId('review-cancel-button', {}, WAIT);
  });

  it('POSTs the cancel and switches to an honest stopping state', async () => {
    const { cancelCalls } = stubFetch({ detail: RUNNING });
    render(<ReviewSubmission catalogVersion={0} />);
    await waitFor(() => expect(document.querySelector('form')).toBeTruthy(), WAIT);
    submitAReview();

    const button = await screen.findByTestId('review-cancel-button', {}, WAIT);
    // ct-button renders a real <button> into its light DOM; click that.
    fireEvent.click(button.querySelector('button') ?? button);

    await waitFor(
      () => expect(cancelCalls).toContain('POST /api/reviews/r-1/cancel'),
      WAIT,
    );
    // The button must not simply sit there looking unpressed, and must not
    // claim the review has stopped either — the pipeline is still running.
    const pending = await screen.findByTestId('review-cancel-pending', {}, WAIT);
    expect(pending.textContent).toMatch(/stopping/i);
    expect(screen.queryByTestId('review-cancel-button')).toBeNull();
  });

  it('says so plainly when the review finished before it could be stopped', async () => {
    stubFetch({ detail: RUNNING, cancelStatus: 409 });
    render(<ReviewSubmission catalogVersion={0} />);
    await waitFor(() => expect(document.querySelector('form')).toBeTruthy(), WAIT);
    submitAReview();

    const button = await screen.findByTestId('review-cancel-button', {}, WAIT);
    fireEvent.click(button.querySelector('button') ?? button);

    const notice = await screen.findByTestId('review-cancel-error', {}, WAIT);
    expect(notice.textContent).toMatch(/finished before it could be stopped/i);
  });
});

// ---------------------------------------------------------------------------
// The terminal-state rendering is testable without driving the whole
// submission flow, and is where the honesty rules live.
// ---------------------------------------------------------------------------

describe('a stopped review reads as stopped, not as broken', () => {
  it('maps CANCELLED to a muted outcome chip, never a danger one', async () => {
    const { OUTCOME_CHIPS } = await import('../outcome');
    const chip = OUTCOME_CHIPS.CANCELLED;
    expect(chip).toBeDefined();
    expect(chip.variant).toBe('muted');
    expect(chip.variant).not.toBe('danger');
  });

  it('produces no failure explanation for a cancelled review', async () => {
    const { explainFailure } = await import('../ReviewSubmission');
    // A cancelled row carries neither a reason token nor a failing stage,
    // because nothing failed. If a stop ever started rendering the red
    // failure banner, this is what would catch it.
    expect(explainFailure({ reason: null, failing_stage: null })).toBeNull();
  });
});
