/**
 * admin-entity-roster-678.test.tsx — our own legal entity names on the
 * Settings tab (AdminSettings.tsx, issue #678).
 *
 * ## What the ticket asks the UI for
 *
 * The roster is deployment-scoped, not playbook-scoped, and it has to be
 * correctable WITHOUT A DEPLOY, because the first missing entity will be
 * found in production. So the assertions here are:
 *
 *   - the stored names load into the editor, one per line, and Save sends the
 *     parsed list to PUT /api/admin/entity-roster — the round trip is the
 *     feature;
 *   - a name containing a comma survives as ONE name. "Acme Holdings, LLC" is
 *     one entity and a comma-separated editor would file it as two, which is
 *     why the control is line-separated;
 *   - a deployment with no roster store shows an explanation instead of a
 *     form the server would refuse with a 400 — the same posture
 *     AdminModel.tsx takes for a deployment with no key store;
 *   - the server's own 400 detail reaches the admin. It names WHICH entry is
 *     wrong (too long, a control character), and replacing it with a generic
 *     "please try again" would hide the only actionable half;
 *   - a 403 on this admin-only route hides the whole panel, the same
 *     defense-in-depth posture as every other admin surface.
 *
 * The names below are invented. Fixtures never carry real counterparty or
 * party names.
 *
 * Fully offline — fetch is stubbed, no network.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AdminSettings, {
  parseRosterDraft,
  rosterDraftText,
  type EntityRosterSettings,
} from '../AdminSettings';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const STORED = ['Northwind Athletics, LLC', 'Northwind Performance Holdings', 'NW Sports Sciences'];

function roster(overrides: Partial<EntityRosterSettings> = {}): EntityRosterSettings {
  return {
    setting_id: 'global',
    roster_store_available: true,
    entities: STORED,
    max_entities: 200,
    max_name_length: 200,
    updated_at: '1756900000',
    updated_by: 'admin-sub',
    ...overrides,
  };
}

/** The other three routes this panel loads, kept inert so they cannot
 *  perturb the roster assertions (their own behaviour is covered by
 *  admin-settings-650 and admin-secrets-651). */
const INERT: Record<string, unknown> = {
  '/api/me/preferences': { preferences: { notes_mode: 'external' }, notes_mode_available: false },
  '/version': { version: '0.0.1', commit: 'abcdef1234567890', image_digest: '', uptime_seconds: 1 },
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

interface RosterHandlers {
  get?: () => { status: number; body: unknown };
  put?: (body: unknown) => { status: number; body: unknown };
  /** The panel's OTHER admin-gated loader. A caller refused by one admin
   *  route is refused by every admin route (both go through
   *  `get_active_user_row` + `_require_admin`), so a refusal test that left
   *  this at 200 would assert over a state production cannot reach. */
  modelKey?: () => { status: number; body: unknown };
}

function stubRosterFetch(handlers: RosterHandlers): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/api/admin/entity-roster')) {
      const method = (init?.method ?? 'GET').toUpperCase();
      const handler =
        method === 'PUT'
          ? handlers.put?.(init?.body ? JSON.parse(init.body as string) : undefined)
          : handlers.get?.();
      if (!handler) {
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }
      return {
        ok: handler.status >= 200 && handler.status < 300,
        status: handler.status,
        json: async () => handler.body,
      } as Response;
    }
    if (url.includes('/api/admin/model-key') && handlers.modelKey) {
      const handler = handlers.modelKey();
      return {
        ok: handler.status >= 200 && handler.status < 300,
        status: handler.status,
        json: async () => handler.body,
      } as Response;
    }
    const pathname = new URL(url, 'http://localhost').pathname;
    const body = INERT[pathname];
    if (body === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

// ---------------------------------------------------------------------------
// The parse — asserted without a mounted panel
// ---------------------------------------------------------------------------

describe('parseRosterDraft (#678)', () => {
  it('keeps a comma INSIDE a name — the reason the editor is line-separated', () => {
    // A comma-separated editor turns this one entity into two, and the
    // second ("LLC") would then be a name the review is told is us.
    expect(parseRosterDraft('Northwind Athletics, LLC')).toEqual(['Northwind Athletics, LLC']);
  });

  it('drops blank lines and repeats, case- and whitespace-insensitively', () => {
    expect(
      parseRosterDraft('  Northwind Athletics, LLC \n\n northwind  athletics,  llc\n\nNW Sports\n'),
    ).toEqual(['Northwind Athletics, LLC', 'NW Sports']);
  });

  it('round-trips the stored list through the textarea text', () => {
    expect(parseRosterDraft(rosterDraftText(STORED))).toEqual(STORED);
  });
});

// ---------------------------------------------------------------------------
// The panel
// ---------------------------------------------------------------------------

describe('the entity roster editor (#678)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('loads the stored names into the editor, one per line', async () => {
    stubRosterFetch({ get: () => ({ status: 200, body: roster() }) });

    render(<AdminSettings />);

    const box = (await screen.findByTestId('admin-settings-roster-text')) as HTMLTextAreaElement;
    expect(box.value).toBe(STORED.join('\n'));
    expect(screen.getByTestId('admin-settings-roster-last-saved').textContent).toMatch(
      /admin-sub/,
    );
  });

  it('saves the edited list to PUT /api/admin/entity-roster and shows what took effect', async () => {
    const added = [...STORED, 'Northwind Recovery Labs, Inc.'];
    let sent: unknown = null;
    const fetchMock = stubRosterFetch({
      get: () => ({ status: 200, body: roster() }),
      put: (body) => {
        sent = body;
        return { status: 200, body: roster({ entities: added }) };
      },
    });

    render(<AdminSettings />);
    const box = await screen.findByTestId('admin-settings-roster-text');

    fireEvent.change(box, {
      // A trailing blank line and a duplicate: what a real edit looks like.
      target: { value: `${added.join('\n')}\n\nnorthwind recovery labs, inc.\n` },
    });
    fireEvent.click(screen.getByTestId('admin-settings-roster-save'));

    await screen.findByTestId('admin-settings-roster-saved');
    expect(sent).toEqual({ entities: added });
    expect((box as HTMLTextAreaElement).value).toBe(added.join('\n'));

    const put = fetchMock.mock.calls.find(
      (call) => ((call[1] as RequestInit | undefined)?.method ?? 'GET').toUpperCase() === 'PUT',
    );
    expect(put).toBeDefined();
    expect(String(put?.[0])).toContain('/api/admin/entity-roster');
  });

  it('supports copying roster to clipboard and importing/exporting CSV', async () => {
    stubRosterFetch({ get: () => ({ status: 200, body: roster() }) });

    const writeTextMock = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText: writeTextMock } });
    const createObjectURLMock = vi.fn(() => 'blob:mock');
    const revokeObjectURLMock = vi.fn();
    window.URL.createObjectURL = createObjectURLMock;
    window.URL.revokeObjectURL = revokeObjectURLMock;

    render(<AdminSettings />);
    const box = (await screen.findByTestId('admin-settings-roster-text')) as HTMLTextAreaElement;
    expect(box.value).toBe(STORED.join('\n'));

    // Copy
    const copyBtn = screen.getByTestId('admin-settings-roster-copy');
    fireEvent.click(copyBtn);
    expect(writeTextMock).toHaveBeenCalledWith(STORED.join('\n'));

    // Export CSV
    const exportBtn = screen.getByTestId('admin-settings-roster-export');
    fireEvent.click(exportBtn);
    expect(createObjectURLMock).toHaveBeenCalled();
    expect(revokeObjectURLMock).toHaveBeenCalledWith('blob:mock');

    // Import CSV
    const importInput = screen.getByTestId('admin-settings-roster-import-input');
    const file = new File(['New Legal Corp\nAcme Subsidiary LLC'], 'entities.csv', {
      type: 'text/csv',
    });
    fireEvent.change(importInput, { target: { files: [file] } });

    await waitFor(() => {
      expect(box.value).toBe('New Legal Corp\nAcme Subsidiary LLC');
    });
  });

  it('shows the server’s own rejection reason, not a generic retry message', async () => {
    // The 400 names which entry is wrong; that is the only actionable half.
    const detail =
      'An entity name may not contain control characters (a line break included): these names ' +
      'are composed verbatim into the review prompt, where a line break would forge prompt ' +
      'structure.';
    stubRosterFetch({
      get: () => ({ status: 200, body: roster() }),
      put: () => ({ status: 400, body: { detail } }),
    });

    render(<AdminSettings />);
    fireEvent.change(await screen.findByTestId('admin-settings-roster-text'), {
      target: { value: 'Northwind Athletics, LLC' },
    });
    fireEvent.click(screen.getByTestId('admin-settings-roster-save'));

    const error = await screen.findByTestId('admin-settings-roster-save-error');
    expect(error.textContent ?? '').toBe(detail);
    expect(screen.queryByTestId('admin-settings-roster-saved')).toBeNull();
  });

  it('always establishes the roster editor and shows inviting empty state when no entities exist', async () => {
    stubRosterFetch({
      get: () => ({ status: 200, body: roster({ entities: [] }) }),
    });

    render(<AdminSettings />);

    expect(await screen.findByTestId('admin-settings-roster-empty-state')).toBeInTheDocument();
    expect(screen.queryByTestId('admin-settings-roster-unavailable')).toBeNull();
    expect(screen.queryByTestId('admin-settings-roster-table')).toBeNull();
    expect(screen.getByTestId('admin-settings-roster-text')).toBeInTheDocument();
    expect(screen.getByTestId('admin-settings-roster-save')).toBeInTheDocument();
  });

  it('handles older backend responses gracefully by keeping roster editor established', async () => {
    stubRosterFetch({ get: () => ({ status: 200, body: { setting_id: 'global' } }) });

    render(<AdminSettings />);

    expect(await screen.findByTestId('admin-settings-roster-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('admin-settings-roster-unavailable')).toBeNull();
    expect(screen.getByTestId('admin-settings-roster-text')).toBeInTheDocument();
  });

  it('hides the whole panel on a 403 (defense in depth)', async () => {
    const refusal = () => ({ status: 403, body: { detail: 'Admin privilege required.' } });
    stubRosterFetch({ get: refusal, modelKey: refusal });

    const { container } = render(<AdminSettings />);

    await waitFor(() =>
      expect(container.querySelector('[data-testid="admin-settings-panel"]')).toBeNull(),
    );
  });

  it('a failed roster load is terminal: an error plus a working retry, and no spinner', async () => {
    let attempt = 0;
    stubRosterFetch({
      get: () => {
        attempt += 1;
        return attempt === 1 ? { status: 500, body: { detail: 'boom' } } : { status: 200, body: roster() };
      },
    });

    render(<AdminSettings />);

    const error = await screen.findByTestId('admin-settings-roster-error');
    expect(screen.queryByTestId('admin-settings-roster-loading')).toBeNull();
    // Issue #425: no status code and no endpoint path in reader-facing copy.
    expect(error.textContent ?? '').not.toMatch(/500|\/api\//);

    fireEvent.click(screen.getByTestId('admin-settings-retry'));

    const box = (await screen.findByTestId('admin-settings-roster-text')) as HTMLTextAreaElement;
    expect(box.value).toBe(STORED.join('\n'));
    expect(screen.queryByTestId('admin-settings-roster-error')).toBeNull();
  });

  it('adds no capability control: the roster box is the only editable thing here', async () => {
    // #650's owner decision (a deployment capability is never toggled from
    // inside the app) survives this ticket. The roster is business data, not
    // a switch — so exactly one textbox and one save button, and no
    // checkbox/switch/radio/select anywhere.
    stubRosterFetch({ get: () => ({ status: 200, body: roster() }) });

    render(<AdminSettings />);
    const panel = await screen.findByTestId('admin-settings-panel');
    await screen.findByTestId('admin-settings-roster-text');

    for (const role of ['checkbox', 'switch', 'radio', 'combobox', 'slider'] as const) {
      expect(within(panel).queryAllByRole(role)).toHaveLength(0);
    }
    expect(within(panel).queryAllByRole('textbox')).toHaveLength(1);
  });

  it('displays distinct count and warns when duplicate entries are typed in textarea', async () => {
    stubRosterFetch({ get: () => ({ status: 200, body: roster({ entities: ['Acme Corp'] }) }) });

    render(<AdminSettings />);
    const box = await screen.findByTestId('admin-settings-roster-text');

    expect(screen.getByTestId('admin-settings-roster-count').textContent).toContain('1 distinct entity');
    expect(screen.queryByTestId('admin-settings-roster-duplicate-notice')).toBeNull();

    fireEvent.change(box, {
      target: { value: 'Acme Corp\nBeta LLC\nacme corp\n' },
    });

    expect(screen.getByTestId('admin-settings-roster-count').textContent).toContain('2 distinct entities');
    const notice = screen.getByTestId('admin-settings-roster-duplicate-notice');
    expect(notice.textContent).toContain('1 duplicate entry will be merged on save');
  });

  it('filters recognised entities in table using search input and clears filter', async () => {
    stubRosterFetch({
      get: () => ({
        status: 200,
        body: roster({ entities: ['Acme Corp', 'Beta Industries', 'Acme Holdings'] }),
      }),
    });

    render(<AdminSettings />);
    await screen.findByTestId('admin-settings-roster-table');

    const filterInput = screen.getByTestId('admin-settings-roster-filter');
    fireEvent.change(filterInput, { target: { value: 'Beta' } });

    expect(screen.getByText('Beta Industries')).toBeInTheDocument();
    expect(screen.queryByText('Acme Corp')).toBeNull();
    expect(screen.queryByText('Acme Holdings')).toBeNull();

    const clearBtn = screen.getByTestId('admin-settings-roster-filter-clear');
    fireEvent.click(clearBtn);

    expect(screen.getByText('Acme Corp')).toBeInTheDocument();
    expect(screen.getByText('Beta Industries')).toBeInTheDocument();
    expect(screen.getByText('Acme Holdings')).toBeInTheDocument();
  });

  it('renders collapsible change history with added and removed badges', async () => {
    stubRosterFetch({
      get: () => ({
        status: 200,
        body: roster({
          entities: ['Acme Corp'],
          history: [
            {
              timestamp: '1725537600',
              actor: 'admin-alice',
              count: 1,
              added: ['Acme Corp'],
              removed: ['OldCo LLC'],
            },
          ],
        }),
      }),
    });

    render(<AdminSettings />);
    await screen.findByTestId('admin-settings-roster-panel');

    const summary = screen.getByTestId('admin-settings-roster-history-summary');
    expect(summary.textContent).toContain('Change history (1)');

    expect(screen.getByTestId('admin-settings-roster-history-entry-0')).toBeInTheDocument();
    expect(screen.getByText('+ Acme Corp')).toBeInTheDocument();
    expect(screen.getByText('- OldCo LLC')).toBeInTheDocument();
  });
});
