/**
 * failed-review-outcome-666.test.tsx — issue #666, restated for issue #133.
 *
 * ## The defect, from prod (#666)
 *
 * `outcome.ts` gave `MANUAL_REVIEW_REQUIRED` and `ERROR_MANUAL_REVIEW_REQUIRED`
 * the SAME label ("Needs manual review") and the SAME chip variant (`warn`),
 * so a pipeline that FAILED read, in History, in the same words and colour as
 * a completed review awaiting a human. #666 split the two chips.
 *
 * ## What changed (#133, owner decision 2026-09-16)
 *
 * A review never concludes as "manual review required". A run that completes
 * is `DONE`; a run that does not is `ERROR`, and its `reason` token says what
 * to do next. Both statuses are retired, so there is no "handed to a human"
 * outcome left to confuse a failure WITH. What #666 protected — a failed run
 * is never painted as anything but a failure — is now pinned the other way
 * round: a stored row still carrying either retired status (there is no
 * migration) renders as `Failed`, in `ERROR`'s colour, on every surface that
 * renders an outcome. History and the Review tab are pinned by
 * `legacy-status-renders-128.test.tsx`; this file keeps the map and the two
 * admin surfaces #666 covered.
 *
 * Every row shape below is one production has written: pre-#133 rows carried
 * these statuses with `decision: None` (`review_spine.py::_terminal`), and the
 * pre-#133 mock pipeline paired `decision: "MANUAL_REVIEW_REQUIRED"` with the
 * same status. The seeded `reason` tokens are the ones those statuses were
 * written with (`critic_schema_invalid`, `model_context_length_exceeded`).
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describeOutcome, OUTCOME_CHIPS } from '../outcome';
import AdminDiagnostics, { RecentFailure } from '../AdminDiagnostics';
import AdminRetention from '../AdminRetention';
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

/** Route-aware transport stub, keyed by "METHOD /path" falling back to the
 *  path alone — the convention admin-retention-475.test.tsx and
 *  toaster-states.test.tsx already share. */
function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  // Issue #733: the catalog is a fixture every scenario needs — the console
  // will not arm its lever without an active playbook.
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

afterEach(() => {
  vi.unstubAllGlobals();
});

// The two review ids used throughout: one row per retired status.
const HANDOFF_ID = 'rev-legacy-manual';
const FAILED_ID = 'rev-legacy-error-manual';

const FAILED = OUTCOME_CHIPS.ERROR;

describe('#666/#133 — the map itself', () => {
  it('has no "manual review" outcome left to paint a failure as', () => {
    expect(Object.keys(OUTCOME_CHIPS)).not.toContain('MANUAL_REVIEW_REQUIRED');
    expect(Object.keys(OUTCOME_CHIPS)).not.toContain('ERROR_MANUAL_REVIEW_REQUIRED');
    for (const chip of Object.values(OUTCOME_CHIPS)) {
      expect(chip.label.toLowerCase()).not.toContain('manual review');
    }
  });

  it('reads both retired statuses as the failure, for every shape production wrote', () => {
    expect(FAILED.label).toBe('Failed');
    expect(FAILED.variant).toBe('danger');
    expect(describeOutcome('MANUAL_REVIEW_REQUIRED', null)).toEqual(FAILED);
    expect(describeOutcome('ERROR_MANUAL_REVIEW_REQUIRED', null)).toEqual(FAILED);
    // The pre-#133 mock pipeline's pairing.
    expect(describeOutcome('MANUAL_REVIEW_REQUIRED', 'MANUAL_REVIEW_REQUIRED')).toEqual(FAILED);
    // A stale decision never outranks the failure (#95).
    expect(describeOutcome('ERROR_MANUAL_REVIEW_REQUIRED', 'REQUEST_CHANGE')).toEqual(FAILED);
  });
});

describe('#666 — surface 2: the admin Diagnostics table', () => {
  const failure = (overrides: Partial<RecentFailure>): RecentFailure => ({
    review_id: HANDOFF_ID,
    created_at: '1700000000',
    failing_stage: 'run_review',
    // The provider rejected the assembled prompt as over the model's context
    // window: before #133 `backend/src/reviews.py`'s STAGE_FAILURE_REASON_STATUS
    // mapped this token to MANUAL_REVIEW_REQUIRED, and it is raised from inside the
    // model call, so `run_review` is its real stage (the same triple
    // review-failure-diagnosis.test.tsx already drives).
    reason: 'model_context_length_exceeded',
    status: 'MANUAL_REVIEW_REQUIRED',
    ...overrides,
  });

  /** The Outcome cell is the row's third `<td>` (Review, Failed at, Outcome,
   *  Stage, Cause, What to do) — no dedicated testid, read positionally the
   *  same way admin-diagnostics.test.tsx reads "Failed at". */
  function outcomeCell(row: HTMLElement): HTMLElement {
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    return within(row).getAllByRole('cell')[2] as HTMLElement;
  }

  it('renders both legacy rows as failures, on the screen that already calls them one', async () => {
    stubFetch({
      '/api/admin/diagnostics/recent-failures': {
        failures: [
          failure({}),
          failure({
            review_id: FAILED_ID,
            reason: 'critic_schema_invalid',
            failing_stage: 'run_review',
            status: 'ERROR_MANUAL_REVIEW_REQUIRED',
          }),
        ],
      },
    });
    render(<AdminDiagnostics />);

    const handoffRow = await screen.findByTestId(`failure-row-${HANDOFF_ID}`);
    const failedRow = screen.getByTestId(`failure-row-${FAILED_ID}`);

    const handoffOutcome = outcomeCell(handoffRow);
    const failedOutcome = outcomeCell(failedRow);

    for (const cell of [handoffOutcome, failedOutcome]) {
      expect(cell.textContent).toContain(FAILED.label);
      expect(cell.textContent).not.toContain('manual review');
      expect(cell.textContent).not.toContain('MANUAL_REVIEW_REQUIRED');
      expect(cell.querySelector('ct-chip')?.getAttribute('variant')).toBe(FAILED.variant);
    }
  });
});

describe('#666 — surface 3: the retention hold picker', () => {
  const review = (overrides: Record<string, unknown>) => ({
    review_id: HANDOFF_ID,
    owner_sub: 'sub-1',
    created_at: 1700000000,
    status: 'MANUAL_REVIEW_REQUIRED',
    decision: null,
    ...overrides,
  });

  it('offers both legacy rows under the failure label', async () => {
    stubFetch({
      '/api/admin/retention': {
        setting_id: 'global',
        retention_window_days: 90,
        pending_reduction: null,
      },
      '/api/admin/retention/holds': { holds: [] },
      '/api/reviews': {
        reviews: [
          review({}),
          review({
            review_id: FAILED_ID,
            created_at: 1700000100,
            status: 'ERROR_MANUAL_REVIEW_REQUIRED',
          }),
        ],
      },
      '/api/users': {
        users: [
          {
            cognito_sub: 'sub-1',
            email: 'jane@example.com',
            status: 'active',
            is_admin: false,
            last_auth_at: 0,
            created_at: 0,
          },
        ],
      },
    });
    render(<AdminRetention />);

    const select = await screen.findByTestId('hold-review-select');
    await waitFor(() => {
      expect(within(select).getAllByRole('option').length).toBeGreaterThan(1);
    });
    const optionFor = (id: string): HTMLOptionElement => {
      const found = within(select).getAllByRole('option').find(
        (option) => (option as HTMLOptionElement).value === id,
      );
      if (!found) {
        throw new Error(`no <option> was rendered for ${id}`);
      }
      return found as HTMLOptionElement;
    };

    const handoffText = optionFor(HANDOFF_ID).textContent ?? '';
    const failedText = optionFor(FAILED_ID).textContent ?? '';

    for (const text of [handoffText, failedText]) {
      expect(text).toContain(`— ${FAILED.label}`);
      expect(text.toLowerCase()).not.toContain('manual review');
    }

    // ...and the "Selected: …" readback under the picker says the same thing.
    fireEvent.change(select, { target: { value: FAILED_ID } });
    const match = await screen.findByTestId('hold-review-id-match');
    expect(match.textContent).toContain(FAILED.label);
    expect(match.textContent?.toLowerCase()).not.toContain('manual review');
  });
});
