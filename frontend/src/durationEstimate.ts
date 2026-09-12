/**
 * durationEstimate.ts — client for `GET /api/review-duration-estimate`
 * (issue #71, 2026-09-05 diagnostic finding G12) and the whole of the copy
 * that turns its four numbers into one line on the progress bar.
 *
 * ## The question this answers
 *
 * The progress bar reports STAGE. "Step 2 of 4, adversarial critic" is true
 * and tells a reviewer nothing about whether to wait or come back after
 * lunch. This module answers the other question, from measurement only:
 * `p50_seconds` / `p90_seconds` over the last 50 finished reviews of the
 * selected playbook, plus the `median_words` of that same sample so the
 * figures can be scaled to the document actually in the toaster.
 *
 * ## Three rules the copy obeys
 *
 *  1. **Never seconds.** "About 3 minutes" is a claim a reviewer can plan
 *     around; "2 minutes 47 seconds" is a countdown that will be wrong, and
 *     visibly wrong, within a tick. Everything rounds to the minute and the
 *     floor is `About a minute` — there is no shorter honest answer.
 *  2. **Past p90 the estimate stops estimating.** It does not keep counting
 *     down into negative territory and it does not pick a new number; it
 *     says `Taking longer than usual` and stops. That wording is load-bearing
 *     in the other direction too: it must NOT read as a failure. The burnt-
 *     toast copy owns failure, and a review at p90+1 second is still running
 *     perfectly normally.
 *  3. **No sample, no sentence.** The route answers `null` below its minimum
 *     sample, and a `null` renders nothing at all. A made-up "about 5
 *     minutes" on a fresh deployment is worse than the silence it replaced.
 *
 * Like `preflight.ts`, `fetchDurationEstimate` NEVER throws and never
 * surfaces a technical error: a network failure, a non-2xx, or a malformed
 * body all resolve to `null`. This is a courtesy line on a progress bar —
 * nothing here may ever be the reason a review is delayed or a banner
 * appears.
 */
import { authorizedFetch } from './api';

export interface DurationEstimate {
  /** Median finished duration, in seconds. `null` below the route's minimum sample. */
  p50Seconds: number | null;
  /** Nearest-rank 90th percentile of the same sample. `null` on the same terms. */
  p90Seconds: number | null;
  /** Median word count of the SAME sampled reviews — the scaling denominator. */
  medianWords: number | null;
  /** How many rows the figures came from. Reported even when it is too small. */
  sampleSize: number;
}

/**
 * The copy for a review that has outrun its own p90. Deliberately not
 * "Something went wrong" or "This is taking too long": nothing has failed,
 * and the burnt-toast surface is the only thing allowed to say otherwise.
 */
export const TAKING_LONGER_COPY = 'Taking longer than usual';

/** Which half of the estimate is on screen — the input to "announce twice". */
export type TimeRemainingPhase = 'estimate' | 'over';

export interface TimeRemaining {
  phase: TimeRemainingPhase;
  /** The visible line on the progress bar. */
  label: string;
  /** The same fact as a sentence, for the polite live region. */
  announcement: string;
}

/**
 * `seconds` scaled for a document of `words`, against a sample whose median
 * document was `medianWords` long.
 *
 * Returns `seconds` UNSCALED when there is no usable denominator — no
 * preflight ran, or the sample carries no word counts (rows written before
 * that stamp existed). The deployment's raw median is still a real
 * measurement and still better than nothing; what would not be honest is
 * multiplying by a zero denominator and reporting "About a minute" for a
 * sixty-page agreement.
 */
export function scaleSeconds(
  seconds: number | null,
  words: number,
  medianWords: number | null,
): number | null {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) {
    return null;
  }
  if (!medianWords || medianWords <= 0 || !words || words <= 0) {
    return seconds;
  }
  return (seconds * words) / medianWords;
}

/**
 * `seconds` as minutes, in words. Never renders a seconds figure, and never
 * renders "About 0 minutes" — under 90 seconds the honest floor is
 * `About a minute`.
 */
export function aboutMinutes(seconds: number): string {
  const minutes = Math.round(seconds / 60);
  return minutes <= 1 ? 'About a minute' : `About ${minutes} minutes`;
}

/**
 * The line to render, or `null` for "say nothing".
 *
 * `null` whenever the route could not answer (too small a sample), which is
 * the case the `p50Seconds === null` check below covers. `elapsedSeconds` is
 * how long THIS review has been running; past the scaled p90 the estimate is
 * replaced rather than recomputed.
 *
 * p90 is scaled by the same factor p50 is. A document twice the size of the
 * sample's median legitimately takes about twice as long, so the threshold
 * for "longer than usual" has to move with it — an unscaled p90 would call
 * every large document unusual at the same wall-clock moment.
 */
export function timeRemaining(
  estimate: DurationEstimate | null,
  words: number,
  elapsedSeconds: number,
): TimeRemaining | null {
  if (!estimate) {
    return null;
  }
  const p50 = scaleSeconds(estimate.p50Seconds, words, estimate.medianWords);
  if (p50 === null) {
    return null;
  }
  const p90 = scaleSeconds(estimate.p90Seconds, words, estimate.medianWords);
  if (p90 !== null && elapsedSeconds > p90) {
    return { phase: 'over', label: TAKING_LONGER_COPY, announcement: `${TAKING_LONGER_COPY}.` };
  }
  const label = aboutMinutes(p50);
  return { phase: 'estimate', label, announcement: `${label} remaining.` };
}

/**
 * A response field as a non-negative finite number, or `null`.
 *
 * `null` is the route's OWN answer for "not enough sample to say", so it has
 * to survive as null rather than becoming a zero; and an older backend
 * answering this path with a 404 body would otherwise coerce `undefined`
 * into a rendered NaN.
 */
function parseMeasurement(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function parseDurationResponse(body: Record<string, unknown>): DurationEstimate {
  return {
    p50Seconds: parseMeasurement(body.p50_seconds),
    p90Seconds: parseMeasurement(body.p90_seconds),
    medianWords: parseMeasurement(body.median_words),
    sampleSize: typeof body.sample_size === 'number' ? body.sample_size : 0,
  };
}

/**
 * GET the measured duration profile for `playbookId`, or `null` on ANY
 * failure — see the module docstring for why this never throws.
 *
 * `words` is sent because it is part of the route's declared contract (the
 * caller's own preflight word count); today's response does not depend on
 * it, which is exactly why the answer is the same for every reviewer of a
 * playbook and the scaling happens here.
 */
export async function fetchDurationEstimate(
  playbookId: string,
  words: number,
): Promise<DurationEstimate | null> {
  if (!playbookId) {
    return null;
  }
  try {
    const query = new URLSearchParams({ playbook_id: playbookId });
    if (words > 0) {
      query.set('words', String(Math.round(words)));
    }
    const response = await authorizedFetch(`/api/review-duration-estimate?${query.toString()}`);
    if (!response.ok) {
      return null;
    }
    const body = (await response.json().catch(() => null)) as Record<string, unknown> | null;
    return body ? parseDurationResponse(body) : null;
  } catch {
    /* a progress-bar courtesy; the review runs identically without it */
    return null;
  }
}
