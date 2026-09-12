/**
 * runner-restarted-62.test.tsx — issue #62.
 *
 * ## The event this copy explains
 *
 * On the Docker Compose target a review runs on an in-process thread pool
 * inside the API container. A Coolify redeploy (or an OOM kill, or an
 * operator restart) takes that process away mid-pass, and nothing is left
 * alive to write a terminal — so until #62 the row simply stayed `RUNNING`
 * forever. `backend/src/runner_recovery.py::recover_orphaned_reviews` now
 * relabels it at the next boot: `status: 'ERROR'`, `reason:
 * 'runner_restarted'`.
 *
 * That token is only half a fix. A reader looking at a failed paid run needs
 * to be told what happened and what to do, and until this file the token
 * resolved to nothing at all — `explainFailure` would have fallen through to
 * the vague `STAGE_EXPLANATIONS` copy, which is the exact defect #670
 * documents for a different token.
 *
 * ## What this file pins
 *
 *   1. `explainFailure` resolves the token to its OWN copy, not the stage
 *      fallback and not another token's.
 *   2. The admin Diagnostics table — the surface a recovered review actually
 *      appears on, since `runner_recovery` writes `ERROR` and
 *      `reviews.list_recent_failures` reads the `ERROR` partition — renders
 *      that copy.
 *   3. Nothing leaks: not the raw `runner_restarted` token, not the raw
 *      `ERROR` status token, and no exception text. (CLAUDE.md: escaped text
 *      only, no raw server exception in the status window.)
 *
 * ## Fixture fidelity
 *
 * The `RecentFailure` row below is the shape the recovery path really
 * produces, field by field: `status: 'ERROR'` and `reason: 'runner_restarted'`
 * are what `runner_recovery._relabel` writes, `failed_at` is stamped by the
 * same update, and `failing_stage` is ABSENT because that write deliberately
 * names no stage (a review killed by a restart has no failing stage, and the
 * one it might already carry is kept rather than overwritten). A fixture that
 * carried a `failing_stage` here would be testing a row production does not
 * write — and would also hide the regression this file cares most about, since
 * a stage fallback would then paper over a missing reason entry.
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import AdminDiagnostics, { RecentFailure } from '../AdminDiagnostics';
import { explainFailure, REASON_EXPLANATIONS } from '../ReviewSubmission';

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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const REVIEW_ID = 'rev-restarted-62';

/** The row `runner_recovery._relabel` produces, as the diagnostics route
 *  projects it. See this file's "Fixture fidelity" note. */
const RECOVERED_ROW: RecentFailure = {
  review_id: REVIEW_ID,
  created_at: '1800000000',
  failed_at: '1800003600',
  reason: 'runner_restarted',
  status: 'ERROR',
};

const EXPECTED_CAUSE = 'The service restarted while your review was running.';
const EXPECTED_FIX =
  'Nothing was charged beyond the passes that had already finished — start the review again.';

function stubFetch(body: unknown): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, json: async () => body }) as Response),
  );
}

describe('#62 — the token resolves to its own explanation', () => {
  it('is the copy the issue specifies, verbatim', () => {
    expect(REASON_EXPLANATIONS.runner_restarted).toEqual({
      cause: EXPECTED_CAUSE,
      fix: EXPECTED_FIX,
    });
  });

  it('wins over the stage fallback, and is nobody else’s copy', () => {
    // The row production writes carries no failing_stage at all.
    expect(explainFailure({ reason: 'runner_restarted' })).toEqual(
      REASON_EXPLANATIONS.runner_restarted,
    );
    // And still wins when a stage IS present — the reason is the specific
    // fact, the stage only says where the pipeline stopped (#442's order).
    expect(
      explainFailure({ reason: 'runner_restarted', failing_stage: 'run_review' }),
    ).toEqual(REASON_EXPLANATIONS.runner_restarted);
    // Not a duplicate of a neighbouring token: "try again" is the right
    // advice for several of these, and a copy-paste would make the screen
    // useless for telling them apart.
    expect(REASON_EXPLANATIONS.runner_restarted).not.toEqual(
      REASON_EXPLANATIONS.model_timeout,
    );
  });
});

describe('#62 — the surface a recovered review lands on', () => {
  it('shows the cause and the fix on the Diagnostics row', async () => {
    stubFetch({ failures: [RECOVERED_ROW] });
    render(<AdminDiagnostics />);

    const row = await screen.findByTestId(`failure-row-${REVIEW_ID}`);
    expect(within(row).getByText(EXPECTED_CAUSE)).toBeTruthy();
    expect(within(row).getByText(EXPECTED_FIX)).toBeTruthy();
  });

  it('leaks no raw token, status or exception text', async () => {
    stubFetch({ failures: [RECOVERED_ROW] });
    render(<AdminDiagnostics />);

    const row = await screen.findByTestId(`failure-row-${REVIEW_ID}`);
    const text = row.textContent ?? '';
    expect(text).not.toContain('runner_restarted');
    expect(text).not.toContain('ERROR');
    expect(text).not.toContain('Traceback');
    expect(text).not.toContain('Exception');
    // The vague fallback must not be what the reader sees.
    expect(text).not.toContain('The exact cause was not identified');
  });
});
