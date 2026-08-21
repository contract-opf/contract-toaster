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
 * Issue #442 adds the second half: the `reason` TOKEN, when the backend
 * managed to classify one, must beat the stage-keyed copy — a review that
 * died because the model account was out of credits has to SAY so, not offer
 * three guesses. And the prose must carry none of what the backend knew and
 * must not surface: no raw `HTTP <n>`, endpoint, or key material (#425).
 *
 * Fully offline — fetch stubbed, no network.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
function stubFailedReview(
  failing_stage: string | null,
  reason = 'unhandled_exception',
  status = 'ERROR',
  normalization_notes?: string | null,
): void {
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
            status,
            decision: null,
            message: null,
            has_output: false,
            failing_stage,
            reason,
            normalization_notes,
          }),
        } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }),
  );
}

async function submitAndFail(
  failing_stage: string | null,
  reason?: string,
  status?: string,
  normalization_notes?: string | null,
): Promise<void> {
  stubFailedReview(failing_stage, reason, status, normalization_notes);
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

describe('the classified reason beats the stage guess (issue #442)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('names an out-of-credits model account, and who fixes it, instead of guessing', async () => {
    // The production case: run_review died on a 402. The stage copy can only
    // say "the model could not complete the review"; the reason knows better.
    await submitAndFail('run_review', 'model_account_out_of_credits');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/run out of credits/i);
    expect(panel).toHaveTextContent(/add funds/i);
    expect(panel).toHaveTextContent(/model & api key/i);
    // The vaguer stage-keyed fallback must NOT be what got rendered.
    expect(panel).not.toHaveTextContent(/exact cause was not identified/i);
    // It must not blame the reader's document for an operator's billing problem.
    expect(panel).toHaveTextContent(/nothing is wrong with your document/i);
    // The token stays visible for an admin to quote, alongside the stage.
    expect(screen.getByTestId('review-failing-stage')).toHaveTextContent('run_review');
    expect(screen.getByTestId('review-failure-reason')).toHaveTextContent(
      'model_account_out_of_credits',
    );
  });

  it('tells a rejected key apart from an unavailable model apart from a rate limit', async () => {
    await submitAndFail('run_review', 'model_key_rejected');
    expect(await screen.findByTestId('review-failure')).toHaveTextContent(/rejected the key/i);

    cleanup();
    vi.unstubAllGlobals();
    await submitAndFail('run_review', 'model_unavailable');
    expect(await screen.findByTestId('review-failure')).toHaveTextContent(/not available/i);

    cleanup();
    vi.unstubAllGlobals();
    await submitAndFail('run_review', 'model_rate_limited');
    expect(await screen.findByTestId('review-failure')).toHaveTextContent(
      /too many were sent at once/i,
    );
  });

  it('falls back to the stage copy for a reason this build has never heard of', async () => {
    // Forward compatibility: a newer backend token must degrade to today's
    // stage-keyed copy, never to a blank panel.
    await submitAndFail('run_review', 'a_token_from_a_newer_backend');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/could not complete the review/i);
    expect(screen.getByTestId('review-failure-reason')).toHaveTextContent(
      'a_token_from_a_newer_backend',
    );
  });

  it('explains a failure that recorded a reason but no stage', async () => {
    // A quarantine writes `reason` with no `failing_stage` at all, so before
    // #442 this review rendered no explanation whatsoever.
    await submitAndFail(null, 'submission_time_bundle_retired', 'QUARANTINED');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/replaced or switched off/i);
    expect(panel).toHaveTextContent(/submit the document again/i);
    expect(screen.queryByTestId('review-failing-stage')).toBeNull();
    expect(screen.getByTestId('review-failure-reason')).toHaveTextContent(
      'submission_time_bundle_retired',
    );
  });

  it('tells the reader it is their document that is too long, and what to do', async () => {
    await submitAndFail('run_review', 'model_context_length_exceeded', 'MANUAL_REVIEW_REQUIRED');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/longer than the model can read/i);
    expect(panel).toHaveTextContent(/split it into smaller documents/i);
  });

  it('explains an OPF playbook the tool could not honestly compose (#479)', async () => {
    await submitAndFail(null, 'opf_knowledge_refused', 'MANUAL_REVIEW_REQUIRED');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/cannot honestly turn into review instructions/i);
    expect(panel).toHaveTextContent(/an admin needs to check/i);
    expect(screen.getByTestId('review-failure-reason')).toHaveTextContent(
      'opf_knowledge_refused',
    );
  });

  it('explains a playbook missing its digest, distinctly from a refusal (#479)', async () => {
    await submitAndFail(null, 'opf_digest_missing', 'MANUAL_REVIEW_REQUIRED');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/missing the reference material/i);
    expect(panel).toHaveTextContent(/fix or re-upload/i);
    expect(screen.getByTestId('review-failure-reason')).toHaveTextContent('opf_digest_missing');
  });

  it('explains an unjudged Floor invariant as a fail-closed stop, not a document problem (#479)', async () => {
    await submitAndFail('run_review', 'floor_invariant_unjudged', 'MANUAL_REVIEW_REQUIRED');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/required rules could not be checked/i);
    expect(panel).toHaveTextContent(/worth submitting again/i);
    expect(screen.getByTestId('review-failure-reason')).toHaveTextContent(
      'floor_invariant_unjudged',
    );
  });

  it('never surfaces a raw status code, endpoint, or provider name (#425)', async () => {
    const tokens = [
      'model_account_out_of_credits',
      'model_key_rejected',
      'model_rate_limited',
      'model_unavailable',
      'model_context_length_exceeded',
      'model_empty_content',
      'model_output_truncated',
    ];
    for (const token of tokens) {
      cleanup();
      vi.unstubAllGlobals();
      await submitAndFail('run_review', token);
      const text = (await screen.findByTestId('review-failure')).textContent ?? '';
      expect(text).not.toMatch(/http/i);
      expect(text).not.toMatch(/openrouter/i);
      expect(text).not.toMatch(/\b(401|402|403|404|429|503)\b/);
      // No bare digits at all in the failure copy — a status code has no
      // route to the screen, and neither does a key or an endpoint port.
      expect(text).not.toMatch(/\d/);
    }
  });
});

describe('unnormalizable_input no longer sends the reader to re-save a .docx (#530)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('names the unreadable tracked change, tells the reader to resolve it in Word, and surfaces the per-paragraph note', async () => {
    const note =
      "Paragraph 'Indemnification': pending tracked change has no resulting_text " +
      '-- malformed revision record; cannot determine the operative text to accept.';
    await submitAndFail(null, 'unnormalizable_input', 'MANUAL_REVIEW_REQUIRED', note);

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(
      /a tracked change the tool could not safely read/i,
    );
    expect(panel).toHaveTextContent(/review that tracked change directly/i);
    // The old, flatly-wrong copy must be gone.
    expect(panel).not.toHaveTextContent(/could not be read as a word document/i);
    expect(panel).not.toHaveTextContent(/saved by word/i);

    expect(screen.getByTestId('review-failure-normalization-notes')).toHaveTextContent(note);
  });

  it('omits the per-paragraph note paragraph when the backend sent none', async () => {
    await submitAndFail(null, 'unnormalizable_input', 'MANUAL_REVIEW_REQUIRED');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/a tracked change the tool could not safely read/i);
    expect(screen.queryByTestId('review-failure-normalization-notes')).toBeNull();
  });
});
