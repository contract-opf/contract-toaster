/**
 * review-failure-diagnosis.test.tsx — a failed review must say WHY.
 *
 * backend/src/reviews.py::record_stage_failure records the real per-stage
 * name that failed, and get_review_detail has always returned `failing_stage`
 * + `reason`. ReviewSubmission used to drop both and render a bare "ERROR",
 * which told the person who has to fix it nothing at all — a missing API key
 * and an unreadable document looked identical on screen.
 *
 * These lock in that the diagnosis reaches the DOM, and that it says what to
 * DO about it, not just what broke.
 *
 * Fully offline — fetch stubbed, no network.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const REVIEW_ID = 'e338b0c1-44f2-4913-a21c-6a901672a25e';

function docx(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

/** Stub the catalog + submit + a terminal FAILED poll for `failing_stage`. */
function stubFailedReview(failing_stage: string | null, reason = 'unhandled_exception'): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();

      if (url.includes('/api/playbooks')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            playbooks: [{ playbook_id: 'eiaa', display_name: 'EIAA', status: 'active' }],
          }),
        } as Response;
      }
      if (method === 'POST' && url.endsWith('/api/reviews')) {
        return {
          ok: true,
          status: 202,
          json: async () => ({ review_id: REVIEW_ID, resumed: false }),
        } as Response;
      }
      if (url.includes(`/api/reviews/${REVIEW_ID}`)) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            review_id: REVIEW_ID,
            status: 'ERROR',
            decision: null,
            message: null,
            has_output: false,
            failing_stage,
            reason,
          }),
        } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }),
  );
}

async function submitAndFail(failing_stage: string | null, reason?: string): Promise<void> {
  stubFailedReview(failing_stage, reason);
  render(<ReviewSubmission />);
  // Wait for the playbook catalog so the submit button is live.
  await screen.findByTestId('review-file-input');
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [docx()] } });
  fireEvent.click(screen.getByTestId('review-submit-button'));
}

describe('a failed review explains itself', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('names the missing API key as the cause, and where to fix it', async () => {
    await submitAndFail('build_model_client');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/no usable model api key/i);
    expect(panel).toHaveTextContent(/model & api key/i);
    // The technical stage stays visible for an admin to quote in a bug report.
    expect(screen.getByTestId('review-failing-stage')).toHaveTextContent('build_model_client');
  });

  it('distinguishes an unreviewable contract type from a key problem', async () => {
    await submitAndFail('load_playbook');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/contract type isn't set up/i);
    expect(panel).not.toHaveTextContent(/api key/i);
  });

  it('explains a model failure without blaming the user', async () => {
    await submitAndFail('run_review');

    expect(await screen.findByTestId('review-failure')).toHaveTextContent(
      /could not complete the review/i,
    );
  });

  it('still says something useful for an unrecognised stage', async () => {
    await submitAndFail('some_new_stage_we_have_not_mapped');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/stopped before it could finish/i);
    expect(screen.getByTestId('review-failing-stage')).toHaveTextContent(
      'some_new_stage_we_have_not_mapped',
    );
  });

  it('shows no failure panel when nothing failed', async () => {
    await submitAndFail(null);

    await screen.findByTestId('review-status');
    expect(screen.queryByTestId('review-failure')).toBeNull();
  });
});
