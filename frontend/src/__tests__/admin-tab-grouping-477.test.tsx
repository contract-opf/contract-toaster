/**
 * admin-tab-grouping-477.test.tsx — issue #477: the eight-tab bar wrapped an
 * orphaned single tab onto a second row at desktop widths. Per the issue's
 * owner-delegated DECISION comment, the fix is a two-tier tab shape rather
 * than a scrollable strip: `Review`/`History` stay a primary tablist every
 * user sees, and the six admin-only panels move into a second, independently
 * labeled "Admin" tablist that is allowed to wrap on its own.
 *
 * This locks in the acceptance criteria that a DOM assertion can check.
 * AC1 (no orphan lone-tab row at 1280/1449) and AC2 (no horizontal body
 * scroll at 375) are layout claims — jsdom does no layout, so THIS FILE
 * DOES NOT VERIFY EITHER, and no other automated test renders the app to
 * measure them either. What actually is evidence: `frontend/scripts/
 * layout-audit.mjs` (`npm run audit:layout`) statically asserts that
 * `.ct-tab-bar__track` keeps `flex-wrap: wrap` and declares neither
 * `overflow-x` nor `white-space: nowrap` — the CSS properties AC1/AC2
 * depend on to be able to wrap an orphaned tab instead of scrolling or
 * clipping it — and fails closed (self-test fixtures) if a later edit
 * loosens that check. That is a guard on the properties the fix relies
 * on, not a rendered measurement of AC1/AC2 themselves; see that
 * script's own docstring, which says so plainly.
 *
 *   - Two independent `role="tablist"` elements, each with its own
 *     accessible name (`aria-label`) — "Sections" (primary, unchanged) and
 *     "Admin" (new) — never one flat eight-tab row.
 *   - The Admin tablist renders ONLY for an admin caller — absent (not
 *     merely hidden/disabled) for a non-admin, matching every admin panel's
 *     existing 403-hide-itself posture.
 *   - Keyboard Home/End cycling stays PER GROUP: End on the primary tablist
 *     lands on History, never spilling into the admin group, and vice
 *     versa — the two tablists are genuinely independent widgets, not one
 *     virtual ring.
 *
 * Same offline convention as admin-gate.test.tsx: aws-amplify mocked, fetch
 * stubbed, no live AWS/Cognito/network.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import App from '../App';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({
    user: { username: 'user-sub', signInDetails: { loginId: 'user@example.com' } },
    signOut: vi.fn(),
  }),
}));

function stubFetch(routes: Record<string, unknown>): void {
  const impl = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    const body = routes[pathname];
    if (body === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
}

const ADMIN_ROUTES = {
  '/version': {
    version: '0.0.1',
    commit: 'abcdef1234567890',
    image_digest: 'sha256:x',
    uptime_seconds: 1,
  },
  '/api/me': { is_admin: true },
  '/api/users': { users: [] },
  '/api/users/sync-status': {
    sync_type: 'workspace',
    last_run_at: null,
    last_run_outcome: null,
    users_deprovisioned_count: 0,
    next_run_at: null,
  },
  '/api/admin/retention': {
    setting_id: 'default',
    retention_window_days: 90,
    pending_reduction: null,
  },
  '/api/admin/retention/holds': { holds: [] },
  '/api/playbooks': { playbooks: [] },
  '/api/admin/diagnostics/recent-failures': { failures: [] },
  '/api/admin/model-key': {
    setting_id: 'global',
    key_store_available: true,
    model_provider: 'openrouter',
    key_set: false,
    key_source: null,
    key_hint: '',
    updated_at: '',
    updated_by: '',
  },
};

describe('admin tab grouping — two-tier tablist shape (#477)', () => {
  it('renders exactly one tablist (Sections) with no Admin group for a non-admin caller', async () => {
    stubFetch({
      '/version': ADMIN_ROUTES['/version'],
      '/api/me': { is_admin: false },
    });

    render(<App />);

    await screen.findByTestId('version-display');
    expect(screen.getByRole('tablist', { name: 'Sections' })).toBeInTheDocument();
    expect(screen.queryByRole('tablist', { name: 'Admin' })).toBeNull();
    // Every admin-only tab label absent, not just its panel.
    expect(screen.queryByRole('tab', { name: 'Users & access' })).toBeNull();
    expect(screen.queryByRole('tab', { name: 'Diagnostics' })).toBeNull();
  });

  it('renders two independent tablists — Sections (2 tabs) and Admin (6 tabs) — for an admin caller', async () => {
    stubFetch(ADMIN_ROUTES);

    render(<App />);

    const primary = await screen.findByRole('tablist', { name: 'Sections' });
    const admin = await screen.findByRole('tablist', { name: 'Admin' });

    expect(within(primary).getAllByRole('tab')).toHaveLength(2);
    expect(within(primary).getByRole('tab', { name: 'Review' })).toBeInTheDocument();
    expect(within(primary).getByRole('tab', { name: 'History' })).toBeInTheDocument();

    const adminTabNames = within(admin)
      .getAllByRole('tab')
      .map((tab) => tab.textContent);
    expect(adminTabNames).toEqual([
      'Users & access',
      'Retention & legal hold',
      'Model & API key',
      'Playbooks',
      'Playbook instructions',
      'Diagnostics',
    ]);
  });

  it('keeps Home/End keyboard cycling scoped per group (no spillover between Sections and Admin)', async () => {
    stubFetch(ADMIN_ROUTES);

    render(<App />);

    const primary = await screen.findByRole('tablist', { name: 'Sections' });
    const reviewTab = within(primary).getByRole('tab', { name: 'Review' });
    reviewTab.focus();
    fireEvent.keyDown(reviewTab, { key: 'End' });

    // End on the primary group lands on History (last of ITS two tabs), not
    // on Diagnostics (last of the admin group) — the two tablists are
    // genuinely independent, not one virtual eight-tab ring.
    await waitFor(() => {
      expect(within(primary).getByRole('tab', { name: 'History' })).toHaveAttribute('aria-selected', 'true');
    });
    expect(within(primary).getByRole('tab', { name: 'History' })).toHaveAttribute('tabindex', '0');

    const admin = screen.getByRole('tablist', { name: 'Admin' });
    const adminTabs = within(admin).getAllByRole('tab');
    // None of the admin group's tabs is "selected" while History is active —
    // but ct-tab-bar.ts's per-instance roving-tabindex fallback still keeps
    // exactly one of them (the first) reachable by a plain Tab keypress, so
    // the whole group isn't silently dropped from tab order.
    expect(adminTabs.every((tab) => tab.getAttribute('aria-selected') === 'false')).toBe(true);
    expect(adminTabs[0]).toHaveAttribute('tabindex', '0');
    for (const tab of adminTabs.slice(1)) {
      expect(tab).toHaveAttribute('tabindex', '-1');
    }
  });
});
