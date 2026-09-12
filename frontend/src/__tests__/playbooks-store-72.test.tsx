/**
 * playbooks-store-72.test.tsx — issue #72: ONE `GET /api/playbooks` for the
 * whole app, and an activation visible in every tab without a reload.
 *
 * ## What was actually wrong
 *
 * `ReviewSubmission.tsx` (the contract-type dial) and `AdminPlaybooks.tsx`
 * (the lifecycle table) each fetched the catalog on mount and kept a private
 * copy. Both are mounted for the whole session — App.tsx renders every
 * tabpanel once and only toggles `hidden` — so the second panel's copy could
 * never learn anything the first one did. Issue #464 patched the symptom with
 * a counter threaded through App.tsx in both directions; a third consumer
 * would have needed a third strand. `src/playbooksStore.ts` replaces the
 * counter with the catalog itself, held once.
 *
 * ## What this file asserts, and why in these terms
 *
 * The two claims that make the store worth having are countable, so they are
 * counted rather than described: N consumers cost ONE request, and one
 * `invalidateCatalog()` costs exactly ONE more that every consumer observes.
 * Each consumer records the full `{status, data, error}` snapshot it was
 * handed on every render, so "observes" means the transition sequence it
 * actually saw — not merely that the final DOM agrees.
 *
 * The panels' own screens are covered where they already were
 * (`admin-playbooks.test.tsx`, `playbook-selector.test.tsx`,
 * `playbook-catalog-sync-464.test.tsx` — that last one renders the REAL
 * <App/> and mutates through the real admin UI, which is what proves the
 * store is wired to the shipped panels and not only to the probes here).
 *
 * ## Fixtures
 *
 * `CATALOG` is the exact shape `backend/src/review_routes.py`'s
 * `_load_playbook_catalog` builds and `get_playbooks` serves:
 * `{playbook_id, display_name, status: "active" | "coming_soon", notes}`,
 * with `notes` the active version's admin-editable note or "". BOTH status
 * values are seeded, because `status === 'active'` is a live branch in the
 * dial's default-selection logic and in the console's projection — a
 * one-variant catalog would leave it green forever.
 *
 * Fully offline: `aws-amplify/auth` is mocked (api.ts's `getToken` imports it
 * dynamically on the sso path) and `fetch` is a stub.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import {
  getCatalog,
  getCatalogState,
  invalidateCatalog,
  subscribeCatalog,
  usePlaybookCatalog,
  type PlaybookCatalogEntry,
  type PlaybookCatalogState,
} from '../playbooksStore';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

/** The server's shape, verbatim — see this file's docstring. */
const CATALOG: PlaybookCatalogEntry[] = [
  {
    playbook_id: 'synthetic-nda-sample',
    display_name: 'Synthetic NDA Sample',
    status: 'active',
    notes: 'Active since the seed.',
  },
  {
    playbook_id: 'dpa',
    display_name: 'DPA',
    // Registered, never activated. The dial renders this one disabled, so the
    // `status === 'active'` branch has both sides seeded.
    status: 'coming_soon',
    notes: '',
  },
];

/** The same catalog after an admin activated the second playbook. */
const CATALOG_AFTER_ACTIVATION: PlaybookCatalogEntry[] = [
  CATALOG[0]!,
  { ...CATALOG[1]!, status: 'active', notes: 'Activated by the admin.' },
];

let catalogCalls: string[] = [];
let nextBody: PlaybookCatalogEntry[] = CATALOG;
let nextStatus = 200;
/**
 * Set true to make the NEXT catalog request hang until `release()` is called.
 * The response body is snapshotted when the request arrives, not when it is
 * released, so a held request really does answer the question that was asked
 * at the time — which is what makes the superseded-read test mean something.
 */
let holdNextCall = false;
let release: (() => void) | null = null;

function stubFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      const method = (init?.method ?? 'GET').toUpperCase();
      if (method === 'GET' && pathname === '/api/playbooks') {
        catalogCalls.push(`${method} ${pathname}`);
        if (nextStatus !== 200) {
          return { ok: false, status: nextStatus, json: async () => ({}) } as Response;
        }
        // A value-copy, never a reference into the fixture: a consumer that
        // held on to the previous array must not appear to "update" without a
        // real refetch (the false positive #464 fix-round-1 caught).
        const snapshot = JSON.parse(JSON.stringify(nextBody)) as PlaybookCatalogEntry[];
        if (holdNextCall) {
          holdNextCall = false;
          await new Promise<void>((resolve) => {
            release = resolve;
          });
        }
        return { ok: true, status: 200, json: async () => ({ playbooks: snapshot }) } as Response;
      }
      throw new Error(`unexpected request: ${method} ${pathname}`);
    }),
  );
}

function catalogCallCount(): number {
  return catalogCalls.filter((call) => call === 'GET /api/playbooks').length;
}

/**
 * Three independent consumers, each recording every snapshot it renders with.
 * Deliberately three separate components rather than one rendered three
 * times: the claim is about consumers of the hook, not about instances of one
 * component sharing a parent's state.
 */
const seen: Record<string, PlaybookCatalogState[]> = {};

function Consumer({ name }: { name: string }): React.ReactElement {
  const catalog = usePlaybookCatalog();
  (seen[name] ??= []).push(catalog);
  return (
    <div data-testid={`consumer-${name}`}>
      <span data-testid={`status-${name}`}>{catalog.status}</span>
      <span data-testid={`names-${name}`}>
        {(catalog.data ?? []).map((entry) => `${entry.display_name}:${entry.status}`).join('|')}
      </span>
      <span data-testid={`error-${name}`}>{String(catalog.error?.httpStatus ?? '')}</span>
    </div>
  );
}

function ThreeConsumers(): React.ReactElement {
  return (
    <>
      <Consumer name="dial" />
      <Consumer name="admin" />
      <Consumer name="history" />
    </>
  );
}

const CONSUMERS = ['dial', 'admin', 'history'] as const;

beforeEach(() => {
  catalogCalls = [];
  nextBody = CATALOG;
  nextStatus = 200;
  holdNextCall = false;
  release = null;
  for (const name of CONSUMERS) {
    delete seen[name];
  }
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('playbooksStore — one catalog for every consumer (#72)', () => {
  it('serves three consumers mounted together from exactly ONE GET /api/playbooks', async () => {
    render(<ThreeConsumers />);

    for (const name of CONSUMERS) {
      await waitFor(() => expect(screen.getByTestId(`status-${name}`).textContent).toBe('ready'));
    }
    // The whole point of the store. Before it, this was three.
    expect(catalogCallCount()).toBe(1);

    // Same catalog, same order, in every consumer — including the
    // registered-but-unactivated entry.
    for (const name of CONSUMERS) {
      expect(screen.getByTestId(`names-${name}`).textContent).toBe(
        'Synthetic NDA Sample:active|DPA:coming_soon',
      );
    }
  });

  it('refetches ONCE on invalidateCatalog(), and every subscribed consumer sees the new catalog', async () => {
    render(<ThreeConsumers />);
    for (const name of CONSUMERS) {
      await waitFor(() => expect(screen.getByTestId(`status-${name}`).textContent).toBe('ready'));
    }
    expect(catalogCallCount()).toBe(1);

    // An admin activates the second playbook in the Playbooks tab. That is
    // the only call the mutation handler makes on this module.
    nextBody = CATALOG_AFTER_ACTIVATION;
    invalidateCatalog();

    await waitFor(() =>
      expect(screen.getByTestId('names-dial').textContent).toBe(
        'Synthetic NDA Sample:active|DPA:active',
      ),
    );
    // One invalidation, one request — not one per consumer.
    expect(catalogCallCount()).toBe(2);

    for (const name of CONSUMERS) {
      expect(screen.getByTestId(`names-${name}`).textContent).toBe(
        'Synthetic NDA Sample:active|DPA:active',
      );
    }
  });

  it('hands every consumer the same {status, data, error} transitions', async () => {
    render(<ThreeConsumers />);
    for (const name of CONSUMERS) {
      await waitFor(() => expect(screen.getByTestId(`status-${name}`).textContent).toBe('ready'));
    }
    nextBody = CATALOG_AFTER_ACTIVATION;
    invalidateCatalog();
    await waitFor(() =>
      expect(screen.getByTestId('names-admin').textContent).toBe(
        'Synthetic NDA Sample:active|DPA:active',
      ),
    );

    for (const name of CONSUMERS) {
      const snapshots = seen[name] ?? [];
      // The sequence of DISTINCT snapshots this consumer was handed. React may
      // re-render a component for its own reasons; what must hold is the order
      // of the store states it observed, and that it observed all of them.
      const distinct = snapshots.filter((snap, index) => index === 0 || snap !== snapshots[index - 1]);
      expect(distinct.map((snap) => snap.status)).toEqual(['loading', 'ready', 'ready']);
      expect(distinct[0]).toEqual({ status: 'loading', data: null, error: null });
      expect(distinct[1]!.data).toEqual(CATALOG);
      expect(distinct[1]!.error).toBeNull();
      expect(distinct[2]!.data).toEqual(CATALOG_AFTER_ACTIVATION);
      expect(distinct[2]!.error).toBeNull();
    }

    // …and all three were handed the SAME objects, not three equal copies.
    const latest = (name: string): PlaybookCatalogState => {
      const snapshots = seen[name]!;
      return snapshots[snapshots.length - 1]!;
    };
    expect(latest('admin')).toBe(latest('dial'));
    expect(latest('history')).toBe(latest('dial'));
  });

  it('reports a failed read as {status: failed} with the HTTP status, and recovers on the next invalidation', async () => {
    nextStatus = 500;
    render(<ThreeConsumers />);

    for (const name of CONSUMERS) {
      await waitFor(() => expect(screen.getByTestId(`status-${name}`).textContent).toBe('failed'));
      // The status is what a panel branches on (AdminPlaybooks hides itself on
      // 403 rather than rendering an error). No copy is carried here.
      expect(screen.getByTestId(`error-${name}`).textContent).toBe('500');
    }
    expect(catalogCallCount()).toBe(1);

    nextStatus = 200;
    invalidateCatalog();

    for (const name of CONSUMERS) {
      await waitFor(() => expect(screen.getByTestId(`status-${name}`).textContent).toBe('ready'));
    }
    expect(catalogCallCount()).toBe(2);
  });

  it('carries the 403 a panel hides itself on, rather than a message', async () => {
    nextStatus = 403;
    render(<ThreeConsumers />);

    await waitFor(() => expect(screen.getByTestId('status-admin').textContent).toBe('failed'));
    expect(screen.getByTestId('error-admin').textContent).toBe('403');
    // Nothing rendered from the store is a sentence — the panels own their
    // copy (CLAUDE.md: escaped text only, and no raw server detail on screen).
    expect(document.body.textContent).not.toMatch(/HTTP\s*\d/i);
    expect(document.body.textContent).not.toContain('/api/');
  });
});

describe('playbooksStore — the module API, without React (#72)', () => {
  it('memoises getCatalog() until invalidateCatalog(), and notifies subscribers', async () => {
    const notifications: PlaybookCatalogState[] = [];
    const unsubscribe = subscribeCatalog(() => notifications.push(getCatalogState()));

    const [a, b, c] = await Promise.all([getCatalog(), getCatalog(), getCatalog()]);
    expect(catalogCallCount()).toBe(1);
    // The same array, not three equal ones — there is one copy of the catalog.
    expect(a).toBe(b);
    expect(b).toBe(c);
    expect(a).toEqual(CATALOG);
    expect(notifications.map((snap) => snap.status)).toEqual(['ready']);

    nextBody = CATALOG_AFTER_ACTIVATION;
    invalidateCatalog();
    await waitFor(() => expect(catalogCallCount()).toBe(2));
    expect(getCatalogState().data).toEqual(CATALOG_AFTER_ACTIVATION);
    expect(notifications.map((snap) => snap.status)).toEqual(['ready', 'ready']);

    unsubscribe();
    // An unsubscribed listener hears nothing more, and with nothing subscribed
    // an invalidation costs no request until someone actually asks again.
    invalidateCatalog();
    expect(catalogCallCount()).toBe(2);
    await getCatalog();
    expect(catalogCallCount()).toBe(3);
    expect(notifications).toHaveLength(2);
  });

  it('does not memoise a failure — the next ask tries again', async () => {
    nextStatus = 500;
    await expect(getCatalog()).rejects.toThrow(/returned HTTP 500/);
    expect(getCatalogState().status).toBe('failed');
    expect(getCatalogState().error?.httpStatus).toBe(500);

    nextStatus = 200;
    await expect(getCatalog()).resolves.toEqual(CATALOG);
    expect(catalogCallCount()).toBe(2);
    expect(getCatalogState().status).toBe('ready');
  });

  it('never lets a superseded read overwrite the newer one', async () => {
    // The ordering hazard the generation counter exists for: a slow first read
    // that lands AFTER an invalidation must not put the stale catalog back.
    const unsubscribe = subscribeCatalog(() => {});
    holdNextCall = true;
    const stale = getCatalog().catch(() => undefined);
    await waitFor(() => expect(catalogCallCount()).toBe(1));
    expect(getCatalogState().status).toBe('loading');

    nextBody = CATALOG_AFTER_ACTIVATION;
    invalidateCatalog();
    await waitFor(() => expect(getCatalogState().data).toEqual(CATALOG_AFTER_ACTIVATION));
    expect(catalogCallCount()).toBe(2);

    // Now let the FIRST, stale read land. It answers a question nobody is
    // asking any more.
    release!();
    await stale;
    expect(getCatalogState().data).toEqual(CATALOG_AFTER_ACTIVATION);
    expect(getCatalogState().status).toBe('ready');
    expect(catalogCallCount()).toBe(2);
    unsubscribe();
  });
});
