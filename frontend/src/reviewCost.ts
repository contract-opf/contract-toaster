/**
 * reviewCost.ts — client for `GET /api/review-cost-estimate` and the copy that
 * turns a review's cost fields into the lines the Review tab and History show.
 *
 * ## Two different facts
 *
 *  1. The ESTIMATE, before a review runs. The route follows the playbook and the
 *     document (`playbook_id`, `words` — the preflight word count, the same
 *     inputs the duration estimate takes) and answers an expected figure with a
 *     low/high range and how it was derived (`estimate_basis`). It is open to
 *     every signed-in user and carries no cap, no spend and no model id.
 *  2. The ACTUAL, after it ran. `actual_usd_cents` is what the review settled
 *     at, read off the owner's own detail and History rows. A row from before
 *     the field existed has none and shows NOTHING — unknown is not $0.00. A
 *     failed review that made model calls has a real cost and says it failed;
 *     one that made none records 0.
 *
 * Like `durationEstimate.ts`, `fetchCostEstimate` NEVER throws: a network
 * failure, a non-2xx or a malformed body all resolve to `null`, because a price
 * line is a courtesy and nothing here may delay a review or raise a banner.
 */
import { authorizedFetch } from './api';
import { formatCostUsdCents } from './coverNote';

/** How the figure was derived. Closed vocabulary the route returns. */
export type CostEstimateBasis = 'policy_default' | 'document' | 'measured';

export interface CostEstimate {
  /** The expected cost, in cents. Always positive when an estimate exists. */
  cents: number;
  /** The low end of the range, or null when the route gave none. */
  lowCents: number | null;
  /** The high end of the range, or null when the route gave none. */
  highCents: number | null;
  /** How it was derived; null from a backend older than the field. */
  basis: CostEstimateBasis | null;
  /** How many finished reviews a measured range came from (0 otherwise). */
  sampleSize: number;
}

function positiveCents(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
}

function isBasis(value: unknown): value is CostEstimateBasis {
  return value === 'policy_default' || value === 'document' || value === 'measured';
}

/**
 * The route's body as an estimate, or null when it carries no usable figure.
 *
 * Strict on purpose. A backend older than this bundle answers with a 404 body,
 * so `Number(undefined)` would paint "$NaN"; and the figure is legitimately
 * `null` on a provider with no per-review basis (the Bedrock path), where
 * "$0.00" would claim a review is free. Anything that is not a positive number
 * of cents is no estimate. A range is kept only when it brackets the figure —
 * a skewed one is dropped rather than shown inverted.
 */
export function parseCostEstimate(body: unknown): CostEstimate | null {
  if (typeof body !== 'object' || body === null) {
    return null;
  }
  const record = body as Record<string, unknown>;
  const cents = positiveCents(record.estimated_usd_cents);
  if (cents === null) {
    return null;
  }
  const low = positiveCents(record.estimated_low_usd_cents);
  const high = positiveCents(record.estimated_high_usd_cents);
  const bracketed = low !== null && high !== null && low <= cents && cents <= high;
  return {
    cents,
    lowCents: bracketed ? low : null,
    highCents: bracketed ? high : null,
    basis: isBasis(record.estimate_basis) ? record.estimate_basis : null,
    sampleSize:
      typeof record.sample_size === 'number' && Number.isFinite(record.sample_size)
        ? record.sample_size
        : 0,
  };
}

/**
 * GET the cost estimate, or `null` on ANY failure. `playbookId` and `words` are
 * both optional: with either missing the route answers the document-blind
 * policy figure, which is still a better line than none.
 */
export async function fetchCostEstimate(
  playbookId: string,
  words: number,
): Promise<CostEstimate | null> {
  try {
    const query = new URLSearchParams();
    if (playbookId && words > 0) {
      query.set('playbook_id', playbookId);
      query.set('words', String(Math.round(words)));
    }
    const suffix = query.toString();
    const response = await authorizedFetch(
      suffix ? `/api/review-cost-estimate?${suffix}` : '/api/review-cost-estimate',
    );
    if (!response.ok) {
      return null;
    }
    const body: unknown = await response.json().catch(() => null);
    return parseCostEstimate(body);
  } catch {
    /* the price is a courtesy; the button works without it */
    return null;
  }
}

/**
 * "Range $0.60–$1.10", or null when there is no range to show. Never worded as
 * a ceiling: the estimate is an expectation, and ARCHITECTURE.md -> Cost shape
 * is explicit that settlement can land on either side of any figure.
 */
export function describeEstimateRange(estimate: CostEstimate | null | undefined): string | null {
  if (!estimate || estimate.lowCents === null || estimate.highCents === null) {
    return null;
  }
  return `Range ${formatCostUsdCents(estimate.lowCents)}–${formatCostUsdCents(estimate.highCents)}`;
}

/** The cost fields a review row or detail may carry. All optional, all unknown
 *  until proven numbers: a row from before them carries none. */
export interface ReviewCostFields {
  estimated_usd_cents?: unknown;
  actual_usd_cents?: unknown;
  actual_cost_failed?: unknown;
}

/** A settled actual in cents, or null when the row recorded none. A genuine 0
 *  (a failed review that made no model call) is a number and survives. */
export function actualCents(fields: ReviewCostFields | null | undefined): number | null {
  const value = fields?.actual_usd_cents;
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

/**
 * The History line for one review's cost, or null for nothing.
 *
 *   estimate and actual  "estimated $0.80 / actual $0.95"
 *   actual only          "actual $0.95"            (a row without an estimate)
 *   neither, or only an estimate
 *                        null — a review still running, or one from before the
 *                        fields existed, has no cost to report
 *
 * A review that failed and still spent says so: the cost of a failed attempt is
 * real, and the line is where a reviewer finds out what it was.
 */
export function describeRowCost(fields: ReviewCostFields | null | undefined): string | null {
  const actual = actualCents(fields);
  if (actual === null) {
    return null;
  }
  const estimate = positiveCents(fields?.estimated_usd_cents);
  const parts =
    estimate === null
      ? `actual ${formatCostUsdCents(actual)}`
      : `estimated ${formatCostUsdCents(estimate)} / actual ${formatCostUsdCents(actual)}`;
  return fields?.actual_cost_failed === true ? `${parts} (failed review)` : parts;
}
