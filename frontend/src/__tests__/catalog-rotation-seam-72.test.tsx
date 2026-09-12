/**
 * catalog-rotation-seam-72.test.tsx — a NON-admin's password rotation must
 * recover the Review tab's contract-type dial (issue #72, extending #635).
 *
 * ## The bug this pins
 *
 * `GET /api/playbooks` is not an admin route, but it IS behind the
 * default-credentials block: it depends on `get_active_user_row`, which calls
 * `enforce_default_credentials_rotation` (backend/src/review_routes.py,
 * backend/src/demo_auth.py), and only `/api/me` and `/api/me/password` are
 * exempt. So a seeded NON-admin — the `user`/`user` row
 * (backend/src/demo_auth.py) still carries `default_credentials_warning` —
 * signs in through the exempt `/api/me`, and the shared catalog's first read
 * is refused. The dial shows its own error copy, which is correct.
 *
 * What must then happen is that rotating the password — the one thing the
 * product asks of them — brings the dial back, in the same session. A reload
 * is not an escape: it destroys the in-memory session identity and signs them
 * out (see adminRefresh.ts, and AdminUsers.tsx's own comment).
 *
 * The seam that makes it happen is App.tsx's `handleCredentialsRotated`,
 * which calls `invalidateCatalog()` beside its `credentialsRefreshKey` bump.
 * It has to live THERE and not inside a consumer: `AdminPlaybooks` — the
 * store's other consumer — is rendered only inside App.tsx's `isAdmin` block,
 * so an invalidation hosted in that panel never fires for exactly the user
 * class this test is about. This file asserts the recovery for a caller whose
 * admin panels do not exist, which is what makes it a test of the seam rather
 * than of the panel.
 *
 * `admin-forbidden-latch-635.test.tsx` covers the admin half — the four admin
 * panels unblanking on the same rotation.
 *
 * Fully offline — aws-amplify is mocked, fetch is stubbed.
 */
import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../App';
import { playbookStop, playbookStops } from './support/consoleSurface';

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
    user: { username: 'user-sub', signInDetails: { loginId: 'user@example.test' } },
    signOut: vi.fn(),
  }),
}));

/**
 * backend/src/demo_auth.py's DEFAULT_CREDENTIALS_ENFORCEMENT_EXEMPT_PATHS,
 * transcribed — they ARE the rotation flow — plus `/version`, which depends
 * on `get_current_user` alone and so never runs the enforcement at all.
 * Everything else, `/api/playbooks` included, is refused until the rotation.
 */
const EXEMPT_PATHS = new Set(['/api/me', '/api/me/password', '/version']);

/** The seeded NON-admin row: no admin claim, still on the shipped password. */
const ME_USER_UNROTATED = {
  username: 'user',
  cognito_sub: 'user-sub',
  is_admin: false,
  default_credentials_warning: true,
};
const ME_USER_ROTATED = { ...ME_USER_UNROTATED, default_credentials_warning: false };

/** The exact shape `_load_playbook_catalog` builds (review_routes.py). */
const CATALOG = {
  playbooks: [
    {
      playbook_id: 'nda',
      display_name: 'NDA',
      status: 'active',
      notes: 'Active since the seed.',
    },
  ],
};

const BODIES: Record<string, unknown> = {
  '/version': { version: '0.0.0-test', commit: 'test', uptime_seconds: 1 },
  '/api/playbooks': CATALOG,
  '/api/reviews': { reviews: [] },
};

/**
 * A fetch stub that enforces default-credentials rotation the way the server
 * does. `state.rotated` is the rotation; flip it and the previously-refused
 * routes start answering, with nothing else changing. Every answered path is
 * recorded, so a test can wait for the catalog read to have actually been
 * refused before asserting the dial is empty — a bare "no options yet"
 * assertion would also pass against a read still in flight.
 */
function stubRotationEnforcingFetch(state: { rotated: boolean }): string[] {
  const answered: string[] = [];
  const impl = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    answered.push(pathname);

    if (pathname === '/api/me') {
      const body = state.rotated ? ME_USER_ROTATED : ME_USER_UNROTATED;
      return { ok: true, status: 200, json: async () => body } as Response;
    }
    if (pathname === '/api/me/password') {
      state.rotated = true;
      return { ok: true, status: 200, json: async () => ({ changed: true }) } as Response;
    }
    if (!EXEMPT_PATHS.has(pathname) && !state.rotated) {
      return {
        ok: false,
        status: 403,
        json: async () => ({
          detail:
            'This account still uses the shipped default password. ' +
            'Change it at POST /api/me/password before continuing.',
        }),
      } as Response;
    }
    const body = BODIES[pathname];
    if (body === undefined) {
      return { ok: true, status: 200, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return answered;
}

/** Wait until `path` has been answered, then let the awaiting code finish. */
async function settleAfterRequest(answered: string[], path: string): Promise<void> {
  await waitFor(() => expect(answered, `${path} was never requested`).toContain(path));
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('issue #72 — a non-admin rotation recovers the Review tab dial', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_AUTH_MODE', 'password');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('brings back the contract types with no admin panel mounted and no reload', async () => {
    const state = { rotated: false };
    const answered = stubRotationEnforcingFetch(state);

    render(<App />);

    // Signed in (GET /api/me is exempt), with the rotation control on screen.
    expect(await screen.findByTestId('change-password-open')).toBeTruthy();

    // The catalog really was READ and really was refused — this is what makes
    // the empty dial below evidence of a 403 rather than of a pending fetch.
    await settleAfterRequest(answered, '/api/playbooks');
    expect(playbookStops()).toHaveLength(0);
    // The console shows the channel in the status window AND summarises it
    // in the register glass, so this copy is legitimately on screen twice.
    expect(screen.getAllByText('Contract types unavailable').length).toBeGreaterThan(0);

    // The point of the test: this caller has NO admin panels at all, so
    // nothing inside one of them can be what refreshes the catalog.
    expect(screen.queryByTestId('admin-playbooks-panel')).toBeNull();
    expect(screen.queryByRole('tab', { name: /playbooks/i })).toBeNull();

    // Rotate, through the real control App wires its callback to.
    fireEvent.click(screen.getByTestId('change-password-open'));
    fireEvent.change(screen.getByTestId('change-password-current'), {
      target: { value: 'seeded-default' },
    });
    fireEvent.change(screen.getByTestId('change-password-new'), {
      target: { value: 'a-rotated-passphrase' },
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('change-password-submit'));
    });
    await screen.findByTestId('change-password-success');

    // The dial recovers in place: same mounted panel, no reload, no sign-out.
    await waitFor(() => expect(playbookStop('nda')?.selectable).toBe(true));
    await waitFor(() =>
      expect(screen.queryAllByText('Contract types unavailable')).toHaveLength(0),
    );
  });
});
