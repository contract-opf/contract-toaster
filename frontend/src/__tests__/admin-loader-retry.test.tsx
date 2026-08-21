/**
 * admin-loader-retry.test.tsx — every remaining admin loader recovers in place
 * (issue #511).
 *
 * #439 gave AdminUsers a terminal `LoadState` with a working "Try again", and
 * its own closing comment recorded the byte-identical wedge in the other admin
 * screens. This covers those: AdminModel (settings + model selection),
 * AdminRetention (settings + legal holds), AdminPlaybooks (catalog + version
 * history).
 *
 * The wedge, in each case: the loader keyed its loading branch off a
 * `T | null` sentinel and put the failure in a separate `error` string, so on
 * a failed fetch BOTH were true — a danger banner reading "Please try again"
 * above a permanent spinner, offering nothing to try. On a password-mode
 * deployment the session token lives in memory (#468), so the only recovery
 * was a reload, which signs the admin out. A transient blip cost the operator
 * their session.
 *
 * Written against the REAL components with only the network transport stubbed,
 * so they exercise the actual load/render path rather than a double of it. The
 * recovery case is the one that matters: it is not enough for the retry
 * control to exist, it has to re-fetch and the screen has to come back.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import AdminModel from '../AdminModel';
import AdminPlaybooks from '../AdminPlaybooks';
import AdminRetention from '../AdminRetention';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const BODIES: Record<string, unknown> = {
  '/api/admin/model-key': {
    key_store_available: true,
    key_set: false,
    key_source: 'none',
    key_hint: null,
    updated_by: null,
    model_provider: 'openrouter',
  },
  '/api/admin/model-selection': {
    selection_store_available: true,
    selected_primary_model_id: null,
    selected_critic_model_id: null,
    selectable: [],
    effective_primary_model_id: 'anthropic/claude-opus-5',
    effective_critic_model_id: 'anthropic/claude-opus-5',
  },
  '/api/admin/retention': {
    retention_window_days: 90,
    min_days: 1,
    max_days: 365,
    updated_by: null,
    updated_at: null,
  },
  '/api/admin/retention/holds': { holds: [] },
  '/api/reviews': { reviews: [] },
  '/api/admin/users': { users: [] },
  '/api/admin/sync-status': { sync_type: 'workspace', last_run_at: null, last_run_outcome: null, users_deprovisioned_count: 0, next_run_at: null },
  '/api/playbooks': { playbooks: [] },
};

/**
 * Fails `failPath` on the first request and serves it normally afterwards, so
 * "the retry actually re-fetched and the screen recovered" is observable
 * rather than assumed.
 */
function stubFetchFailingOnce(failPath: string) {
  let failed = false;
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (pathname === failPath && !failed) {
      failed = true;
      return { ok: false, status: 500, json: async () => ({}) } as Response;
    }
    if (pathname.includes('/versions')) {
      return { ok: true, status: 200, json: async () => ({ versions: [] }) } as Response;
    }
    // EXACT match, never a prefix: `/api/admin/retention` is a prefix of
    // `/api/admin/retention/holds`, and serving the settings body for the
    // holds request produced an `undefined.length` crash that looked like a
    // component bug rather than a stub bug.
    const body = BODIES[pathname] ?? {};
    return { ok: true, status: 200, json: async () => body } as Response;
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * Every loader under test, as (screen, the endpoint to fail, the testids of
 * its error banner, its retry control, and the spinner that must NOT still be
 * showing beside them).
 */
const LOADERS = [
  {
    name: 'AdminModel — model key settings',
    render: () => render(<AdminModel />),
    failPath: '/api/admin/model-key',
    error: 'admin-model-error',
    retry: 'admin-model-retry',
    spinner: 'admin-model-loading',
  },
  {
    name: 'AdminModel — model selection',
    render: () => render(<AdminModel />),
    failPath: '/api/admin/model-selection',
    error: 'admin-model-selection-error',
    retry: 'admin-model-selection-retry',
    spinner: 'admin-model-selection-loading',
  },
  {
    name: 'AdminRetention — retention settings',
    render: () => render(<AdminRetention />),
    failPath: '/api/admin/retention',
    error: 'admin-retention-error',
    retry: 'admin-retention-retry',
    spinner: 'admin-retention-loading',
  },
  {
    name: 'AdminRetention — legal holds',
    render: () => render(<AdminRetention />),
    failPath: '/api/admin/retention/holds',
    error: 'legal-holds-error',
    retry: 'legal-holds-retry',
    spinner: 'legal-holds-loading',
  },
  {
    name: 'AdminPlaybooks — catalog',
    render: () => render(<AdminPlaybooks />),
    failPath: '/api/playbooks',
    error: 'admin-playbooks-error',
    retry: 'admin-playbooks-retry',
    spinner: 'admin-playbooks-loading',
  },
];

describe.each(LOADERS)('$name', ({ render: renderScreen, failPath, error, retry, spinner }) => {
  it('shows an error AND a retry, never a permanent spinner', async () => {
    vi.stubGlobal('fetch', stubFetchFailingOnce(failPath));
    renderScreen();

    await screen.findByTestId(error);
    // The whole point: the banner and the spinner used to coexist, so the
    // screen said "please try again" while showing no way to.
    expect(screen.queryByTestId(spinner)).toBeNull();
    expect(screen.getByTestId(retry)).toBeTruthy();
  });

  it('the retry re-fetches in place and the screen recovers — no reload', async () => {
    vi.stubGlobal('fetch', stubFetchFailingOnce(failPath));
    renderScreen();

    fireEvent.click(await screen.findByTestId(retry));
    // Recovery, not merely a control that exists: the error is gone because a
    // second request actually succeeded.
    await waitFor(() => expect(screen.queryByTestId(error)).toBeNull());
  });
});

describe('AdminPlaybooks — version history', () => {
  // Not table-driven with the rest: this loader only runs once a playbook has
  // been selected, so the failure has to be provoked through the UI.
  const CATALOG = {
    playbooks: [
      { playbook_id: 'eiaa', display_name: 'Affiliation', status: 'active', notes: '' },
    ],
  };

  function stub() {
    let failed = false;
    return vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      if (pathname.includes('/versions')) {
        if (!failed) {
          failed = true;
          return { ok: false, status: 500, json: async () => ({}) } as Response;
        }
        return { ok: true, status: 200, json: async () => ({ versions: [] }) } as Response;
      }
      if (pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => CATALOG } as Response;
      }
      return { ok: true, status: 200, json: async () => ({}) } as Response;
    });
  }

  async function openVersionHistory() {
    vi.stubGlobal('fetch', stub());
    render(<AdminPlaybooks />);
    const row = await screen.findByTestId('playbook-row-eiaa');
    // Whichever control on the row opens the history — found by its accessible
    // name rather than a testid, so this keeps working if the row's buttons
    // are reordered.
    const trigger = Array.from(row.querySelectorAll('button')).find((button) =>
      /version|history/i.test(button.textContent ?? ''),
    );
    if (!trigger) {
      throw new Error(
        `no control on the playbook row opens its history; buttons were: ${Array.from(
          row.querySelectorAll('button'),
        )
          .map((b) => JSON.stringify(b.textContent))
          .join(', ')}`,
      );
    }
    fireEvent.click(trigger);
  }

  it('a failed version fetch shows an error and a retry, not a permanent spinner', async () => {
    await openVersionHistory();
    await screen.findByTestId('admin-playbooks-versions-error');
    expect(screen.queryByTestId('admin-playbooks-versions-loading')).toBeNull();
    expect(screen.getByTestId('admin-playbooks-versions-retry')).toBeTruthy();
  });

  it('the retry re-fetches that playbook in place', async () => {
    await openVersionHistory();
    fireEvent.click(await screen.findByTestId('admin-playbooks-versions-retry'));
    await waitFor(() =>
      expect(screen.queryByTestId('admin-playbooks-versions-error')).toBeNull(),
    );
  });
});
