/**
 * admin-users-identity.test.tsx — every Users row is identifiable (issue #441).
 *
 * The shipped bug: `UserRow.email` was declared required and the first cell
 * rendered `{u.email}` unconditionally. Password-mode users carry no `email`
 * field at all (`backend/src/demo_auth.py` writes `username`, never `email`),
 * so on a password-mode deployment EVERY row's identity cell rendered empty —
 * observed live on 2026-08-01 with two blank-identity rows, each still
 * offering Suspend / Deprovision / Revoke admin. An admin was being asked to
 * take irreversible access decisions against rows they could not tell apart,
 * which also defeats the confirm step from #428: a confirm prompt is no
 * protection when you cannot tell which row you armed.
 *
 * These tests drive the REAL component with only the network transport
 * stubbed — nothing about AdminUsers is mocked — so they exercise the actual
 * fetch/render path. Against the pre-fix component:
 *
 *   - the username-only row renders an EMPTY identity cell (the bug);
 *   - the neither-email-nor-username row renders an EMPTY identity cell;
 *   - the column header reads "Email", which is wrong for a deployment that
 *     has no emails at all.
 *
 * Fully offline — aws-amplify/auth is mocked, fetch is stubbed per test.
 * jsdom runs with `css: false` (vitest.config.ts), so these assert structure
 * and text content only, never computed styles.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
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

/** An SSO row, as the pre-token Lambda's JIT-create path writes it (#33). */
const SSO_USER = {
  cognito_sub: 'sub-sso',
  email: 'sso.person@example.com',
  status: 'active',
  is_admin: false,
  last_auth_at: 0,
  created_at: 0,
  admission: 'jit',
};

/**
 * A password-mode row, matching what `seed_demo_users` actually writes:
 * `username`, and NO `email` key whatsoever.
 */
const PASSWORD_USER = {
  cognito_sub: 'sub-password',
  username: 'admin',
  user_type: 'password',
  status: 'active',
  is_admin: true,
  last_auth_at: 0,
  created_at: 0,
  admission: 'seed',
};

/** A degenerate row carrying neither identifier — must still be readable. */
const ANONYMOUS_USER = {
  cognito_sub: 'sub-anonymous',
  status: 'active',
  is_admin: false,
  last_auth_at: 0,
  created_at: 0,
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

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AdminUsers — every row is identifiable (#441)', () => {
  it('renders an SSO user by their email', async () => {
    stubUsers([SSO_USER]);

    render(<AdminUsers />);

    const cell = await screen.findByTestId('user-identity-sub-sso');
    expect(cell.textContent).toBe('sso.person@example.com');
  });

  it('renders a password-mode user by their username (blank before the fix)', async () => {
    stubUsers([PASSWORD_USER]);

    render(<AdminUsers />);

    const cell = await screen.findByTestId('user-identity-sub-password');
    // The defect: this cell was empty, because the row has no `email` field.
    expect((cell.textContent ?? '').trim()).not.toBe('');
    expect(cell.textContent).toBe('admin');
  });

  it('renders a visible fallback when the row has neither email nor username', async () => {
    stubUsers([ANONYMOUS_USER]);

    render(<AdminUsers />);

    const cell = await screen.findByTestId('user-identity-sub-anonymous');
    expect((cell.textContent ?? '').trim()).not.toBe('');
    // The subject is the only remaining handle on the row; showing it beats a
    // blank cell above a Deprovision button.
    expect(cell.textContent).toContain('sub-anonymous');
  });

  it('never leaves an identity cell blank for any mix of rows on one table', async () => {
    stubUsers([SSO_USER, PASSWORD_USER, ANONYMOUS_USER]);

    render(<AdminUsers />);

    await screen.findByTestId('users-table');
    for (const sub of ['sub-sso', 'sub-password', 'sub-anonymous']) {
      const cell = screen.getByTestId(`user-identity-${sub}`);
      expect((cell.textContent ?? '').trim()).not.toBe('');
    }
  });

  it('treats a present-but-empty email as no identity rather than rendering blank', async () => {
    stubUsers([{ ...PASSWORD_USER, email: '   ' }]);

    render(<AdminUsers />);

    const cell = await screen.findByTestId('user-identity-sub-password');
    expect(cell.textContent).toBe('admin');
  });

  it('labels the identity column honestly for both deployment targets', async () => {
    stubUsers([PASSWORD_USER]);

    render(<AdminUsers />);

    const header = await screen.findByTestId('users-identity-header');
    const label = (header.textContent ?? '').toLowerCase();
    // "Email" alone is a lie on a password-mode deployment, which has none.
    expect(label).not.toBe('email');
    expect(label).toContain('username');
  });
});
