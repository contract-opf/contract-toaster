/**
 * legacy-status-renders-128.test.tsx — issue #133 (retired terminal statuses).
 *
 * Owner decision 2026-09-16: a review never concludes as "manual review
 * required". A run that completes is `DONE`; a run that does not is `ERROR`,
 * and its `reason` token is what tells the attorney what to do. The backend
 * no longer writes `MANUAL_REVIEW_REQUIRED` / `ERROR_MANUAL_REVIEW_REQUIRED`
 * — but rows stored before that change still carry them, and there is no
 * migration. The ticket's acceptance criterion: such a row still renders as a
 * FAILURE in History and in the console — never a blank, never the raw wire
 * token, never a third "manual review" outcome.
 *
 * Fixture rule: every row and detail below is a shape production has
 * written. Before #133, `scripts/review_spine.py::_terminal` wrote these
 * statuses with `decision: None` and a `reason` token
 * (`document_too_large` from the step-14 gate, `leakage_detected` from the
 * leakage gate), and `backend/src/pipeline_runner.py::_mock_decision` paired
 * `decision: "MANUAL_REVIEW_REQUIRED"` with the same status. A current
 * `ERROR` row with the same reason — what `reviews.record_stage_failure` and
 * `pipeline_runner._write_real_terminal` write since #133 — is seeded beside
 * them, so "renders like the failure it was" is compared against the real
 * modern shape rather than against a hard-coded string alone.
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

import { OUTCOME_CHIPS } from '../outcome';
import ReviewHistory, { HistoryRow } from '../ReviewHistory';
import ReviewSubmission, { REASON_EXPLANATIONS } from '../ReviewSubmission';
import { DEFAULT_PLAYBOOKS } from './support/consoleSurface';

vi.mock('../auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

vi.mock('aws-amplify/auth', () => ({
  // eslint-disable-next-line @typescript-eslint/require-await
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const LEGACY_STATUSES = ['MANUAL_REVIEW_REQUIRED', 'ERROR_MANUAL_REVIEW_REQUIRED'] as const;
const FAILED = OUTCOME_CHIPS.ERROR;

/** Route-aware transport stub keyed by "METHOD /path", falling back to the
 *  bare path — the convention the other console/History suites share. */
function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  routes = { '/api/playbooks': DEFAULT_PLAYBOOKS, ...routes };
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

describe('#133 — the History tab', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  const row = (overrides: Partial<HistoryRow>): HistoryRow => ({
    review_id: 'rev-modern-error',
    playbook_id: 'synthetic-nda-sample',
    status: 'ERROR',
    decision: null,
    created_at: '1800000200',
    updated_at: '1800000300',
    policy_version: 3,
    posture_version: 2,
    primary_model_id: 'vendor/primary-of-that-day',
    critic_model_id: 'vendor/critic-of-that-day',
    has_output: false,
    has_input: true,
    ...overrides,
  });

  const rows: HistoryRow[] = [
    row({}),
    row({ review_id: 'rev-legacy-manual', status: 'MANUAL_REVIEW_REQUIRED' }),
    row({ review_id: 'rev-legacy-error-manual', status: 'ERROR_MANUAL_REVIEW_REQUIRED' }),
    // The pre-#133 mock pipeline's pairing: the retired value as a DECISION.
    row({
      review_id: 'rev-legacy-mock',
      status: 'MANUAL_REVIEW_REQUIRED',
      decision: 'MANUAL_REVIEW_REQUIRED',
    }),
    // A control that must NOT read as a failure.
    row({ review_id: 'rev-done', status: 'DONE', decision: 'ACCEPT' }),
  ];
  const legacyIds = ['rev-legacy-manual', 'rev-legacy-error-manual', 'rev-legacy-mock'];

  it('renders every legacy row exactly as the modern ERROR row renders', async () => {
    stubFetch({ '/api/reviews': { reviews: rows } });
    render(<ReviewHistory />);

    const modern = await screen.findByTestId('history-outcome-rev-modern-error');
    expect(modern.textContent).toContain(FAILED.label);
    for (const id of legacyIds) {
      const cell = screen.getByTestId(`history-outcome-${id}`);
      expect(cell.textContent?.trim()).not.toBe('');
      expect(cell.textContent).toBe(modern.textContent);
      expect(cell.querySelector('ct-chip')?.getAttribute('variant')).toBe(FAILED.variant);
      expect(cell.textContent).not.toContain('MANUAL_REVIEW_REQUIRED');
      expect(cell.textContent?.toLowerCase()).not.toContain('manual review');
    }
    expect(screen.getByTestId('history-outcome-rev-done').textContent).not.toContain(
      FAILED.label,
    );
  });

  it('lists legacy rows under the "Failed / Stopped" filter, with the modern failure', async () => {
    stubFetch({ '/api/reviews': { reviews: rows } });
    render(<ReviewHistory />);
    await screen.findByTestId('history-outcome-rev-modern-error');

    fireEvent.click(screen.getByTestId('history-filter-failed'));

    expect(screen.getByTestId('history-outcome-rev-modern-error')).toBeTruthy();
    for (const id of legacyIds) {
      expect(screen.getByTestId(`history-outcome-${id}`)).toBeTruthy();
    }
    expect(screen.queryByTestId('history-outcome-rev-done')).toBeNull();
  });
});

describe('#133 — the console (Review tab)', () => {
  const REVIEW_ID = '7c9e6a41-2b3d-4f58-9a0c-1d5e8f2b64a7';

  function docxFile(): File {
    return new File(['contents'], 'contract.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });
  }

  function stubDetail(status: string, reason: string): void {
    stubFetch({
      'POST /api/reviews': { review_id: REVIEW_ID, resumed: false },
      [`GET /api/reviews/${REVIEW_ID}`]: {
        review_id: REVIEW_ID,
        status,
        reason,
        // A non-DONE row never carries a decision (review_spine.py::_terminal),
        // and since #133 no failure carries a status-keyed `message` either.
        decision: null,
        message: null,
        has_output: false,
        created_at: String(Math.floor(Date.now() / 1000)),
      },
    });
  }

  /**
   * Submit one document and read back what the console headlines once the
   * review is terminal. Issue #151's rule for every test that renders
   * ReviewSubmission: wait for the lever by AWAITED STATE against a real-time
   * budget (never an iteration count), draining the fake clock by zero on
   * every pass — the helper in poll-gives-up-122.test.tsx, copied.
   */
  async function headlineAndAlert(): Promise<{ outcome: string; page: string }> {
    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await vi.runAllTimersAsync();
    await vi.waitFor(
      async () => {
        await vi.advanceTimersByTimeAsync(0);
        expect(screen.getByTestId('review-submit-button').getAttribute('aria-disabled')).toBe(
          'false',
        );
      },
      { interval: 0, timeout: 12_000 },
    );
    fireEvent.click(screen.getByTestId('review-submit-button'));
    let outcome = '';
    await vi.waitFor(
      async () => {
        await vi.advanceTimersByTimeAsync(0);
        outcome = screen.getByTestId('review-outcome').textContent ?? '';
      },
      { interval: 0, timeout: 12_000 },
    );
    return { outcome, page: document.body.textContent ?? '' };
  }

  /** Warm the component once so the first case does not pay the cold-import
   *  cost inside its own wait budget (same reason as poll-gives-up-122). */
  beforeAll(async () => {
    vi.useFakeTimers();
    window.sessionStorage.clear();
    stubFetch({});
    render(<ReviewSubmission />);
    await vi.runAllTimersAsync();
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    window.sessionStorage.clear();
  });

  beforeEach(() => {
    vi.useFakeTimers();
    window.sessionStorage.clear();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  it('headlines the modern ERROR row as the failure, with its reason copy', async () => {
    stubDetail('ERROR', 'document_too_large');
    const { outcome, page } = await headlineAndAlert();
    expect(outcome).toBe(FAILED.label);
    expect(page).toContain(REASON_EXPLANATIONS.document_too_large.cause);
  });

  for (const legacy of LEGACY_STATUSES) {
    it(`headlines a stored ${legacy} detail as the failure, not a blank`, async () => {
      stubDetail(legacy, 'document_too_large');
      const { outcome, page } = await headlineAndAlert();
      expect(outcome.trim()).not.toBe('');
      expect(outcome).toBe(FAILED.label);
      // The reason is what explains it — the copy keyed on it is unchanged.
      expect(page).toContain(REASON_EXPLANATIONS.document_too_large.cause);
      expect(page).not.toContain('MANUAL_REVIEW_REQUIRED');
      expect(page).not.toContain('Needs a human');
    });
  }
});
