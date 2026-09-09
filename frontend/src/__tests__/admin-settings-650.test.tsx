/**
 * admin-settings-650.test.tsx — the Settings tab (AdminSettings.tsx, issue
 * #650, epic #649).
 *
 * ## What this ticket actually asks for, after the owner decision
 *
 * #650 was filed as a feature-switch REGISTRY — an admin control that turns
 * notes mode on and off from inside the app. The owner overturned that on
 * 2026-09-01 ("we can just leave the feature flag in compose file forever"),
 * leaving this ticket as the Settings *surface*: the tab beside Diagnostics
 * that later hosts secret rotation (#651), deployed-version identity (#652)
 * and spend (#653) — and, today, the one thing kept from the original
 * design, epic #649's through-line:
 *
 *   the UI can answer "why is the product behaving this way?" — internal
 *   notes are unavailable BECAUSE THE DEPLOYMENT HAS NOT ENABLED THEM — as
 *   read-only diagnostic text rather than as a control.
 *
 * So the assertions here are, in order of what would actually regress:
 *
 *   - the unavailable row EXPLAINS. A row that only repeats the Review tab's
 *     own "unavailable" answers nothing, which is the whole defect: it must
 *     name the deployment as the decider AND the variable that decides;
 *   - the available row does NOT tell an operator to go set a variable that
 *     is already set, and points at where the per-review choice really lives;
 *   - there is NO CAPABILITY CONTROL. The owner's decision is a boundary,
 *     not a detail: no checkbox, switch, radio or select may appear on this
 *     screen, or the registry has grown back. (Issue #678 later added the
 *     entity-roster editor here — business data, not a switch, and with no
 *     fail-safe direction to protect; see admin-entity-roster-678.test.tsx.)
 *   - the fail-safe direction survives a payload that does not carry the
 *     field at all (a backend older than the frontend — #613's documented,
 *     twice-observed skew): anything but an explicit `true` reads as "not
 *     enabled", never as "enabled";
 *   - a 403 hides the panel (defense in depth, the same posture as every
 *     other admin panel), and a rotation re-load un-hides it (#635);
 *   - a failed load is TERMINAL — error plus a working retry, never an error
 *     and a spinner at once (#439) — and its copy carries no HTTP status or
 *     endpoint (#425);
 *   - through the real App: an admin gets the tab and can deep-link to it; a
 *     non-admin never mounts the panel at all (#234/#235).
 *
 * The panel reads `notes_mode_available` from GET /api/me/preferences — the
 * SAME field the Review tab's control reads, written by
 * `backend/src/user_preferences.py::get_preferences` as
 * `config.notes_mode_enabled()`. Every stubbed body below is that route's
 * real shape (`{ preferences, notes_mode_available }`).
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AdminSettings, { capabilityRows } from '../AdminSettings';
import App from '../App';

// `../auth` is deliberately NOT mocked: this file mounts the whole App as
// well as the panel on its own, and forcing password mode would put the
// login form on screen instead of the app shell. The Amplify session below
// is the only credential seam `authorizedFetch` needs, same as
// admin-tab-grouping-599.test.tsx.
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

/** One canned answer for every call, in GET /api/me/preferences' real shape. */
function stubPreferences(response: { status: number; body: unknown }): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async () => ({
    ok: response.status >= 200 && response.status < 300,
    status: response.status,
    json: async () => response.body,
  }));
  vi.stubGlobal('fetch', impl);
  return impl as unknown as ReturnType<typeof vi.fn>;
}

/** A first answer, then a different one for every later call — the shape a
 *  retry (or a post-rotation re-load) needs to be observable. */
function stubPreferencesSequence(
  responses: { status: number; body: unknown }[],
): ReturnType<typeof vi.fn> {
  let call = 0;
  const impl = vi.fn(async () => {
    const response = responses[Math.min(call, responses.length - 1)];
    call += 1;
    return {
      ok: response.status >= 200 && response.status < 300,
      status: response.status,
      json: async () => response.body,
    };
  });
  vi.stubGlobal('fetch', impl);
  return impl as unknown as ReturnType<typeof vi.fn>;
}

function preferencesBody(notesModeAvailable: boolean): unknown {
  // Exactly what `get_preferences` returns: the resolved preference map plus
  // the projected kill switch. `notes_mode` is the documented default.
  return { preferences: { notes_mode: 'external' }, notes_mode_available: notesModeAvailable };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// The copy itself — asserted on the exported row builder, so the CLAIMS are
// pinned without pinning a sentence. Each assertion below is a property the
// row must have to do its job, not a transcription of its wording.
// ---------------------------------------------------------------------------

describe('the capability row explains rather than repeats (#650, epic #649)', () => {
  it('names the deployment as the decider and the variable that decides, when internal notes are off', () => {
    const [row] = capabilityRows({ internalNotes: false });

    expect(row.enabled).toBe(false);
    // The defect this ticket exists to close: the Review tab already says
    // "unavailable". A row that says no more than that is not an
    // explanation, so both halves of the cause must be present.
    expect(row.why).toMatch(/deployment has not enabled/i);
    expect(row.why).toContain('NOTES_MODE_ENABLED');
    // …and where it is actually set. Epic #649's evidence: the deploy host's
    // environment-variable list does NOT reach an inline `environment:`
    // block, which is what made the #648 attempt look like a no-op.
    expect(row.why).toContain('environment:');
    // The state must say WHO decided, not just yes/no.
    expect(row.state).toMatch(/deployment/i);
  });

  it('does not tell an operator to set a variable that is already set, when internal notes are on', () => {
    const [row] = capabilityRows({ internalNotes: true });

    expect(row.enabled).toBe(true);
    expect(row.why).not.toContain('NOTES_MODE_ENABLED');
    // The per-review choice lives on the Review tab — the reason there is no
    // control here. If this stopped being said, the screen would read as a
    // toggle that failed to render.
    expect(row.why).toContain('Review');
  });
});

// ---------------------------------------------------------------------------
// The panel
// ---------------------------------------------------------------------------

describe('AdminSettings panel (#650)', () => {
  it('renders the unavailable capability with its explanation', async () => {
    stubPreferences({ status: 200, body: preferencesBody(false) });

    render(<AdminSettings />);

    const why = await screen.findByTestId('capability-why-internal-notes');
    expect(why.textContent ?? '').toBe(capabilityRows({ internalNotes: false })[0].why);
    expect(screen.getByTestId('capability-state-internal-notes').textContent).toBe(
      capabilityRows({ internalNotes: false })[0].state,
    );
  });

  it('renders the enabled capability when the deployment allows internal notes', async () => {
    stubPreferences({ status: 200, body: preferencesBody(true) });

    render(<AdminSettings />);

    const why = await screen.findByTestId('capability-why-internal-notes');
    expect(why.textContent ?? '').toBe(capabilityRows({ internalNotes: true })[0].why);
    // The enabled row must not carry the "how to turn it on" instructions.
    expect(screen.getByTestId('admin-settings-panel').textContent ?? '').not.toContain(
      'NOTES_MODE_ENABLED',
    );
  });

  it('reads a payload with no notes_mode_available at all as NOT enabled (fail-safe)', async () => {
    // Reachable in production: the frontend bundle and the backend image can
    // be different commits on this deployment — #613's cache-hit skew, seen
    // twice on 2026-08-23 — so a browser running this code can be talking to
    // a backend that predates the field. A missing gate must never read as
    // an open gate for a #572-gated capability.
    stubPreferences({ status: 200, body: { preferences: {} } });

    render(<AdminSettings />);

    const state = await screen.findByTestId('capability-state-internal-notes');
    expect(state.textContent).toBe(capabilityRows({ internalNotes: false })[0].state);
  });

  it('offers NO capability control — the owner dropped the switch registry (#650, 2026-09-01)', async () => {
    // This blanket stub answers every route with the preferences body, which
    // carries no `roster_store_available` — so #678's entity-roster editor
    // correctly renders as "this deployment keeps no roster" and no control
    // at all. The same no-switch assertion against a deployment that DOES
    // have a roster store (where the roster's own textbox is present and is
    // the only one) lives in admin-entity-roster-678.test.tsx; keep both, or
    // this one silently stops covering the store-available case.
    stubPreferences({ status: 200, body: preferencesBody(false) });

    render(<AdminSettings />);

    const panel = await screen.findByTestId('admin-settings-panel');
    await screen.findByTestId('admin-settings-roster-panel');
    for (const role of ['checkbox', 'switch', 'radio', 'combobox', 'slider'] as const) {
      expect(within(panel).queryAllByRole(role)).toHaveLength(0);
    }
    expect(within(panel).queryAllByRole('textbox')).toHaveLength(1);

    // Capabilities panel itself has no controls or buttons
    const capabilities = screen.getByTestId('admin-settings-capabilities-panel');
    for (const role of ['checkbox', 'switch', 'radio', 'combobox', 'textbox', 'slider', 'button'] as const) {
      expect(within(capabilities).queryAllByRole(role)).toHaveLength(0);
    }
  });

  it('hides itself entirely on a 403 (defense in depth)', async () => {
    stubPreferences({ status: 403, body: { detail: 'forbidden' } });

    const { container } = render(<AdminSettings />);

    await waitFor(() => expect(container.querySelector('[data-testid="admin-settings-panel"]')).toBeNull());
  });

  it('re-loads on credentialsRefreshKey so a rotation un-hides it (#635)', async () => {
    // First load 403s (the caller still holds the shipped default password —
    // /api/me is exempt from that enforcement, this route is not), then the
    // rotation lands and the same route answers.
    stubPreferencesSequence([
      { status: 403, body: { detail: 'forbidden' } },
      { status: 200, body: preferencesBody(false) },
    ]);

    const { container, rerender } = render(<AdminSettings credentialsRefreshKey={0} />);
    await waitFor(() =>
      expect(container.querySelector('[data-testid="admin-settings-panel"]')).toBeNull(),
    );

    rerender(<AdminSettings credentialsRefreshKey={1} />);

    expect(await screen.findByTestId('capability-why-internal-notes')).toBeInTheDocument();
  });

  it('a failed load is terminal: an error plus a working retry, and no spinner', async () => {
    stubPreferencesSequence([
      { status: 500, body: { detail: 'boom' } },
      { status: 200, body: preferencesBody(true) },
    ]);

    render(<AdminSettings />);

    const error = await screen.findByTestId('admin-settings-error');
    expect(screen.queryByTestId('admin-settings-loading')).toBeNull();
    // Issue #425: no status code, no endpoint path in reader-facing copy.
    expect(error.textContent ?? '').not.toMatch(/500|\/api\//);

    fireEvent.click(screen.getByTestId('admin-settings-retry'));

    expect(await screen.findByTestId('capability-why-internal-notes')).toBeInTheDocument();
    expect(screen.queryByTestId('admin-settings-error')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Through the real App — the tab itself
// ---------------------------------------------------------------------------

const ADMIN_ROUTES: Record<string, unknown> = {
  '/version': {
    version: '0.0.1',
    commit: 'abcdef1234567890',
    image_digest: 'sha256:x',
    uptime_seconds: 1,
  },
  '/api/me': { is_admin: true },
  '/api/me/preferences': preferencesBody(false),
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
    key_fingerprint: '',
    updated_at: '',
    updated_by: '',
  },
  // Issue #678: the panel's fourth loader. Inert here (no store) so the tab
  // assertions below see the same screen they always did.
  '/api/admin/entity-roster': {
    setting_id: 'global',
    roster_store_available: false,
    entities: [],
    max_entities: 200,
    max_name_length: 200,
    updated_at: '',
    updated_by: '',
  },
};

function stubRoutes(routes: Record<string, unknown>): void {
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

describe('the Settings tab in the app shell (#650)', () => {
  it('an admin caller deep-linking to "#/admin/settings" opens directly to Settings', async () => {
    window.location.hash = '#/admin/settings';
    await new Promise((resolve) => setTimeout(resolve, 0));
    stubRoutes(ADMIN_ROUTES);

    render(<App />);

    const sections = await screen.findByRole('tablist', { name: 'Sections' });
    const settingsTab = await within(sections).findByRole('tab', { name: 'Settings' });
    await waitFor(() => expect(settingsTab).toHaveAttribute('aria-selected', 'true'));
    // The panel is really the one behind that tab, and it has loaded.
    expect(await screen.findByTestId('capability-why-internal-notes')).toBeInTheDocument();
  });

  it('a non-admin caller gets no Settings tab and never mounts the panel', async () => {
    window.location.hash = '';
    stubRoutes({
      '/version': ADMIN_ROUTES['/version'],
      '/api/me': { is_admin: false },
      '/api/me/preferences': ADMIN_ROUTES['/api/me/preferences'],
    });

    render(<App />);

    await screen.findByTestId('version-display');
    expect(screen.queryByRole('tab', { name: 'Settings' })).toBeNull();
    expect(screen.queryByTestId('admin-settings-panel')).toBeNull();
  });
});
