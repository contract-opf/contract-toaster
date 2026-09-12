/**
 * admin-forbidden-latch-635.test.tsx — a 403 must not latch an admin panel
 * blank for the rest of the session (issue #635).
 *
 * The mechanism being fixed, link by link:
 *
 *   1. `GET /api/me` is exempt from default-credentials rotation enforcement
 *      (`DEFAULT_CREDENTIALS_ENFORCEMENT_EXEMPT_PATHS` in
 *      backend/src/demo_auth.py), so a seeded admin who has NOT yet rotated
 *      still resolves `is_admin: true` and every admin panel mounts.
 *   2. Those panels' own data routes are NOT exempt — they depend on
 *      `get_active_user_row`, which calls `enforce_default_credentials_rotation`
 *      (backend/src/main.py) and raises HTTP 403 until the password is rotated.
 *   3. Each panel latched that 403 into `isForbidden` and rendered `null`, and
 *      nothing ever set it back to false.
 *   4. App.tsx mounts every panel ONCE and toggles `hidden`, so a tab switch
 *      never remounts and the mount effects never re-fire.
 *   5. A page reload is not a recovery either — it destroys the in-memory
 *      session identity and signs the operator out (AdminUsers.tsx's own
 *      comment records this).
 *
 * So the panels stayed permanently blank after the operator did exactly what
 * the product told them to do.
 *
 * The stub below models the REAL enforcement rather than blanket-403ing: the
 * two exempt paths (`/api/me`, `/api/me/password`) and `/version` (which
 * depends on `get_current_user` only, never `get_active_user_row`) answer 200
 * throughout; every other path answers 403 until `rotated` flips, exactly as
 * `enforce_default_credentials_rotation` does. A stub that refused everything
 * could not distinguish this bug from "the caller is simply not an admin".
 *
 * Fully offline — aws-amplify is mocked, fetch is stubbed.
 */
import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import AdminModel from '../AdminModel';
import AdminPlaybooks from '../AdminPlaybooks';
import AdminRetention from '../AdminRetention';
import AdminUsers from '../AdminUsers';
import App from '../App';
import { invalidateCatalog } from '../playbooksStore';

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
    user: { username: 'admin-sub', signInDetails: { loginId: 'admin@example.test' } },
    signOut: vi.fn(),
  }),
}));

/**
 * Every path that keeps answering while the account is still unrotated. The
 * first two are backend/src/demo_auth.py's
 * DEFAULT_CREDENTIALS_ENFORCEMENT_EXEMPT_PATHS transcribed — they ARE the
 * rotation flow, so refusing them would leave the operator no way out of the
 * block at all.
 */
const EXEMPT_PATHS = new Set([
  '/api/me',
  '/api/me/password',
  // Not in that frozenset, but equally unaffected: `/version` depends on
  // `get_current_user` alone (backend/src/main.py), never on
  // `get_active_user_row`, so rotation enforcement never runs for it.
  '/version',
]);

const ME_ADMIN_UNROTATED = {
  username: 'admin',
  cognito_sub: 'admin-sub',
  is_admin: true,
  default_credentials_warning: true,
};
const ME_ADMIN_ROTATED = { ...ME_ADMIN_UNROTATED, default_credentials_warning: false };

/** Mount-time bodies for every route the four admin panels load. */
const BODIES: Record<string, unknown> = {
  '/version': { version: '0.0.0-test', commit: 'test', uptime_seconds: 1 },
  '/api/users': { users: [] },
  '/api/users/sync-status': {
    sync_type: 'workspace',
    last_run_at: null,
    last_run_outcome: null,
    users_deprovisioned_count: 0,
    next_run_at: null,
  },
  '/api/admin/auth-mode': { auth_mode: 'password', auth_mode_options: ['password', 'sso', 'both'] },
  '/api/admin/model-key': {
    key_store_available: true,
    key_set: false,
    key_source: 'none',
    key_fingerprint: null,
    updated_by: null,
    model_provider: 'openrouter',
  },
  // The WHOLE body `get_model_selection_settings` sends
  // (backend/src/model_settings.py), not a subset (issue #68).
  //
  // This fixture used to carry six fields. `isModelSelectionSettings`
  // (AdminModel.tsx) requires `default_primary`, `default_critic` and both
  // `pricing_basis_*` before the panel will price anything, so the stub was a
  // shape the server cannot send and every mount here quietly took the
  // "unexpected body" error path instead of the success path these tests
  // claim to exercise. It went unnoticed because the only evidence was a
  // `console.error` line in a wall of them; #68's console guard is what
  // surfaced it. `default_primary` is also one of `selectable` — an invariant
  // the server has guaranteed since issue #589.
  '/api/admin/model-selection': {
    setting_id: 'models',
    selection_store_available: true,
    model_provider: 'openrouter',
    selectable: [
      {
        model_id: 'anthropic/claude-opus-5',
        display_name: 'Claude Opus 5',
        tier: 'Highest',
        note: 'Test catalogue entry.',
        cost_per_million_input_usd: 5,
        cost_per_million_output_usd: 25,
        context_length: 200000,
      },
    ],
    default_primary: {
      model_id: 'anthropic/claude-opus-5',
      cost_per_million_input_usd: 5,
      cost_per_million_output_usd: 25,
    },
    default_critic: {
      model_id: 'anthropic/claude-opus-5',
      cost_per_million_input_usd: 5,
      cost_per_million_output_usd: 25,
    },
    pricing_basis_primary: { input_tokens: 60000, output_tokens: 8000 },
    pricing_basis_critic: { input_tokens: 70000, output_tokens: 5000 },
    selected_primary_model_id: '',
    selected_critic_model_id: '',
    effective_primary_model_id: 'anthropic/claude-opus-5',
    effective_critic_model_id: 'anthropic/claude-opus-5',
    primary_source: 'default',
    critic_source: 'default',
    updated_at: '',
    updated_by: '',
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
  '/api/playbooks': { playbooks: [] },
};

/**
 * A fetch stub that enforces default-credentials rotation the way the server
 * does. `state.rotated` is the operator's password rotation; flip it and the
 * previously-refused routes start answering, with nothing else changing.
 * Every answered path is recorded, so a test can wait for a panel's loader to
 * have actually been refused before asserting that it rendered blank — a bare
 * "no panel yet" assertion would also pass against a panel that simply had
 * not finished mounting.
 */
function stubRotationEnforcingFetch(state: { rotated: boolean }): string[] {
  const answered: string[] = [];
  const impl = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    answered.push(pathname);

    if (pathname === '/api/me') {
      const body = state.rotated ? ME_ADMIN_ROTATED : ME_ADMIN_UNROTATED;
      return { ok: true, status: 200, json: async () => body } as Response;
    }
    if (pathname === '/api/me/password') {
      // The rotation itself. POSTing it is what clears the block server-side.
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

/**
 * Wait until each `paths` entry has been answered, then let the awaiting
 * component code run to completion, so "the panel is blank" is an assertion
 * about a panel that HAS been refused rather than one still in flight.
 */
async function settleAfterRequests(answered: string[], paths: string[]): Promise<void> {
  await waitFor(() => {
    for (const path of paths) {
      expect(answered, `${path} was never requested`).toContain(path);
    }
  });
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

// ---------------------------------------------------------------------------
// Per-panel: the latch clears and the loaders re-fire on a rotation bump.
// ---------------------------------------------------------------------------

interface PanelCase {
  name: string;
  testId: string;
  loaderPath: string;
  element: (key: number) => React.ReactElement;
  /**
   * What re-runs this panel's loader after a rotation, for the panels whose
   * loader is NOT driven by the `credentialsRefreshKey` prop. Issue #72 moved
   * AdminPlaybooks' catalog into the shared store, and with it the seam:
   * App.tsx's `handleCredentialsRotated` calls `invalidateCatalog()` (it must
   * — AdminPlaybooks never mounts for a non-admin, whose Review-tab dial is
   * behind the same refused route). Rendered standalone, this test has to
   * play that part itself, exactly as App.tsx does.
   */
  rotate?: () => void;
}

const PANELS: PanelCase[] = [
  {
    name: 'AdminUsers',
    testId: 'admin-users-panel',
    loaderPath: '/api/users',
    element: (key: number) => <AdminUsers credentialsRefreshKey={key} />,
  },
  {
    name: 'AdminRetention',
    testId: 'admin-retention-panel',
    loaderPath: '/api/admin/retention',
    element: (key: number) => <AdminRetention credentialsRefreshKey={key} />,
  },
  {
    name: 'AdminModel',
    testId: 'admin-model-panel',
    loaderPath: '/api/admin/model-key',
    element: (key: number) => <AdminModel credentialsRefreshKey={key} />,
  },
  {
    name: 'AdminPlaybooks',
    testId: 'admin-playbooks-panel',
    loaderPath: '/api/playbooks',
    element: () => <AdminPlaybooks />,
    rotate: () => invalidateCatalog(),
  },
];

describe.each(PANELS)('$name — a rotation 403 does not latch the panel blank (#635)', (panel) => {
  it('renders blank while unrotated, then recovers when the refresh key is bumped', async () => {
    const state = { rotated: false };
    const answered = stubRotationEnforcingFetch(state);

    const view = render(panel.element(0));
    // Blank because its data route answered 403 — not because it is still
    // mounting; `settleAfterRequests` is what makes those two distinguishable.
    await settleAfterRequests(answered, [panel.loaderPath]);
    expect(screen.queryByTestId(panel.testId)).toBeNull();

    // The operator rotates their password. Only the server-side refusal
    // changes; the panel is NOT remounted (App.tsx keeps it mounted and
    // toggles `hidden`), so this rerender is the real production sequence.
    state.rotated = true;
    await act(async () => {
      view.rerender(panel.element(1));
      panel.rotate?.();
    });

    expect(await screen.findByTestId(panel.testId)).toBeTruthy();
  });

  it('stays blank for a caller the server still refuses after the bump', async () => {
    // The security half: clearing the latch must depend on the SERVER
    // answering, not on the key changing. A non-admin is refused forever.
    const state = { rotated: false };
    const answered = stubRotationEnforcingFetch(state);

    const view = render(panel.element(0));
    await settleAfterRequests(answered, [panel.loaderPath]);
    expect(screen.queryByTestId(panel.testId)).toBeNull();

    answered.length = 0;
    await act(async () => {
      view.rerender(panel.element(1));
      panel.rotate?.();
    });
    await settleAfterRequests(answered, [panel.loaderPath]);
    expect(screen.queryByTestId(panel.testId)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// End to end through App: the ticket's own reproduction, with the rotation
// driven through the real ChangePassword control rather than a prop.
// ---------------------------------------------------------------------------

describe('App — rotating the seeded password recovers every admin panel (#635)', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_AUTH_MODE', 'password');
  });

  it('unblanks all four panels without a remount or a reload', async () => {
    const state = { rotated: false };
    const answered = stubRotationEnforcingFetch(state);

    render(<App />);

    // The seeded admin is signed in (GET /api/me is exempt) and the admin
    // chrome is present…
    expect(await screen.findByTestId('change-password-open')).toBeTruthy();
    // …every panel really did mount and really was refused — waiting on the
    // four loader routes is what proves it, so the blank assertions below
    // cannot pass merely because the panels had not mounted yet.
    await settleAfterRequests(
      answered,
      PANELS.map((panel) => panel.loaderPath),
    );
    for (const { testId } of PANELS) {
      expect(screen.queryByTestId(testId)).toBeNull();
    }

    // Rotate, through the real control App wires `onChanged` to.
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

    for (const { name, testId } of PANELS) {
      await waitFor(
        () => expect(screen.queryByTestId(testId), `${name} stayed blank`).not.toBeNull(),
      );
    }
  });
});
