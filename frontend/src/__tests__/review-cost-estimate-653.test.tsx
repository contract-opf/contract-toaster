/**
 * review-cost-estimate-653.test.tsx — the per-review cost line on the Review
 * tab (issue #653 Scope item 2, epic #649).
 *
 * Scope item 2 asks for "estimated cost of a review with the currently
 * selected models, shown where a reviewer can see it BEFORE submitting".
 * The rest of #653 lands on the admin-gated Settings tab, and
 * `AdminSettings.tsx` renders nothing at all on the 403 a non-admin gets from
 * `GET /api/admin/spend` — so an estimate that lived only there would be
 * invisible to exactly the audience this item names. The claims:
 *
 *   1. It renders on the SUBMISSION form, with no file chosen and nothing
 *      submitted — i.e. while the decision is still open. A number that only
 *      appeared after upload would answer the question too late.
 *   2. It comes from `GET /api/review-cost-estimate`, the non-admin route.
 *      Asserted on the URL actually fetched, so a later "just read the admin
 *      ledger" refactor fails here rather than silently 403ing every reviewer.
 *   3. It RENDERS THE SERVER'S NUMBER, asserted at two different values, so a
 *      hardcoded string could not pass. The wire is cents; the copy is money.
 *   4. A failed or absent read renders nothing and BLOCKS NOTHING — the form
 *      still submits. The price is context for a decision, not a precondition
 *      for making it.
 *   5. A figure that is not money (null on a provider with no per-review
 *      basis, zero, negative, a string) renders nothing rather than "$NaN" or
 *      a claim that a review is free.
 *
 * The copy is asserted NOT to read as a ceiling: ARCHITECTURE.md -> Cost
 * shape documents that settlement reconciles spend in both directions, and
 * this figure is the EXPECTED cost of an ordinary document, which a longer
 * one exceeds. "Up to" or "maximum" would be a promise it does not make.
 *
 * Asserts on rendered text / testids only — never computed styles
 * (vitest.config.ts runs jsdom with `css: false`). Fully offline: Amplify
 * auth is mocked and fetch is stubbed per test.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { DEFAULT_PLAYBOOKS, pressSubmit } from './support/consoleSurface';

/** No price is CLAIMED — the register says so with an em dash rather than by
 *  rendering nothing. Issue #733. */
function expectsNoPrice(label?: string): void {
  expect(screen.getByTestId('review-cost-estimate').textContent ?? '', label).not.toMatch(
    /\$/,
  );
}

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const ESTIMATE_PATH = '/api/review-cost-estimate';

/** Every route the form reads on mount, plus whatever a test overrides. An
 *  unlisted route answers 404 — which is also the shape "this deployment has
 *  no such route" takes against a backend older than this bundle. */
function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
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

function baseRoutes(estimate: unknown): Record<string, unknown> {
  const routes: Record<string, unknown> = {
    // Issue #733: the console will not arm its lever without an active
    // playbook, and this file is about the price, not the catalog.
    '/api/playbooks': DEFAULT_PLAYBOOKS,
    'GET /api/me/preferences': { preferences: {}, notes_mode_available: false },
    'POST /api/reviews': { review_id: 'rev-c1', resumed: false },
    'GET /api/reviews/rev-c1': {
      review_id: 'rev-c1',
      status: 'PENDING',
      decision: null,
      message: null,
      has_output: false,
    },
  };
  if (estimate !== undefined) {
    routes[`GET ${ESTIMATE_PATH}`] = estimate;
  }
  return routes;
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

function estimateText(): string | null {
  return screen.queryByTestId('review-cost-estimate')?.textContent ?? null;
}

function fetchedPaths(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls.map(([input]) => new URL(String(input), 'http://localhost').pathname);
}

/** Submissions only. `GET /api/reviews?scope=mine` is the History read the
 *  form makes on mount, so a path-only check would call an untouched form
 *  "already submitted". */
function submitCount(fetchMock: ReturnType<typeof vi.fn>): number {
  return fetchMock.mock.calls.filter(
    ([input, init]) =>
      new URL(String(input), 'http://localhost').pathname === '/api/reviews' &&
      ((init as RequestInit | undefined)?.method ?? 'GET').toUpperCase() === 'POST',
  ).length;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('the reviewer sees what the next review costs, before submitting (#653)', () => {
  it('renders the estimate from the non-admin route with nothing submitted yet', async () => {
    const fetchMock = stubFetch(
      baseRoutes({ estimated_usd_cents: 79, worst_case_reservation_usd_cents: 686 }),
    );
    render(<ReviewSubmission />);

    await screen.findByTestId('review-cost-estimate');
    // Still on the empty form: no file chosen, no POST made. The whole point
    // of Scope item 2 is that this is readable while the decision is open.
    expect(submitCount(fetchMock)).toBe(0);
    expect(fetchedPaths(fetchMock)).toContain(ESTIMATE_PATH);
    expect(estimateText()).toContain('$0.79');
  });

  it('does not word it as a ceiling on the bill', async () => {
    stubFetch(baseRoutes({ estimated_usd_cents: 79, worst_case_reservation_usd_cents: 686 }));
    render(<ReviewSubmission />);

    await screen.findByTestId('review-cost-estimate');
    // It is the EXPECTED cost of an ordinary document, and the console's
    // readback line says so (issue #733). What it may not do is word it as a
    // ceiling.
    const text = (document.body.textContent ?? '').toLowerCase();
    expect(text).toMatch(/typical document length/);
    expect(text).not.toContain('up to');
    expect(text).not.toContain('maximum');
    expect(text).not.toContain('at most');
  });

  it('renders the number the server sent, not a constant', async () => {
    stubFetch(baseRoutes({ estimated_usd_cents: 110 }));
    const first = render(<ReviewSubmission />);
    await screen.findByTestId('review-cost-estimate');
    expect(estimateText()).toContain('$1.10');
    first.unmount();

    // A dearer model selection is, from the client's side, exactly this: a
    // different number on the same route.
    stubFetch(baseRoutes({ estimated_usd_cents: 1954 }));
    render(<ReviewSubmission />);
    await waitFor(() => expect(estimateText()).toContain('$19.54'));
  });

  it('renders nothing — and blocks nothing — when the read fails', async () => {
    const fetchMock = stubFetch(baseRoutes(undefined));
    render(<ReviewSubmission />);
    // Wait for a read the form definitely makes, so "nothing rendered" is a
    // settled state rather than a race with the mount effects.
    await waitFor(() => expect(fetchedPaths(fetchMock)).toContain(ESTIMATE_PATH));

    expectsNoPrice();
    // No error banner for it either: a price that did not load is not an
    // error the reviewer can act on.
    expect(screen.queryByTestId('review-submit-error')).toBeNull();

    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await pressSubmit();
    await waitFor(() => expect(submitCount(fetchMock)).toBe(1));
  });

  it('renders nothing for a figure that is not money', async () => {
    // `null` is the real shape on a provider with no per-review token basis
    // (the Bedrock path); the rest are the shapes a skewed or broken backend
    // produces. None of them may become "$0.00" or "$NaN".
    for (const bad of [null, 0, -1, 'lots']) {
      const fetchMock = stubFetch(baseRoutes({ estimated_usd_cents: bad }));
      const view = render(<ReviewSubmission />);
      await waitFor(() => expect(fetchedPaths(fetchMock)).toContain(ESTIMATE_PATH));
      expectsNoPrice(String(bad));
      view.unmount();
    }
  });

  it('still shows that figure once the review has finished, and calls it an estimate', async () => {
    // Issue #735 (owner decision H2). Both surfaces answer "what did this cost?"
    // with the estimate captured for the review — the old tree because its line
    // is the estimate and nothing else, the console because a terminal status no
    // longer relabels the glass "Final review cost" and blanks the figure. What
    // neither may do is claim a settled bill this deployment cannot compute:
    // there is no settlement projection, so there is no final cost to report.
    const routes = baseRoutes({ estimated_usd_cents: 79 });
    routes['GET /api/reviews/rev-c1'] = {
      review_id: 'rev-c1',
      status: 'DONE',
      decision: 'ACCEPT',
      message: null,
      has_output: false,
      has_input: true,
    };
    const fetchMock = stubFetch(routes);
    render(<ReviewSubmission />);
    await waitFor(() => expect(estimateText()).toContain('$0.79'));

    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await pressSubmit();
    await waitFor(() => expect(submitCount(fetchMock)).toBe(1));
    // Terminal by the console's own marker, and without opening anything: a
    // finished review's facts sit behind an overlay, and this test is about
    // the register, which is on the page regardless.
    await waitFor(() => {
      expect(document.querySelector('.od-console')?.getAttribute('data-terminal')).toBe(
        'true',
      );
    });

    await waitFor(() => expect(estimateText()).toContain('$0.79'));
    expect((estimateText() ?? '').toLowerCase()).not.toContain('final review cost');
  });
});
