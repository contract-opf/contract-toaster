/**
 * aws-catch-failure-rows-105.test.tsx — issue #105, the reader-facing half.
 *
 * ## The row this file is about
 *
 * On the AWS target a stage that raises is caught by Step Functions and
 * routed to the inlined `TransitionToError<Stage>` Lambda in
 * `infra/lib/nested/pipeline-stack.ts`, which writes exactly this shape onto
 * the reviews row:
 *
 *     { status: 'ERROR', failing_stage: <the real stage name>,
 *       reason: 'unhandled_exception', error_reason: <error NAME only>,
 *       failed_at: <epoch seconds> }
 *
 * `reason` is always the unclassified sentinel, and honestly so: a Step
 * Functions Catch sees an error name and a stage, never the model or document
 * facts `classify_failure_reason` reads. Every reader of `reason` therefore
 * has to fall through it to the stage — and two of them did not do the right
 * thing with that:
 *
 *   1. `explainFailure` skipped the sentinel correctly, but eight of the nine
 *      AWS stage names had no `STAGE_EXPLANATIONS` entry, so every failure
 *      that can happen mid-review resolved to the generic
 *      "The review stopped before it could finish."
 *   2. `detectConsecutiveIncidents` did NOT skip the sentinel, and it
 *      clusters on `reason` before `failing_stage`. Once #105 started writing
 *      `reason` at all, every AWS ERROR row carried the same token — so the
 *      reason branch always won, the banner read `reason
 *      "unhandled_exception"`, and "Filter to this reason" searched a token
 *      every AWS failure matches. The accurate per-stage names were reachable
 *      nowhere.
 *
 * ## Why the stage list is read out of the CDK source
 *
 * No gate in this repo models what an infra Lambda actually writes — jsdom
 * tests build their own row fixtures — which is exactly how a hardcoded
 * `failing_stage: 'pipeline'` survived. So the stage vocabulary here is
 * parsed from `withStageErrorHandling(…, '<token>', …)` in
 * `pipeline-stack.ts` rather than retyped: adding a tenth stage to the state
 * machine without giving its reader copy turns this file red.
 *
 * Fully offline — fetch is stubbed, no network, no cdk synth.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import AdminDiagnostics, { detectConsecutiveIncidents, RecentFailure } from '../AdminDiagnostics';
import { explainFailure, UNCLASSIFIED_REASON } from '../ReviewSubmission';

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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// Anchor on this file, not the working directory. (Do NOT write
// `new URL('...', import.meta.url)` — Vite's asset plugin rewrites that form.)
const PIPELINE_STACK = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../infra/lib/nested/pipeline-stack.ts',
);

/** Every `failing_stage` token the AWS Catch target can write, read from the
 *  `withStageErrorHandling(<task>, '<token>', '<idSuffix>')` calls that are
 *  the single place those tokens are declared. */
function awsStageTokens(): string[] {
  const source = readFileSync(PIPELINE_STACK, 'utf8');
  const tokens = [...source.matchAll(/withStageErrorHandling\([^,]+,\s*'([a-z_]+)'/g)].map(
    (match) => match[1],
  );
  expect(tokens.length, 'withStageErrorHandling call sites found in pipeline-stack.ts').toBe(9);
  return tokens;
}

// The generic object `explainFailure` returns for a stage it has never heard
// of — the string the issue's Evidence block quotes as the defect.
const GENERIC_FALLBACK = {
  cause: 'The review stopped before it could finish.',
  fix: 'Please try again, or contact an admin if it keeps happening.',
};

/** A row in the shape `TransitionToError<Stage>` writes. */
function awsFailureRow(stage: string, reviewId: string): RecentFailure {
  return {
    review_id: reviewId,
    created_at: '1800000000',
    failed_at: '1800000042',
    failing_stage: stage,
    reason: UNCLASSIFIED_REASON,
    status: 'ERROR',
  };
}

function stubFetch(body: unknown): void {
  vi.stubGlobal(
    'fetch',
    // eslint-disable-next-line @typescript-eslint/require-await
    vi.fn(async () => ({ ok: true, status: 200, json: async () => body }) as Response),
  );
}

describe('#105 — the submitter is told what actually broke', () => {
  it('every AWS stage the Catch target can name has its own copy', () => {
    for (const stage of awsStageTokens()) {
      const explanation = explainFailure({ reason: UNCLASSIFIED_REASON, failing_stage: stage });
      expect(explanation, `${stage} resolves to an explanation`).not.toBeNull();
      expect(explanation, `${stage} still returns the generic fallback`).not.toEqual(
        GENERIC_FALLBACK,
      );
      expect(explanation!.cause.length, `${stage} cause is non-empty`).toBeGreaterThan(0);
      expect(explanation!.fix.length, `${stage} fix is non-empty`).toBeGreaterThan(0);
    }
  });

  it('gives different stages different copy, so the reader can tell them apart', () => {
    // A copy-paste across all nine would pass the check above while telling
    // the reader nothing — the same trap #62's copy test names.
    const extract = explainFailure({ reason: UNCLASSIFIED_REASON, failing_stage: 'extract' });
    const retrieve = explainFailure({ reason: UNCLASSIFIED_REASON, failing_stage: 'retrieve' });
    expect(extract).not.toEqual(retrieve);
  });

  it('still falls back for a stage no build has heard of', () => {
    // The fallback itself is not removed — it is what an unknown token gets.
    expect(
      explainFailure({ reason: UNCLASSIFIED_REASON, failing_stage: 'pipeline' }),
    ).toEqual(GENERIC_FALLBACK);
  });

  it('renders the stage copy on the Diagnostics row, not the raw token', async () => {
    const row = awsFailureRow('extract', 'rev-aws-extract');
    const expected = explainFailure(row)!;
    stubFetch({ failures: [row] });
    render(<AdminDiagnostics />);

    const rendered = await screen.findByTestId('failure-row-rev-aws-extract');
    expect(within(rendered).getByText(expected.cause)).toBeTruthy();
    expect(within(rendered).getByText(expected.fix)).toBeTruthy();
    expect(rendered.textContent ?? '').not.toContain(UNCLASSIFIED_REASON);
  });
});

describe('#105 — the incident banner clusters on the stage, not the sentinel', () => {
  it('ignores the unclassified sentinel and names the stage instead', () => {
    const rows = [
      awsFailureRow('extract', 'r-1'),
      awsFailureRow('extract', 'r-2'),
      awsFailureRow('extract', 'r-3'),
    ];
    expect(detectConsecutiveIncidents(rows)).toEqual({
      type: 'stage',
      value: 'extract',
      count: 3,
    });
  });

  it('reports nothing when only the sentinel is shared and the stages differ', () => {
    // Three unrelated AWS failures in a row are not an incident. Before the
    // fix this returned `reason "unhandled_exception" × 3` — a banner that
    // fires on any three AWS failures whatsoever.
    const rows = [
      awsFailureRow('extract', 'r-1'),
      awsFailureRow('redline', 'r-2'),
      awsFailureRow('persist', 'r-3'),
    ];
    expect(detectConsecutiveIncidents(rows)).toBeNull();
  });

  it('still clusters on a reason that IS classified', () => {
    // The skip is narrow: only the "we don't know" token loses to the stage.
    const rows = ['r-1', 'r-2', 'r-3'].map((id) => ({
      ...awsFailureRow('run_review', id),
      reason: 'model_account_out_of_credits',
    }));
    expect(detectConsecutiveIncidents(rows)).toEqual({
      type: 'reason',
      value: 'model_account_out_of_credits',
      count: 3,
    });
  });

  it('offers "Filter to this stage" on the banner for a run of AWS failures', async () => {
    stubFetch({
      failures: [
        awsFailureRow('redline', 'r-1'),
        awsFailureRow('redline', 'r-2'),
        awsFailureRow('redline', 'r-3'),
      ],
    });
    render(<AdminDiagnostics />);

    const banner = await screen.findByTestId('diagnostics-incident-banner');
    expect(banner.textContent ?? '').toContain('stage "redline"');
    expect(banner.textContent ?? '').not.toContain(UNCLASSIFIED_REASON);
    expect(screen.getByTestId('diagnostics-filter-incident-btn').textContent).toBe(
      'Filter to this stage',
    );
  });
});
