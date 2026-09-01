/**
 * admin-users-auth-mode-toggle-246.test.tsx — WRITING the demo auth mode
 * from the Users & access screen (issue #246).
 *
 * `POST /api/admin/auth-mode` has existed since #232 and, until this
 * ticket, nothing in `frontend/src/` had ever called it — the mode could
 * only be changed by hand-rolling an API request. #474 landed the READ
 * (`loadAuthMode`) and the mode-dependent behaviour it feeds
 * (`availableAddUserTypes`, `showSyncCard`); this locks in the write half:
 *
 *   1. **The control's choices come from the server.** They are rendered
 *      from the `auth_mode_options` the GET already returns
 *      (backend/src/demo_auth.py::AUTH_MODE_OPTIONS), never hard-coded
 *      here, and the stored mode is the selected one.
 *   2. **Selecting a mode POSTs it**, body `{"auth_mode": "<value>"}`.
 *   3. **The mode-dependent UI reacts IN PLACE on success** — the
 *      Workspace-sync card goes away and Add-user narrows to `password`
 *      with no page reload and no remount. A reload would destroy the
 *      in-memory session token (auth.ts) and sign the operator out, which
 *      is the same defect #439 fixed for the loaders on this screen; it is
 *      proven here by the panel node's identity surviving the change and
 *      by the mount-time GETs never being re-issued.
 *   4. **Failure degrades in place**: an inline error, the server-supplied
 *      `detail` when there is one, friendly copy when there is not, the
 *      previous mode still in effect, and no success notice.
 *
 * Fixture provenance (nothing here is a shape the server cannot produce):
 * the GET body is exactly `demo_auth.get_auth_mode_settings`'s return, the
 * success POST body is what `demo_auth.set_auth_mode` returns (it re-reads
 * and returns those same settings), and the two failure bodies are the
 * `HTTPException` details `set_auth_mode` raises — 403 from `_require_admin`
 * and, for the no-detail branch, a bare server error.
 *
 * Fully offline — aws-amplify/auth is mocked, fetch is stubbed per test.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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

const REVIEWER_ROW = {
  cognito_sub: 'sub-reviewer',
  email: 'reviewer@example.com',
  status: 'active',
  is_admin: false,
  last_auth_at: 0,
  created_at: 0,
  admission: 'jit',
};

/** Verbatim `backend/src/demo_auth.py::AUTH_MODE_OPTIONS` — the labels the
 * control must render rather than inventing its own. */
const AUTH_MODE_OPTIONS = [
  { value: 'sso', label: 'Access only (single sign-on)' },
  { value: 'password', label: 'Username and password' },
  { value: 'both', label: 'Both — single sign-on and username/password' },
];

/** `demo_auth.get_auth_mode_settings`'s exact return shape — also what
 * `set_auth_mode` returns after a successful write. */
function authModeSettings(mode: string): Record<string, unknown> {
  return {
    setting_id: 'global',
    auth_mode: mode,
    default_auth_mode: 'sso',
    auth_mode_options: AUTH_MODE_OPTIONS,
  };
}

interface Recorded {
  method: string;
  pathname: string;
  body: unknown;
}

interface PostOutcome {
  status: number;
  body: unknown;
}

let requests: Recorded[] = [];

/**
 * Route stub. GET /api/admin/auth-mode serves `storedMode` (or 500s when
 * it is null); POST /api/admin/auth-mode serves `postOutcome`, defaulting
 * to the real server behaviour: store the posted mode and echo the
 * refreshed settings back.
 */
function stubRoutes(storedMode: string | null, postOutcome?: PostOutcome): void {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    const method = (init?.method ?? 'GET').toUpperCase();
    const rawBody = init?.body;
    const body = typeof rawBody === 'string' ? JSON.parse(rawBody) : undefined;
    requests.push({ method, pathname, body });

    if (method === 'GET' && pathname === '/api/users') {
      return { ok: true, status: 200, json: async () => ({ users: [REVIEWER_ROW] }) } as Response;
    }
    if (method === 'GET' && pathname === '/api/users/sync-status') {
      return { ok: true, status: 200, json: async () => SYNC_STATUS_OK } as Response;
    }
    if (method === 'GET' && pathname === '/api/admin/auth-mode') {
      if (storedMode === null) {
        return { ok: false, status: 500, json: async () => ({}) } as Response;
      }
      return { ok: true, status: 200, json: async () => authModeSettings(storedMode) } as Response;
    }
    if (method === 'POST' && pathname === '/api/admin/auth-mode') {
      const outcome =
        postOutcome ??
        ({
          status: 200,
          body: authModeSettings(String((body as { auth_mode?: unknown }).auth_mode)),
        } as PostOutcome);
      return {
        ok: outcome.status >= 200 && outcome.status < 300,
        status: outcome.status,
        json: async () => outcome.body,
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

describe('AdminUsers — auth-mode toggle renders server-supplied choices (#246)', () => {
  it('renders one option per auth_mode_options entry, with the stored mode selected', async () => {
    stubRoutes('sso');
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');

    const select = (await screen.findByTestId(
      'admin-users-auth-mode-select',
    )) as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => ({ value: o.value, label: o.textContent }))).toEqual(
      AUTH_MODE_OPTIONS.map((o) => ({ value: o.value, label: o.label })),
    );
    expect(select.value).toBe('sso');
  });

  it('never displays an unrecognised stored mode as one of its own choices', async () => {
    // `get_auth_mode_settings` returns the stored row's `auth_mode`
    // UNVALIDATED — only the POST validates — so a value written to the
    // settings table out-of-band really does reach the SPA. Without the
    // placeholder the browser would show the first option and misreport the
    // live setting as `sso`.
    stubRoutes('legacy-mode');
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');

    const select = (await screen.findByTestId(
      'admin-users-auth-mode-select',
    )) as HTMLSelectElement;
    expect(select.value).toBe('');
    expect(Array.from(select.options).map((o) => o.value)).not.toContain('legacy-mode');
    expect(select.options[0].disabled).toBe(true);

    // Still writable: choosing a real mode from here works normally.
    fireEvent.change(select, { target: { value: 'both' } });
    await waitFor(() => {
      expect(requestsMatching('POST', '/api/admin/auth-mode')).toHaveLength(1);
    });
    expect(requestsMatching('POST', '/api/admin/auth-mode')[0].body).toEqual({ auth_mode: 'both' });
    expect(
      (screen.getByTestId('admin-users-auth-mode-select') as HTMLSelectElement).value,
    ).toBe('both');
  });

  it('renders no toggle at all when the auth-mode probe fails (nothing to source choices from)', async () => {
    stubRoutes(null);
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');

    // Degrades exactly as loadAuthMode already does: unknown mode, no
    // invented choices — and never a write control it cannot label.
    await waitFor(() => {
      expect(screen.queryByTestId('admin-users-auth-mode-select')).toBeNull();
    });
    expect(screen.queryByTestId('admin-users-auth-mode-panel')).toBeNull();
    expect(requestsMatching('POST', '/api/admin/auth-mode')).toHaveLength(0);
  });
});

describe('AdminUsers — selecting a mode writes it and the screen reacts in place (#246)', () => {
  it('POSTs the selected mode and narrows the screen without a reload', async () => {
    stubRoutes('sso');
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');
    // Precondition: sso mode shows the sync card and offers both add types.
    expect(await screen.findByTestId('sync-status-panel')).toBeInTheDocument();
    const panelBefore = screen.getByTestId('admin-users-panel');

    fireEvent.change(await screen.findByTestId('admin-users-auth-mode-select'), {
      target: { value: 'password' },
    });

    await waitFor(() => {
      expect(requestsMatching('POST', '/api/admin/auth-mode')).toHaveLength(1);
    });
    const [req] = requestsMatching('POST', '/api/admin/auth-mode');
    expect(req.body).toEqual({ auth_mode: 'password' });

    // Mode-dependent UI reacts immediately (#474's two consumers).
    await waitFor(() => {
      expect(screen.queryByTestId('sync-status-panel')).toBeNull();
    });
    fireEvent.click(screen.getByTestId('admin-users-add-toggle'));
    expect(screen.getByTestId('admin-users-add-username')).toBeInTheDocument();
    expect(screen.queryByTestId('admin-users-add-type')).toBeNull();
    expect(screen.queryByTestId('admin-users-add-email')).toBeNull();

    // No reload: the SAME panel node is still mounted, so no remount
    // happened, and the mount-time loads were never re-run.
    expect(screen.getByTestId('admin-users-panel')).toBe(panelBefore);
    expect(requestsMatching('GET', '/api/users')).toHaveLength(1);
    expect(requestsMatching('GET', '/api/users/sync-status')).toHaveLength(1);

    // The control now shows the new mode, confirmed by the server's echo.
    expect((screen.getByTestId('admin-users-auth-mode-select') as HTMLSelectElement).value).toBe(
      'password',
    );
    expect(await screen.findByTestId('admin-users-auth-mode-notice')).toBeInTheDocument();
    expect(screen.queryByTestId('admin-users-auth-mode-error')).toBeNull();
  });

  it('widens the screen again when the mode is set back to both', async () => {
    stubRoutes('password');
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');
    await waitFor(() => {
      expect(screen.queryByTestId('sync-status-panel')).toBeNull();
    });

    fireEvent.change(await screen.findByTestId('admin-users-auth-mode-select'), {
      target: { value: 'both' },
    });

    await waitFor(() => {
      expect(requestsMatching('POST', '/api/admin/auth-mode')).toHaveLength(1);
    });
    expect(requestsMatching('POST', '/api/admin/auth-mode')[0].body).toEqual({ auth_mode: 'both' });
    expect(await screen.findByTestId('sync-status-panel')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('admin-users-add-toggle'));
    expect(screen.getByTestId('admin-users-add-type')).toBeInTheDocument();
  });
});

describe('AdminUsers — a failed mode change degrades in place (#246)', () => {
  it('surfaces the server detail on a 403 and leaves the previous mode in effect', async () => {
    stubRoutes('sso', {
      status: 403,
      body: { detail: 'Admin privilege required to change the auth-mode setting.' },
    });
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');
    const panelBefore = screen.getByTestId('admin-users-panel');

    fireEvent.change(await screen.findByTestId('admin-users-auth-mode-select'), {
      target: { value: 'password' },
    });

    expect(await screen.findByTestId('admin-users-auth-mode-error')).toHaveTextContent(
      'Admin privilege required to change the auth-mode setting.',
    );
    // No false success, and the OLD mode still governs the screen.
    expect(screen.queryByTestId('admin-users-auth-mode-notice')).toBeNull();
    expect((screen.getByTestId('admin-users-auth-mode-select') as HTMLSelectElement).value).toBe(
      'sso',
    );
    expect(screen.getByTestId('sync-status-panel')).toBeInTheDocument();
    // In place: same panel node, no re-run of the mount-time loads.
    expect(screen.getByTestId('admin-users-panel')).toBe(panelBefore);
    expect(requestsMatching('GET', '/api/users')).toHaveLength(1);
  });

  it('falls back to friendly copy — never a raw endpoint or HTTP status — when there is no detail', async () => {
    stubRoutes('sso', { status: 500, body: {} });
    render(<AdminUsers />);
    await screen.findByTestId('user-row-sub-reviewer');

    fireEvent.change(await screen.findByTestId('admin-users-auth-mode-select'), {
      target: { value: 'both' },
    });

    const errorEl = await screen.findByTestId('admin-users-auth-mode-error');
    const text = errorEl.textContent ?? '';
    expect(text.length).toBeGreaterThan(0);
    expect(text).not.toMatch(/HTTP\s*\d{3}/i);
    expect(text).not.toContain('/api/');
    expect(screen.queryByTestId('admin-users-auth-mode-notice')).toBeNull();
  });
});
