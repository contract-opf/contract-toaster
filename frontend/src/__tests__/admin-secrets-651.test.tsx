/**
 * admin-secrets-651.test.tsx — the secrets inventory on the Settings tab
 * (AdminSettings.tsx, issue #651, epic #649).
 *
 * ## What #651 asks of the UI
 *
 * An admin must be able to see WHICH secret is loaded, when it was set and by
 * whom, and how to rotate it — **without the value, or any part of it, ever
 * reaching the browser.** The write half already lives on the Models tab; this
 * screen is the read-only inventory that says where things stand.
 *
 * So the assertions here are, in order of what would actually regress:
 *
 *   - the fingerprint is what is shown, and it is NOT a mask. The backend
 *     used to answer `key_hint` — the key's own last four characters — and
 *     this screen must never render a field of that kind again;
 *   - the rows EXPLAIN rotation rather than offering it: each one names where
 *     the secret is actually set, and the model-key row names the screen that
 *     rotates it (which really exists — "Models" — because copy that points
 *     an admin at a dead end is worse than no copy);
 *   - the session-signing secret says plainly what #651's own carve-out
 *     demanded: rotating it signs everyone out, which is why it is rotated in
 *     the deployment and not here;
 *   - there is still NO CONTROL on this screen (#650's owner decision). The
 *     new table must not have smuggled in an input for a credential;
 *   - a 403 hides the panel — the inventory reads an admin-only route;
 *   - the two loads are independent and each failure is terminal: the
 *     capability table survives a secrets failure, and vice versa (#439).
 *
 * The stubbed body is exactly `GET /api/admin/model-key`'s real shape
 * (backend/src/model_settings.py::get_model_key_settings): the same keys, and
 * a `key_fingerprint` that is what that function returns — eight hex
 * characters — never a masked value, because the server cannot produce one.
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AdminSettings, { secretRows } from '../AdminSettings';
import { type ModelKeySettings } from '../AdminModel';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

/** GET /api/admin/model-key's real shape. */
function keySettings(overrides: Partial<ModelKeySettings> = {}): ModelKeySettings {
  return {
    setting_id: 'global',
    key_store_available: true,
    model_provider: 'openrouter',
    key_set: false,
    key_source: null,
    key_fingerprint: '',
    updated_at: '',
    updated_by: '',
    ...overrides,
  };
}

const PREFERENCES = { preferences: { notes_mode: 'external' }, notes_mode_available: true };

/**
 * Route-aware fetch stub. The panel makes TWO calls now, and a stub that
 * answered both with one body would hide exactly the bug this file is here
 * for — a screen that renders the wrong payload's fields.
 */
function stubRoutes(
  responses: Record<string, { status: number; body: unknown } | { status: number; body: unknown }[]>,
): ReturnType<typeof vi.fn> {
  const counts: Record<string, number> = {};
  const impl = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    const entry = responses[pathname];
    if (entry === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    const sequence = Array.isArray(entry) ? entry : [entry];
    const index = Math.min(counts[pathname] ?? 0, sequence.length - 1);
    counts[pathname] = (counts[pathname] ?? 0) + 1;
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

function stubOk(key: ModelKeySettings): ReturnType<typeof vi.fn> {
  return stubRoutes({
    '/api/me/preferences': { status: 200, body: PREFERENCES },
    '/api/admin/model-key': { status: 200, body: key },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// The copy, asserted on the exported row builder — the CLAIMS are pinned
// without pinning a sentence, same convention as capabilityRows (#650).
// ---------------------------------------------------------------------------

function rowById(key: ModelKeySettings | null, id: string) {
  const row = secretRows(key).find((candidate) => candidate.id === id);
  if (!row) {
    throw new Error(`no secret row ${id}`);
  }
  return row;
}

describe('the secrets inventory explains rather than exposes (#651)', () => {
  it('an app-set key shows its fingerprint and says it can never be read back', () => {
    const row = rowById(
      keySettings({ key_set: true, key_source: 'admin', key_fingerprint: 'a1b2c3d4' }),
      'model-api-key',
    );

    expect(row.fingerprint).toBe('a1b2c3d4');
    // The rotation copy has to carry both halves an operator needs: where to
    // do it, and that looking the old value up is not an option.
    expect(row.rotation).toMatch(/Models/);
    expect(row.rotation).toMatch(/read the saved key back|never/i);
    expect(row.rotation).toMatch(/fingerprint/i);
  });

  it('renders when and by whom the key was last set', () => {
    // 1 January 2021, 00:00:00 UTC as unix seconds — the shape the backend
    // stores (`str(int(time.time()))`). Asserted against the same conversion
    // rather than a literal date, because the rendered string is the reader's
    // LOCAL time and a literal would only pass in one timezone.
    const at = (updated_at: string) =>
      rowById(
        keySettings({
          key_set: true,
          key_source: 'admin',
          key_fingerprint: 'a1b2c3d4',
          updated_at,
          updated_by: 'admin-1',
        }),
        'model-api-key',
      ).lastSet;

    expect(at('1609459200')).toBe(`${new Date(1609459200 * 1000).toLocaleString()} by admin-1`);
    // Genuinely derived from the stamp, not a constant that happens to parse:
    // a different second renders differently.
    expect(at('1609459200')).not.toBe(at('1612137600'));
  });

  it('renders no "last set" at all for a stamp the backend never wrote', () => {
    // '' is what get_model_key_settings returns when no row exists; a screen
    // that ran it through Date() would print "Invalid Date" or the epoch.
    for (const updated_at of ['', '0', 'not-a-number']) {
      expect(
        rowById(keySettings({ key_set: true, key_source: 'admin', updated_at }), 'model-api-key')
          .lastSet,
      ).toBe('');
    }
  });

  it('says nothing about "when" for a key this app never wrote', () => {
    const row = rowById(
      keySettings({ key_set: true, key_source: 'env', key_fingerprint: 'a1b2c3d4' }),
      'model-api-key',
    );

    expect(row.lastSet).toBe('');
    // It still fingerprints — an env key is the one actually in force, and an
    // operator needs to know which one it is.
    expect(row.fingerprint).toBe('a1b2c3d4');
    expect(row.rotation).toMatch(/OPENROUTER_API_KEY/);
  });

  it('warns, rather than shrugs, when no key is configured anywhere', () => {
    const row = rowById(keySettings(), 'model-api-key');

    expect(row.stateVariant).toBe('danger');
    expect(row.rotation).toMatch(/every review will fail/i);
    expect(row.fingerprint).toBe('');
  });

  it('does not call an absent key "set by the deployment" when there is no app store', () => {
    // The AWS/Bedrock target: `key_store_available` is false, and the key may
    // still be absent. Two different answers, and only one of them is true.
    const noStore = (key_source: 'env' | null) =>
      rowById(
        keySettings({
          key_store_available: false,
          key_set: key_source !== null,
          key_source,
          key_fingerprint: key_source === null ? '' : 'a1b2c3d4',
        }),
        'model-api-key',
      );

    expect(noStore('env').state).toMatch(/deployment/i);
    expect(noStore(null).state).toMatch(/not set/i);
    // Neither one sends the admin to a rotation screen this target has not got.
    expect(noStore(null).rotation).not.toMatch(/Rotate it under/);
  });

  it('distinguishes "not set" from "we could not ask"', () => {
    // Only one of those is an emergency, and a screen that renders a failed
    // load as "Not set" would send an admin to rotate a working key.
    const unknown = rowById(null, 'model-api-key');
    expect(unknown.state).not.toMatch(/not set/i);
    expect(unknown.rotation).toMatch(/could not read/i);
  });

  it('states plainly that rotating the session secret signs everyone out', () => {
    // #651 put DEMO_TOKEN_SECRET in scope ONLY if the UI could say this
    // plainly. It is out of scope for rotation here, so the row has to carry
    // the sentence AND say where it is actually rotated.
    const row = rowById(keySettings(), 'session-signing-secret');

    expect(row.rotation).toMatch(/DEMO_TOKEN_SECRET/);
    expect(row.rotation).toMatch(/signed out|signs everyone out/i);
    expect(row.rotation).toMatch(/deployment/i);
    // No fingerprint is claimed for a secret no endpoint reports one for —
    // inventing one would need the value in the browser.
    expect(row.fingerprint).toBe('');
  });

  it('never derives a row field from a secret’s own characters', () => {
    // The regression guard: `key_hint` was `"…" + api_key.slice(-4)`. No row
    // field may look like that again, whatever the payload says.
    for (const key of [
      keySettings({ key_set: true, key_source: 'admin', key_fingerprint: 'a1b2c3d4' }),
      keySettings({ key_set: true, key_source: 'env', key_fingerprint: 'a1b2c3d4' }),
      keySettings(),
      null,
    ]) {
      for (const row of secretRows(key)) {
        expect(row.fingerprint).not.toMatch(/…/);
        expect(row.fingerprint).not.toMatch(/sk-or/);
        expect(`${row.state} ${row.lastSet}`).not.toMatch(/sk-or|…[A-Za-z0-9]{4}/);
      }
    }
  });
});

// ---------------------------------------------------------------------------
// The panel.
// ---------------------------------------------------------------------------

describe('the Settings panel renders the inventory (#651)', () => {
  it('shows the fingerprint of a saved key, and no masked value anywhere', async () => {
    stubOk(
      keySettings({
        key_set: true,
        key_source: 'admin',
        key_fingerprint: 'a1b2c3d4',
        updated_at: '1609459200',
        updated_by: 'admin-1',
      }),
    );

    render(<AdminSettings />);

    const fingerprint = await screen.findByTestId('secret-fingerprint-model-api-key');
    expect(fingerprint).toHaveTextContent('a1b2c3d4');
    expect(await screen.findByTestId('secret-last-set-model-api-key')).toHaveTextContent('admin-1');
    // Nothing on the screen is a mask: the ellipsis-plus-tail shape the old
    // `key_hint` rendered must not appear.
    expect(document.body.textContent ?? '').not.toMatch(/…[A-Za-z0-9]{4}/);
  });

  it('lists the session-signing secret alongside it, as deployment-managed', async () => {
    stubOk(keySettings({ key_set: true, key_source: 'env', key_fingerprint: 'a1b2c3d4' }));

    render(<AdminSettings />);

    const row = await screen.findByTestId('secret-rotation-session-signing-secret');
    expect(row).toHaveTextContent(/signed out/i);
    expect(await screen.findByTestId('secret-fingerprint-session-signing-secret')).toHaveTextContent(
      '—',
    );
  });

  it('offers NO control — the inventory reads, it never rotates', async () => {
    stubOk(keySettings({ key_set: true, key_source: 'admin', key_fingerprint: 'a1b2c3d4' }));

    render(<AdminSettings />);

    const panel = await screen.findByTestId('admin-settings-secrets-panel');
    for (const role of ['checkbox', 'switch', 'radio', 'combobox', 'textbox', 'slider'] as const) {
      expect(within(panel).queryAllByRole(role)).toHaveLength(0);
    }
    expect(within(panel).queryAllByRole('button')).toHaveLength(0);
    // Specifically: no password field. The rotation form lives on Models, and
    // duplicating a credential input here would mean two places to keep
    // write-only.
    expect(panel.querySelectorAll('input')).toHaveLength(0);
  });

  it('hides the whole panel on a 403 — the inventory is admin-only', async () => {
    stubRoutes({
      '/api/me/preferences': { status: 200, body: PREFERENCES },
      '/api/admin/model-key': { status: 403, body: { detail: 'Admin privilege required.' } },
    });

    const { container } = render(<AdminSettings />);

    await waitFor(() =>
      expect(container.querySelector('[data-testid="admin-settings-panel"]')).toBeNull(),
    );
  });

  it('keeps the capability table when only the secrets load fails, and retries both', async () => {
    stubRoutes({
      '/api/me/preferences': { status: 200, body: PREFERENCES },
      '/api/admin/model-key': [
        { status: 500, body: { detail: 'boom' } },
        { status: 200, body: keySettings({ key_set: true, key_source: 'admin', key_fingerprint: 'a1b2c3d4' }) },
      ],
    });

    render(<AdminSettings />);

    const error = await screen.findByTestId('admin-settings-secrets-error');
    // Issue #425: no status code and no endpoint path in reader-facing copy.
    expect(error.textContent ?? '').not.toMatch(/500|\/api\//);
    // Terminal: an error, never an error plus a spinner (#439).
    expect(screen.queryByTestId('admin-settings-secrets-loading')).toBeNull();
    // The independent load survived.
    expect(await screen.findByTestId('capability-why-internal-notes')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('admin-settings-retry'));

    expect(await screen.findByTestId('secret-fingerprint-model-api-key')).toHaveTextContent(
      'a1b2c3d4',
    );
    expect(screen.queryByTestId('admin-settings-secrets-error')).toBeNull();
  });
});
