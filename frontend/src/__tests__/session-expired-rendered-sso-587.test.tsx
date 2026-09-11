/**
 * session-expired-rendered-sso-587.test.tsx — issue #587, SSO-mode gap.
 *
 * session-expired-rendered-587.test.tsx locks in the RENDERED-level 401
 * guarantee for password mode. That file's own fix-round-1 investigation
 * found a second, real defect the ticket's AC does not exempt: SSO mode
 * (the AWS default — `authMode()` resolves 'sso' when VITE_AUTH_MODE is
 * unset) never subscribed to `onSessionExpired` at all. `PasswordApp` was
 * the only subscriber in the whole codebase, so a 401 in SSO mode fired the
 * central notifier (api.ts, issue #487) into an empty listener set: the
 * authenticated shell and any already-fetched History rows stayed on
 * screen with no signal that the session had died.
 *
 * Issue #56 moved `SsoApp`/`SsoShell` into `src/SsoShell.tsx` and made
 * App.tsx load it with `React.lazy`, so the signed-in shell is now one
 * `Suspense` boundary away from `render(<App/>)` — hence `findByTestId`
 * where this file used to read `getByTestId` on the first paint. The
 * module-level `vi.mock('@aws-amplify/ui-react', …)` below still applies:
 * a lazily imported module resolves through the same mocked registry.
 *
 * The fix (SsoShell.tsx's `SsoApp`/`SsoShell`) subscribes and calls Amplify's own
 * `signOut()` on expiry — the same mechanism the user's own sign-out
 * button already uses to force the Authenticator back to its signed-out
 * surface — and shows the `session-expired` banner from `SsoShell`, which
 * sits above the Authenticator so it survives that unmount. This file
 * proves that at the DOM level, mirroring the password-mode test: a real
 * signed-in render with a populated History table, then a 401 on a
 * subsequent authenticated call, must make the signed-in shell disappear —
 * not merely flip an internal flag — plus a positive control proving an
 * ordinary 200 does not trigger the same transition, plus a third case
 * proving the banner actually clears again once the user signs back in
 * (fix-round-3 finding: an earlier version of this fix left the banner
 * pinned above the authenticated shell forever, since nothing ever reset
 * it — `SsoApp` now clears it on its own mount, mirroring how
 * `PasswordLogin`'s `onAuthenticated` clears password mode's flag).
 *
 * The Amplify `Authenticator`/`useAuthenticator` pair is mocked with a
 * small shared, stateful store (not the static object literal other
 * SSO-adjacent tests use) specifically so that calling the mocked
 * `signOut()` actually flips what's on screen — a static mock would make
 * this test unable to fail.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../App';

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn(async () => ({ tokens: {} })) }));

const authStore = vi.hoisted(() => {
  let signedIn = true;
  const listeners = new Set<() => void>();
  return {
    isSignedIn: () => signedIn,
    signOut: () => {
      signedIn = false;
      listeners.forEach((listener) => listener());
    },
    // Simulates the user completing the hosted-UI sign-in flow again after
    // an expiry — the only way, in this mock, that `SsoApp` remounts.
    signIn: () => {
      signedIn = true;
      listeners.forEach((listener) => listener());
    },
    reset: () => {
      signedIn = true;
    },
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
});

vi.mock('@aws-amplify/ui-react', async () => {
  const React = await vi.importActual<typeof import('react')>('react');
  function useAuthStore(): boolean {
    return React.useSyncExternalStore(authStore.subscribe, authStore.isSignedIn);
  }
  return {
    Authenticator: ({ children }: { children: () => React.ReactElement }) => {
      const signedIn = useAuthStore();
      if (!signedIn) {
        return React.createElement(
          'div',
          { 'data-testid': 'sso-signin-gate' },
          'Sign in with Google',
          React.createElement(
            'button',
            { 'data-testid': 'sso-signin-gate-submit', onClick: authStore.signIn },
            'Sign In with Google',
          ),
        );
      }
      return children();
    },
    useAuthenticator: () => {
      useAuthStore();
      return {
        user: { username: 'sso-sub', signInDetails: { loginId: 'reviewer@example.com' } },
        signOut: authStore.signOut,
      };
    },
  };
});

const VERSION_OK = {
  version: '0.0.1',
  commit: 'abcdef1234567890',
  image_digest: 'sha256:x',
  uptime_seconds: 1,
};

const HISTORY_ROW = {
  review_id: 'row-under-test',
  playbook_id: 'demo-nda',
  status: 'DONE',
  decision: 'DONE',
  created_at: 1700000000,
  has_output: true,
  has_input: false,
};

/**
 * Every authenticated route succeeds until `expireNow()` is called, after
 * which every authenticated route 401s.
 */
function stubFetchWithExpiry(): {
  fetchMock: ReturnType<typeof vi.fn>;
  expireNow: () => void;
  reauthenticate: () => void;
} {
  let expired = false;
  const routes: Record<string, unknown> = {
    '/version': VERSION_OK,
    '/api/me': { is_admin: false },
    '/api/playbooks': { playbooks: [] },
    '/api/reviews': { reviews: [HISTORY_ROW] },
    '/api/reviews/row-under-test/output': { url: 'https://example.com/signed-output-url' },
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const pathname = new URL(String(input), 'http://localhost').pathname;
    if (expired) {
      return { ok: false, status: 401, json: async () => ({ detail: 'Not authenticated.' }) } as Response;
    }
    const body = routes[pathname];
    if (body === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return {
    fetchMock,
    expireNow: () => {
      expired = true;
    },
    // A real re-sign-in gets a fresh, valid session — mirrors that by
    // clearing the fetch fixture's own expiry, distinct from the DOM-level
    // `authStore.signIn()` that flips the mocked Authenticator/gate.
    reauthenticate: () => {
      expired = false;
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  authStore.reset();
});

describe('issue #587 — SSO mode: a 401 mid-session clears the signed-in UI', () => {
  it('a 401 forces the Authenticator back to its signed-out surface and drops rows', async () => {
    const { expireNow } = stubFetchWithExpiry();
    render(<App />);

    expect((await screen.findByTestId('user-email')).textContent).toBe('reviewer@example.com');
    await screen.findByTestId('history-row-row-under-test');

    expireNow();
    fireEvent.click(screen.getByTestId('history-download-output-row-under-test'));

    await screen.findByTestId('sso-signin-gate');
    expect(screen.queryByTestId('history-row-row-under-test')).toBeNull();
    expect(screen.queryByTestId('user-email')).toBeNull();
    expect(document.body.textContent).not.toContain('demo-nda');
    // The AC requires telling the user WHY, not just showing the sign-in
    // surface — the same requirement password mode's `session-expired`
    // banner satisfies (session-expired-rendered-587.test.tsx).
    expect(screen.getByTestId('session-expired')).toHaveTextContent(
      'Your session expired — sign in to continue.',
    );
  });

  it('positive control: an ordinary 200 does not sign the app out or drop rows', async () => {
    const { fetchMock } = stubFetchWithExpiry();
    render(<App />);

    expect((await screen.findByTestId('user-email')).textContent).toBe('reviewer@example.com');
    await screen.findByTestId('history-row-row-under-test');

    fireEvent.click(screen.getByTestId('history-download-output-row-under-test'));
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([input]) =>
        String(input).includes('/api/reviews/row-under-test/output'),
      );
      expect(call, 'the download route must actually have been called').toBeTruthy();
    });
    const outputCall = fetchMock.mock.results.find((_, index) =>
      String(fetchMock.mock.calls[index]?.[0]).includes('/api/reviews/row-under-test/output'),
    );
    const outputResponse = (await outputCall?.value) as Response;
    expect(outputResponse.status).toBe(200);

    expect(screen.queryByTestId('sso-signin-gate')).toBeNull();
    expect(screen.getByTestId('user-email').textContent).toBe('reviewer@example.com');
    expect(screen.getByTestId('history-row-row-under-test')).toBeTruthy();
    expect(screen.queryByTestId('session-expired')).toBeNull();
  });

  it('fix-round-3 regression: the banner clears once the user signs back in', async () => {
    const { expireNow, reauthenticate } = stubFetchWithExpiry();
    render(<App />);

    await screen.findByTestId('history-row-row-under-test');

    expireNow();
    fireEvent.click(screen.getByTestId('history-download-output-row-under-test'));

    await screen.findByTestId('sso-signin-gate');
    expect(screen.getByTestId('session-expired')).toHaveTextContent(
      'Your session expired — sign in to continue.',
    );

    // A real re-sign-in gets a fresh, valid session; simulate that before
    // driving the mocked hosted-UI sign-in, or the immediate post-sign-in
    // fetches would still 401 and re-trip the same expiry path.
    reauthenticate();
    // Sign back in through the mocked hosted UI — this is what an earlier
    // version of the fix never wired up, leaving the banner pinned above
    // the authenticated shell forever.
    fireEvent.click(screen.getByTestId('sso-signin-gate-submit'));

    await screen.findByTestId('user-email');
    expect(screen.queryByTestId('session-expired')).toBeNull();
  });
});
