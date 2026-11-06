/**
 * first-visit-no-expired-banner.test.tsx — a visitor who has never signed in
 * must be shown the sign-in form, not "Your session expired".
 *
 * `PasswordApp` restores a session on mount by probing GET /api/me. For a
 * first-time visitor (no cookie) that probe is answered 401, and because it
 * went through `authorizedFetch` the central session-expired notifier fired
 * for it: the banner meant for a session that lapsed mid-use appeared on the
 * very first page load. The probe is an "am I signed in?" question, not a
 * request on behalf of a signed-in person, so it opts out of the notifier
 * (`suppressSessionExpiredNotification`), exactly as the version footer's
 * idle poll does.
 *
 * The mid-session case — a 401 AFTER sign-in must still show the banner — is
 * session-expired-rendered-587.test.tsx and is not repeated here.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import App from '../App';

// Amplify is never used in password mode, but App.tsx imports it unconditionally.
// eslint-disable-next-line @typescript-eslint/require-await
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn(async () => ({ tokens: {} })) }));
vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({ user: { username: 'x' }, signOut: vi.fn() }),
}));

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe('password mode, first visit with no session cookie', () => {
  it('shows the sign-in form and no "session expired" banner when the restore probe is answered 401', async () => {
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    // Every route a signed-out caller can reach answers 401, as the real
    // server does for GET /api/me with no cookie.
    const fetchMock = vi.fn<(input: RequestInfo | URL) => Promise<Response>>(() =>
      Promise.resolve({
        ok: false,
        status: 401,
        json: () => Promise.resolve({ detail: 'Not authenticated.' }),
      } as Response),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    await screen.findByTestId('password-login');
    // The probe really ran and really was refused; the banner is what must not follow.
    // eslint-disable-next-line @typescript-eslint/no-base-to-string
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/api/me'))).toBe(true);
    expect(screen.queryByTestId('session-expired')).toBeNull();
  });
});
