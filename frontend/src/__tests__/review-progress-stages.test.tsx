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
import { stages } from '../orbit-diner/state';
import type { Stage } from '../orbit-diner/types';
import {
  DEFAULT_PLAYBOOKS,
  findReviewResult,
  pressSubmit,
  progressBar,
  progressStep,
  stateBadge,
} from './support/consoleSurface';
import { STAGE_VIGNETTES, stageNumber } from '../toaster/stageTheater';

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
      // Issue #733: the console needs an active playbook before it will submit.
      if (pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
      }
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
  await pressSubmit();
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

    // Issue #733: one progressbar, whichever surface draws it. Both report
    // the step through ARIA, which is the fact a screen reader gets.
    await waitFor(() => expect(progressStep()).toBe(step));
    const bar = progressBar()!;
    expect(bar.getAttribute('aria-valuemin')).toBe('1');
    expect(bar.getAttribute('aria-valuemax')).toBe('4');
    expect(bar.getAttribute('aria-valuetext')).toContain(`Step ${step} of 4`);

    // The doneness LEVEL is what the stylesheet darkens on. css:false in
    // jsdom, so assert the hook, never a colour: the console darkens the slice
    // itself through a filter custom property, keyed to the reported stage.
    const slice = document.querySelector<HTMLElement>('[data-part="slice"]')!;
    expect(slice.getAttribute('style')).toContain('--od-doneness');
    expect(slice.getAttribute('style')).toContain(stages[token as Stage].filter);

    // The short label this fixture names is the one the stage table carries —
    // the fixture is a second reading of the wire contract, not a copy of the
    // table that could drift from it.
    expect(stages[token as Stage].label).toBe(label);

    // The text is the information and the accessibility — never the
    // darkening alone.
    const text = screen.getByTestId('review-progress-step-text');
    expect(text.textContent).toContain(`Step ${step} of 4`);
    // One of the polite regions names this stage, in the caption from that
    // same stage table (issue #733).
    const spoken = stages[token as Stage].caption;
    const polite = Array.from(
      document.querySelectorAll<HTMLElement>('[aria-live="polite"]'),
    );
    expect(polite.some((el) => (el.textContent ?? '').includes(spoken))).toBe(true);
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
    expect(progressStep()).toBe(4);
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
      expect(await screen.findByTestId(stateBadge('progress'))).toBeInTheDocument();
      expect(document.body.textContent ?? '').toContain('Toasting your review…');
      // …but claiming nothing about which step: the console keeps one bar and
      // reports no step on it, so nothing on screen names one (issue #733).
      expect(progressStep()).toBeNull();
      expect(document.body.textContent ?? '').not.toMatch(/Step \d of 4/);
    },
  );

  it('replaces the information-free indeterminate bar once a real stage is known', async () => {
    const detail = { current: runningDetail(null) };
    stubPollingFetch('rev-progress', detail);
    await submit();

    // Before any stage lands, the information-free signal is the honest one.
    await screen.findByTestId(stateBadge('progress'));
    expect(progressStep()).toBeNull();

    detail.current = runningDetail('critic_pass');
    await waitFor(
      () => {
        expect(progressStep()).toBe(2);
      },
      { timeout: 6000 },
    );
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
    await findReviewResult();

    expect(progressBar()).toBeNull();
    expect(screen.queryByTestId('review-progress-step-text')).toBeNull();
    expect(screen.queryByTestId(stateBadge('progress'))).toBeNull();
  });

  it('maps tokens to step numbers, and unknown tokens to no step', () => {
    // The token list is a wire contract with scripts/review_spine.py's
    // PROGRESS_STAGES — order here IS the step numbering. Issue #727 folded
    // the hand-written `PROGRESS_STEPS` into `STAGE_VIGNETTES`, which is a
    // projection of the console's `stages`, so this now pins the ORDER that
    // projection produces rather than a second literal of the same four.
    expect(STAGE_VIGNETTES.map((entry) => entry.token)).toEqual([
      'primary_pass',
      'critic_pass',
      'reconciliation',
      'redline',
    ]);
    expect(stageNumber('primary_pass')).toBe(1);
    expect(stageNumber('redline')).toBe(4);
    expect(stageNumber('run_review')).toBe(0);
    expect(stageNumber(null)).toBe(0);
    expect(stageNumber(undefined)).toBe(0);
    expect(stageNumber('')).toBe(0);
  });
});
