/**
 * admin-gate.test.tsx — admin panel visibility gated on the probed role,
 * not a 403 round-trip (issue #234).
 *
 * The fix in App.tsx probes GET /api/me on mount (issue #235's capability
 * route) and only mounts AdminUsers/AdminRetention once that probe
 * resolves `is_admin: true`. This locks in that a non-admin caller never
 * sees any admin chrome — not even momentarily.
 *
 * The negative test below deliberately leaves /api/users,
 * /api/users/sync-status, /api/admin/retention, and
 * /api/admin/retention/holds unstubbed (any call 404s via the fetch stub),
 * so a pre-fix "render both panels unconditionally, hide on 403"
 * implementation would still flash `admin-users-panel` / `Loading users…`
 * / the break-glass note synchronously on first render, before those
 * 404s ever come back. This test must be RED against that implementation
 * and GREEN once rendering is gated on the /api/me probe instead.
 *
 * Fully offline — aws-amplify/auth and @aws-amplify/ui-react are mocked,
 * fetch is stubbed. No live AWS/Cognito/network.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
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

const ADMIN_CHROME_TESTIDS = [
  'admin-users-panel',
  'admin-retention-panel',
  'sync-status-panel',
  'break-glass-note',
  'retention-slider-panel',
];

function expectNoAdminChrome(): void {
  for (const testId of ADMIN_CHROME_TESTIDS) {
    expect(screen.queryByTestId(testId)).toBeNull();
  }
  expect(screen.queryByText('Loading users…')).toBeNull();
  expect(screen.queryByText(/break-glass/i)).toBeNull();
}

describe('admin panel visibility — gated on probed role (#234)', () => {
  it('never renders admin panels/nav for a non-admin caller, at any point, without relying on a 403', async () => {
    stubFetch({
      '/version': {
        version: '0.0.1',
        commit: 'abcdef1234567890',
        image_digest: 'sha256:x',
        uptime_seconds: 1,
      },
      '/api/me': { is_admin: false },
      // Deliberately no /api/users, /api/users/sync-status,
      // /api/admin/retention, /api/admin/retention/holds routes — a
      // render-then-403-hide implementation would still flash its
      // loading/panel chrome before these unstubbed (404) calls settle.
    });

    render(<App />);

    // Assert immediately, before any effect has had a chance to settle:
    // this is what catches the synchronous "renders unconditionally,
    // hides itself later" bug — a pre-fix build shows admin chrome on the
    // very first paint, regardless of what /api/me or the 403s say.
    expectNoAdminChrome();

    // Let every effect (version fetch, /api/me probe, and any admin-panel
    // fetch a buggy implementation might fire) settle, then assert again.
    await screen.findByTestId('version-display');
    await waitFor(() => {
      expectNoAdminChrome();
    });

    // Give a final tick for any stray admin fetch to resolve and confirm
    // the panels still never appeared.
    expectNoAdminChrome();
  });

  it('renders admin panels for a caller whose probed role is admin', async () => {
    stubFetch({
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
    });

    render(<App />);

    expect(await screen.findByTestId('admin-users-panel')).toBeInTheDocument();
    expect(await screen.findByTestId('admin-retention-panel')).toBeInTheDocument();
    expect(await screen.findByTestId('break-glass-note')).toBeInTheDocument();
  });
});
