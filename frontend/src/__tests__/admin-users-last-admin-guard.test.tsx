/**
 * admin-users-last-admin-guard.test.tsx — the UI half of the last-active-admin
 * guard and state-appropriate actions (issue #473).
 *
 * `tests/test_admin_users_ui_92.py` only greps the component's source text
 * for the literal words "Suspend"/"Deprovision"/"Reactivate" — it would still
 * pass if those buttons were unconditionally hidden or permanently disabled,
 * so it proves nothing about when each button appears or is enabled. These
 * tests drive the REAL component with only the network transport stubbed and
 * assert the actual render tree:
 *
 *   1. Reactivate is offered only on a non-active row (never on `active`).
 *   2. Suspend is offered only on a non-suspended row.
 *   3. With exactly one active admin, that row's Suspend/Deprovision/Revoke
 *      admin buttons are disabled and carry the same title copy
 *      `AdminUsers.tsx`'s `LAST_ADMIN_TITLE` uses (mirrors
 *      `backend/src/users.py::update_user`'s 409 detail verbatim).
 *   4. With two active admins, those same buttons are enabled on both rows.
 *   5. Self-recognition (`GET /api/me` -> `cognito_sub`, `loadMe` in
 *      `AdminUsers.tsx`) drives the Revoke-admin confirm copy: the caller's
 *      own row gets "...your own admin access", every other row gets the
 *      generic "...revoke admin". Without a test stubbing `/api/me`, `mySub`
 *      stays null in every other test here and `isSelf` is always false —
 *      `loadMe` itself has no coverage anywhere else.
 *
 * Fully offline — aws-amplify/auth is mocked, fetch is stubbed per test.
 * jsdom runs with `css: false` (vitest.config.ts), so these assert structure
 * (disabled/title/presence) and text content only, never computed styles.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import AdminUsers from '../AdminUsers';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

// Mirrors backend/src/users.py::update_user's 409 detail / AdminUsers.tsx's
// LAST_ADMIN_TITLE exactly (issue #473) — not imported, since the constant
// is intentionally not exported; matched by content the same way
// admin-playbooks.test.tsx matches its own button title copy.
const LAST_ADMIN_TITLE = 'This is the only admin account — add another admin first.';

const SYNC_STATUS_OK = {
  sync_type: 'workspace',
  last_run_at: null,
  last_run_outcome: null,
  users_deprovisioned_count: 0,
  next_run_at: null,
};

const ACTIVE_ADMIN = {
  cognito_sub: 'sub-admin-1',
  email: 'admin1@example.com',
  status: 'active',
  is_admin: true,
  last_auth_at: 0,
  created_at: 0,
  admission: 'jit',
};

const SECOND_ACTIVE_ADMIN = {
  cognito_sub: 'sub-admin-2',
  email: 'admin2@example.com',
  status: 'active',
  is_admin: true,
  last_auth_at: 0,
  created_at: 0,
  admission: 'jit',
};

const ACTIVE_REVIEWER = {
  cognito_sub: 'sub-reviewer',
  email: 'reviewer@example.com',
  status: 'active',
  is_admin: false,
  last_auth_at: 0,
  created_at: 0,
  admission: 'jit',
};

const SUSPENDED_REVIEWER = {
  cognito_sub: 'sub-suspended',
  email: 'suspended@example.com',
  status: 'suspended',
  is_admin: false,
  last_auth_at: 0,
  created_at: 0,
  admission: 'jit',
};

type Body = Record<string, unknown>;

/** Stub only the network transport; the component under test is the real one. */
function stubFetch(byPath: Record<string, Body>): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      const body = byPath[pathname];
      return { ok: body !== undefined, status: body === undefined ? 404 : 200, json: async () => body ?? {} } as Response;
    }),
  );
}

function stubUsers(users: unknown[]): void {
  stubFetch({ '/api/users': { users }, '/api/users/sync-status': SYNC_STATUS_OK });
}

/** Like `stubUsers`, but also serves `/api/me` so self-recognition resolves. */
function stubUsersWithMe(users: unknown[], mySub: string): void {
  stubFetch({
    '/api/users': { users },
    '/api/users/sync-status': SYNC_STATUS_OK,
    '/api/me': { cognito_sub: mySub },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Resolve a button by its visible label WITHIN a given row's real <button>. */
function buttonWithin(row: HTMLElement, label: string): HTMLElement {
  const el = within(row).getByText(label);
  const btn = el.closest('button');
  if (!btn) {
    throw new Error(`"${label}" is not rendered inside a <button>`);
  }
  return btn;
}

function queryButtonWithin(row: HTMLElement, label: string): HTMLElement | null {
  const el = within(row).queryByText(label);
  return el ? el.closest('button') : null;
}

describe('AdminUsers — Reactivate/Suspend are state-appropriate only (#473)', () => {
  it('does not offer Reactivate on an active row', async () => {
    stubUsers([ACTIVE_REVIEWER]);
    render(<AdminUsers />);

    const row = await screen.findByTestId('user-row-sub-reviewer');
    expect(queryButtonWithin(row, 'Reactivate')).toBeNull();
  });

  it('offers Reactivate on a suspended row', async () => {
    stubUsers([SUSPENDED_REVIEWER]);
    render(<AdminUsers />);

    const row = await screen.findByTestId('user-row-sub-suspended');
    expect(queryButtonWithin(row, 'Reactivate')).not.toBeNull();
  });

  it('does not offer Suspend on an already-suspended row', async () => {
    stubUsers([SUSPENDED_REVIEWER]);
    render(<AdminUsers />);

    const row = await screen.findByTestId('user-row-sub-suspended');
    expect(queryButtonWithin(row, 'Suspend')).toBeNull();
  });

  it('offers Suspend on an active row', async () => {
    stubUsers([ACTIVE_REVIEWER]);
    render(<AdminUsers />);

    const row = await screen.findByTestId('user-row-sub-reviewer');
    expect(queryButtonWithin(row, 'Suspend')).not.toBeNull();
  });
});

describe('AdminUsers — last-active-admin guard disables the dangerous actions (#473)', () => {
  it('disables Suspend/Deprovision/Revoke admin on the sole active admin, with the LAST_ADMIN_TITLE hint', async () => {
    stubUsers([ACTIVE_ADMIN]);
    render(<AdminUsers />);

    const row = await screen.findByTestId('user-row-sub-admin-1');

    const suspend = buttonWithin(row, 'Suspend');
    expect(suspend).toBeDisabled();
    expect(suspend.closest('ct-button')?.getAttribute('title')).toBe(LAST_ADMIN_TITLE);

    const deprovision = buttonWithin(row, 'Deprovision');
    expect(deprovision).toBeDisabled();
    expect(deprovision.closest('ct-button')?.getAttribute('title')).toBe(LAST_ADMIN_TITLE);

    const revoke = buttonWithin(row, 'Revoke admin');
    expect(revoke).toBeDisabled();
    expect(revoke.closest('ct-button')?.getAttribute('title')).toBe(LAST_ADMIN_TITLE);
  });

  it('enables Suspend/Deprovision/Revoke admin on both rows once a second active admin exists', async () => {
    stubUsers([ACTIVE_ADMIN, SECOND_ACTIVE_ADMIN]);
    render(<AdminUsers />);

    for (const sub of ['sub-admin-1', 'sub-admin-2']) {
      const row = await screen.findByTestId(`user-row-${sub}`);

      const suspend = buttonWithin(row, 'Suspend');
      expect(suspend).not.toBeDisabled();
      expect(suspend.closest('ct-button')?.getAttribute('title')).toBeNull();

      const deprovision = buttonWithin(row, 'Deprovision');
      expect(deprovision).not.toBeDisabled();
      expect(deprovision.closest('ct-button')?.getAttribute('title')).toBeNull();

      const revoke = buttonWithin(row, 'Revoke admin');
      expect(revoke).not.toBeDisabled();
      expect(revoke.closest('ct-button')?.getAttribute('title')).toBeNull();
    }
  });
});

describe('AdminUsers — self-recognition drives the Revoke-admin confirm copy (#473)', () => {
  it('is self-aware on the caller\'s own row and generic on another', async () => {
    stubUsersWithMe([ACTIVE_ADMIN, SECOND_ACTIVE_ADMIN], 'sub-admin-1');
    render(<AdminUsers />);

    const selfRow = await screen.findByTestId('user-row-sub-admin-1');
    const otherRow = await screen.findByTestId('user-row-sub-admin-2');

    // `confirm` is a plain JS property on the light-DOM `ct-button` (not a
    // reflected attribute — see ct-button.ts's `get/set confirm`), and
    // `/api/me` resolves after the initial render, so read it via `waitFor`
    // rather than asserting synchronously or clicking to arm the button.
    const selfCtButton = buttonWithin(selfRow, 'Revoke admin').closest('ct-button') as
      | (HTMLElement & { confirm: string })
      | null;
    const otherCtButton = buttonWithin(otherRow, 'Revoke admin').closest('ct-button') as
      | (HTMLElement & { confirm: string })
      | null;
    if (!selfCtButton || !otherCtButton) {
      throw new Error('Revoke admin button is not rendered inside a <ct-button>');
    }

    await waitFor(() => {
      expect(selfCtButton.confirm).toBe('Click again to remove your own admin access');
    });
    expect(otherCtButton.confirm).toBe('Click again to revoke admin');
  });
});
