/**
 * password-auth.test.tsx — Docker Compose password-mode auth path (VITE_AUTH_MODE=password).
 *
 * Covers the pieces the Docker Compose deployment adds (issue #468 rewrote
 * this from an in-memory-Bearer-token design to an httpOnly session cookie):
 *   1. PasswordLogin posts to /api/auth/login with `credentials: 'same-origin'`
 *      (so a real browser stores the Set-Cookie response) and, on success,
 *      reports the identity — it never reads or holds a token.
 *   2. getToken() always resolves '' in password mode; the session cookie,
 *      not a Bearer header, is the credential.
 *   3. <App/> in password mode renders the login gate, not the Cognito
 *      Authenticator, when there is no valid session cookie yet.
 *   4. Login failures render friendly copy only — never a raw `HTTP <n>`
 *      status or endpoint path (issue #425), the same rule
 *      resilience-a11y.test.tsx enforces for the review screens.
 *   5. Sign-out POSTs /api/auth/logout (same-origin credentials) so the
 *      server clears the cookie.
 *   6. The reload-survival acceptance criterion itself: on mount, <App/>
 *      probes GET /api/me and, when it resolves a username (a valid
 *      session cookie is still present), renders straight past the login
 *      gate — never re-prompting for a password just because in-memory
 *      React state reset on the reload.
 *
 * See backend/src/demo_auth.py's session-cookie posture comment for the
 * full rationale (why an in-memory token was never actually stronger
 * against XSS than the cookie this replaces it with).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import PasswordLogin from '../PasswordLogin';
import App from '../App';
import { getToken } from '../auth';

// Amplify is never used in password mode, but App.tsx imports it — mock so the
// import resolves without a real Cognito/Amplify runtime.
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn(async () => ({ tokens: {} })) }));
vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({ user: { username: 'x' }, signOut: vi.fn() }),
}));

function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const pathname = new URL(String(input), 'http://localhost').pathname;
    const body = routes[pathname];
    if (body === undefined) return { ok: false, status: 404, json: async () => ({}) } as Response;
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** Stub every fetch with one canned failure response (login is the only call). */
function stubLoginFailure(status: number, json: () => Promise<unknown>): void {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status, json }) as unknown as Response));
}

function submitLogin(): void {
  render(<PasswordLogin onAuthenticated={vi.fn()} />);
  fireEvent.change(screen.getByTestId('login-username'), { target: { value: 'x' } });
  fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'y' } });
  fireEvent.click(screen.getByTestId('login-submit'));
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe('PasswordLogin', () => {
  it('logs in with same-origin credentials and reports the identity, holding no token', async () => {
    const fetchMock = stubFetch({ '/api/auth/login': { username: 'admin', is_admin: true } });
    const onAuthenticated = vi.fn();
    render(<PasswordLogin onAuthenticated={onAuthenticated} />);

    fireEvent.change(screen.getByTestId('login-username'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'admin' } });
    fireEvent.click(screen.getByTestId('login-submit'));

    await waitFor(() =>
      expect(onAuthenticated).toHaveBeenCalledWith({ username: 'admin', isAdmin: true }),
    );

    // The session lives in the Set-Cookie the browser stores itself — this
    // request must carry `credentials: 'same-origin'` so that happens, and
    // there is nothing left for this component to read out of the response
    // and hold onto (no token field in play at all).
    const [, init] = fetchMock.mock.calls[0] as [unknown, RequestInit];
    expect(init.credentials).toBe('same-origin');
  });

  it('shows an error on a failed login', async () => {
    stubFetch({}); // 404 for the login route
    render(<PasswordLogin onAuthenticated={vi.fn()} />);
    fireEvent.change(screen.getByTestId('login-username'), { target: { value: 'x' } });
    fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'y' } });
    fireEvent.click(screen.getByTestId('login-submit'));
    await screen.findByTestId('login-error');
  });
});

// ---------------------------------------------------------------------------
// Friendly login errors (issue #425) — the rendered banner must never carry a
// raw `HTTP <n>` status or an /api/ path; the technical detail goes to the
// console only.
// ---------------------------------------------------------------------------
describe('PasswordLogin error copy', () => {
  function assertFriendly(text: string, status: number): void {
    expect(text).not.toMatch(/HTTP/i);
    expect(text).not.toContain(String(status));
    expect(text).not.toMatch(/\/api\//);
    expect(text.trim().length).toBeGreaterThan(0);
  }

  it('renders no HTTP status code when the failure body carries no detail', async () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    stubLoginFailure(503, async () => ({}));
    submitLogin();

    const banner = await screen.findByTestId('login-error');
    assertFriendly(banner.textContent ?? '', 503);
    expect(logged).toHaveBeenCalled();
  });

  it('renders no HTTP status code when the failure body is not JSON', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    stubLoginFailure(500, () => Promise.reject(new SyntaxError('Unexpected token < in JSON')));
    submitLogin();

    const banner = await screen.findByTestId('login-error');
    assertFriendly(banner.textContent ?? '', 500);
  });

  it('still renders the server-supplied rejection message verbatim', async () => {
    stubLoginFailure(401, async () => ({ detail: 'Invalid username or password.' }));
    submitLogin();

    const banner = await screen.findByTestId('login-error');
    expect(banner.textContent).toBe('Invalid username or password.');
  });
});

describe('getToken in password mode', () => {
  it('always resolves the empty string — the session is a cookie, not a held token', async () => {
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    expect(await getToken()).toBe('');
  });
});

describe('App in password mode', () => {
  it('renders the password login gate, not the Cognito authenticator, with no session cookie', async () => {
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    stubFetch({}); // GET /api/me 404s (no cookie) -> falls through to the login gate
    render(<App />);
    expect(await screen.findByTestId('password-login')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Reload survival (issue #468's actual acceptance criterion: "Sign in ->
// reload -> still signed in"). `identity` is React state and is always null
// on the very first render after a reload, whether or not the httpOnly
// session cookie is still valid — this is what proves the SPA doesn't just
// rely on that state, but asks the server first.
// ---------------------------------------------------------------------------
describe('Session restore on mount (issue #468)', () => {
  it('skips the login gate and renders signed in when GET /api/me resolves a valid session', async () => {
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    stubFetch({
      '/api/me': { is_admin: false, cognito_sub: 'local:alice', username: 'alice' },
      '/version': { version: '0.0.0', commit: 'deadbeef', image_digest: '', uptime_seconds: 0 },
    });
    render(<App />);

    await waitFor(() => expect(screen.getByTestId('user-email').textContent).toBe('alice'));
    expect(screen.queryByTestId('password-login')).toBeNull();
  });

  it('shows the login gate, not signed in, when GET /api/me 401s (no/expired cookie)', async () => {
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    stubFetch({}); // every route 404s, including /api/me
    render(<App />);

    await screen.findByTestId('password-login');
    expect(screen.queryByTestId('user-email')).toBeNull();
  });
});

describe('Sign out in password mode (issue #468)', () => {
  it('POSTs /api/auth/logout with same-origin credentials so the server clears the cookie', async () => {
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    const fetchMock = stubFetch({
      '/api/auth/login': { username: 'admin', is_admin: true },
      '/api/me': { is_admin: true },
      '/version': { version: '0.0.0', commit: 'deadbeef', image_digest: '', uptime_seconds: 0 },
      '/api/auth/logout': { signed_out: true },
    });
    render(<App />);

    // The restore-session probe (this same /api/me stub, pre-login) resolves
    // first and — carrying no `username` — falls through to the login gate.
    await screen.findByTestId('password-login');
    fireEvent.change(screen.getByTestId('login-username'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'admin' } });
    fireEvent.click(screen.getByTestId('login-submit'));

    const signOutButton = await screen.findByText('Sign out');
    fireEvent.click(signOutButton);

    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/auth/logout'))).toBe(
        true,
      ),
    );
    const logoutCall = fetchMock.mock.calls.find(([input]) => String(input).includes('/api/auth/logout'));
    const [, init] = logoutCall as [unknown, RequestInit];
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('same-origin');

    // Back to the login gate — no client-held credential means there's
    // nothing to leave signed in once the cookie is cleared server-side.
    await screen.findByTestId('password-login');
  });

  it('stays signed in and surfaces an error when /api/auth/logout fails', async () => {
    // Regression test: the SPA must never show a signed-out UI (login gate)
    // while the session cookie is still valid server-side — the mount-time
    // restore probe would just sign it straight back in on the next reload.
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const pathname = new URL(String(input), 'http://localhost').pathname;
      if (pathname === '/api/auth/logout') {
        return { ok: false, status: 500, json: async () => ({}) } as Response;
      }
      const routes: Record<string, unknown> = {
        '/api/auth/login': { username: 'admin', is_admin: true },
        '/api/me': { is_admin: true },
        '/version': { version: '0.0.0', commit: 'deadbeef', image_digest: '', uptime_seconds: 0 },
      };
      const body = routes[pathname];
      if (body === undefined) return { ok: false, status: 404, json: async () => ({}) } as Response;
      return { ok: true, status: 200, json: async () => body } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    await screen.findByTestId('password-login');
    fireEvent.change(screen.getByTestId('login-username'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'admin' } });
    fireEvent.click(screen.getByTestId('login-submit'));

    const signOutButton = await screen.findByText('Sign out');
    fireEvent.click(signOutButton);

    await screen.findByTestId('sign-out-error');
    // Still signed in — the failed logout must not drop `identity` and show
    // the login gate over a session the server never actually cleared.
    expect(screen.queryByTestId('password-login')).toBeNull();
    expect(screen.getByTestId('user-email').textContent).toBe('admin');
    expect(logged).toHaveBeenCalled();
  });
});
