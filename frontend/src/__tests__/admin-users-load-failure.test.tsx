/**
 * admin-users-load-failure.test.tsx — a failed load is a TERMINAL, RECOVERABLE
 * state, not a contradictory one (issue #439).
 *
 * The shipped bug: `loadUsers`' catch set `error` and never touched `users`,
 * so `users === null` persisted and the component rendered BOTH the danger
 * banner ("We couldn't load the users list. Please try again.") and, forever,
 * `<CtProgress label="Loading users…">`. There was no terminal failed state
 * and no retry control — the copy said "Please try again" while offering
 * nothing to try, and because the session token is in memory only
 * (`frontend/src/auth.ts`) the sole way to retry was a page reload, which
 * signs the operator out. `loadSyncStatus` had the identical shape.
 *
 * These tests are written against the REAL component with only the network
 * transport stubbed — nothing about AdminUsers itself is mocked — so they
 * exercise the actual load/render path rather than a double of it. Each one
 * fails against the pre-fix component:
 *
 *   1. `admin-users-loading` is still in the document alongside the error.
 *   2. there is no `admin-users-retry` control at all.
 *   3. `sync-status-loading` is still in the document after the sync load
 *      failed.
 *
 * Fully offline — aws-amplify/auth is mocked, fetch is stubbed per test.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import AdminUsers from '../AdminUsers';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const SYNC_STATUS_OK = {
  sync_type: 'workspace',
  last_run_at: null,
  last_run_outcome: null,
  users_deprovisioned_count: 0,
  next_run_at: null,
};

const USER_ROW = {
  cognito_sub: 'sub-1',
  email: 'admin@example.com',
  status: 'active',
  is_admin: true,
  last_auth_at: 0,
  created_at: 0,
  admission: 'jit',
};

type Responder = (pathname: string) => { ok: boolean; status: number; body: unknown };

/** Stub only the network transport; the component under test is the real one. */
function stubFetch(responder: Responder): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    const { ok, status, body } = responder(pathname);
    return { ok, status, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AdminUsers — failed users load is terminal and recoverable (#439)', () => {
  it('shows the error with NO loading indicator and NO empty state', async () => {
    stubFetch((pathname) =>
      pathname === '/api/users'
        ? { ok: false, status: 500, body: {} }
        : { ok: true, status: 200, body: SYNC_STATUS_OK },
    );

    render(<AdminUsers />);

    await screen.findByTestId('admin-users-error');

    // The defect: the loader coexisted with the error, forever.
    expect(screen.queryByTestId('admin-users-loading')).toBeNull();
    expect(screen.queryByText('Loading users…')).toBeNull();
    // An empty workspace and a failed load must stay distinguishable — a
    // failure must NOT claim "No users yet."
    expect(screen.queryByTestId('admin-users-empty')).toBeNull();
    expect(screen.queryByTestId('users-table')).toBeNull();
  });

  it('offers a retry control that re-runs the load in place (no reload, no re-login)', async () => {
    let usersCalls = 0;
    const fetchMock = stubFetch((pathname) => {
      if (pathname === '/api/users') {
        usersCalls += 1;
        return usersCalls === 1
          ? { ok: false, status: 500, body: {} }
          : { ok: true, status: 200, body: { users: [USER_ROW] } };
      }
      return { ok: true, status: 200, body: SYNC_STATUS_OK };
    });

    render(<AdminUsers />);
    await screen.findByTestId('admin-users-error');

    const retry = await screen.findByTestId('admin-users-retry');
    // type="button" — it can never submit a form or navigate the SPA away,
    // which would destroy the in-memory session token (auth.ts).
    expect(retry).toHaveAttribute('type', 'button');

    fireEvent.click(retry);

    // Recovered in place: the table renders and the error banner is gone.
    expect(await screen.findByTestId('user-row-sub-1')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByTestId('admin-users-error')).toBeNull();
    });
    expect(usersCalls).toBe(2);
    // Same mounted component, re-fetched — no reload happened.
    expect(fetchMock).toHaveBeenCalled();
    expect(screen.getByTestId('admin-users-panel')).toBeInTheDocument();
  });

  it('renders the friendly copy only — never a raw endpoint or HTTP status', async () => {
    stubFetch((pathname) =>
      pathname === '/api/users'
        ? { ok: false, status: 500, body: {} }
        : { ok: true, status: 200, body: SYNC_STATUS_OK },
    );

    render(<AdminUsers />);

    const errorEl = await screen.findByTestId('admin-users-error');
    const text = errorEl.textContent ?? '';
    expect(text).not.toMatch(/HTTP\s*\d{3}/i);
    expect(text).not.toContain('/api/');
    expect(text.length).toBeGreaterThan(0);
  });
});

describe('AdminUsers — failed sync-status load is terminal and recoverable (#439)', () => {
  it('shows a sync error with NO "Loading sync status…" left behind', async () => {
    stubFetch((pathname) =>
      pathname === '/api/users/sync-status'
        ? { ok: false, status: 500, body: {} }
        : { ok: true, status: 200, body: { users: [] } },
    );

    render(<AdminUsers />);

    const syncError = await screen.findByTestId('sync-status-error');
    expect(syncError.textContent ?? '').not.toMatch(/HTTP\s*\d{3}/i);
    expect(screen.queryByTestId('sync-status-loading')).toBeNull();
    expect(screen.queryByText('Loading sync status…')).toBeNull();
  });

  it('recovers the sync panel in place via its own retry control', async () => {
    let syncCalls = 0;
    stubFetch((pathname) => {
      if (pathname === '/api/users/sync-status') {
        syncCalls += 1;
        return syncCalls === 1
          ? { ok: false, status: 500, body: {} }
          : { ok: true, status: 200, body: SYNC_STATUS_OK };
      }
      return { ok: true, status: 200, body: { users: [] } };
    });

    render(<AdminUsers />);
    await screen.findByTestId('sync-status-error');

    fireEvent.click(screen.getByTestId('sync-status-retry'));

    expect(await screen.findByTestId('sync-last-run')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByTestId('sync-status-error')).toBeNull();
    });
    expect(syncCalls).toBe(2);
  });
});
