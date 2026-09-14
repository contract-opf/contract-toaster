/**
 * time-remaining-71.test.tsx — the measured time-remaining line on the
 * progress bar (issue #71, 2026-09-05 diagnostic finding G12).
 *
 * ## What this locks fixed
 *
 * The progress bar reported STAGE and never time. "Step 2 of 4, adversarial
 * critic" is true and does not answer the only question a reviewer actually
 * has: wait, or come back later? #71 adds one line, built from measurement —
 * `GET /api/review-duration-estimate` returns the p50/p90 of the last 50
 * FINISHED reviews of this playbook plus the median word count of that same
 * sample, and the panel scales those to the document in the toaster.
 *
 * ## The rules these tests enforce
 *
 *   - **Minutes, never seconds.** A per-second countdown on a pipeline whose
 *     own progress is reported in four coarse stages would be visibly wrong
 *     within a tick. Everything rounds to the minute, and the floor is
 *     "About a minute" — asserted as the absence of any seconds string, not
 *     just as the presence of the right minute figure.
 *   - **Scaled by the document.** p50 120s against a 1000-word median sample,
 *     for a 2000-word upload, is four minutes and not two. A line that
 *     ignored the size would be the same number for an NDA and an MSA.
 *   - **Past p90 it stops estimating, and does not claim a failure.** The
 *     copy is "Taking longer than usual" — the burnt-toast surface owns
 *     failure, and a review one second past p90 is running normally.
 *   - **Two announcements, not one per tick.** The polite region is
 *     interrupted exactly twice across ten minutes of ticks: when the
 *     estimate appears, and when it is superseded.
 *   - **No sample, no sentence.** Nulls from the route render nothing at all.
 *
 * `vi.useFakeTimers` drives the clock, because unlike the stage indicator
 * (review-progress-stages.test.tsx, which deliberately uses none) this line
 * IS time-driven — that is the thing under test.
 *
 * ## Two clocks, and which test gets which (issue #92)
 *
 * Most of the tests below arrange with `shouldAdvanceTime: true`, because the
 * arrange phase leans on `waitFor`, and `waitFor` under Vitest polls the
 * global timers — with a frozen clock it can never settle. That flag lets
 * REAL elapsed time tick the fake clock on top of every explicit advance,
 * which is harmless for an assertion that is nowhere near a threshold.
 *
 * It is NOT harmless for the ten-minute tick loop, which sits exactly on one:
 * the scaled p90 is 600 s and `timeRemaining` crosses at `> p90`, so sixty
 * 10 s advances land one millisecond short and the crossing was only ever
 * delivered by ~1.7 s of accumulated real-time bleed. Sinon's
 * `shouldAdvanceTime` bleed is a real 20 ms interval ticking the fake clock a
 * fixed 20 ms per firing, and a loaded CI runner coalesces the missed
 * firings — so the bleed shrinks below the second the assertion needed and
 * the `over` phase never arrives. That test therefore runs on a FROZEN clock
 * and arranges without `waitFor` (`startReviewOnAFrozenClock` below), so the
 * only time that passes is time the test asked for.
 *
 * Fully offline: aws-amplify/auth is mocked and fetch is stubbed per test.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { DEFAULT_PLAYBOOKS, pressSubmit } from './support/consoleSurface';
import {
  TAKING_LONGER_COPY,
  aboutMinutes,
  timeRemaining,
  type DurationEstimate,
} from '../durationEstimate';

vi.mock('aws-amplify/auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const REVIEW_ID = 'rev-time-remaining';

/** The wire shape of `GET /api/review-duration-estimate`. */
interface EstimateBody {
  p50_seconds: number | null;
  p90_seconds: number | null;
  median_words: number | null;
  sample_size: number;
}

/** The issue's own fixture: two minutes at a thousand words, p90 at five. */
const MEASURED: EstimateBody = {
  p50_seconds: 120,
  p90_seconds: 300,
  median_words: 1000,
  sample_size: 12,
};

/** The document every DOM test below uploads — twice the sample's median. */
const DOC_WORDS = 2000;

/**
 * `MEASURED`'s p90 scaled to that document: 300 s against a 1000-word median
 * sample is 600 s for a 2000-word upload. DERIVED from the fixture rather
 * than written out as ten minutes, so the tick loop and the estimate it is
 * chasing cannot drift apart if the fixture is ever retuned. (The scaling
 * rule itself is pinned on the pure function in the first describe block;
 * this is only the same arithmetic, restated where the loop can use it.)
 */
const SCALED_P90_MS =
  ((MEASURED.p90_seconds ?? 0) * DOC_WORDS * 1000) / (MEASURED.median_words ?? DOC_WORDS);

/** One step of the tick loop — the cadence the panel's elapsed clock ticks at. */
const TICK_STEP_MS = 10_000;

/** What the route answers before a deployment has five finished reviews. */
const TOO_THIN: EstimateBody = {
  p50_seconds: null,
  p90_seconds: null,
  median_words: null,
  sample_size: 3,
};

function preflightBody(wordCount: number): Record<string, unknown> {
  return {
    word_count: wordCount,
    page_estimate: Math.max(1, Math.round(wordCount / 400)),
    paragraph_count: 12,
    title: null,
    classification: 'unavailable',
    agreement_type_guess: null,
    paper_side: 'unclear',
    confidence: null,
    one_line_summary: null,
    match: null,
    injection_scan: null,
    party_recognised: null,
  };
}

/**
 * The panel's whole world: a catalog, a preflight, a duration estimate, a
 * submit, and a review that stays RUNNING for as long as the test cares to
 * advance the clock.
 *
 * `estimate: null` serves a 404 on the estimate route — the older-backend and
 * network-failure case, which must be indistinguishable from "no line".
 */
function stubPanelFetch(options: {
  estimate: EstimateBody | null;
  words: number;
}): { estimateCalls: () => string[] } {
  const estimateCalls: string[] = [];
  vi.stubGlobal(
    'fetch',
    // eslint-disable-next-line @typescript-eslint/require-await
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // eslint-disable-next-line @typescript-eslint/no-base-to-string
      const url = typeof input === 'string' ? input : input.toString();
      const parsed = new URL(url, 'http://localhost');
      const method = (init?.method ?? 'GET').toUpperCase();
      if (parsed.pathname === '/api/playbooks') {
        // eslint-disable-next-line @typescript-eslint/require-await
        return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
      }
      if (parsed.pathname === '/api/review-duration-estimate') {
        estimateCalls.push(parsed.search);
        if (!options.estimate) {
          // eslint-disable-next-line @typescript-eslint/require-await
          return { ok: false, status: 404, json: async () => ({}) } as Response;
        }
        // eslint-disable-next-line @typescript-eslint/require-await
        return { ok: true, status: 200, json: async () => options.estimate } as Response;
      }
      if (method === 'POST' && parsed.pathname === '/api/reviews/preflight') {
        return {
          ok: true,
          status: 200,
          // eslint-disable-next-line @typescript-eslint/require-await
          json: async () => preflightBody(options.words),
        } as Response;
      }
      if (method === 'POST' && parsed.pathname === '/api/reviews') {
        return {
          ok: true,
          status: 200,
          // eslint-disable-next-line @typescript-eslint/require-await
          json: async () => ({ review_id: REVIEW_ID, resumed: false }),
        } as Response;
      }
      if (parsed.pathname === `/api/reviews/${REVIEW_ID}`) {
        return {
          ok: true,
          status: 200,
          // eslint-disable-next-line @typescript-eslint/require-await
          json: async () => ({
            review_id: REVIEW_ID,
            status: 'RUNNING',
            decision: null,
            message: null,
            has_output: false,
            progress_stage: 'primary_pass',
          }),
        } as Response;
      }
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }),
  );
  return { estimateCalls: () => estimateCalls };
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

/** Choose a file, wait for the preflight to land, then pull the lever. */
async function startReview(): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  await pressSubmit();
  await screen.findByTestId('review-status');
}

/**
 * The same arrange, on a clock that moves only when this test says so
 * (issue #92).
 *
 * `startReview` above cannot be reused here: it waits with `waitFor` and
 * `findBy*`, which poll the global timers, and a frozen clock never delivers
 * that poll. Everything the arrange needs — the playbook catalog, the
 * preflight, the duration estimate — resolves on microtasks and zero-delay
 * timers, and no repeating timer exists until the review is in flight, so
 * draining them terminates. This is the pattern `review-submit-stall.test.tsx`
 * already uses for the same reason.
 *
 * `act` wraps each drain so React has flushed every scheduled render before
 * the DOM is read: outside `act`, a state update from a timer callback is
 * flushed by React's own scheduler on a macrotask this test does not control,
 * which is the second thing that made the tick loop sample stale text.
 */
async function startReviewOnAFrozenClock(): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  await act(async () => {
    await vi.runAllTimersAsync();
  });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  // The submit POST and the first poll are promise chains, not delays: let
  // them settle without moving the clock, so the elapsed counter this test is
  // about starts from zero.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

function lineText(): string | null {
  return screen.queryByTestId('review-time-remaining')?.textContent ?? null;
}

function announcement(): string {
  return screen.getByTestId('review-ready-announcement').textContent ?? '';
}

/**
 * The arithmetic, pinned exactly. The DOM tests below drive the same
 * functions through a real render, but a one-second threshold asserted
 * through a clock that `shouldAdvanceTime` also moves in real time is a
 * flake; the crossing itself belongs here, where it is deterministic.
 */
describe('the copy rules, exactly (#71)', () => {
  const measured: DurationEstimate = {
    p50Seconds: 120,
    p90Seconds: 300,
    medianWords: 1000,
    sampleSize: 12,
  };

  it('crosses into "taking longer" one second past the SCALED p90, not the raw one', () => {
    // 300s at the sample's median; 600s for a document twice that size.
    expect(timeRemaining(measured, 2000, 600)?.label).toBe('About 4 minutes');
    expect(timeRemaining(measured, 2000, 601)?.label).toBe(TAKING_LONGER_COPY);
    // An unscaled threshold would have flipped at 301s. It must not.
    expect(timeRemaining(measured, 2000, 301)?.phase).toBe('estimate');
  });

  it('rounds to the minute and never says "0 minutes" or a seconds figure', () => {
    expect(aboutMinutes(0)).toBe('About a minute');
    expect(aboutMinutes(45)).toBe('About a minute');
    expect(aboutMinutes(89)).toBe('About a minute');
    expect(aboutMinutes(90)).toBe('About 2 minutes');
    expect(aboutMinutes(3600)).toBe('About 60 minutes');
    for (const seconds of [0, 1, 45, 89, 90, 200, 3600]) {
      expect(aboutMinutes(seconds)).not.toMatch(/second/i);
    }
  });

  it('says nothing at all when the route could not answer', () => {
    expect(timeRemaining(null, 2000, 10)).toBeNull();
    expect(
      timeRemaining(
        { p50Seconds: null, p90Seconds: null, medianWords: null, sampleSize: 3 },
        2000,
        10,
      ),
    ).toBeNull();
  });

  it('keeps estimating when only the p90 is missing', () => {
    const noP90: DurationEstimate = { ...measured, p90Seconds: null };
    expect(timeRemaining(noP90, 2000, 99_999)?.label).toBe('About 4 minutes');
  });
});

describe('the progress bar says how long this is going to take (#71)', () => {
  it('scales p50 by the preflight word count and renders whole minutes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubPanelFetch({ estimate: MEASURED, words: 2000 });

    await startReview();

    // 120s at the sample's 1000-word median, for a 2000-word upload.
    await waitFor(() => expect(lineText()).toBe('About 4 minutes'));
  });

  it('never renders a seconds figure — a sub-minute estimate reads "About a minute"', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // 20s scaled by 2000/1000 is 40 seconds. The honest floor, not "About 0
    // minutes" and not "40 seconds".
    stubPanelFetch({
      estimate: { ...MEASURED, p50_seconds: 20 },
      words: 2000,
    });

    await startReview();

    await waitFor(() => expect(lineText()).toBe('About a minute'));
    const bar = screen.getByTestId('toaster-state-progress');
    expect(bar.textContent ?? '').not.toMatch(/second/i);
    expect(announcement()).not.toMatch(/second/i);
  });

  it('switches to "Taking longer than usual" past the scaled p90, without claiming a failure', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubPanelFetch({ estimate: MEASURED, words: 2000 });

    await startReview();
    await waitFor(() => expect(lineText()).toBe('About 4 minutes'));

    // p90 is 300s at the median, so 600s for this document. Well inside it,
    // the estimate is still the estimate. (The exact crossing is pinned
    // deterministically on the pure function below — `shouldAdvanceTime`
    // lets real time leak into this clock, so a one-second boundary
    // asserted through the DOM would be a flake, not a contract.)
    await vi.advanceTimersByTimeAsync(120_000);
    expect(lineText()).toBe('About 4 minutes');

    await vi.advanceTimersByTimeAsync(600_000);
    await waitFor(() => expect(lineText()).toBe('Taking longer than usual'));

    // The burnt-toast copy owns failure. This line must not borrow it.
    const bar = screen.getByTestId('toaster-state-progress');
    expect(bar.textContent ?? '').not.toMatch(/fail|error|wrong|problem|stuck/i);
  });

  it('announces exactly twice across ten minutes of ticks', async () => {
    // FROZEN, not `shouldAdvanceTime` — issue #92, and the file docstring for
    // why this one test differs from its neighbours.
    vi.useFakeTimers();
    stubPanelFetch({ estimate: MEASURED, words: DOC_WORDS });

    await startReviewOnAFrozenClock();

    // No `waitFor`: on a frozen clock the arrange above has already settled,
    // so a retry loop here would only hide a render that never happened.
    expect(lineText()).toBe('About 4 minutes');

    // Every distinct thing the ONE polite region has held. A per-tick
    // announcement would pile up dozens of entries here.
    const spoken: string[] = [];
    const record = () => {
      const current = announcement().trim();
      if (current && current !== spoken[spoken.length - 1]) {
        spoken.push(current);
      }
    };
    record();

    const clockAtFirstTick = Date.now();
    let advanced = 0;
    // One step PAST the scaled p90, deliberately. `timeRemaining` crosses at
    // `elapsedSeconds > p90`, so a loop that stops AT 600 s is still in the
    // `estimate` phase by one millisecond — which is exactly why the old
    // version of this test needed a second of real-time bleed to go green,
    // and why it went red on a runner that did not supply one.
    while (advanced <= SCALED_P90_MS) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(TICK_STEP_MS);
      });
      advanced += TICK_STEP_MS;
      record();
    }

    // The clock moved by exactly what was asked for and not one tick more:
    // the assertion below is now a property of this test's own advances,
    // not of how loaded the machine was.
    expect(Date.now() - clockAtFirstTick).toBe(advanced);
    expect(advanced).toBe(SCALED_P90_MS + TICK_STEP_MS);
    // Sixty-one samples, two announcements.
    expect(spoken).toEqual(['About 4 minutes remaining.', 'Taking longer than usual.']);
    expect(lineText()).toBe(TAKING_LONGER_COPY);
  });

  it('renders no estimate at all when the sample is too thin to answer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubPanelFetch({ estimate: TOO_THIN, words: 2000 });

    await startReview();
    await screen.findByTestId('toaster-state-progress');

    await vi.advanceTimersByTimeAsync(30_000);
    expect(lineText()).toBeNull();
    expect(announcement().trim()).toBe('');
  });

  it('renders no estimate when the route is unavailable', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubPanelFetch({ estimate: null, words: 2000 });

    await startReview();
    await screen.findByTestId('toaster-state-progress');

    await vi.advanceTimersByTimeAsync(30_000);
    expect(lineText()).toBeNull();
  });

  it('falls back to the unscaled median when the sample carries no word counts', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // Rows written before the word-count stamp existed: the durations are
    // still real measurements, so the line answers — it just cannot scale.
    stubPanelFetch({
      estimate: { ...MEASURED, median_words: null },
      words: 2000,
    });

    await startReview();

    await waitFor(() => expect(lineText()).toBe('About 2 minutes'));
  });

  it('asks the route for the selected playbook and the preflight word count', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { estimateCalls } = stubPanelFetch({ estimate: MEASURED, words: 2000 });

    await startReview();
    await waitFor(() => expect(lineText()).toBe('About 4 minutes'));

    const asked = estimateCalls();
    expect(asked.some((search) => search.includes('playbook_id=nda'))).toBe(true);
    expect(asked.some((search) => search.includes('words=2000'))).toBe(true);
  });

  it('shows nothing before a review is in flight', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubPanelFetch({ estimate: MEASURED, words: 2000 });

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await screen.findByTestId('review-submit-button');

    await vi.advanceTimersByTimeAsync(5_000);
    // The estimate has landed, but nothing is toasting: an idle panel must
    // not claim a review is four minutes from finishing.
    expect(lineText()).toBeNull();
  });
});
