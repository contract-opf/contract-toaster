/**
 * review-history.test.tsx — the History tab (ReviewHistory.tsx, issue #449).
 *
 * The tab exists so a user can answer, about a review that finished days ago:
 * *what did you produce, and how did you produce it?* So the tests that matter
 * are about the honesty of that record, not about chrome:
 *
 *   - **"Not recorded" is never a guess.** A review that ran before the
 *     per-step model ids were persisted must render an explicit "not
 *     recorded" — never today's configured model, and never a blank cell that
 *     reads as "no model". The same rule covers the playbook version.
 *   - **A purged document is an explicit dead end, not a dead link.** When the
 *     backend answers 410 Gone, the row must SAY the file is gone and keep
 *     saying it; handing the browser a URL that 404s is the failure mode this
 *     ticket forbids.
 *   - **The instructions that governed each review are reachable** — via a
 *     per-row expander, because guidance is free text that does not belong
 *     squeezed into a table cell.
 *   - **Newest first, as the server ordered them.** The screen must not
 *     re-sort; `created_at` is a string epoch on the row and a client-side
 *     sort would quietly get it wrong.
 *   - **Owner-scoped by request.** The screen asks for `?scope=mine` so an
 *     admin opening History sees their own, not every user's.
 *   - the empty state is an IN-TABLE row (`.ct-table__empty`,
 *     AdminUsers.tsx's convention, not AdminRetention.tsx's divergent one);
 *   - a failed load is TERMINAL — an error plus a working retry, never an
 *     error and a spinner at once (#439) — and its copy carries no HTTP
 *     status code or endpoint (#425).
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ReviewHistory, { HistoryRow } from '../ReviewHistory';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

// The model configured "today". A historic row must never be relabelled with
// it — the assertion this file exists for.
const TODAYS_MODEL = 'anthropic/claude-opus-4.8';

const MODERN: HistoryRow = {
  review_id: 'rev-modern',
  playbook_id: 'synthetic-nda-sample',
  status: 'DONE',
  decision: 'REQUEST_CHANGE',
  created_at: '1800000200',
  updated_at: '1800000300',
  policy_version: 3,
  posture_version: 2,
  primary_model_id: 'vendor/primary-of-that-day',
  critic_model_id: 'vendor/critic-of-that-day',
  has_output: true,
  has_input: true,
};

const HISTORIC: HistoryRow = {
  review_id: 'rev-historic',
  playbook_id: 'synthetic-nda-sample',
  status: 'DONE',
  decision: 'ACCEPT',
  created_at: '1700000000',
  updated_at: '1700000100',
  policy_version: null,
  posture_version: null,
  primary_model_id: null,
  critic_model_id: null,
  has_output: false,
  has_input: false,
};

interface StubbedCall {
  status: number;
  body: unknown;
}

let anchorClickSpy: ReturnType<typeof vi.spyOn>;
let fetchMock: ReturnType<typeof vi.fn>;

/**
 * Route-aware fetch stub. Keyed on the request path so the component's own
 * call sequence (list first, then per-row detail/presign on demand) is
 * exercised rather than assumed.
 */
function stubRoutes(routes: Record<string, StubbedCall | StubbedCall[]>): void {
  const cursors: Record<string, number> = {};
  fetchMock = vi.fn(async (url: string) => {
    const path = String(url);
    const key = Object.keys(routes).find((candidate) => path.includes(candidate));
    if (!key) {
      throw new Error(`unstubbed request: ${path}`);
    }
    const entry = routes[key]!;
    const list = Array.isArray(entry) ? entry : [entry];
    const index = Math.min(cursors[key] ?? 0, list.length - 1);
    cursors[key] = index + 1;
    const response = list[index]!;
    return {
      ok: response.status >= 200 && response.status < 300,
      status: response.status,
      json: async () => response.body,
    };
  });
  vi.stubGlobal('fetch', fetchMock);
}

function listOf(...rows: HistoryRow[]): StubbedCall {
  return { status: 200, body: { reviews: rows } };
}

beforeEach(() => {
  vi.restoreAllMocks();
  anchorClickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('History — the provenance record', () => {
  it('asks for the caller’s OWN history, newest first, and renders it in that order', async () => {
    stubRoutes({ '/api/reviews': listOf(MODERN, HISTORIC) });
    render(<ReviewHistory />);

    await screen.findByTestId('history-row-rev-modern');

    // Owner-scoped by request: an admin opening History sees their own.
    const requested = String(fetchMock.mock.calls[0]![0]);
    expect(requested).toContain('scope=mine');

    // Server order preserved — the screen must not re-sort.
    const rows = screen.getAllByTestId(/^history-row-/);
    expect(rows.map((row) => row.getAttribute('data-testid'))).toEqual([
      'history-row-rev-modern',
      'history-row-rev-historic',
    ]);
  });

  it('names the models that ran each step', async () => {
    stubRoutes({ '/api/reviews': listOf(MODERN) });
    render(<ReviewHistory />);

    const cell = await screen.findByTestId('history-models-rev-modern');
    expect(cell.textContent).toContain('vendor/primary-of-that-day');
    expect(cell.textContent).toContain('vendor/critic-of-that-day');
  });

  it('says "not recorded" for a review that predates the model fields — never today’s model', async () => {
    stubRoutes({ '/api/reviews': listOf(HISTORIC) });
    render(<ReviewHistory />);

    const cell = await screen.findByTestId('history-models-rev-historic');
    expect(cell.textContent?.toLowerCase()).toContain('not recorded');
    // The load-bearing negative: no model id may be invented for this row.
    expect(cell.textContent).not.toContain(TODAYS_MODEL);
    expect(cell.textContent).not.toContain('vendor/');
    // And the cell is not merely blank, which would read as "no model ran".
    expect(cell.textContent?.trim()).not.toBe('');
  });

  it('shows the playbook and its version, and says so when no version was recorded', async () => {
    stubRoutes({ '/api/reviews': listOf(MODERN, HISTORIC) });
    render(<ReviewHistory />);

    const modern = await screen.findByTestId('history-playbook-rev-modern');
    expect(modern.textContent).toContain('synthetic-nda-sample');
    expect(modern.textContent).toContain('3');

    const historic = screen.getByTestId('history-playbook-rev-historic');
    expect(historic.textContent).toContain('synthetic-nda-sample');
    expect(historic.textContent?.toLowerCase()).toContain('not recorded');
  });

  it('shows when each review was toasted', async () => {
    stubRoutes({ '/api/reviews': listOf(MODERN) });
    render(<ReviewHistory />);

    const cell = await screen.findByTestId('history-toasted-rev-modern');
    expect(cell.textContent?.trim()).not.toBe('');
    expect(cell.textContent).not.toContain('Invalid Date');
  });
});

describe('History — the instructions that governed a review', () => {
  it('reveals the guidance on demand, from the review’s own detail record', async () => {
    stubRoutes({
      '/api/reviews?': listOf(MODERN),
      '/api/reviews/rev-modern': {
        status: 200,
        body: { review_id: 'rev-modern', toaster_guidance: 'Be lenient on payment terms.' },
      },
    });
    render(<ReviewHistory />);

    await screen.findByTestId('history-row-rev-modern');
    // Free text is not squeezed into a cell — it is not on screen until asked for.
    expect(screen.queryByText(/Be lenient on payment terms\./)).toBeNull();

    fireEvent.click(screen.getByTestId('history-guidance-toggle-rev-modern'));

    const panel = await screen.findByTestId('history-guidance-rev-modern');
    expect(panel.textContent).toContain('Be lenient on payment terms.');
  });

  it('says so plainly when a review carried no instructions', async () => {
    stubRoutes({
      '/api/reviews?': listOf(MODERN),
      '/api/reviews/rev-modern': {
        status: 200,
        body: { review_id: 'rev-modern', toaster_guidance: null },
      },
    });
    render(<ReviewHistory />);

    await screen.findByTestId('history-row-rev-modern');
    fireEvent.click(screen.getByTestId('history-guidance-toggle-rev-modern'));

    const panel = await screen.findByTestId('history-guidance-rev-modern');
    expect(panel.textContent?.toLowerCase()).toContain('no instructions');
  });

  /**
   * A failed guidance fetch must not be terminal.
   *
   * The screen caches guidance per review so reopening an expander does not
   * re-hit the API for a record that cannot change — correct for a LOADED
   * record, wrong for a FAILED one. Treating a cached failure as "already
   * loaded" makes one transient 500 permanent: the instructions for that row
   * become unreachable for the life of the page, which is exactly the
   * terminal dead end the main load deliberately avoids with its retry (and
   * the failure mode #439 flags on sibling screens).
   */
  it('refetches the instructions when a failed attempt is reopened, instead of caching the error', async () => {
    stubRoutes({
      '/api/reviews?': listOf(MODERN),
      '/api/reviews/rev-modern': [
        { status: 500, body: {} },
        { status: 200, body: { review_id: 'rev-modern', toaster_guidance: 'Second time lucky.' } },
      ],
    });
    render(<ReviewHistory />);

    await screen.findByTestId('history-row-rev-modern');
    fireEvent.click(screen.getByTestId('history-guidance-toggle-rev-modern'));

    const failed = await screen.findByTestId('history-guidance-rev-modern');
    await waitFor(() => expect(failed.textContent?.toLowerCase()).toContain("couldn't load"));

    // Collapse, then reopen: the second open must go back to the network.
    fireEvent.click(screen.getByTestId('history-guidance-toggle-rev-modern'));
    fireEvent.click(screen.getByTestId('history-guidance-toggle-rev-modern'));

    const recovered = await screen.findByTestId('history-guidance-rev-modern');
    await waitFor(() => expect(recovered.textContent).toContain('Second time lucky.'));
  });

  it('offers a working retry inside the expander when the instructions fail to load', async () => {
    stubRoutes({
      '/api/reviews?': listOf(MODERN),
      '/api/reviews/rev-modern': [
        { status: 500, body: {} },
        { status: 200, body: { review_id: 'rev-modern', toaster_guidance: 'Recovered in place.' } },
      ],
    });
    render(<ReviewHistory />);

    await screen.findByTestId('history-row-rev-modern');
    fireEvent.click(screen.getByTestId('history-guidance-toggle-rev-modern'));

    // The failure is recoverable WITHOUT collapsing — a user who leaves the
    // expander open still has a way forward.
    const retry = await screen.findByTestId('history-guidance-retry-rev-modern');
    fireEvent.click(retry);

    const panel = await screen.findByTestId('history-guidance-rev-modern');
    await waitFor(() => expect(panel.textContent).toContain('Recovered in place.'));
    expect(screen.queryByTestId('history-guidance-retry-rev-modern')).toBeNull();
  });
});

describe('History — re-downloading past work', () => {
  it('hands the redline to the browser through a temporary anchor', async () => {
    stubRoutes({
      '/api/reviews?': listOf(MODERN),
      '/output': { status: 200, body: { url: 'https://example.test/presigned-redline' } },
    });
    render(<ReviewHistory />);

    await screen.findByTestId('history-row-rev-modern');
    fireEvent.click(screen.getByTestId('history-download-output-rev-modern'));

    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalledTimes(1));
  });

  it('hands the input document to the browser the same way', async () => {
    stubRoutes({
      '/api/reviews?': listOf(MODERN),
      '/input': { status: 200, body: { url: 'https://example.test/presigned-input' } },
    });
    render(<ReviewHistory />);

    await screen.findByTestId('history-row-rev-modern');
    fireEvent.click(screen.getByTestId('history-download-input-rev-modern'));

    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalledTimes(1));
  });

  it('offers no redline download for a review that produced none', async () => {
    stubRoutes({ '/api/reviews': listOf(HISTORIC) });
    render(<ReviewHistory />);

    await screen.findByTestId('history-row-rev-historic');
    expect(screen.queryByTestId('history-download-output-rev-historic')).toBeNull();
    const actions = screen.getByTestId('history-actions-rev-historic');
    expect(actions.textContent?.toLowerCase()).toContain('no redline');
  });

  it('renders a purged document as explicitly unavailable, and downloads nothing', async () => {
    stubRoutes({
      '/api/reviews?': listOf(MODERN),
      '/input': { status: 410, body: { detail: 'This document is no longer available.' } },
    });
    render(<ReviewHistory />);

    await screen.findByTestId('history-row-rev-modern');
    fireEvent.click(screen.getByTestId('history-download-input-rev-modern'));

    const message = await screen.findByTestId('history-action-message-rev-modern');
    expect(message.textContent?.toLowerCase()).toContain('no longer available');
    // A dead link is the failure mode this ticket forbids: nothing was handed
    // to the browser.
    expect(anchorClickSpy).not.toHaveBeenCalled();
    // And the unavailable state PERSISTS — it is not a flash the user misses.
    expect(screen.getByTestId('history-action-message-rev-modern')).toBeTruthy();
  });
});

describe('History — load states', () => {
  it('renders an in-table empty row when there is no history yet', async () => {
    stubRoutes({ '/api/reviews': listOf() });
    render(<ReviewHistory />);

    const empty = await screen.findByTestId('review-history-empty');
    expect(empty.tagName).toBe('TD');
    expect(empty.className).toContain('ct-table__empty');
    // The empty state lives INSIDE the table (AdminUsers.tsx's convention).
    const table = screen.getByTestId('history-table');
    expect(within(table).getByTestId('review-history-empty')).toBeTruthy();
  });

  it('a failed load is terminal: an error and a working retry, never a spinner too', async () => {
    stubRoutes({
      '/api/reviews': [
        { status: 500, body: {} },
        listOf(MODERN),
      ],
    });
    render(<ReviewHistory />);

    const error = await screen.findByTestId('review-history-error');
    expect(screen.queryByTestId('review-history-loading')).toBeNull();

    // #425: no HTTP status code, no endpoint path in rendered copy.
    expect(error.textContent).not.toMatch(/\b[45]\d\d\b/);
    expect(error.textContent).not.toContain('/api/');

    fireEvent.click(screen.getByTestId('review-history-retry'));
    expect(await screen.findByTestId('history-row-rev-modern')).toBeTruthy();
    expect(screen.queryByTestId('review-history-error')).toBeNull();
  });
});
