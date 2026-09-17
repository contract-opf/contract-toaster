/**
 * unnormalizable-copy-100.test.tsx — a control-character refusal must not be
 * explained as a tracked change (issue #100).
 *
 * `_screen_control_characters` (issue #632) and a malformed tracked-change
 * record both fail closed with the SAME `reason: "unnormalizable_input"`
 * (`scripts/normalize_input.py::build_unnormalizable_report`). Before this
 * issue, `REASON_EXPLANATIONS.unnormalizable_input`
 * (`frontend/src/ReviewSubmission.tsx`) was the only copy either one ever
 * got — written for the tracked-change case: "A paragraph in your document
 * has a tracked change the tool could not safely read... See the paragraph
 * named below." For a control-character refusal there is no tracked change,
 * and no paragraph is named anywhere in `normalization_notes` (that note is
 * deliberately COUNTS-ONLY — see `normalize_input.py`'s module docstring,
 * "Control-character screen"). Ordinary paper trips this: ZWNJ (Persian/Urdu
 * orthography), LRM/RLM (Hebrew/Arabic mixed with Latin), ZWSP from a
 * browser or Google Docs paste.
 *
 * `get_review_detail` (backend/src/reviews.py) now projects a structured
 * `reason_detail` alongside `reason` for this one branch
 * ("suspicious_control_characters"), and `explainFailure` prefers a
 * `REASON_DETAIL_EXPLANATIONS` entry over the plain `reason` copy when one
 * is present. This file locks that in from the DOM the reader actually
 * sees, and locks in that the ordinary tracked-change refusal is completely
 * unaffected.
 *
 * Fully offline — fetch stubbed, no network.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { selectedPlaybookId } from './support/consoleSurface';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const REVIEW_ID = 'b3e97b9c-6dc1-4b0a-8f5a-2c1a9e5a3d40';

function docx(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

/** Stub the catalog + submit + a terminal MANUAL_REVIEW_REQUIRED poll,
 * carrying whatever `reason_detail` (and `normalization_notes`) the caller
 * passes — the exact shape `get_review_detail` returns. */
function stubUnnormalizableReview(
  normalization_notes: string,
  reason_detail?: string | null,
): void {
  vi.stubGlobal(
    'fetch',
    // eslint-disable-next-line @typescript-eslint/require-await
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // eslint-disable-next-line @typescript-eslint/no-base-to-string
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();

      if (url.includes('/api/playbooks')) {
        return {
          ok: true,
          status: 200,
          // eslint-disable-next-line @typescript-eslint/require-await
          json: async () => ({
            playbooks: [{ playbook_id: 'eiaa', display_name: 'EIAA', status: 'active' }],
          }),
        } as Response;
      }
      if (method === 'POST' && url.endsWith('/api/reviews')) {
        return {
          ok: true,
          status: 202,
          // eslint-disable-next-line @typescript-eslint/require-await
          json: async () => ({ review_id: REVIEW_ID, resumed: false }),
        } as Response;
      }
      if (url.includes(`/api/reviews/${REVIEW_ID}`)) {
        return {
          ok: true,
          status: 200,
          // eslint-disable-next-line @typescript-eslint/require-await
          json: async () => ({
            review_id: REVIEW_ID,
            status: 'MANUAL_REVIEW_REQUIRED',
            decision: null,
            message: null,
            has_output: false,
            failing_stage: null,
            reason: 'unnormalizable_input',
            reason_detail,
            normalization_notes,
          }),
        } as Response;
      }
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }),
  );
}

async function submitAndFail(
  normalization_notes: string,
  reason_detail?: string | null,
): Promise<void> {
  stubUnnormalizableReview(normalization_notes, reason_detail);
  render(<ReviewSubmission />);
  await screen.findByTestId('review-file-input');
  // The lever is dead until the playbook catalog lands — see
  // review-failure-diagnosis.test.tsx's `submitAndFail` for why this wait is
  // required rather than optional.
  await waitFor(() => expect(selectedPlaybookId()).toBe('eiaa'));
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [docx()] } });
  fireEvent.click(screen.getByTestId('review-submit-button'));
}

describe('a control-character refusal is not explained as a tracked change (issue #100)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('names invisible control characters, not a tracked change, when reason_detail says so', async () => {
    const note =
      'Suspicious control characters in extracted text: 1 zero-width or ' +
      "bidirectional control character(s) detected (first at offset 11) -- " +
      "invisible characters in the model's input are a spoofing and " +
      'prompt-injection surface; refusing rather than stripping.';
    await submitAndFail(note, 'suspicious_control_characters');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).not.toHaveTextContent(/tracked change/i);
    expect(panel).not.toHaveTextContent(/paragraph named below/i);
    expect(panel).toHaveTextContent(/invisible control characters/i);

    // The counts-only note itself still renders alongside the copy.
    expect(screen.getByTestId('review-failure-normalization-notes')).toHaveTextContent(note);
  });

  it('still gives the tracked-change copy when no reason_detail is present', async () => {
    // The #530 case this table was originally written for — a genuinely
    // malformed tracked-change record — must be completely unaffected.
    const note =
      "Paragraph 'Indemnification': pending tracked change has no resulting_text " +
      '-- malformed revision record; cannot determine the operative text to accept.';
    await submitAndFail(note, null);

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/a tracked change the tool could not safely read/i);
    expect(panel).toHaveTextContent(/review that tracked change directly/i);
  });

  it('falls back to the tracked-change copy for a reason_detail token this build does not know', async () => {
    // Forward compatibility: an unrecognised reason_detail must not blank
    // the panel or crash the render — it degrades to today's default.
    await submitAndFail('some future sub-reason note', 'some_future_sub_reason');

    const panel = await screen.findByTestId('review-failure');
    expect(panel).toHaveTextContent(/a tracked change the tool could not safely read/i);
  });
});
