/**
 * playbooksStore.ts — ONE shared, memoised `GET /api/playbooks` for the whole
 * SPA (issue #72; 2026-09-05 diagnostic finding G13, action B13).
 *
 * ## The bug this closes
 *
 * `ReviewSubmission.tsx` (the contract-type dial) and `AdminPlaybooks.tsx`
 * (the lifecycle table) each fetched the catalog on mount and kept a private
 * copy. Both panels are mounted for the whole session — App.tsx renders every
 * tabpanel once and only toggles `hidden` — so nothing ever re-ran the other
 * panel's fetch. Issue #464 patched the worst symptom with `catalogVersion`, a
 * bare counter threaded App.tsx → ReviewSubmission and a matching
 * `onCatalogChange` callback threaded AdminPlaybooks → App.tsx: two props, one
 * fetch each, and a third consumer would have needed a third strand. This
 * module replaces that wiring with the thing the counter was standing in for —
 * the catalog itself, held once.
 *
 * ## Shape, and why it is a module-level store rather than a context
 *
 * Same reasoning as `api.ts`'s session-expired notifier: `invalidateCatalog()`
 * is called from mutation handlers and from App.tsx's password-rotation
 * callback, neither of which is a place a hook can be called, and a provider
 * would have to wrap a tree that `main.tsx` does not otherwise own. The React
 * face of it is `usePlaybookCatalog()`, a `useSyncExternalStore` over the same
 * state — React 18.3 ships that hook, so this adds no dependency.
 *
 * Memory only. Nothing here touches `localStorage`/`sessionStorage` — see
 * CLAUDE.md's hard rule and `src/__tests__/security-posture.test.tsx`.
 *
 * ## Error copy lives in the panels, not here
 *
 * The state carries the HTTP status and the raw failure; each panel maps that
 * to its OWN user-facing sentence ("We couldn't load your playbooks…" vs.
 * "We couldn't load the list of contract types right now."), exactly as
 * before. What this module does own is the one `console.error` of the
 * technical detail — the job `friendlyErrorMessage` does for single-fetch call
 * sites (api.ts) — because there is now one fetch, so there should be one log
 * line, not one per consumer. The thrown message keeps the
 * `… returned HTTP <code>` shape the suite-wide console guard allows
 * (`src/__tests__/support/consoleErrorGuard.ts`).
 */
import { useEffect, useSyncExternalStore } from 'react';

import { authorizedFetch } from './api';

/** Catalog status. Exactly two values exist (issue #433 removed the third). */
export type PlaybookCatalogStatus = 'active' | 'coming_soon';

/**
 * Mirrors `backend/src/review_routes.py::_load_playbook_catalog`. Declared
 * here rather than in a panel now that the fetch is shared; `AdminPlaybooks`
 * re-exports it so its existing importers are unaffected.
 */
export interface PlaybookCatalogEntry {
  playbook_id: string;
  display_name: string;
  status: PlaybookCatalogStatus;
  /** The currently-active version's admin-editable note, or "". */
  notes: string;
}

/** What failed, in the terms a panel needs to decide what to render. */
export interface PlaybookCatalogError {
  /**
   * The status the server answered with, or `null` when the request never got
   * an answer (a dropped connection, a body that would not parse).
   * `AdminPlaybooks` reads this: a 403 hides the panel outright rather than
   * rendering an error, and no client-side admin claim is trusted for that.
   */
  httpStatus: number | null;
  /** The underlying failure. Logged once (below); never rendered. */
  cause: unknown;
}

/**
 * The snapshot every consumer sees.
 *
 * `status` is the SETTLED state, not "is a request in flight": a refetch
 * triggered by `invalidateCatalog()` leaves the previous `status`/`data` in
 * place until the new answer lands. That is deliberate and matches what the
 * two panels did before this module existed — neither dropped back to a
 * spinner after an admin mutation, and blanking the dial mid-rename would be
 * a regression, not a loading state. `status` is therefore `'loading'` only
 * until the FIRST answer.
 */
export type PlaybookCatalogState =
  | { status: 'loading'; data: null; error: null }
  | { status: 'ready'; data: PlaybookCatalogEntry[]; error: null }
  /** `data` is whatever was last read successfully — `null` if that never happened. */
  | { status: 'failed'; data: PlaybookCatalogEntry[] | null; error: PlaybookCatalogError };

const INITIAL_STATE: PlaybookCatalogState = { status: 'loading', data: null, error: null };

type Listener = () => void;

const listeners = new Set<Listener>();
let state: PlaybookCatalogState = INITIAL_STATE;
/** The in-flight (or completed) read, memoised until `invalidateCatalog()`. */
let inFlight: Promise<PlaybookCatalogEntry[]> | null = null;
/**
 * Bumped by every `invalidateCatalog()` / reset. A load whose generation is no
 * longer current writes nothing: an answer to a question that was asked again
 * must never overwrite the newer answer, whichever order they arrive in.
 */
let generation = 0;

function publish(next: PlaybookCatalogState): void {
  state = next;
  // Copy first: a listener that unsubscribes itself (React does, on unmount)
  // must not mutate the set being iterated.
  for (const listener of Array.from(listeners)) {
    listener();
  }
}

/** Subscribe to catalog changes. Returns an unsubscribe. */
export function subscribeCatalog(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The current snapshot. Stable by reference until something changes. */
export function getCatalogState(): PlaybookCatalogState {
  return state;
}

/** A non-OK answer, carrying the status the panels branch on. */
class HttpCatalogError extends Error {
  readonly httpStatus: number;

  constructor(httpStatus: number) {
    // The console guard's suite-wide allowance is this exact shape
    // (`/returned HTTP \d{3}/`) — keep it.
    super(`GET /api/playbooks returned HTTP ${httpStatus}`);
    this.name = 'HttpCatalogError';
    this.httpStatus = httpStatus;
  }
}

async function load(forGeneration: number): Promise<PlaybookCatalogEntry[]> {
  try {
    const response = await authorizedFetch('/api/playbooks');
    if (!response.ok) {
      throw new HttpCatalogError(response.status);
    }
    const body = (await response.json()) as { playbooks?: PlaybookCatalogEntry[] };
    const entries = body.playbooks ?? [];
    if (forGeneration === generation) {
      publish({ status: 'ready', data: entries, error: null });
    }
    return entries;
  } catch (err) {
    if (forGeneration === generation) {
      // The ONE place the technical detail is logged — see this module's
      // docstring. Rendered output only ever gets the panel's own copy.
      // eslint-disable-next-line no-console
      console.error(err);
      publish({
        status: 'failed',
        // Keep whatever was last read: a failed refresh does not un-know the
        // catalog, and the dial should not empty itself because one poll of
        // the list timed out.
        data: state.data,
        error: { httpStatus: err instanceof HttpCatalogError ? err.httpStatus : null, cause: err },
      });
    }
    throw err;
  }
}

/**
 * The catalog, fetched at most once per invalidation however many consumers
 * ask. Rejects if the read failed; the rejection carries no copy — read
 * `getCatalogState().error` for that.
 */
export function getCatalog(): Promise<PlaybookCatalogEntry[]> {
  if (inFlight === null) {
    const forGeneration = ++generation;
    const promise: Promise<PlaybookCatalogEntry[]> = load(forGeneration).catch(
      (err: unknown): never => {
        // A failure is not memoised: the next consumer to mount (or the next
        // "Reload contract types" click) must be able to try again.
        if (inFlight === promise) {
          inFlight = null;
        }
        throw err;
      },
    );
    inFlight = promise;
  }
  return inFlight;
}

/**
 * Drop the memoised catalog and, if anything is currently subscribed, read it
 * again once — so an activation in the Playbooks tab is visible on the Review
 * tab's dial without a reload. With no subscribers there is nothing to update,
 * so the refetch is simply deferred to the next `getCatalog()`.
 */
export function invalidateCatalog(): void {
  inFlight = null;
  // Abandon any read already in flight BEFORE starting the new one, so a
  // slower older response cannot land on top of the newer one.
  generation += 1;
  if (listeners.size > 0) {
    void getCatalog().catch(() => {
      // Already recorded in `state.error` and logged once by `load`.
    });
  }
}

/**
 * Test seam. Production code never calls this — `src/setupTests.ts` calls it
 * between tests so one test's catalog cannot leak into the next (vitest
 * isolates module state per FILE, not per test).
 */
export function __resetPlaybookCatalog(): void {
  listeners.clear();
  inFlight = null;
  generation += 1;
  state = INITIAL_STATE;
}

/**
 * The React face of the store. Every consumer sees the same snapshot and the
 * same single network call; mounting a second or third one costs nothing.
 */
export function usePlaybookCatalog(): PlaybookCatalogState {
  const snapshot = useSyncExternalStore(subscribeCatalog, getCatalogState, getCatalogState);
  useEffect(() => {
    void getCatalog().catch(() => {
      // The failure is in the snapshot this hook returns; nothing to do here.
    });
  }, []);
  return snapshot;
}
