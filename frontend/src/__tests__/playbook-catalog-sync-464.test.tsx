/**
 * playbook-catalog-sync-464.test.tsx — issue #464: the contract-type dial
 * went stale after an admin rename/remove because ReviewSubmission.tsx's
 * catalog copy (`fetchCatalog`, fetched once on mount) and AdminPlaybooks'
 * own copy (refreshed only for itself after a mutation) had no shared
 * signal between them. Both panels stay mounted at once (App.tsx's
 * `hidden`-attribute tab scheme), so the measured bug was real: rename the
 * only playbook in the Playbooks tab, switch to Review without reloading,
 * and the dial still showed the OLD name; remove it, and the dial kept
 * offering it as a selectable (but guaranteed-503) option instead of the
 * "nothing to review against" state.
 *
 * The fix threads a plain refresh signal (`catalogVersion`, App.tsx) from
 * AdminPlaybooks' mutation handlers to ReviewSubmission's own catalog
 * fetch, so both stay in sync without a reload. This test renders the
 * REAL <App/> (not the two components in isolation) so it exercises the
 * actual wiring between them, mutates through the real admin UI (not a
 * direct state poke), and asserts the dial's rendered options — the
 * pattern issue #464's own acceptance criteria calls for.
 *
 * Fully offline — aws-amplify/auth and @aws-amplify/ui-react are mocked;
 * fetch is a stateful stub over an in-memory catalog so a PATCH/DELETE
 * through the admin UI is reflected on the next GET /api/playbooks, same
 * as the real backend.
 *
 * IMPORTANT invariant: GET /api/playbooks below must hand back a
 * value-copy of `catalog`, never the same array/object references it
 * holds. ReviewSubmission.fetchCatalog stores whatever it's given via
 * setPlaybooks(entries) by reference; if the stub aliased `catalog`
 * directly, PATCH/DELETE mutating that shared object in place would make
 * the dial appear to "update" on the next render even with NO refetch at
 * all — i.e. even against production code where the #464 wiring was never
 * added. That false positive was caught and fixed for #464 fix-round-1:
 * confirmed the un-copied stub passed both tests against a pre-fix
 * worktree (HEAD c1b74fa, no catalogVersion/onCatalogChange present).
 */
import { describe, expect, it, vi } from 'vitest';
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

vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({
    user: { username: 'admin-sub', signInDetails: { loginId: 'admin@example.com' } },
    signOut: vi.fn(),
  }),
}));

interface CatalogEntry {
  playbook_id: string;
  display_name: string;
  status: 'active' | 'coming_soon';
  notes: string;
}

const STATIC_ROUTES: Record<string, unknown> = {
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

/**
 * A stateful fetch stub: GET /api/playbooks always reflects `catalog`'s
 * CURRENT contents (mutated in place by PATCH/DELETE below), same as the
 * real backend's `_load_playbook_catalog` reading live DB overrides on
 * every request — never a snapshot frozen at stub-setup time.
 */
function stubStatefulFetch(catalog: CatalogEntry[]): void {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    const method = (init?.method ?? 'GET').toUpperCase();

    if (method === 'GET' && pathname === '/api/playbooks') {
      // Return a value-copy, never a reference into `catalog`. If a
      // component stored the array returned here by reference (instead of
      // actually refetching), a later in-place PATCH/DELETE mutation below
      // would silently "update" that stale reference too, and the test
      // would pass even against unwired production code. Snapshotting on
      // every request is what forces a real refetch to be observable.
      const snapshot = JSON.parse(JSON.stringify(catalog)) as CatalogEntry[];
      return { ok: true, status: 200, json: async () => ({ playbooks: snapshot }) } as Response;
    }

    const renameMatch = /^\/api\/admin\/playbooks\/([^/]+)$/.exec(pathname);
    if (renameMatch && method === 'PATCH') {
      const playbookId = decodeURIComponent(renameMatch[1]);
      const body = JSON.parse((init?.body as string) ?? '{}') as { display_name?: string };
      const entry = catalog.find((e) => e.playbook_id === playbookId);
      if (entry && body.display_name) {
        entry.display_name = body.display_name;
      }
      return { ok: true, status: 200, json: async () => ({ ok: true }) } as Response;
    }
    if (renameMatch && method === 'DELETE') {
      const playbookId = decodeURIComponent(renameMatch[1]);
      const index = catalog.findIndex((e) => e.playbook_id === playbookId);
      if (index >= 0) {
        catalog.splice(index, 1);
      }
      return { ok: true, status: 200, json: async () => ({ ok: true }) } as Response;
    }

    if (method === 'GET' && pathname.endsWith('/versions')) {
      return { ok: true, status: 200, json: async () => ({ versions: [] }) } as Response;
    }

    const staticBody = STATIC_ROUTES[pathname];
    if (staticBody !== undefined) {
      return { ok: true, status: 200, json: async () => staticBody } as Response;
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', impl);
}

async function goToTab(name: string): Promise<void> {
  fireEvent.click(await screen.findByRole('tab', { name }));
}

describe('contract-type dial stays in sync with admin Playbooks mutations (#464)', () => {
  it('reflects a rename on the Review tab dial without a reload', async () => {
    const catalog: CatalogEntry[] = [
      {
        playbook_id: 'synthetic-nda-sample',
        display_name: 'Synthetic NDA Sample',
        status: 'active',
        notes: '',
      },
    ];
    stubStatefulFetch(catalog);

    render(<App />);
    await screen.findByTestId('version-display');

    // The Review tab's dial starts with the ORIGINAL name.
    await screen.findByTestId('review-playbook-dial');
    expect(screen.getByTestId('review-playbook-option-synthetic-nda-sample')).toHaveTextContent(
      'Synthetic NDA Sample',
    );

    // Rename it from the Playbooks admin tab — the real UI flow, not a
    // direct state poke.
    await goToTab('Playbooks');
    await screen.findByTestId('playbook-row-synthetic-nda-sample');
    fireEvent.click(screen.getByTestId('playbook-rename-synthetic-nda-sample'));
    fireEvent.change(screen.getByTestId('playbook-rename-input-synthetic-nda-sample'), {
      target: { value: 'Renamed NDA Playbook (464)' },
    });
    fireEvent.click(screen.getByTestId('playbook-rename-save-synthetic-nda-sample'));

    await waitFor(() => {
      expect(screen.getByTestId('playbook-row-synthetic-nda-sample')).toHaveTextContent(
        'Renamed NDA Playbook (464)',
      );
    });

    // Switch back to Review WITHOUT a reload — this component never
    // unmounted (every tabpanel stays mounted, only `hidden` toggles).
    await goToTab('Review');

    await waitFor(() => {
      expect(screen.getByTestId('review-playbook-option-synthetic-nda-sample')).toHaveTextContent(
        'Renamed NDA Playbook (464)',
      );
    });
  });

  it('shows the no-playbooks state on the Review tab after removing the last playbook, without a reload', async () => {
    const catalog: CatalogEntry[] = [
      {
        playbook_id: 'synthetic-nda-sample',
        display_name: 'Synthetic NDA Sample',
        status: 'active',
        notes: '',
      },
    ];
    stubStatefulFetch(catalog);

    render(<App />);
    await screen.findByTestId('version-display');
    await screen.findByTestId('review-playbook-dial');

    // Remove the only playbook from the Playbooks admin tab (§14 two-click
    // confirm: one click arms, a second removes).
    await goToTab('Playbooks');
    await screen.findByTestId('playbook-row-synthetic-nda-sample');
    const removeButton = screen.getByTestId('playbook-remove-synthetic-nda-sample');
    fireEvent.click(removeButton);
    fireEvent.click(removeButton);

    await waitFor(() => {
      expect(screen.getByTestId('admin-playbooks-empty')).toBeInTheDocument();
    });

    // Switch to Review WITHOUT a reload. This is Case 2 from the issue: the
    // dial must show the "nothing to review against" state, not keep
    // offering the removed playbook as a selectable dead option.
    await goToTab('Review');

    await waitFor(() => {
      expect(screen.queryByTestId('review-playbook-dial')).toBeNull();
      expect(
        screen.queryByTestId('review-playbook-option-synthetic-nda-sample'),
      ).toBeNull();
    });
    expect(screen.getByTestId('review-no-playbooks')).toBeInTheDocument();
  });
});
