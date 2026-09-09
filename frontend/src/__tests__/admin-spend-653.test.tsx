/**
 * admin-spend-653.test.tsx — the spend card on the Settings tab
 * (AdminSettings.tsx, issue #653, epic #649).
 *
 * ## What this ticket asks for
 *
 * "Show spend on the Settings tab: the daily cap, what has been spent today,
 * what is left, and the estimated cost of the next review" — and move the cap
 * itself into the app so it can be adjusted without editing a prod compose
 * file that also holds plaintext secrets (epic #649's evidence).
 *
 * So the assertions here are, in order of what would actually regress:
 *
 *   - every figure on the card is the SERVER's, rendered as money. A screen
 *     that recomputed "remaining" or "would the next review be admitted"
 *     client-side could disagree with `reserve_spend`'s own condition, which
 *     is the one thing this card must never do. The payloads below are
 *     therefore internally INCONSISTENT on purpose in one test — a client
 *     doing its own arithmetic would print its answer, not the server's;
 *   - the next-review figure TRACKS THE MODEL SELECTION: a payload priced
 *     from a dearer pair renders a bigger number (#653's own verification
 *     line, "change the models and the number must change");
 *   - the refusal notice flips with the server's boolean, in BOTH directions,
 *     and says what to do about it;
 *   - the cap SAVE posts CENTS (the wire format) from a DOLLARS field, and
 *     re-reads the ledger afterwards rather than trusting its own echo —
 *     because "what is left" and "would the next review run" are both
 *     functions of the cap that was just changed;
 *   - a mistyped cap is refused CLIENT-SIDE rather than posted, because
 *     `null` on the wire means "revert to the deployment's cap": posting a
 *     NaN would be a silent cap change wearing a typo's clothes;
 *   - clearing is a distinct intent — it posts null, so the deployment's own
 *     value keeps applying as it changes instead of freezing today's copy;
 *   - a deployment with no settings store gets an explanation, not a dead
 *     form (`cap_store_available: false` — the AWS target);
 *   - a 403 hides the whole panel, and a failed spend load is terminal: an
 *     error plus the shared retry, never an error and a spinner at once
 *     (#439), with no HTTP status or endpoint in the copy (#425).
 *
 * Every stubbed body is `GET /api/admin/spend`'s real shape, as
 * `backend/src/admin_dashboard.py::get_spend_ledger` returns it — which is
 * why the cap arrives under `cap_setting` and the day's billed total under
 * `today.settled_usd_cents` rather than as flat fields.
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import AdminSettings, {
  capSourceNote,
  formatUsdCents,
  parseCapDollars,
  reviewsRemaining,
  type SpendCapSetting,
  type SpendLedger,
} from '../AdminSettings';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const PREFERENCES = { preferences: { notes_mode: 'external' }, notes_mode_available: true };

const MODEL_KEY = {
  setting_id: 'global',
  key_store_available: true,
  model_provider: 'openrouter',
  key_set: false,
  key_source: null,
  key_fingerprint: '',
  updated_at: '',
  updated_by: '',
};

const VERSION = {
  version: '0.0.1',
  commit: 'abcdef1234567890',
  image_digest: 'sha256:x',
  uptime_seconds: 1,
};

const ROSTER = {
  setting_id: 'global',
  roster_store_available: false,
  entities: [],
  max_entities: 50,
  max_name_length: 200,
  updated_at: '',
  updated_by: '',
};

interface CapOverrides {
  cap_setting?: Partial<SpendCapSetting>;
  today?: { settled_usd_cents?: number };
  [key: string]: unknown;
}

/**
 * A ledger body in the route's real shape. $20.00 cap, nothing spent, a review
 * that reserves $6.86 and is expected to cost $0.79 — the two figures
 * ARCHITECTURE.md -> Cost shape and model-policy/openrouter.json give as of
 * 2026-09-04, used here purely as a plausible order of magnitude.
 */
function ledgerBody(overrides: CapOverrides = {}): Record<string, unknown> {
  const { cap_setting: capOverrides, today: todayOverrides, ...rest } = overrides;
  return {
    today: { reserved_usd_cents: 0, settled_usd_cents: 0, ...(todayOverrides ?? {}) },
    days: [],
    window_days: 1,
    daily_cap_usd_cents: 2000,
    daily_cap_source: 'env',
    spent_today_usd_cents: 0,
    remaining_usd_cents: 2000,
    worst_case_reservation_usd_cents: 686,
    estimated_review_usd_cents: 79,
    next_review_admissible: true,
    cap_setting: {
      setting_id: 'spend',
      cap_store_available: true,
      stored_daily_cap_usd_cents: null,
      env_daily_cap_usd_cents: 2000,
      default_daily_cap_usd_cents: 2000,
      daily_cap_usd_cents: 2000,
      daily_cap_source: 'env',
      min_daily_cap_usd_cents: 1,
      max_daily_cap_usd_cents: 1000000,
      updated_at: '',
      updated_by: '',
      ...(capOverrides ?? {}),
    },
    ...rest,
  };
}

interface RouteAnswer {
  status: number;
  body: unknown;
}

/**
 * Route-aware fetch stub. The panel makes five calls; answering them all with
 * one body would hide exactly the bug this file exists for — a screen that
 * renders the wrong payload's fields.
 */
function stubRoutes(
  responses: Record<string, RouteAnswer | RouteAnswer[]>,
): ReturnType<typeof vi.fn> {
  const counts: Record<string, number> = {};
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${init?.method ?? 'GET'} ${pathname}`;
    const entry = responses[key] ?? responses[pathname];
    if (entry === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    const sequence = Array.isArray(entry) ? entry : [entry];
    const index = Math.min(counts[key] ?? 0, sequence.length - 1);
    counts[key] = (counts[key] ?? 0) + 1;
    const chosen = sequence[index];
    return {
      ok: chosen.status >= 200 && chosen.status < 300,
      status: chosen.status,
      json: async () => chosen.body,
    } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl as unknown as ReturnType<typeof vi.fn>;
}

function stubOk(
  spend: RouteAnswer | RouteAnswer[],
  extra: Record<string, RouteAnswer | RouteAnswer[]> = {},
): ReturnType<typeof vi.fn> {
  return stubRoutes({
    '/api/me/preferences': { status: 200, body: PREFERENCES },
    '/api/admin/model-key': { status: 200, body: MODEL_KEY },
    '/api/admin/entity-roster': { status: 200, body: ROSTER },
    '/version': { status: 200, body: VERSION },
    '/api/admin/spend': spend,
    ...extra,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// The pure helpers — the claims pinned without pinning a sentence of layout.
// ---------------------------------------------------------------------------

describe('the money helpers (#653)', () => {
  it('renders whole cents as two-decimal dollars', () => {
    expect(formatUsdCents(2000)).toBe('$20.00');
    expect(formatUsdCents(686)).toBe('$6.86');
    expect(formatUsdCents(0)).toBe('$0.00');
    expect(formatUsdCents(1)).toBe('$0.01');
  });

  it('counts the remaining reviews against the WORST case, not the expected cost', () => {
    // 1000c left; a review is expected to cost 79c but RESERVES 686c, and the
    // reservation is what a submission is refused against. Counting against
    // the expected cost would promise 12 reviews the gate would decline.
    const remaining = reviewsRemaining({
      remaining_usd_cents: 1000,
      worst_case_reservation_usd_cents: 686,
      estimated_review_usd_cents: 79,
    } as SpendLedger);
    expect(remaining).toBe(1);
  });

  it('says nothing rather than Infinity when a review reserves nothing', () => {
    expect(
      reviewsRemaining({
        remaining_usd_cents: 1000,
        worst_case_reservation_usd_cents: 0,
      } as SpendLedger),
    ).toBeNull();
  });

  it('names where the cap in force came from, in all three cases', () => {
    const base = {
      env_daily_cap_usd_cents: 2000,
      default_daily_cap_usd_cents: 2000,
    } as SpendCapSetting;
    expect(capSourceNote({ ...base, daily_cap_source: 'admin' })).toContain('Set here');
    expect(capSourceNote({ ...base, daily_cap_source: 'admin' })).toContain('$20.00');
    expect(capSourceNote({ ...base, daily_cap_source: 'env' })).toContain(
      'deployment environment',
    );
    expect(capSourceNote({ ...base, daily_cap_source: 'default' })).toContain('built-in default');
  });

  it('parses a dollars field into cents, and refuses anything that is not a cap', () => {
    expect(parseCapDollars('20')).toBe(2000);
    expect(parseCapDollars('12.50')).toBe(1250);
    expect(parseCapDollars(' $7.05 ')).toBe(705);
    // Each of these would otherwise post a NaN — and `null` on the wire means
    // "revert to the deployment's cap", so a NaN is a silent cap change.
    for (const bad of ['', '   ', 'twenty', '20.001', '-5', '1e3', '20,00']) {
      expect(parseCapDollars(bad)).toBeNull();
    }
  });
});

// ---------------------------------------------------------------------------
// The rendered card.
// ---------------------------------------------------------------------------

describe('the spend card renders the server’s figures (#653)', () => {
  it('shows the cap, what is committed, what is billed, what is left and the next review', async () => {
    // Deliberately INCONSISTENT: 5000 − 1234 is 3766, but the server says
    // 3700. A card doing its own arithmetic would print 3766 and would then
    // be free to disagree with `reserve_spend`'s condition, which is the one
    // thing it must never do.
    stubOk({
      status: 200,
      body: ledgerBody({
        daily_cap_usd_cents: 5000,
        spent_today_usd_cents: 1234,
        remaining_usd_cents: 3700,
        worst_case_reservation_usd_cents: 686,
        estimated_review_usd_cents: 79,
        today: { settled_usd_cents: 42 },
      }),
    });
    render(<AdminSettings />);

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-spend-panel')).toBeInTheDocument();
    });
    expect(screen.getByTestId('spend-value-cap')).toHaveTextContent('$50.00');
    expect(screen.getByTestId('spend-value-spent')).toHaveTextContent('$12.34');
    expect(screen.getByTestId('spend-value-settled')).toHaveTextContent('$0.42');
    expect(screen.getByTestId('spend-value-remaining')).toHaveTextContent('$37.00');
    expect(screen.getByTestId('spend-value-next')).toHaveTextContent('$0.79');
    // The reservation is stated too — it is the figure that can refuse a
    // submission, and the expected cost alone would understate that by ~9x.
    expect(screen.getByTestId('spend-meaning-next')).toHaveTextContent('$6.86');
  });

  it('the next-review figures move when the model selection changes the pricing', async () => {
    stubOk({
      status: 200,
      body: ledgerBody({
        worst_case_reservation_usd_cents: 4400,
        estimated_review_usd_cents: 510,
      }),
    });
    render(<AdminSettings />);

    await waitFor(() => {
      expect(screen.getByTestId('spend-value-next')).toHaveTextContent('$5.10');
    });
    expect(screen.getByTestId('spend-meaning-next')).toHaveTextContent('$44.00');
  });

  it('shows an em dash rather than a number when the provider carries no basis', async () => {
    // The Bedrock path: `estimated_review_usd_cents` is null. Rendering a 0
    // there would claim a review is free.
    stubOk({ status: 200, body: ledgerBody({ estimated_review_usd_cents: null }) });
    render(<AdminSettings />);

    await waitFor(() => {
      expect(screen.getByTestId('spend-value-next')).toHaveTextContent('—');
    });
    expect(screen.getByTestId('spend-value-next')).not.toHaveTextContent('$0.00');
  });

  it('warns when the server says the next review would be refused, and not before', async () => {
    stubOk({ status: 200, body: ledgerBody({ next_review_admissible: true }) });
    const { unmount } = render(<AdminSettings />);
    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-spend-panel')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('admin-settings-spend-blocked')).toBeNull();
    unmount();

    vi.unstubAllGlobals();
    stubOk({
      status: 200,
      body: ledgerBody({
        next_review_admissible: false,
        spent_today_usd_cents: 1900,
        remaining_usd_cents: 100,
      }),
    });
    render(<AdminSettings />);
    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-spend-blocked')).toBeInTheDocument();
    });
    // Says what to do about it, not merely that something is wrong.
    expect(screen.getByTestId('admin-settings-spend-blocked')).toHaveTextContent(/Raise the cap/i);
  });
});

// ---------------------------------------------------------------------------
// Changing the cap.
// ---------------------------------------------------------------------------

describe('the cap is settable from the app (#653)', () => {
  it('posts CENTS from a dollars field and re-reads the ledger afterwards', async () => {
    const fetchMock = stubOk(
      [
        { status: 200, body: ledgerBody() },
        // The re-read after the save: the new cap and the headroom it implies.
        {
          status: 200,
          body: ledgerBody({
            daily_cap_usd_cents: 4500,
            remaining_usd_cents: 4500,
            cap_setting: {
              stored_daily_cap_usd_cents: 4500,
              daily_cap_usd_cents: 4500,
              daily_cap_source: 'admin',
            },
          }),
        },
      ],
      { 'POST /api/admin/spend-cap': { status: 200, body: {} } },
    );
    render(<AdminSettings />);

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-cap-input')).toBeInTheDocument();
    });
    // The field starts on the cap in force, in dollars.
    expect(screen.getByTestId('admin-settings-cap-input')).toHaveValue('20.00');

    fireEvent.change(screen.getByTestId('admin-settings-cap-input'), {
      target: { value: '45' },
    });
    fireEvent.click(screen.getByTestId('admin-settings-cap-save'));

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-cap-saved')).toBeInTheDocument();
    });
    const post = fetchMock.mock.calls.find(
      (call) => (call[1] as RequestInit | undefined)?.method === 'POST',
    );
    expect(post).toBeDefined();
    expect(JSON.parse((post?.[1] as RequestInit).body as string)).toEqual({
      daily_cap_usd_cents: 4500,
    });

    // Re-read, not an echo of what we sent: the figures the cap changes have
    // to come back from the server that now enforces it.
    const ledgerReads = fetchMock.mock.calls.filter(
      (call) =>
        String(call[0]).includes('/api/admin/spend?') &&
        (call[1] as RequestInit | undefined)?.method !== 'POST',
    );
    expect(ledgerReads.length).toBeGreaterThanOrEqual(2);
    expect(screen.getByTestId('spend-value-cap')).toHaveTextContent('$45.00');
    expect(screen.getByTestId('spend-value-remaining')).toHaveTextContent('$45.00');
  });

  it('refuses a mistyped cap client-side instead of posting it', async () => {
    const fetchMock = stubOk(
      { status: 200, body: ledgerBody() },
      { 'POST /api/admin/spend-cap': { status: 200, body: {} } },
    );
    render(<AdminSettings />);

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-cap-input')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('admin-settings-cap-input'), {
      target: { value: 'twenty quid' },
    });
    fireEvent.click(screen.getByTestId('admin-settings-cap-save'));

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-cap-save-error')).toBeInTheDocument();
    });
    // Nothing was sent. `null` on the wire MEANS "revert to the deployment's
    // cap", so posting a NaN would have been a silent cap change.
    expect(
      fetchMock.mock.calls.some((call) => (call[1] as RequestInit | undefined)?.method === 'POST'),
    ).toBe(false);
  });

  it('clearing posts null, so the deployment’s own cap keeps applying as it changes', async () => {
    const fetchMock = stubOk(
      {
        status: 200,
        body: ledgerBody({
          daily_cap_usd_cents: 4500,
          cap_setting: {
            stored_daily_cap_usd_cents: 4500,
            daily_cap_usd_cents: 4500,
            daily_cap_source: 'admin',
          },
        }),
      },
      { 'POST /api/admin/spend-cap': { status: 200, body: {} } },
    );
    render(<AdminSettings />);

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-cap-clear')).toBeInTheDocument();
    });
    // The button names the value that will take over, so clearing is not a
    // leap in the dark.
    expect(screen.getByTestId('admin-settings-cap-clear')).toHaveTextContent('$20.00');

    fireEvent.click(screen.getByTestId('admin-settings-cap-clear'));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        (call) => (call[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(post).toBeDefined();
      expect(JSON.parse((post?.[1] as RequestInit).body as string)).toEqual({
        daily_cap_usd_cents: null,
      });
    });
  });

  it('offers no clear button when nothing is stored — there is nothing to clear', async () => {
    stubOk({ status: 200, body: ledgerBody() });
    render(<AdminSettings />);

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-cap-input')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('admin-settings-cap-clear')).toBeNull();
  });

  it('shows the server’s own 400 detail rather than replacing it', async () => {
    stubOk(
      { status: 200, body: ledgerBody() },
      {
        'POST /api/admin/spend-cap': {
          status: 400,
          body: { detail: 'daily_cap_usd_cents must be between 1 and 1000000 cents.' },
        },
      },
    );
    render(<AdminSettings />);

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-cap-input')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId('admin-settings-cap-input'), {
      target: { value: '99999999' },
    });
    fireEvent.click(screen.getByTestId('admin-settings-cap-save'));

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-cap-save-error')).toHaveTextContent(
        'must be between 1 and 1000000 cents',
      );
    });
  });

  it('explains rather than offering a dead form when the deployment has no store', async () => {
    stubOk({
      status: 200,
      body: ledgerBody({ cap_setting: { cap_store_available: false } }),
    });
    render(<AdminSettings />);

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-spend-unavailable')).toBeInTheDocument();
    });
    expect(screen.getByTestId('admin-settings-spend-unavailable')).toHaveTextContent(
      'DAILY_SPEND_CAP_USD_CENTS',
    );
    expect(screen.queryByTestId('admin-settings-cap-input')).toBeNull();
    // The figures still render — a deployment that cannot change the cap can
    // still be told what it is.
    expect(screen.getByTestId('spend-value-cap')).toHaveTextContent('$20.00');
  });
});

// ---------------------------------------------------------------------------
// Load states.
// ---------------------------------------------------------------------------

describe('the spend card’s load states (#653)', () => {
  it('a 403 on the spend read hides the whole panel', async () => {
    stubOk({ status: 403, body: { detail: 'Admin privilege required.' } });
    const { container } = render(<AdminSettings />);
    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('a failed spend load is terminal: an error and a retry, never a spinner too', async () => {
    stubOk({ status: 500, body: {} });
    render(<AdminSettings />);

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-spend-error')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('admin-settings-spend-loading')).toBeNull();
    expect(screen.getByTestId('admin-settings-retry')).toBeInTheDocument();
    // #425: no HTTP status, no endpoint in the copy an operator reads.
    const copy = screen.getByTestId('admin-settings-spend-error').textContent ?? '';
    expect(copy).not.toMatch(/500|\/api\//);
  });

  it('the shared retry re-reads spend', async () => {
    const fetchMock = stubOk([
      { status: 500, body: {} },
      { status: 200, body: ledgerBody() },
    ]);
    render(<AdminSettings />);

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-retry')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('admin-settings-retry'));

    await waitFor(() => {
      expect(screen.getByTestId('admin-settings-spend-panel')).toBeInTheDocument();
    });
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).includes('/api/admin/spend')).length,
    ).toBeGreaterThanOrEqual(2);
  });
});
