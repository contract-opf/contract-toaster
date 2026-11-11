/**
 * review-cost-actual.test.tsx — the input-aware estimate and the actual cost a
 * review settled at, as the Review tab and History show them.
 *
 * Two facts, kept apart:
 *
 *   1. The ESTIMATE before a run: an expected figure with a low/high range that
 *      follows the playbook and the document (`GET /api/review-cost-estimate`
 *      with `playbook_id` and `words`). Worded as an expectation, never a
 *      ceiling; a range is shown only when it brackets the figure on screen.
 *   2. The ACTUAL after it: `actual_usd_cents` off the owner's own detail and
 *      History rows. A row that carries none shows NOTHING — unknown is not
 *      $0.00 — a failed review that still spent says so, and a failed review
 *      that made no model call is a real `$0.00`.
 *
 * Pure-function blocks first (`reviewCost.ts`, the projection, the console's
 * copy), then the rendered History row, then the whole path through the real
 * panel. Asserts on text and test ids only — `vitest.config.ts` runs jsdom with
 * `css: false`. Fully offline: Amplify auth is mocked and fetch is stubbed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import ReviewHistory, { type HistoryRow } from '../ReviewHistory';
import { OrbitDiner } from '../orbit-diner/OrbitDiner';
import { costNote, costRangeText } from '../orbit-diner/state';
import { toReviewModel, type ReviewProjectionState } from '../orbit-diner/projection';
import type { Cost, Playbook, ReviewModel } from '../orbit-diner/types';
import {
  actualCents,
  describeEstimateRange,
  describeRowCost,
  fetchCostEstimate,
  parseCostEstimate,
} from '../reviewCost';
import { DEFAULT_PLAYBOOKS, pressSubmit } from './support/consoleSurface';

vi.mock('aws-amplify/auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const PLAYBOOKS: Playbook[] = [{ playbook_id: 'nda', display_name: 'NDA', status: 'active' }];

/** What the route answers for a 60-page agreement against a 4-clause playbook. */
const DOCUMENT_ESTIMATE = {
  estimated_usd_cents: 80,
  estimated_low_usd_cents: 60,
  estimated_high_usd_cents: 110,
  estimate_basis: 'document',
  sample_size: 0,
  worst_case_reservation_usd_cents: 1106,
};

// ---------------------------------------------------------------------------
// reviewCost.ts
// ---------------------------------------------------------------------------

describe('parseCostEstimate', () => {
  it('reads the figure, its range, the basis and the sample', () => {
    expect(parseCostEstimate({ ...DOCUMENT_ESTIMATE, estimate_basis: 'measured', sample_size: 12 }))
      .toEqual({ cents: 80, lowCents: 60, highCents: 110, basis: 'measured', sampleSize: 12 });
  });

  it('is no estimate for anything that is not a positive number of cents', () => {
    for (const bad of [null, 0, -1, 'lots', undefined, Number.NaN]) {
      expect(parseCostEstimate({ estimated_usd_cents: bad }), String(bad)).toBeNull();
    }
    expect(parseCostEstimate(null)).toBeNull();
    expect(parseCostEstimate('x')).toBeNull();
  });

  it('keeps the figure from a backend older than the range fields, with no range', () => {
    expect(parseCostEstimate({ estimated_usd_cents: 98 })).toEqual({
      cents: 98,
      lowCents: null,
      highCents: null,
      basis: null,
      sampleSize: 0,
    });
  });

  it('drops a range that does not bracket the figure rather than showing it inverted', () => {
    for (const [low, high] of [
      [120, 140],
      [10, 50],
      [110, 60],
      [null, 110],
    ]) {
      const parsed = parseCostEstimate({
        ...DOCUMENT_ESTIMATE,
        estimated_low_usd_cents: low,
        estimated_high_usd_cents: high,
      });
      expect(parsed?.cents).toBe(80);
      expect(parsed?.lowCents, `${String(low)}-${String(high)}`).toBeNull();
      expect(parsed?.highCents).toBeNull();
    }
  });

  it('ignores a basis outside the closed vocabulary', () => {
    expect(parseCostEstimate({ ...DOCUMENT_ESTIMATE, estimate_basis: 'anthropic/claude' })?.basis)
      .toBeNull();
  });
});

describe('describeEstimateRange', () => {
  it('words the range as an expectation, never a ceiling', () => {
    const text = describeEstimateRange(parseCostEstimate(DOCUMENT_ESTIMATE));
    expect(text).toBe('Range $0.60–$1.10');
    expect((text ?? '').toLowerCase()).not.toMatch(/up to|maximum|at most/);
  });

  it('is null with no range', () => {
    expect(describeEstimateRange(null)).toBeNull();
    expect(describeEstimateRange(parseCostEstimate({ estimated_usd_cents: 98 }))).toBeNull();
  });
});

describe('describeRowCost', () => {
  it('shows estimated against actual when both were recorded', () => {
    expect(describeRowCost({ estimated_usd_cents: 80, actual_usd_cents: 95 })).toBe(
      'estimated $0.80 / actual $0.95',
    );
  });

  it('shows just the actual for a row with no estimate', () => {
    expect(describeRowCost({ actual_usd_cents: 95 })).toBe('actual $0.95');
    expect(describeRowCost({ estimated_usd_cents: null, actual_usd_cents: 95 })).toBe(
      'actual $0.95',
    );
  });

  it('shows nothing for a row with no recorded cost — unknown is not free', () => {
    expect(describeRowCost({})).toBeNull();
    expect(describeRowCost(null)).toBeNull();
    expect(describeRowCost(undefined)).toBeNull();
    // An estimate alone is a review still running, or one that never settled.
    expect(describeRowCost({ estimated_usd_cents: 80 })).toBeNull();
    for (const bad of [null, 'free', -1, Number.NaN]) {
      expect(describeRowCost({ actual_usd_cents: bad }), String(bad)).toBeNull();
    }
  });

  it('shows a real zero: a failed review that made no model call cost nothing', () => {
    expect(describeRowCost({ actual_usd_cents: 0, actual_cost_failed: true })).toBe(
      'actual $0.00 (failed review)',
    );
    expect(actualCents({ actual_usd_cents: 0 })).toBe(0);
  });

  it('says a failed review that still spent failed', () => {
    expect(
      describeRowCost({
        estimated_usd_cents: 80,
        actual_usd_cents: 55,
        actual_cost_failed: true,
      }),
    ).toBe('estimated $0.80 / actual $0.55 (failed review)');
    expect(describeRowCost({ actual_usd_cents: 55, actual_cost_failed: false })).toBe(
      'actual $0.55',
    );
  });
});

describe('fetchCostEstimate', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stub(body: unknown, ok = true): ReturnType<typeof vi.fn> {
    // eslint-disable-next-line @typescript-eslint/require-await
    const impl = vi.fn(async () => ({ ok, status: ok ? 200 : 500, json: async () => body }));
    vi.stubGlobal('fetch', impl);
    return impl;
  }

  function requested(impl: ReturnType<typeof vi.fn>): string {
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    return String(impl.mock.calls[0]![0]);
  }

  it('sends the playbook and the document size so the figure follows them', async () => {
    const impl = stub(DOCUMENT_ESTIMATE);
    const estimate = await fetchCostEstimate('nda', 12_345.4);
    const url = new URL(requested(impl), 'http://localhost');
    expect(url.pathname).toBe('/api/review-cost-estimate');
    expect(url.searchParams.get('playbook_id')).toBe('nda');
    expect(url.searchParams.get('words')).toBe('12345');
    expect(estimate?.cents).toBe(80);
  });

  it('asks the document-blind question when there is no playbook or no word count', async () => {
    for (const [playbook, words] of [
      ['', 500],
      ['nda', 0],
    ] as const) {
      const impl = stub({ estimated_usd_cents: 98 });
      await fetchCostEstimate(playbook, words);
      expect(new URL(requested(impl), 'http://localhost').search).toBe('');
    }
  });

  it('never throws: a failed read, a non-2xx and a malformed body are all null', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('offline'))));
    expect(await fetchCostEstimate('nda', 100)).toBeNull();
    stub({}, false);
    expect(await fetchCostEstimate('nda', 100)).toBeNull();
    stub('<html>');
    expect(await fetchCostEstimate('nda', 100)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// The projection and the console's copy
// ---------------------------------------------------------------------------

const baseState = (over: Partial<ReviewProjectionState> = {}): ReviewProjectionState => ({
  file: null,
  submitting: false,
  reviewId: null,
  detail: null,
  playbookId: 'nda',
  browning: 'medium',
  notesMode: 'external',
  toasterGuidance: '',
  appliedGuidance: null,
  dispositionNote: '',
  playbooks: PLAYBOOKS,
  notesModeInternalAvailable: false,
  muted: false,
  notificationsSupported: true,
  ...over,
});

describe('the projected cost', () => {
  it('claims the document basis and carries the range for a document-aware estimate', () => {
    expect(
      toReviewModel(
        baseState({
          reviewCostUsdCents: 80,
          reviewCostRange: { lowCents: 60, highCents: 110, basis: 'document' },
        }),
      ).cost,
    ).toEqual({
      kind: 'estimated',
      cents: 80,
      basis: 'document-model',
      lowCents: 60,
      highCents: 110,
    });
  });

  it('stays the typical figure with no range for the document-blind estimate', () => {
    expect(toReviewModel(baseState({ reviewCostUsdCents: 98 })).cost).toEqual({
      kind: 'estimated',
      cents: 98,
      basis: 'typical',
    });
    expect(
      toReviewModel(
        baseState({
          reviewCostUsdCents: 98,
          reviewCostRange: { lowCents: 49, highCents: 196, basis: 'policy_default' },
        }),
      ).cost.basis,
    ).toBe('typical');
  });

  it('never shows a range that does not bracket the figure on screen', () => {
    const cost = toReviewModel(
      baseState({
        reviewCostUsdCents: 80,
        reviewCostRange: { lowCents: 120, highCents: 200, basis: 'measured' },
      }),
    ).cost;
    expect(cost.lowCents).toBeUndefined();
    expect(cost.highCents).toBeUndefined();
  });

  it('becomes a complete settlement once the server settled the review, beside its estimate', () => {
    const cost = toReviewModel(
      baseState({
        reviewCostUsdCents: 80,
        capturedReviewCostUsdCents: 80,
        actualReviewCostUsdCents: 95,
      }),
    ).cost;
    expect(cost).toEqual({
      kind: 'settled',
      cents: 95,
      settlementComplete: true,
      failed: false,
      estimatedCents: 80,
    });
  });

  it('settles with no estimate to compare when none was captured', () => {
    expect(
      toReviewModel(
        baseState({ capturedReviewCostUsdCents: null, actualReviewCostUsdCents: 95 }),
      ).cost,
    ).toEqual({ kind: 'settled', cents: 95, settlementComplete: true, failed: false });
  });

  it('carries a failed review’s cost, including a real zero', () => {
    for (const cents of [55, 0]) {
      const cost = toReviewModel(
        baseState({ actualReviewCostUsdCents: cents, actualReviewCostFailed: true }),
      ).cost;
      expect(cost.kind).toBe('settled');
      expect(cost.cents).toBe(cents);
      expect(cost.failed).toBe(true);
    }
  });

  it('keeps an unknown cost unknown: no actual means no settlement, never $0.00', () => {
    for (const actual of [undefined, null, -1, Number.NaN]) {
      const cost = toReviewModel(
        baseState({ reviewCostUsdCents: 80, actualReviewCostUsdCents: actual }),
      ).cost;
      expect(cost.kind, String(actual)).toBe('estimated');
    }
  });

  it('never folds the cover note into either figure', () => {
    const cost = toReviewModel(
      baseState({ actualReviewCostUsdCents: 95, coverNoteCostCents: 4 }),
    ).cost;
    expect(cost.cents).toBe(95);
    expect(cost.coverCents).toBe(4);
  });
});

describe('the console copy', () => {
  it('words the range, and only for an estimate that carries one', () => {
    expect(costRangeText({ kind: 'estimated', cents: 80, lowCents: 60, highCents: 110 })).toBe(
      'Range $0.60–$1.10',
    );
    expect(costRangeText({ kind: 'estimated', cents: 80 })).toBeNull();
    expect(costRangeText({ kind: 'settled', cents: 80, lowCents: 60, highCents: 110 })).toBeNull();
  });

  it('states the final cost beside the estimate, and says when the review failed', () => {
    expect(
      costNote({ kind: 'settled', cents: 95, settlementComplete: true, estimatedCents: 80 }),
    ).toBe('Final cost $0.95. It was estimated at $0.80.');
    expect(costNote({ kind: 'settled', cents: 95, settlementComplete: true })).toBe(
      'Final cost $0.95.',
    );
    expect(
      costNote({ kind: 'settled', cents: 55, settlementComplete: true, failed: true }),
    ).toBe('Final cost $0.55. This review failed; that is what the attempt still cost.');
    expect(costNote({ kind: 'settled', cents: 0, settlementComplete: true, failed: true })).toBe(
      'Final cost $0.00. This review failed; that is what the attempt still cost.',
    );
  });

  it('says nothing for an unknown cost', () => {
    expect(costNote({ kind: 'unavailable' })).toBeNull();
    expect(costNote({ kind: 'estimated', cents: 80 })).toBeNull();
    expect(costNote({ kind: 'settled', cents: 95 })).toBeNull();
  });
});

function consoleModel(patch: Partial<ReviewModel> = {}): ReviewModel {
  return {
    status: 'EMPTY',
    fileSelected: false,
    preferences: {
      playbookId: 'nda',
      intensity: 'medium',
      notesMode: 'none',
      instructions: '',
      dispositionNote: '',
    },
    playbooks: PLAYBOOKS,
    internalNotesAvailable: false,
    browningReadback: 'Medium markup.',
    browningNote: 'Medium markup.',
    guidancePrecedence: 'Your instructions govern the playbook’s positions.',
    cost: { kind: 'unavailable' },
    muted: true,
    notification: 'unsupported',
    ...patch,
  };
}

function renderConsole(cost: Cost, patch: Partial<ReviewModel> = {}): void {
  render(
    <OrbitDiner
      model={consoleModel({ cost, ...patch })}
      onFile={() => {}}
      onPreferences={() => {}}
      onAction={() => {}}
    />,
  );
}

describe('the rendered console', () => {
  it('shows the range under the estimate, claiming the document basis, before submitting', () => {
    renderConsole({
      kind: 'estimated',
      cents: 80,
      basis: 'document-model',
      lowCents: 60,
      highCents: 110,
    });
    expect(screen.getByTestId('review-cost-estimate').querySelector('output')?.textContent).toBe(
      '$0.80',
    );
    expect(screen.getByTestId('review-cost-range').textContent).toBe('Range $0.60–$1.10.');
    expect(document.body.textContent).toContain('Based on this document and the active model policy.');
    const text = (document.body.textContent ?? '').toLowerCase();
    expect(text).not.toContain('up to');
    expect(text).not.toContain('maximum');
  });

  it('shows no range for the typical figure', () => {
    renderConsole({ kind: 'estimated', cents: 98, basis: 'typical' });
    expect(screen.queryByTestId('review-cost-range')).toBeNull();
  });

  it('shows the final cost and the estimate on a finished review', () => {
    renderConsole(
      { kind: 'settled', cents: 95, settlementComplete: true, estimatedCents: 80, failed: false },
      { status: 'DONE' },
    );
    const price = screen.getByTestId('review-cost-estimate');
    expect(price.querySelector('span')?.textContent).toBe('Final review cost');
    expect(price.querySelector('output')?.textContent).toBe('$0.95');
    expect(screen.getByTestId('review-cost-note').textContent).toBe(
      'Final cost $0.95. It was estimated at $0.80.',
    );
  });

  it('says a failed review failed and still shows what it cost', () => {
    renderConsole(
      { kind: 'settled', cents: 55, settlementComplete: true, failed: true },
      { status: 'ERROR' },
    );
    expect(screen.getByTestId('review-cost-estimate').querySelector('output')?.textContent).toBe(
      '$0.55',
    );
    expect(screen.getByTestId('review-cost-note').textContent).toContain('This review failed');
  });

  it('shows a failed review that made no model call as a real $0.00', () => {
    renderConsole({ kind: 'settled', cents: 0, settlementComplete: true, failed: true }, {
      status: 'ERROR',
    });
    expect(screen.getByTestId('review-cost-estimate').querySelector('output')?.textContent).toBe(
      '$0.00',
    );
  });

  it('says nothing about cost on a finished review that has none recorded', () => {
    renderConsole({ kind: 'unavailable' }, { status: 'DONE' });
    expect(screen.queryByTestId('review-cost-note')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

function historyRow(over: Partial<HistoryRow> & { review_id: string }): HistoryRow {
  return {
    playbook_id: 'synthetic-nda-sample',
    status: 'DONE',
    decision: 'REQUEST_CHANGE',
    created_at: '1800000200',
    updated_at: '1800000300',
    has_output: true,
    has_input: true,
    ...over,
  };
}

describe('History rows', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubList(rows: HistoryRow[]): void {
    // eslint-disable-next-line @typescript-eslint/require-await
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      // eslint-disable-next-line @typescript-eslint/require-await
      json: async () => ({ reviews: rows }),
    })));
  }

  it('shows estimated against actual, just the actual, and nothing, as each row warrants', async () => {
    stubList([
      historyRow({
        review_id: 'rev-both',
        estimated_usd_cents: 80,
        actual_usd_cents: 95,
        actual_cost_failed: false,
      }),
      historyRow({ review_id: 'rev-actual-only', actual_usd_cents: 95 }),
      // A row from before the cost fields: nothing recorded, nothing shown.
      historyRow({ review_id: 'rev-legacy' }),
      // An estimate with no settlement (still running, or never settled).
      historyRow({ review_id: 'rev-quoted', status: 'RUNNING', estimated_usd_cents: 80 }),
    ]);
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-rev-both');

    expect(screen.getByTestId('history-cost-rev-both').textContent).toBe(
      'estimated $0.80 / actual $0.95',
    );
    expect(screen.getByTestId('history-cost-rev-actual-only').textContent).toBe('actual $0.95');
    expect(screen.queryByTestId('history-cost-rev-legacy')).toBeNull();
    expect(screen.queryByTestId('history-cost-rev-quoted')).toBeNull();
    // Never a fabricated zero for the row that recorded nothing.
    expect(screen.getByTestId('history-outcome-rev-legacy').textContent ?? '').not.toMatch(/\$/);
  });

  it('shows a failed review’s cost and marks it failed', async () => {
    stubList([
      historyRow({
        review_id: 'rev-failed-spent',
        status: 'ERROR',
        decision: null,
        estimated_usd_cents: 80,
        actual_usd_cents: 55,
        actual_cost_failed: true,
      }),
      historyRow({
        review_id: 'rev-failed-free',
        status: 'ERROR',
        decision: null,
        actual_usd_cents: 0,
        actual_cost_failed: true,
      }),
    ]);
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-rev-failed-spent');
    expect(screen.getByTestId('history-cost-rev-failed-spent').textContent).toBe(
      'estimated $0.80 / actual $0.55 (failed review)',
    );
    expect(screen.getByTestId('history-cost-rev-failed-free').textContent).toBe(
      'actual $0.00 (failed review)',
    );
  });

  it('paints cost as escaped text and never as markup', async () => {
    stubList([
      historyRow({
        review_id: 'rev-hostile',
        // A skewed server cannot smuggle markup through a number field.
        actual_usd_cents: '<img src=x onerror=alert(1)>' as unknown as number,
      }),
    ]);
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-rev-hostile');
    expect(screen.queryByTestId('history-cost-rev-hostile')).toBeNull();
    expect(document.querySelector('img')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// The whole path through the real panel
// ---------------------------------------------------------------------------

function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  // eslint-disable-next-line @typescript-eslint/require-await
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    // eslint-disable-next-line @typescript-eslint/no-base-to-string
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const body = routes[key];
    if (body === undefined) {
      // eslint-disable-next-line @typescript-eslint/require-await
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    // eslint-disable-next-line @typescript-eslint/require-await
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function panelRoutes(detail: Record<string, unknown>): Record<string, unknown> {
  return {
    '/api/playbooks': DEFAULT_PLAYBOOKS,
    'GET /api/me/preferences': { preferences: {}, notes_mode_available: false },
    'GET /api/review-cost-estimate': DOCUMENT_ESTIMATE,
    'POST /api/reviews': { review_id: 'rev-c1', resumed: false },
    'GET /api/reviews/rev-c1': {
      review_id: 'rev-c1',
      has_output: false,
      has_input: true,
      message: null,
      ...detail,
    },
  };
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

async function submitAndFinish(): Promise<void> {
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  await pressSubmit();
  await waitFor(() => {
    expect(document.querySelector('.od-console')?.getAttribute('data-terminal')).toBe('true');
  });
}

describe('the Review tab, end to end', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows the estimate with its range before anything is submitted', async () => {
    stubFetch(panelRoutes({ status: 'PENDING', decision: null }));
    render(<ReviewSubmission />);
    await waitFor(() =>
      expect(screen.getByTestId('review-cost-estimate').textContent).toContain('$0.80'),
    );
    await waitFor(() =>
      expect(screen.getByTestId('review-cost-range').textContent).toBe('Range $0.60–$1.10.'),
    );
  });

  it('keeps the quoted range off a backend that predates it', async () => {
    const routes = panelRoutes({ status: 'PENDING', decision: null });
    routes['GET /api/review-cost-estimate'] = { estimated_usd_cents: 98 };
    stubFetch(routes);
    render(<ReviewSubmission />);
    await waitFor(() =>
      expect(screen.getByTestId('review-cost-estimate').textContent).toContain('$0.98'),
    );
    expect(screen.queryByTestId('review-cost-range')).toBeNull();
    expect(document.body.textContent).toMatch(/typical document length/);
  });

  it('shows what a finished review actually cost, beside what it was quoted at', async () => {
    stubFetch(
      panelRoutes({
        status: 'DONE',
        decision: 'ACCEPT',
        actual_usd_cents: 95,
        actual_cost_failed: false,
        estimated_usd_cents: 80,
      }),
    );
    render(<ReviewSubmission />);
    await waitFor(() =>
      expect(screen.getByTestId('review-cost-estimate').textContent).toContain('$0.80'),
    );
    await submitAndFinish();
    await waitFor(() => {
      const price = screen.getByTestId('review-cost-estimate');
      expect(price.querySelector('span')?.textContent).toBe('Final review cost');
      expect(price.querySelector('output')?.textContent).toBe('$0.95');
    });
    expect(screen.getByTestId('review-cost-note').textContent).toBe(
      'Final cost $0.95. It was estimated at $0.80.',
    );
  });

  it('shows a failed review’s cost and says it failed', async () => {
    stubFetch(
      panelRoutes({
        status: 'ERROR',
        decision: null,
        reason: 'unhandled_exception',
        actual_usd_cents: 55,
        actual_cost_failed: true,
      }),
    );
    render(<ReviewSubmission />);
    await waitFor(() =>
      expect(screen.getByTestId('review-cost-estimate').textContent).toContain('$0.80'),
    );
    await submitAndFinish();
    await waitFor(() =>
      expect(screen.getByTestId('review-cost-estimate').querySelector('output')?.textContent).toBe(
        '$0.55',
      ),
    );
    expect(screen.getByTestId('review-cost-note').textContent).toContain('This review failed');
  });

  it('keeps showing the estimate on a finished review whose cost was never recorded', async () => {
    stubFetch(panelRoutes({ status: 'DONE', decision: 'ACCEPT' }));
    render(<ReviewSubmission />);
    await waitFor(() =>
      expect(screen.getByTestId('review-cost-estimate').textContent).toContain('$0.80'),
    );
    await submitAndFinish();
    const price = screen.getByTestId('review-cost-estimate');
    expect(price.querySelector('span')?.textContent).toBe('Estimated review cost');
    expect(price.querySelector('output')?.textContent).toBe('$0.80');
    expect(screen.queryByTestId('review-cost-note')).toBeNull();
  });
});
