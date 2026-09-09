/**
 * failed-review-outcome-666.test.tsx — issue #666.
 *
 * ## The defect, from prod
 *
 * `outcome.ts` gave `MANUAL_REVIEW_REQUIRED` and `ERROR_MANUAL_REVIEW_REQUIRED`
 * the SAME label ("Needs manual review") and the SAME chip variant (`warn`).
 * They are not the same event:
 *
 *   - `MANUAL_REVIEW_REQUIRED` — the pipeline reached a terminal answer of
 *     its own and handed the document to a legal admin.
 *   - `ERROR_MANUAL_REVIEW_REQUIRED` — the pipeline FAILED and fell back
 *     closed. `scripts/critic_review_pass.py`'s exhausted-retry terminal is
 *     one producer; `backend/src/reviews.py`'s `STATUS_USER_MESSAGES` opens
 *     that status's own copy with "A pipeline error prevented automatic
 *     review of your document", and `scripts/review_spine.py::_terminal`
 *     writes `decision: None` for it because no verdict was ever reached.
 *
 * So a failed run read, in History, in the same words and the same colour as
 * a completed one awaiting a human.
 *
 * ## What this file pins
 *
 * That the two render DIFFERENTLY — label and, where the surface paints one,
 * variant — on every surface that renders an outcome. All four go through
 * `describeOutcome`, which is exactly why one wrong map entry reached all of
 * them at once; these are the four independent renderings that prove the fix
 * actually arrives where a reviewer looks.
 *
 * Every row shape below is one production writes: a SYSTEM status never
 * carries a decision (`review_spine.py::_terminal` sets `decision: None`),
 * and each seeded `reason` is a token its own producer emits alongside that
 * exact status — `critic_schema_invalid` from
 * `scripts/critic_review_pass.py`'s exhausted-retry terminal
 * (ERROR_MANUAL_REVIEW_REQUIRED) and `model_context_length_exceeded` from
 * `backend/src/reviews.py`'s `STAGE_FAILURE_REASON_STATUS`
 * (MANUAL_REVIEW_REQUIRED).
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describeOutcome, OUTCOME_CHIPS } from '../outcome';
import ReviewHistory, { HistoryRow } from '../ReviewHistory';
import AdminDiagnostics, { RecentFailure } from '../AdminDiagnostics';
import AdminRetention from '../AdminRetention';
import ReviewSubmission from '../ReviewSubmission';
import {
  DEFAULT_PLAYBOOKS,
  findReviewResult,
  pressSubmit,
} from './support/consoleSurface';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

vi.mock('aws-amplify/auth', () => ({
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
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const body = routes[key];
    if (body === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

// The two review ids used throughout, so every surface below is asserting
// about the same pair of rows.
const HANDOFF_ID = 'rev-handoff';
const FAILED_ID = 'rev-failed';

describe('#666 — the map itself', () => {
  it('gives the failed status its own label AND its own variant, not the verdict’s', () => {
    const handoff = OUTCOME_CHIPS.MANUAL_REVIEW_REQUIRED;
    const failed = OUTCOME_CHIPS.ERROR_MANUAL_REVIEW_REQUIRED;

    expect(handoff.label).toBe('Needs manual review');
    expect(handoff.variant).toBe('warn');

    expect(failed.label).not.toBe(handoff.label);
    expect(failed.variant).not.toBe(handoff.variant);
    // It must read as "the review did not finish". `ERROR`'s own treatment is
    // what it sits beside, so it leads with the same word.
    expect(failed.label.startsWith(OUTCOME_CHIPS.ERROR.label)).toBe(true);
    expect(failed.variant).toBe(OUTCOME_CHIPS.ERROR.variant);
    // ...but it is still not the SAME chip as a bare ERROR: the handoff half
    // is real information and survives.
    expect(failed.label).not.toBe(OUTCOME_CHIPS.ERROR.label);
  });

  it('holds for the shapes production actually writes, decision included', () => {
    // A SYSTEM status never carries a decision (review_spine.py::_terminal).
    expect(describeOutcome('ERROR_MANUAL_REVIEW_REQUIRED', null)).not.toEqual(
      describeOutcome('MANUAL_REVIEW_REQUIRED', null),
    );
    // The mock pipeline (backend/src/pipeline_runner.py::_mock_decision) is
    // the one writer that pairs decision="MANUAL_REVIEW_REQUIRED" with that
    // status — still the handoff chip, still not the failure one.
    expect(describeOutcome('MANUAL_REVIEW_REQUIRED', 'MANUAL_REVIEW_REQUIRED')).toEqual(
      OUTCOME_CHIPS.MANUAL_REVIEW_REQUIRED,
    );
    expect(describeOutcome('ERROR_MANUAL_REVIEW_REQUIRED', null)).toEqual(
      OUTCOME_CHIPS.ERROR_MANUAL_REVIEW_REQUIRED,
    );
  });
});

describe('#666 — surface 1: the History tab', () => {
  const row = (overrides: Partial<HistoryRow>): HistoryRow => ({
    review_id: HANDOFF_ID,
    playbook_id: 'synthetic-nda-sample',
    status: 'MANUAL_REVIEW_REQUIRED',
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

  it('does not paint a failed review in the same words and colour as a completed one', async () => {
    stubFetch({
      '/api/reviews': {
        reviews: [
          row({}),
          row({ review_id: FAILED_ID, status: 'ERROR_MANUAL_REVIEW_REQUIRED' }),
        ],
      },
    });
    render(<ReviewHistory />);

    const handoffCell = await screen.findByTestId(`history-outcome-${HANDOFF_ID}`);
    const failedCell = screen.getByTestId(`history-outcome-${FAILED_ID}`);

    expect(handoffCell.textContent).toContain('Needs manual review');
    expect(failedCell.textContent).not.toBe(handoffCell.textContent);
    expect(failedCell.textContent).toContain(OUTCOME_CHIPS.ERROR_MANUAL_REVIEW_REQUIRED.label);
    expect(failedCell.querySelector('ct-chip')?.getAttribute('variant')).not.toBe(
      handoffCell.querySelector('ct-chip')?.getAttribute('variant'),
    );
    // The raw wire token never reaches the cell either way (#470).
    expect(failedCell.textContent).not.toContain('ERROR_MANUAL_REVIEW_REQUIRED');
  });
});

describe('#666 — surface 2: the admin Diagnostics table', () => {
  const failure = (overrides: Partial<RecentFailure>): RecentFailure => ({
    review_id: HANDOFF_ID,
    created_at: '1700000000',
    failing_stage: 'run_review',
    // The provider rejected the assembled prompt as over the model's context
    // window: `backend/src/reviews.py`'s STAGE_FAILURE_REASON_STATUS maps
    // this token to MANUAL_REVIEW_REQUIRED, and it is raised from inside the
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
    return within(row).getAllByRole('cell')[2] as HTMLElement;
  }

  it('tells the failed row apart from the handed-off one, on the screen that already calls it a failure', async () => {
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

    expect(handoffOutcome.textContent).toContain('Needs manual review');
    expect(failedOutcome.textContent).not.toBe(handoffOutcome.textContent);
    expect(failedOutcome.textContent).toContain(OUTCOME_CHIPS.ERROR_MANUAL_REVIEW_REQUIRED.label);
    expect(failedOutcome.querySelector('ct-chip')?.getAttribute('variant')).not.toBe(
      handoffOutcome.querySelector('ct-chip')?.getAttribute('variant'),
    );
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

  it('does not offer a failed review under the same words as a completed one', async () => {
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

    expect(handoffText).toContain('Needs manual review');
    expect(failedText).toContain(OUTCOME_CHIPS.ERROR_MANUAL_REVIEW_REQUIRED.label);
    expect(failedText).not.toContain(`— ${OUTCOME_CHIPS.MANUAL_REVIEW_REQUIRED.label}`);

    // ...and the "Selected: …" readback under the picker says the same thing.
    fireEvent.change(select, { target: { value: FAILED_ID } });
    const match = await screen.findByTestId('hold-review-id-match');
    expect(match.textContent).toContain(OUTCOME_CHIPS.ERROR_MANUAL_REVIEW_REQUIRED.label);
    expect(match.textContent).not.toContain(`— ${OUTCOME_CHIPS.MANUAL_REVIEW_REQUIRED.label}`);
  });
});

describe('#666 — surface 4: the Review tab result panel', () => {
  function docxFile(): File {
    return new File(['contents'], 'contract.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });
  }

  /** Submit one document, poll once, and read back the outcome headline the
   *  submitter sees for `status`. */
  async function headlineFor(status: string): Promise<{ label: string; color: string }> {
    stubFetch({
      'POST /api/reviews': { review_id: `rev-${status}`, resumed: false },
      [`GET /api/reviews/rev-${status}`]: {
        review_id: `rev-${status}`,
        status,
        // A SYSTEM status never carries a decision — review_spine.py::_terminal.
        decision: null,
        message: 'A legal admin will review it.',
        has_output: false,
      },
    });
    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await pressSubmit();
    await findReviewResult();
    const headline = await screen.findByTestId('review-outcome');
    return {
      label: headline.textContent ?? '',
      // How the surface paints the difference: an inline colour var on the old
      // headline, the console's own `data-status` on its root (issue #733).
      // Either way, two outcomes that looked identical would compare equal.
      color:
        headline.getAttribute('style') ??
        document.querySelector('.od-console')?.getAttribute('data-status') ??
        '',
    };
  }

  it('does not headline a failed review with the completed one’s words or colour', async () => {
    const handoff = await headlineFor('MANUAL_REVIEW_REQUIRED');
    cleanup();
    vi.unstubAllGlobals();
    const failed = await headlineFor('ERROR_MANUAL_REVIEW_REQUIRED');

    expect(handoff.label).toBe('Needs manual review');
    expect(failed.label).toBe(OUTCOME_CHIPS.ERROR_MANUAL_REVIEW_REQUIRED.label);
    expect(failed.label).not.toBe(handoff.label);
    // The variant has to be painted, not only worded: a state that changed its
    // words but not its appearance would leave these identical.
    expect(failed.color).not.toBe(handoff.color);
  });
});
