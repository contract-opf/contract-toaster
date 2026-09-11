/**
 * lazy-panels-56.test.tsx — issue #56: the admin panels and History are
 * code-split WITHOUT breaking the "every panel stays mounted" invariant.
 *
 * #56 turned the six `Admin*` imports and `ReviewHistory` in App.tsx into
 * `React.lazy(() => import(…))` behind a `<Suspense fallback={<CtPanelSkeleton
 * />}>` inside each panel's existing `<ErrorBoundary>`. That is a chunking
 * change, and the obvious way to get it wrong is to reach for the OTHER thing
 * code splitting usually implies — rendering a panel only while its tab is
 * selected. App.tsx:598 forbids exactly that:
 *
 *     "every panel stays MOUNTED at once; visibility is toggled via the
 *      `hidden` attribute so ReviewSubmission's polling and the admin panels'
 *      state persist across tab switches"
 *
 * Unmounting on tab switch would still pass `npm run audit:bundle`, still
 * shrink the entry chunk, and still look right in a browser — and it would
 * silently throw away a running review's poll state and every panel's loaded
 * data the moment the reviewer looked at another tab. So this file asserts
 * the two halves together:
 *
 *   1. LAZY: the first render puts the skeleton fallback on screen (proof a
 *      real Suspense boundary is in the tree, not a static import that merely
 *      looks lazy), and all six `admin-*-panel` test ids resolve through it
 *      with `findByTestId`.
 *   2. STILL MOUNTED: after switching tabs, every panel is not just present
 *      but the SAME DOM NODE it was before — a remount would satisfy
 *      "present", and only node identity distinguishes the two. Only the
 *      enclosing tabpanel's `hidden` attribute changes.
 *
 * Fixtures: every stubbed body below is a real route's real shape, taken from
 * the producers in `backend/src/` — `/api/me` (`main.py`'s capability probe,
 * #235), `/api/users` + `/api/users/sync-status` (`users.py`, which writes
 * `users_deprovisioned_count`), `/api/admin/retention` +
 * `/api/admin/retention/holds` (`retention.py`), `/api/playbooks`,
 * `/api/admin/diagnostics/recent-failures`, `/api/admin/model-key`
 * (`model_settings.py`, which returns `key_fingerprint` and never the key)
 * and `/api/me/preferences` (`user_preferences.py::get_preferences`, whose
 * `{ preferences, notes_mode_available }` shape the Settings panel reads).
 * The same set admin-tab-grouping-599.test.tsx drives the real App with.
 *
 * Fully offline — aws-amplify/auth and @aws-amplify/ui-react are mocked,
 * fetch is stubbed. No live AWS/Cognito/network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../App';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

// The module-level mock still applies to a LAZILY imported `SsoShell.tsx`:
// `React.lazy`'s `import()` goes through the same module registry vitest
// installed this mock into.
vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({
    user: { username: 'user-sub', signInDetails: { loginId: 'admin@example.com' } },
    signOut: vi.fn(),
  }),
}));

const ADMIN_ROUTES: Record<string, unknown> = {
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
  '/api/me/preferences': { preferences: { notes_mode: 'external' }, notes_mode_available: false },
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

/**
 * Explicit window for anything that has to wait on a lazy chunk, well over
 * @testing-library's 1000 ms default. Under the full suite's parallelism
 * Vitest transforms and imports seven panel modules on demand here, and the
 * default window is not enough for the first of them (observed failing in
 * `scripts/check-frontend.sh`, green when this file runs alone). Inside
 * vitest.config.ts's 15 s per-test budget, and inside what
 * test-budget-coherence-634.test.ts requires of a declared window.
 */
const LAZY_CHUNK_MS = 10_000;

function stubFetch(routes: Record<string, unknown>): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const pathname = new URL(String(input), 'http://localhost').pathname;
      const body = routes[pathname];
      if (body === undefined) {
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }
      return { ok: true, status: 200, json: async () => body } as Response;
    }),
  );
}

/** The six admin tabpanels, as `[tab label, panel test id]`. Order is
 *  App.tsx's TAB_DEFS order. */
const ADMIN_PANELS: [string, string][] = [
  ['Users', 'admin-users-panel'],
  ['Retention', 'admin-retention-panel'],
  ['Models', 'admin-model-panel'],
  ['Playbooks', 'admin-playbooks-panel'],
  ['Settings', 'admin-settings-panel'],
  ['Diagnostics', 'admin-diagnostics-panel'],
];

/** The `<section role="tabpanel">` wrapping a panel — the element that
 *  carries the `hidden` attribute App.tsx toggles. */
function tabpanelOf(testId: string): HTMLElement {
  const panel = screen.getByTestId(testId).closest('[role="tabpanel"]');
  expect(panel, `${testId} must sit inside a role=tabpanel section`).not.toBeNull();
  return panel as HTMLElement;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('issue #56 — code-split panels still all mount at once', () => {
  it('suspends on first paint and then resolves all six admin panels through the boundary', async () => {
    stubFetch(ADMIN_ROUTES);
    render(<App />);

    // (1) A real Suspense boundary is in the tree. On the very first paint
    // the admin probe has not resolved, so the only lazy panel rendered is
    // History — and it is showing the skeleton, not its content. A static
    // import dressed up to look lazy would render nothing here at all.
    expect(screen.getAllByTestId('panel-skeleton').length).toBeGreaterThan(0);

    // (2) Every admin panel arrives through that boundary. `findByTestId`,
    // not `getByTestId`: the chunk has to load first.
    for (const [, testId] of ADMIN_PANELS) {
      expect(await screen.findByTestId(testId, {}, { timeout: LAZY_CHUNK_MS })).toBeInTheDocument();
    }
    // History is lazy too and mounts for every signed-in caller.
    expect(
      await screen.findByTestId('review-history-panel', {}, { timeout: LAZY_CHUNK_MS }),
    ).toBeInTheDocument();

    // …and the fallback is transient: no skeleton is left behind once every
    // chunk has resolved.
    await waitFor(() => expect(screen.queryByTestId('panel-skeleton')).toBeNull(), {
      timeout: LAZY_CHUNK_MS,
    });
  });

  it('switching tabs changes only `hidden` — no panel is unmounted or remounted', async () => {
    stubFetch(ADMIN_ROUTES);
    render(<App />);

    for (const [, testId] of ADMIN_PANELS) {
      await screen.findByTestId(testId, {}, { timeout: LAZY_CHUNK_MS });
    }

    // Identity, not mere presence. A `hidden`-toggling implementation keeps
    // the very same DOM nodes across a tab switch; an implementation that
    // renders only the active tab hands back NEW nodes each time (and loses
    // the panel's state with the old ones), which is the regression this
    // ticket's lazy boundaries make easy to introduce by accident.
    const before = new Map(ADMIN_PANELS.map(([, testId]) => [testId, screen.getByTestId(testId)]));

    for (const [label, activeTestId] of ADMIN_PANELS) {
      fireEvent.click(screen.getByRole('tab', { name: label }));
      await waitFor(() =>
        expect(tabpanelOf(activeTestId)).not.toHaveAttribute('hidden'),
      );

      for (const [, testId] of ADMIN_PANELS) {
        const node = screen.getByTestId(testId);
        expect(node, `${testId} must survive switching to the ${label} tab`).toBe(
          before.get(testId),
        );
        // Only `hidden` differs between the selected panel and the others.
        expect(tabpanelOf(testId).hasAttribute('hidden')).toBe(testId !== activeTestId);
      }

      // The Review tabpanel is mounted throughout too — that is the one whose
      // in-flight poll the invariant was written for.
      expect(screen.getByTestId('review-file-input')).toBeInTheDocument();
    }
  });
});
