/**
 * admin-tab-grouping-599.test.tsx — issue #599: flatten the tab bar back to
 * one row, reversing issue #477's two-tablist DECISION (owner directive,
 * 2026-08-20). #477 split the admin-only panels into their own labeled
 * "Admin" tablist beneath the primary Review/History row so an orphaned
 * last tab wouldn't wrap looking like an accident; the owner has now looked
 * at the shipped result and wants a single flat, ordered set of
 * destinations instead. This file REPLACES admin-tab-grouping-477.test.tsx
 * (deleted, not left alongside this one — it asserted the two-tablist shape
 * this ticket removes) and is the rendered-output counterpart to
 * `tests/test_review_history_449.py::test_history_tab_is_not_admin_gated`'s
 * source-level check that History is never admin-gated.
 *
 * AC1 (no orphan lone-tab row) and AC2 (no horizontal body scroll at 375)
 * are layout claims — jsdom does no layout, so THIS FILE DOES NOT VERIFY
 * EITHER, same caveat as the file it replaces. What actually backs "eight
 * [seven at #599, eight again with #650's Settings] tabs must still be
 * usable at narrow widths" is unchanged:
 * `frontend/scripts/layout-audit.mjs`'s check 3 statically asserts that
 * `.ct-tab-bar__track` keeps `flex-wrap: wrap` and declares neither
 * `overflow-x` nor `white-space: nowrap` (`npm run audit:layout`) — #599
 * did not touch that CSS, so that guard still applies unchanged to the now-
 * single tablist.
 *
 *   - Exactly ONE `role="tablist"` (accessible name "Sections", the
 *     `ct-tab-bar` default) — never a second "Admin" tablist.
 *   - A non-admin caller sees only Review and History; every admin-only tab
 *     name (old or new label) is absent, not merely hidden/disabled.
 *   - An admin caller sees all eight tabs (seven at #599, plus Settings —
 *     issue #650), in the owner's specified order
 *     and with the renamed labels ("Users & access" → "Users",
 *     "Retention & legal hold" → "Retention", "Model & API key" →
 *     "Models"; "Playbooks" and "Diagnostics" keep their labels — there is
 *     no separate "Playbook instructions" tab, per issue #605).
 *   - Keyboard Home/End cycling covers the WHOLE tablist now — End lands on
 *     Diagnostics (the last tab overall), not on History (the old primary
 *     group's own last tab) — proving the two groups really did merge into
 *     one ring rather than just being rendered side by side.
 *
 * Same offline convention as the file this replaces: aws-amplify mocked,
 * fetch stubbed, no live AWS/Cognito/network.
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
  // Issue #650's Settings panel reads the caller's own preferences route for
  // `notes_mode_available` (the #572 kill switch, projected to the client).
  '/api/me/preferences': { preferences: {}, notes_mode_available: false },
  '/api/admin/model-key': {
    setting_id: 'global',
    key_store_available: true,
    model_provider: 'openrouter',
    key_set: false,
    key_source: null,
    key_fingerprint: '',
    updated_at: '',
    updated_by: '',
  },
};

describe('flat tab bar (#599, reverses #477)', () => {
  it('renders exactly one "Sections" tablist with only Review and History for a non-admin caller', async () => {
    stubFetch({
      '/version': ADMIN_ROUTES['/version'],
      '/api/me': { is_admin: false },
    });

    render(<App />);

    await screen.findByTestId('version-display');
    const tablists = screen.getAllByRole('tablist');
    expect(tablists).toHaveLength(1);
    expect(screen.getByRole('tablist', { name: 'Sections' })).toBeInTheDocument();
    expect(screen.queryByRole('tablist', { name: 'Admin' })).toBeNull();

    const tabNames = within(tablists[0]).getAllByRole('tab').map((tab) => tab.textContent);
    expect(tabNames).toEqual(['Review', 'History']);

    // Every admin-only tab absent under BOTH its old and new label.
    for (const name of ['Users & access', 'Users', 'Retention & legal hold', 'Retention', 'Model & API key', 'Models', 'Playbooks', 'Settings', 'Diagnostics']) {
      expect(screen.queryByRole('tab', { name })).toBeNull();
    }
  });

  it('renders one flat "Sections" tablist with all eight tabs, in order, for an admin caller', async () => {
    stubFetch(ADMIN_ROUTES);

    render(<App />);

    const tablists = await screen.findAllByRole('tablist');
    expect(tablists).toHaveLength(1);
    const sections = screen.getByRole('tablist', { name: 'Sections' });

    // The ADMIN tabs arrive asynchronously (the caller's admin capability is
    // fetched, not known at first paint), so the tablist exists before it is
    // complete. Wait for the LAST one specifically: asserting the moment the
    // tablist appears reads whatever subset happens to have rendered, which
    // passes on a fast machine and fails under any latency (issue #634).
    await within(sections).findByRole('tab', { name: 'Diagnostics' });

    const tabNames = within(sections).getAllByRole('tab').map((tab) => tab.textContent);
    expect(tabNames).toEqual([
      'Review',
      'History',
      'Users',
      'Retention',
      'Models',
      'Playbooks',
      // Settings (issue #650) sits between Playbooks and Diagnostics:
      // configuration belongs with the configuration tabs, and Diagnostics
      // keeps its documented last position.
      'Settings',
      'Diagnostics',
    ]);
  });

  it('Home/End keyboard cycling spans the whole flat tablist (End lands on Diagnostics, not History)', async () => {
    stubFetch(ADMIN_ROUTES);

    render(<App />);

    const sections = await screen.findByRole('tablist', { name: 'Sections' });
    // Same asynchrony as the test above: pressing End before the admin tabs
    // have rendered moves to whatever the last tab is AT THAT MOMENT (History,
    // under the pre-#599 primary group), and the assertion below then fails on
    // a tab that only appeared afterwards. Wait for the real last tab first so
    // this measures the keyboard contract rather than a render race.
    await within(sections).findByRole('tab', { name: 'Diagnostics' });

    const reviewTab = within(sections).getByRole('tab', { name: 'Review' });
    reviewTab.focus();
    fireEvent.keyDown(reviewTab, { key: 'End' });

    // Proves the old two-ring split is really gone: End from Review now
    // reaches the LAST tab overall (Diagnostics), not History (which was
    // the primary group's own last tab under #477).
    await waitFor(() => {
      expect(within(sections).getByRole('tab', { name: 'Diagnostics' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });
    expect(within(sections).getByRole('tab', { name: 'Diagnostics' })).toHaveAttribute('tabindex', '0');

    const diagnosticsTab = within(sections).getByRole('tab', { name: 'Diagnostics' });
    fireEvent.keyDown(diagnosticsTab, { key: 'Home' });
    await waitFor(() => {
      expect(within(sections).getByRole('tab', { name: 'Review' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });
  });
});
