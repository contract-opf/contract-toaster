/**
 * orbit-diner-review-cost-735.test.tsx — what the register's price glass says,
 * and when (issue #735, owner decision H2, epic #729).
 *
 * The delivered kit made the five terminal statuses their own case in
 * `costText`: an em dash unless a COMPLETE settlement carried a number. This
 * deployment has no settlement projection — `projectCost` only ever emits
 * `estimated` or `unavailable` — so every finished review read
 *
 *     FINAL REVIEW COST    —
 *
 * which is wrong twice over: it claims a final cost that does not exist, and
 * it throws away the estimate that WAS captured for that review.
 *
 * H2 gives the glass four states, and this file is one describe per state:
 *
 *   - **Estimated review cost** before submission and after a terminal status,
 *     showing the estimate captured for THAT review;
 *   - **Review reservation** for a confirmed hold;
 *   - **Final review cost** only for a complete settlement carrying a numeric
 *     `cents`, INCLUDING a genuine zero;
 *   - **Estimate unavailable** when there is no estimate at all.
 *
 * Two harnesses, deliberately. The label/figure pairs are asserted against a
 * directly mounted `<OrbitDiner>` because `Cost` states this app cannot yet
 * produce (a hold, a settlement) are reachable only by handing the component a
 * model — and they are what the label promise is ABOUT, so leaving them
 * untested until a settlement projection exists would leave the honesty claim
 * unproven exactly where it matters. The end-to-end block then proves the
 * whole path — fetched estimate, submit, poll to DONE — through the real
 * panel, which is the run that used to print the em dash.
 *
 * Asserts on rendered text and testids only; `vitest.config.ts` runs jsdom
 * with `css: false`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { OrbitDiner } from '../orbit-diner/OrbitDiner';
import type { Cost, Playbook, ReviewModel, Status } from '../orbit-diner/types';

const PLAYBOOKS: Playbook[] = [
  { playbook_id: 'nda', display_name: 'NDA', status: 'active' },
];

/** The five statuses a review can END on. */
const TERMINAL: Status[] = [
  'DONE',
  'ERROR',
  'MANUAL_REVIEW_REQUIRED',
  'ERROR_MANUAL_REVIEW_REQUIRED',
  'CANCELLED',
];

function consoleModel(patch: Partial<ReviewModel> = {}): ReviewModel {
  return {
    status: 'DONE',
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

/** The register's price glass: `[label, figure]`. */
function glass(cost: Cost, patch: Partial<ReviewModel> = {}): [string, string] {
  const view = render(
    <OrbitDiner
      model={consoleModel({ cost, ...patch })}
      onFile={() => {}}
      onPreferences={() => {}}
      onAction={() => {}}
    />,
  );
  const price = screen.getByTestId('review-cost-estimate');
  const label = price.querySelector('span')?.textContent ?? '';
  const figure = price.querySelector('output')?.textContent ?? '';
  view.unmount();
  return [label, figure];
}

describe('Estimated review cost (#735 H2)', () => {
  it('keeps the captured estimate on every terminal status', () => {
    for (const status of TERMINAL) {
      const [label, figure] = glass(
        { kind: 'estimated', cents: 79, basis: 'typical' },
        { status },
      );
      expect(label, status).toBe('Estimated review cost');
      expect(figure, status).toBe('$0.79');
    }
  });

  it('is the same figure before submission', () => {
    expect(
      glass({ kind: 'estimated', cents: 79, basis: 'typical' }, { status: 'EMPTY' }),
    ).toEqual(['Estimated review cost', '$0.79']);
  });

  it('never labels a terminal status a final cost without a settlement', () => {
    // The exact regression: `terminal ? 'Final review cost'` labelled a figure
    // this deployment cannot produce, and the em dash under it was `costText`
    // declining to invent one.
    for (const status of TERMINAL) {
      const priced = glass(
        { kind: 'estimated', cents: 79, basis: 'typical' },
        { status },
      );
      expect(priced, status).toEqual(['Estimated review cost', '$0.79']);
      const unpriced = glass({ kind: 'unavailable' }, { status });
      expect(unpriced, status).toEqual(['Estimate unavailable', '—']);
    }
  });
});

describe('Review reservation (#735 H2)', () => {
  it('shows the authorised hold, or the word Held when the amount is not known', () => {
    expect(glass({ kind: 'held', holdCents: 686 }, { status: 'RUNNING' })).toEqual([
      'Review reservation',
      '$6.86',
    ]);
    expect(glass({ kind: 'held' }, { status: 'RUNNING' })).toEqual([
      'Review reservation',
      'Held',
    ]);
  });
});

describe('Final review cost (#735 H2)', () => {
  it('appears only for a complete settlement carrying a number', () => {
    expect(
      glass({ kind: 'settled', cents: 104, settlementComplete: true }),
    ).toEqual(['Final review cost', '$1.04']);
  });

  it('prints a genuine complete zero rather than suppressing it', () => {
    // A ledger that really settled at nothing is a fact. What must never be
    // inferred is a $0.00 for a review nobody priced — the case below.
    expect(glass({ kind: 'settled', cents: 0, settlementComplete: true })).toEqual([
      'Final review cost',
      '$0.00',
    ]);
  });

  it('does not appear while the settlement is still incomplete', () => {
    const [label, figure] = glass({ kind: 'settled', cents: 104 });
    expect(label).toBe('Review settlement');
    expect(figure).toBe('Pending');
  });
});

describe('Estimate unavailable (#735 H2)', () => {
  it('is what the glass reads when there is no estimate at all', () => {
    expect(glass({ kind: 'unavailable' }, { status: 'EMPTY' })).toEqual([
      'Estimate unavailable',
      '—',
    ]);
  });

  it('never gives a cancelled or failed review an inferred $0.00', () => {
    for (const status of ['CANCELLED', 'ERROR'] as Status[]) {
      const [label, figure] = glass({ kind: 'unavailable' }, { status });
      expect(label, status).toBe('Estimate unavailable');
      expect(figure, status).not.toContain('$');
    }
  });

  it('says it once — as the label, not also on the live status line', () => {
    render(
      <OrbitDiner
        model={consoleModel({ status: 'EMPTY', cost: { kind: 'unavailable' } })}
        onFile={() => {}}
        onPreferences={() => {}}
        onAction={() => {}}
      />,
    );
    expect(screen.getAllByText('Estimate unavailable')).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// End to end, through the real panel
// ---------------------------------------------------------------------------

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

/** What `GET /api/review-cost-estimate` answers with for the next render. */
let estimateCents: number | null = 79;

/**
 * Held open, `GET /api/review-cost-estimate` never lands — which is the state
 * a reviewer who submits quickly captures, because the panel's estimate starts
 * at `null` and only the in-flight mount fetch ever moves it.
 */
let estimateGate: Promise<void> | null = null;

/** The terminal status the polled review settles on. */
let polledStatus: 'DONE' | 'ERROR' = 'DONE';

/** When true, `POST /api/reviews` 503s instead of starting a review. */
let postFails = false;

function mockFetch(): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const path = new URL(url, 'http://localhost').pathname;
    const method = (init?.method ?? 'GET').toUpperCase();
    const ok = (body: unknown) =>
      ({ ok: true, status: 200, json: async () => body }) as Response;
    if (path === '/api/playbooks') return ok({ playbooks: PLAYBOOKS });
    if (path === '/api/me/preferences')
      return ok({ preferences: {}, notes_mode_available: false });
    if (path === '/api/review-cost-estimate') {
      if (estimateGate) await estimateGate;
      return ok({ estimated_usd_cents: estimateCents });
    }
    if (path === '/api/reviews' && method === 'POST') {
      if (postFails) {
        return {
          ok: false,
          status: 503,
          json: async () => ({ detail: 'The toaster is offline.' }),
        } as Response;
      }
      return ok({ review_id: 'rev-735', resumed: false });
    }
    if (path === '/api/reviews/rev-735')
      return ok({
        review_id: 'rev-735',
        status: polledStatus,
        decision: polledStatus === 'DONE' ? 'ACCEPT' : null,
        message: polledStatus === 'DONE' ? null : 'The model could not finish this document.',
        has_output: false,
        has_input: true,
      });
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function docxFile(name = 'contract.docx'): File {
  return new File(['x'], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

async function submitOnce(): Promise<void> {
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  const lever = await screen.findByTestId('review-submit-button');
  await waitFor(() => {
    if (lever.getAttribute('aria-disabled') === 'true') {
      throw new Error('the lever is not armed yet');
    }
  });
  fireEvent.click(lever);
}

function priceText(): string {
  return screen.getByTestId('review-cost-estimate').textContent ?? '';
}

function consoleStatus(): string | null | undefined {
  return document.querySelector('.od-console')?.getAttribute('data-status');
}

describe('a finished review keeps the estimate it was quoted at (#735)', () => {
  beforeEach(() => {
    estimateCents = 79;
    estimateGate = null;
    polledStatus = 'DONE';
    postFails = false;
    mockFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads "Estimated review cost $0.79" once the review is DONE', async () => {
    render(<ReviewSubmission />);
    await waitFor(() => expect(priceText()).toContain('$0.79'));

    await submitOnce();
    await waitFor(() =>
      expect(document.querySelector('.od-console')?.getAttribute('data-status')).toBe(
        'done',
      ),
    );

    // The regression this ticket exists for: the glass used to read
    // "Final review cost —" here, having discarded the captured estimate and
    // claimed a settlement figure that does not exist.
    expect(priceText()).toContain('Estimated review cost');
    expect(priceText()).toContain('$0.79');
    expect(priceText()).not.toContain('Final review cost');
  });

  it('shows nothing rather than $0.00 when the deployment quotes no price', async () => {
    estimateCents = null;
    render(<ReviewSubmission />);
    await submitOnce();
    await waitFor(() =>
      expect(document.querySelector('.od-console')?.getAttribute('data-status')).toBe(
        'done',
      ),
    );

    expect(priceText()).toContain('Estimate unavailable');
    expect(priceText()).not.toContain('$');
  });
});

describe('a reset takes the captured estimate off the screen with the review (#735)', () => {
  beforeEach(() => {
    estimateCents = 79;
    estimateGate = null;
    polledStatus = 'ERROR';
    postFails = false;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('goes back to the live figure after a submit that captured no estimate', async () => {
    // The reviewer submits before the price arrives. `reviewCostUsdCents` is
    // still `null`, so the review is captured at `null` — a real submit that
    // was quoted nothing, which the glass must keep saying for THAT review.
    let landEstimate!: () => void;
    estimateGate = new Promise<void>((resolve) => {
      landEstimate = resolve;
    });
    const impl = mockFetch();
    render(<ReviewSubmission />);

    await submitOnce();
    await waitFor(() => expect(consoleStatus()).toBe('error'));
    expect(priceText()).toContain('Estimate unavailable');

    // The estimate lands now, after the capture. It does not move the failed
    // review's glass: that review really was submitted without a price.
    await act(async () => {
      landEstimate();
      await Promise.all(impl.mock.results.map((result) => result.value));
    });
    expect(priceText()).toContain('Estimate unavailable');

    // "Toast another slice" — the review leaves the screen, and with it the
    // figure captured for it. The regression: `resetForRetry` kept the
    // captured `null`, and because `projectCost` prefers any captured value
    // over the live one, a pre-submission screen with no review on it read
    // "Estimate unavailable" for the rest of the session.
    fireEvent.click(screen.getByTestId('review-retry-button'));
    await waitFor(() => expect(consoleStatus()).toBe('empty'));
    await waitFor(() => expect(priceText()).toContain('$0.79'));
    expect(priceText()).toContain('Estimated review cost');
    expect(priceText()).not.toContain('Estimate unavailable');
  });
});

describe('a failed re-submit does not price the next document at the last one (#735)', () => {
  beforeEach(() => {
    estimateCents = 79;
    estimateGate = null;
    polledStatus = 'ERROR';
    postFails = false;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('falls back to the live figure when the second POST fails', async () => {
    // `submitReview` drops the previous review off the screen synchronously
    // (`setDetail(null)`, `setReviewId(null)`) BEFORE the POST. When the POST
    // then throws, the panel is back on a pre-submission screen, so the figure
    // captured for the review that just left must have gone with it.
    let landEstimate!: () => void;
    estimateGate = new Promise<void>((resolve) => {
      landEstimate = resolve;
    });
    const impl = mockFetch();
    render(<ReviewSubmission />);

    // Review one is submitted before the price lands, so it captures `null`.
    await submitOnce();
    await waitFor(() => expect(consoleStatus()).toBe('error'));
    expect(priceText()).toContain('Estimate unavailable');

    // The estimate arrives afterwards. The live figure is now a known $0.79.
    await act(async () => {
      landEstimate();
      await Promise.all(impl.mock.results.map((result) => result.value));
    });

    // A second document, and the lever — which `canSubmit` arms on a terminal
    // status — WITHOUT pressing "Toast another slice" first. The POST fails.
    postFails = true;
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile('second.docx')] },
    });
    fireEvent.click(await screen.findByTestId('review-submit-button'));

    // No review on screen: a pre-submission screen for the second document.
    await waitFor(() => expect(consoleStatus()).toBe('loaded'));
    await waitFor(() => expect(priceText()).toContain('$0.79'));
    expect(priceText()).toContain('Estimated review cost');
    expect(priceText()).not.toContain('Estimate unavailable');
  });
});
