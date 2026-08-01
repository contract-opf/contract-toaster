/**
 * password-auth.test.tsx — Docker Compose password-mode auth path (VITE_AUTH_MODE=password).
 *
 * Covers the pieces the Docker Compose deployment adds:
 *   1. PasswordLogin posts to /api/auth/login and, on success, stores the demo
 *      token in the in-memory auth module and reports the identity.
 *   2. getToken() returns that stored token in password mode (and never touches
 *      Amplify).
 *   3. <App/> in password mode renders the login gate, not the Cognito
 *      Authenticator.
 *   4. Login failures render friendly copy only — never a raw `HTTP <n>` status
 *      or endpoint path (issue #425), the same rule resilience-a11y.test.tsx
 *      enforces for the review screens.
 *
 * The token is deliberately in-memory only (no localStorage/sessionStorage) —
 * the security-posture source guard forbids Storage.setItem in components.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import PasswordLogin from '../PasswordLogin';
import App from '../App';
import { getToken, getDemoToken, setDemoToken } from '../auth';

// Amplify is never used in password mode, but App.tsx imports it — mock so the
// import resolves without a real Cognito/Amplify runtime.
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn(async () => ({ tokens: {} })) }));
vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({ user: { username: 'x' }, signOut: vi.fn() }),
}));

function stubFetch(routes: Record<string, unknown>): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const pathname = new URL(String(input), 'http://localhost').pathname;
      const body = routes[pathname];
      if (body === undefined) return { ok: false, status: 404, json: async () => ({}) } as Response;
      return { ok: true, status: 200, json: async () => body } as Response;
    }),
  );
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
  setDemoToken(null);
});

afterEach(() => {
  vi.unstubAllEnvs();
  setDemoToken(null);
});

describe('PasswordLogin', () => {
  it('logs in, stores the token, and reports the identity', async () => {
    stubFetch({ '/api/auth/login': { token: 'demo.jwt.token', username: 'admin', is_admin: true } });
    const onAuthenticated = vi.fn();
    render(<PasswordLogin onAuthenticated={onAuthenticated} />);

    fireEvent.change(screen.getByTestId('login-username'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'admin' } });
    fireEvent.click(screen.getByTestId('login-submit'));

    await waitFor(() =>
      expect(onAuthenticated).toHaveBeenCalledWith({ username: 'admin', isAdmin: true }),
    );
    expect(getDemoToken()).toBe('demo.jwt.token');
  });

  it('shows an error and stores no token on a failed login', async () => {
    stubFetch({}); // 404 for the login route
    render(<PasswordLogin onAuthenticated={vi.fn()} />);
    fireEvent.change(screen.getByTestId('login-username'), { target: { value: 'x' } });
    fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'y' } });
    fireEvent.click(screen.getByTestId('login-submit'));
    await screen.findByTestId('login-error');
    expect(getDemoToken()).toBeNull();
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
    expect(getDemoToken()).toBeNull();
  });

  it('renders no HTTP status code when the failure body is not JSON', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    stubLoginFailure(500, () => Promise.reject(new SyntaxError('Unexpected token < in JSON')));
    submitLogin();

    const banner = await screen.findByTestId('login-error');
    assertFriendly(banner.textContent ?? '', 500);
    expect(getDemoToken()).toBeNull();
  });

  it('still renders the server-supplied rejection message verbatim', async () => {
    stubLoginFailure(401, async () => ({ detail: 'Invalid username or password.' }));
    submitLogin();

    const banner = await screen.findByTestId('login-error');
    expect(banner.textContent).toBe('Invalid username or password.');
  });
});

describe('getToken in password mode', () => {
  it('returns the stored demo token without calling Amplify', async () => {
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    setDemoToken('the-demo-token');
    expect(await getToken()).toBe('the-demo-token');
  });

  it('returns empty string before login', async () => {
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    expect(await getToken()).toBe('');
  });
});

describe('App in password mode', () => {
  it('renders the password login gate, not the Cognito authenticator', async () => {
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    render(<App />);
    expect(await screen.findByTestId('password-login')).toBeTruthy();
  });
});
