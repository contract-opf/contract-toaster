/**
 * admin-users-auth-mode-add-remove-474.test.tsx — auth-mode-aware Users &
 * access screen (issue #474).
 *
 * Three things this locks in, driving the REAL component with only the
 * network transport stubbed:
 *
 *   1. **Workspace sync card visibility follows the stored auth mode.**
 *      Hidden on `password`, shown on `sso`/`both`, and shown when the
 *      `/api/admin/auth-mode` probe fails or hasn't resolved yet (fail open
 *      — see AdminUsers.tsx's `showSyncCard` docstring).
 *   2. **Add user is mode-appropriate.** `password` mode offers only the
 *      username/password fields (no type selector); `sso` mode offers only
 *      the email field; `both` (and an unprobeable mode) offers a type
 *      selector. Submitting issues the right POST /api/users body and
 *      re-fetches the table rather than optimistically splicing a row in.
 *   3. **Remove is a real hard-delete, distinct from Deprovision.** It is a
 *      two-click confirm (no DELETE on the first click), and it is disabled
 *      on the caller's own row — mirroring
 *      backend/src/demo_auth.py::remove_user's unconditional self-removal
 *      guard, not the last-active-admin guard Deprovision uses.
 *
 * Fully offline — aws-amplify/auth is mocked, fetch is stubbed per test.
 * jsdom runs with `css: false` (vitest.config.ts): structure/text/testids
 * only, never computed styles.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

const ADMIN_ROW = {
  cognito_sub: 'sub-admin-1',
  email: 'admin1@example.com',
  status: 'active',
  is_admin: true,
  last_auth_at: 0,
  created_at: 0,
  admission: 'jit',
};

const REVIEWER_ROW = {
  cognito_sub: 'sub-reviewer',
  email: 'reviewer@example.com',
  status: 'active',
  is_admin: false,
  last_auth_at: 0,
  created_at: 0,
  admission: 'jit',
};

interface Recorded {
  method: string;
  pathname: string;
  body: unknown;
}

interface Handler {
  method: string;
  suffix: string;
  status: number;
  body: unknown;
}

let requests: Recorded[] = [];

/**
 * Route stub keyed on method+pathname. `overrides` win first; otherwise
 * GET /api/users, /api/users/sync-status and /api/admin/auth-mode (when
 * `authMode` is non-null) resolve to the happy path, everything else 404s.
 */
function stubRoutes(users: unknown[], authMode: string | null, overrides: Handler[] = []): void {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    const method = (init?.method ?? 'GET').toUpperCase();
    const rawBody = init?.body;
    const body = typeof rawBody === 'string' ? JSON.parse(rawBody) : undefined;
    requests.push({ method, pathname, body });

    const override = overrides.find((h) => h.method === method && pathname.endsWith(h.suffix));
    if (override) {
      return {
        ok: override.status >= 200 && override.status < 300,
        status: override.status,
        json: async () => override.body,
      } as Response;
    }
    if (method === 'GET' && pathname === '/api/users') {
      return { ok: true, status: 200, json: async () => ({ users }) } as Response;
    }
    if (method === 'GET' && pathname === '/api/users/sync-status') {
      return { ok: true, status: 200, json: async () => SYNC_STATUS_OK } as Response;
    }
    if (method === 'GET' && pathname === '/api/admin/auth-mode') {
      if (authMode === null) {
        return { ok: false, status: 500, json: async () => ({}) } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          setting_id: 'global',
          auth_mode: authMode,
          default_auth_mode: 'sso',
          auth_mode_options: [],
        }),
      } as Response;
    }
    if (method === 'GET' && pathname === '/api/me') {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', impl);
}

function requestsMatching(method: string, suffix: string): Recorded[] {
  return requests.filter((r) => r.method === method && r.pathname.endsWith(suffix));
}

beforeEach(() => {
  requests = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// 1. Sync card visibility follows the auth mode.
// ---------------------------------------------------------------------------

describe('AdminUsers — Workspace sync card follows the auth mode (#474)', () => {
  it('hides the sync card on a password-mode deployment', async () => {
    stubRoutes([ADMIN_ROW], 'password');
    render(<AdminUsers />);

    await screen.findByTestId('user-row-sub-admin-1');
    await waitFor(() => {
      expect(screen.queryByTestId('sync-status-panel')).toBeNull();
    });
  });

  it('shows the sync card on an sso-mode deployment', async () => {
    stubRoutes([ADMIN_ROW], 'sso');
    render(<AdminUsers />);

    await screen.findByTestId('user-row-sub-admin-1');
    expect(await screen.findByTestId('sync-status-panel')).toBeInTheDocument();
  });

  it('shows the sync card on a both-mode deployment', async () => {
    stubRoutes([ADMIN_ROW], 'both');
    render(<AdminUsers />);

    await screen.findByTestId('user-row-sub-admin-1');
    expect(await screen.findByTestId('sync-status-panel')).toBeInTheDocument();
  });

  it('fails open (keeps the sync card) when the auth-mode probe fails', async () => {
    stubRoutes([ADMIN_ROW], null);
    render(<AdminUsers />);

    await screen.findByTestId('user-row-sub-admin-1');
    expect(await screen.findByTestId('sync-status-panel')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 2. Add user — mode-appropriate fields + the right POST body.
// ---------------------------------------------------------------------------

describe('AdminUsers — Add user is mode-appropriate (#474)', () => {
  it('offers only username/password fields in password mode (no type selector)', async () => {
    stubRoutes([REVIEWER_ROW], 'password');
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');

    fireEvent.click(screen.getByTestId('admin-users-add-toggle'));

    expect(screen.getByTestId('admin-users-add-username')).toBeInTheDocument();
    expect(screen.getByTestId('admin-users-add-password')).toBeInTheDocument();
    expect(screen.queryByTestId('admin-users-add-type')).toBeNull();
    expect(screen.queryByTestId('admin-users-add-email')).toBeNull();
  });

  it('offers only an email field in sso mode (no type selector)', async () => {
    stubRoutes([REVIEWER_ROW], 'sso');
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');

    fireEvent.click(screen.getByTestId('admin-users-add-toggle'));

    expect(screen.getByTestId('admin-users-add-email')).toBeInTheDocument();
    expect(screen.queryByTestId('admin-users-add-type')).toBeNull();
    expect(screen.queryByTestId('admin-users-add-username')).toBeNull();
  });

  it('offers a type selector in both mode', async () => {
    stubRoutes([REVIEWER_ROW], 'both');
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');

    fireEvent.click(screen.getByTestId('admin-users-add-toggle'));

    expect(screen.getByTestId('admin-users-add-type')).toBeInTheDocument();
  });

  it('submits POST /api/users with the password-type body and re-fetches the table', async () => {
    stubRoutes([REVIEWER_ROW], 'password', [
      {
        method: 'POST',
        suffix: '/api/users',
        status: 200,
        body: { cognito_sub: 'local:newbie', username: 'newbie', is_admin: false, status: 'active' },
      },
    ]);
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');

    fireEvent.click(screen.getByTestId('admin-users-add-toggle'));
    fireEvent.change(screen.getByTestId('admin-users-add-username'), { target: { value: 'newbie' } });
    fireEvent.change(screen.getByTestId('admin-users-add-password'), { target: { value: 'correct-horse' } });
    fireEvent.click(screen.getByTestId('admin-users-add-submit'));

    await waitFor(() => {
      expect(requestsMatching('POST', '/api/users')).toHaveLength(1);
    });
    const [req] = requestsMatching('POST', '/api/users');
    expect(req.body).toEqual({
      user_type: 'password',
      username: 'newbie',
      password: 'correct-horse',
      is_admin: false,
    });

    // No optimistic UI: GET /api/users is re-issued after the POST resolves.
    await waitFor(() => {
      expect(requestsMatching('GET', '/api/users').length).toBeGreaterThan(1);
    });

    // The typed password is shown exactly once, in the success banner.
    expect(await screen.findByTestId('admin-users-add-password-once')).toHaveTextContent(
      'correct-horse',
    );
  });

  it('submits POST /api/users with the sso-type body', async () => {
    stubRoutes([REVIEWER_ROW], 'sso', [
      {
        method: 'POST',
        suffix: '/api/users',
        status: 200,
        body: { cognito_sub: 'pending-sso:new@example.com', email: 'new@example.com', is_admin: false, status: 'active' },
      },
    ]);
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');

    fireEvent.click(screen.getByTestId('admin-users-add-toggle'));
    fireEvent.change(screen.getByTestId('admin-users-add-email'), {
      target: { value: 'new@example.com' },
    });
    fireEvent.click(screen.getByTestId('admin-users-add-submit'));

    await waitFor(() => {
      expect(requestsMatching('POST', '/api/users')).toHaveLength(1);
    });
    const [req] = requestsMatching('POST', '/api/users');
    expect(req.body).toEqual({ user_type: 'sso', email: 'new@example.com', is_admin: false });

    // No password to show for an SSO add.
    expect(screen.queryByTestId('admin-users-add-password-once')).toBeNull();
  });

  it('surfaces the server-supplied detail verbatim on a 409 (duplicate) without an extra GET', async () => {
    stubRoutes([REVIEWER_ROW], 'sso', [
      {
        method: 'POST',
        suffix: '/api/users',
        status: 409,
        body: { detail: 'A user with this identity already exists.' },
      },
    ]);
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');

    fireEvent.click(screen.getByTestId('admin-users-add-toggle'));
    fireEvent.change(screen.getByTestId('admin-users-add-email'), {
      target: { value: 'reviewer@example.com' },
    });
    fireEvent.click(screen.getByTestId('admin-users-add-submit'));

    expect(await screen.findByTestId('admin-users-add-error')).toHaveTextContent(
      'A user with this identity already exists.',
    );
    // Failed add — the table is not re-fetched.
    expect(requestsMatching('GET', '/api/users')).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 3. Remove — hard delete, two-click confirm, blocked on self.
// ---------------------------------------------------------------------------

describe('AdminUsers — Remove is a distinct hard delete (#474)', () => {
  it('does not call DELETE on the first (arming) click', async () => {
    stubRoutes([ADMIN_ROW, REVIEWER_ROW], 'sso');
    render(<AdminUsers />);
    const row = await screen.findByTestId('user-row-sub-reviewer');

    fireEvent.click(within(row).getByTestId('user-remove-sub-reviewer'));

    expect(requestsMatching('DELETE', '/api/users/sub-reviewer')).toHaveLength(0);
  });

  it('calls DELETE /api/users/{sub} on the second (confirming) click and re-fetches', async () => {
    stubRoutes([ADMIN_ROW, REVIEWER_ROW], 'sso', [
      { method: 'DELETE', suffix: '/api/users/sub-reviewer', status: 200, body: { removed: true } },
    ]);
    render(<AdminUsers />);
    const row = await screen.findByTestId('user-row-sub-reviewer');
    const removeBtn = within(row).getByTestId('user-remove-sub-reviewer');

    fireEvent.click(removeBtn);
    fireEvent.click(removeBtn);

    await waitFor(() => {
      expect(requestsMatching('DELETE', '/api/users/sub-reviewer')).toHaveLength(1);
    });
    await waitFor(() => {
      expect(requestsMatching('GET', '/api/users').length).toBeGreaterThan(1);
    });
  });

  it('is disabled on the caller\'s own row, regardless of other active admins', async () => {
    const secondAdmin = { ...ADMIN_ROW, cognito_sub: 'sub-admin-2', email: 'admin2@example.com' };
    stubRoutes([ADMIN_ROW, secondAdmin], 'sso');
    // /api/me resolves to sub-admin-1 so isSelf is true for that row.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const pathname = new URL(url, 'http://localhost').pathname;
        const method = (init?.method ?? 'GET').toUpperCase();
        if (method === 'GET' && pathname === '/api/users') {
          return { ok: true, status: 200, json: async () => ({ users: [ADMIN_ROW, secondAdmin] }) } as Response;
        }
        if (method === 'GET' && pathname === '/api/users/sync-status') {
          return { ok: true, status: 200, json: async () => SYNC_STATUS_OK } as Response;
        }
        if (method === 'GET' && pathname === '/api/admin/auth-mode') {
          return {
            ok: true,
            status: 200,
            json: async () => ({ setting_id: 'global', auth_mode: 'sso', default_auth_mode: 'sso', auth_mode_options: [] }),
          } as Response;
        }
        if (method === 'GET' && pathname === '/api/me') {
          return { ok: true, status: 200, json: async () => ({ cognito_sub: 'sub-admin-1' }) } as Response;
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );
    render(<AdminUsers />);

    const selfRow = await screen.findByTestId('user-row-sub-admin-1');
    const otherRow = await screen.findByTestId('user-row-sub-admin-2');

    await waitFor(() => {
      expect(within(selfRow).getByTestId('user-remove-sub-admin-1')).toBeDisabled();
    });
    expect(within(otherRow).getByTestId('user-remove-sub-admin-2')).not.toBeDisabled();
  });
});
