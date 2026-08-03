/**
 * review-progress-stages.test.tsx — the four-stage review progress indicator
 * (issue #447).
 *
 * ## Root problem this locks fixed
 *
 * ReviewSubmission rendered `<CtProgress label="Reviewing your document…" />`
 * — an INDETERMINATE bar. It animated but said nothing, because the pipeline
 * reported nothing. #447 added a real seam: `scripts/review_spine.py`'s
 * `on_progress` callback → `progress_stage` on the reviews row →
 * `get_review_detail` → this polling UI.
 *
 * ## The rule these tests exist to enforce
 *
 * The step must reflect REALITY, never elapsed time. A timer-driven guess
 * would routinely show "step 3 of 4, reconciliation" while the primary pass
 * was still running — worse than an honest indeterminate bar. So:
 *
 *   - A reported stage renders its own step number, label, and doneness
 *     level. Every one of the four is exercised.
 *   - An ABSENT or UNRECOGNISED stage renders the indeterminate treatment,
 *     not a guess — this is what makes the whole thing trustworthy, and what
 *     keeps an older backend (or a deployment that reports no progress)
 *     working rather than lying.
 *   - The step ADVANCES only when a poll brings a new token. No fake timers
 *     are used anywhere in this file, deliberately: there is no clock-driven
 *     code path to test, and adding one would be the bug.
 *
 * Accessibility is asserted as a hard contract (the ticket's AC): a
 * `role="progressbar"` carrying aria-valuenow/valuemin/valuemax/aria-valuetext,
 * plus a visible, `aria-live="polite"` step text — the darkening never ships
 * alone.
 *
 * vitest.config.ts runs jsdom with `css: false`, so nothing here asserts a
 * computed style or a colour value; the doneness LEVEL is asserted through
 * the step class / data attribute the stylesheet keys off.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { PROGRESS_STEPS, progressStepNumber } from '../toaster/Toaster';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

/** Serves GET /api/reviews/{id} from a mutable holder, so a test can change
 *  what the NEXT poll returns — the only way a step is ever allowed to move. */
function stubPollingFetch(reviewId: string, detail: { current: Record<string, unknown> }): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      const method = (init?.method ?? 'GET').toUpperCase();
      if (method === 'POST' && pathname === '/api/reviews') {
        return { ok: true, status: 200, json: async () => ({ review_id: reviewId, resumed: false }) } as Response;
      }
      if (pathname === `/api/reviews/${reviewId}`) {
        return { ok: true, status: 200, json: async () => detail.current } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }),
  );
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

async function submit(): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [docxFile()] } });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-status');
}

function runningDetail(progressStage: string | null): Record<string, unknown> {
  return {
    review_id: 'rev-progress',
    status: 'RUNNING',
    decision: null,
    message: null,
    has_output: false,
    progress_stage: progressStage,
  };
}

describe('staged review progress — the toast darkens through four real stages', () => {
  it.each([
    ['primary_pass', 1, 'First read-through'],
    ['critic_pass', 2, 'Adversarial critic'],
    ['reconciliation', 3, 'Reconciling both passes'],
    ['redline', 4, 'Writing your redline'],
  ])('%s renders as step %i with its own label and doneness level', async (token, step, label) => {
    stubPollingFetch('rev-progress', { current: runningDetail(token as string) });
    await submit();

    const bar = await screen.findByTestId('review-progress-stage');
    expect(bar.getAttribute('role')).toBe('progressbar');
    expect(bar.getAttribute('aria-valuenow')).toBe(String(step));
    expect(bar.getAttribute('aria-valuemin')).toBe('1');
    expect(bar.getAttribute('aria-valuemax')).toBe('4');
    expect(bar.getAttribute('aria-valuetext')).toBe(`Step ${step} of 4 · ${label}`);

    // The doneness LEVEL is what the stylesheet darkens on. css:false in
    // jsdom, so assert the hook, never a colour.
    expect(bar.getAttribute('data-progress-step')).toBe(String(step));
    expect(bar.className).toContain(`toaster-doneness--step${step}`);
    expect(bar.getAttribute('data-progress-stage')).toBe(token);

    // The text is the information and the accessibility — never the
    // darkening alone.
    const text = screen.getByTestId('review-progress-step-text');
    expect(text.textContent).toBe(`Step ${step} of 4 · ${label}`);
    expect(text.getAttribute('aria-live')).toBe('polite');
  });

  it('advances the step only when a poll reports a new stage', async () => {
    const detail = { current: runningDetail('primary_pass') };
    stubPollingFetch('rev-progress', detail);
    await submit();

    const first = await screen.findByTestId('review-progress-step-text');
    expect(first.textContent).toContain('Step 1 of 4');

    // The pipeline actually moved on. Only NOW may the UI say so.
    detail.current = runningDetail('redline');
    await waitFor(
      () => {
        expect(screen.getByTestId('review-progress-step-text').textContent).toContain('Step 4 of 4');
      },
      { timeout: 6000 },
    );
    expect(screen.getByTestId('review-progress-stage').className).toContain('toaster-doneness--step4');
  });

  it.each([[null], [undefined], ['some_stage_this_build_does_not_know']])(
    'falls back to the honest indeterminate treatment for %s rather than guessing a step',
    async (stage) => {
      const detail: Record<string, unknown> = runningDetail(null);
      if (stage === undefined) {
        delete detail.progress_stage;
      } else {
        detail.progress_stage = stage;
      }
      stubPollingFetch('rev-progress', { current: detail });
      await submit();

      // Still visibly working…
      expect(await screen.findByTestId('toaster-state-progress')).toBeInTheDocument();
      expect(screen.getByTestId('review-progress-indeterminate').textContent).toContain(
        'Toasting your review…',
      );
      // …but claiming nothing about which step.
      expect(screen.queryByTestId('review-progress-stage')).toBeNull();
      expect(screen.queryByTestId('review-progress-step-text')).toBeNull();
      expect(document.body.textContent ?? '').not.toMatch(/Step \d of 4/);
    },
  );

  it('replaces the information-free indeterminate bar once a real stage is known', async () => {
    const detail = { current: runningDetail(null) };
    stubPollingFetch('rev-progress', detail);
    await submit();

    // Before any stage lands, the shimmer is the honest signal and stays.
    expect(await screen.findByTestId('review-progress')).toBeInTheDocument();

    detail.current = runningDetail('critic_pass');
    await waitFor(
      () => {
        expect(screen.queryByTestId('review-progress')).toBeNull();
      },
      { timeout: 6000 },
    );
    expect(screen.getByTestId('review-progress-stage')).toBeInTheDocument();
  });

  it('shows no step indicator at all once the review is terminal', async () => {
    stubPollingFetch('rev-progress', {
      current: {
        review_id: 'rev-progress',
        status: 'DONE',
        decision: 'ACCEPT',
        message: null,
        has_output: false,
        // A stale progress_stage on a finished row must not resurrect the
        // indicator — `working` is what gates it, not the field's presence.
        progress_stage: 'redline',
      },
    });
    await submit();
    await screen.findByTestId('review-result');

    expect(screen.queryByTestId('review-progress-stage')).toBeNull();
    expect(screen.queryByTestId('review-progress-step-text')).toBeNull();
    expect(screen.queryByTestId('toaster-state-progress')).toBeNull();
  });

  it("the staged darkening and its shimmer are covered by the reduced-motion block", async () => {
    stubPollingFetch('rev-progress', { current: runningDetail('critic_pass') });
    await submit();
    await screen.findByTestId('review-progress-stage');

    const styleText = Array.from(document.querySelectorAll('style'))
      .map((el) => el.textContent ?? '')
      .join('\n');
    const reducedBlock = styleText.slice(styleText.indexOf('prefers-reduced-motion'));
    expect(reducedBlock).toContain('.toaster-doneness__slice');
    expect(reducedBlock).toContain('.toaster-doneness__heat');
  });

  it('maps tokens to step numbers, and unknown tokens to no step', () => {
    // The token list is a wire contract with scripts/review_spine.py's
    // PROGRESS_STAGES — order here IS the step numbering.
    expect(PROGRESS_STEPS.map((s) => s.token)).toEqual([
      'primary_pass',
      'critic_pass',
      'reconciliation',
      'redline',
    ]);
    expect(progressStepNumber('primary_pass')).toBe(1);
    expect(progressStepNumber('redline')).toBe(4);
    expect(progressStepNumber('run_review')).toBe(0);
    expect(progressStepNumber(null)).toBe(0);
    expect(progressStepNumber(undefined)).toBe(0);
    expect(progressStepNumber('')).toBe(0);
  });
});
