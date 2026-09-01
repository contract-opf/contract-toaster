/**
 * session-expired-rendered-587.test.tsx — issue #587.
 *
 * `onSessionExpired`/`authorizedFetch` (api.ts, issue #487) and the
 * `PasswordApp` listener that drops `identity` on it (App.tsx) already
 * exist and, per this file's own tests, work correctly at the rendered
 * level in password mode. What issue #587 demands, and what nothing
 * exercised before this file, is the RENDERED-level guarantee: after a real
 * signed-in load that put rows on screen, a 401 must make those rows
 * actually disappear from the DOM and the signed-out view actually appear —
 * not merely that some state variable flipped. This test drives the whole
 * password-mode `<App/>` (login -> a populated History table -> a 401 on a
 * subsequent authenticated call) and asserts at the DOM level, plus a
 * positive control proving an ordinary 200 response does NOT trigger the
 * same transition.
 *
 * Root-cause note (fix-round 1). The live prod observation (2026-08-21, on
 * 09f5188, deployed in password mode per deploy/dts/frontend.Dockerfile) is
 * NOT reproduced by this file — it passes unmodified against the pre-round
 * code, and password mode's `onSessionExpired` wiring in App.tsx predates
 * that commit (landed in 1d010c1 for #487, confirmed an ancestor of 09f5188
 * via `git merge-base --is-ancestor`). Two candidates were checked and
 * ruled out for password mode specifically: `getToken()` (auth.ts:42-44)
 * returns a literal `''` synchronously in password mode — it cannot reject
 * or skip `authorizedFetch`'s status check. A real, separate 401-handling
 * gap DOES exist — SSO mode never subscribed `onSessionExpired` at all
 * (only `PasswordApp` did) — and is fixed in this round (App.tsx's
 * `SsoApp`) with its own rendered-level coverage in
 * session-expired-rendered-sso-587.test.tsx. But the live report's own
 * deployment target is password mode, which that gap does not touch. Absent
 * further evidence, the password-mode live observation is most consistent
 * with a stale bundle (an old build artifact still being served, predating
 * even 1d010c1) rather than a code defect on 09f5188 — flagging this back
 * to the ticket rather than claiming it as reproduced or fixed here.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../App';

// Amplify is never used in password mode, but App.tsx imports it unconditionally.
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn(async () => ({ tokens: {} })) }));
vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({ user: { username: 'x' }, signOut: vi.fn() }),
}));

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
 * which every authenticated route 401s — a real "session died mid-use", not
 * a canned single response. `/api/auth/login` always succeeds regardless
 * (login itself is never the thing under test).
 */
function stubFetchWithExpiry(): { fetchMock: ReturnType<typeof vi.fn>; expireNow: () => void } {
  let expired = false;
  // The mount-time restore probe (GET /api/me, before login) must NOT carry
  // a username — the real reload-survival gate is exercised elsewhere
  // (password-auth.test.tsx); this fixture's `/api/me` starts empty and
  // only starts returning an identity once the login route has actually
  // been hit, so the test genuinely walks through the login gate.
  let loggedIn = false;
  const routes: Record<string, unknown> = {
    '/version': VERSION_OK,
    '/api/playbooks': { playbooks: [] },
    '/api/reviews': { reviews: [HISTORY_ROW] },
    '/api/reviews/row-under-test/output': { url: 'https://example.com/signed-output-url' },
    '/api/reviews/row-under-test/input': { url: 'https://example.com/signed-input-url' },
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const pathname = new URL(String(input), 'http://localhost').pathname;
    if (pathname === '/api/auth/login') {
      loggedIn = true;
      return { ok: true, status: 200, json: async () => ({ username: 'admin', is_admin: true }) } as Response;
    }
    if (pathname === '/api/me') {
      if (!loggedIn) {
        return { ok: false, status: 401, json: async () => ({}) } as Response;
      }
      if (expired) {
        return { ok: false, status: 401, json: async () => ({ detail: 'Not authenticated.' }) } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ is_admin: true, cognito_sub: 'local:admin', username: 'admin' }),
      } as Response;
    }
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
  };
}

async function loginAndSeeHistoryRow(): Promise<void> {
  vi.stubEnv('VITE_AUTH_MODE', 'password');
  render(<App />);

  const loginForm = await screen.findByTestId('password-login');
  fireEvent.change(screen.getByTestId('login-username'), { target: { value: 'admin' } });
  fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'admin' } });
  fireEvent.click(screen.getByTestId('login-submit'));

  await waitFor(() => expect(screen.getByTestId('user-email').textContent).toBe('admin'));
  // The login form is gone once sign-in actually succeeds.
  expect(loginForm).not.toBeInTheDocument();
  await screen.findByTestId('history-row-row-under-test');
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe('issue #587 — a 401 mid-session clears the signed-in UI, not just a flag', () => {
  it('rendered-level regression coverage: a stale row must not survive the 401', async () => {
    // This is characterization/regression coverage of pre-existing
    // password-mode wiring, not a fail-first proof for #587: run unmodified
    // against HEAD (no diff from this round applied) and it passes, because
    // password mode's `onSessionExpired` wiring in PasswordApp already
    // existed before this branch (predates it — see the module docstring's
    // root-cause note). What this file adds is the rendered-level assertion
    // of that pre-existing wiring — that a 401 actually clears the DOM, not
    // just some state variable — which nothing exercised before this file.
    const { expireNow } = stubFetchWithExpiry();
    await loginAndSeeHistoryRow();

    expireNow();
    // Any authenticated call fires the notifier; the Redline download
    // button is a real click on a real authenticated call.
    fireEvent.click(screen.getByTestId('history-download-output-row-under-test'));

    await screen.findByTestId('password-login');
    expect(screen.queryByTestId('history-row-row-under-test')).toBeNull();
    expect(screen.queryByTestId('user-email')).toBeNull();
    expect(document.body.textContent).not.toContain('demo-nda');
    // The AC requires telling the user WHY, not just showing the login form.
    expect(screen.getByTestId('session-expired')).toHaveTextContent(
      'Your session expired — sign in to continue.',
    );
  });

  it('positive control: an ordinary 200 does not sign the app out or drop rows', async () => {
    const { fetchMock } = stubFetchWithExpiry();
    await loginAndSeeHistoryRow();

    // A 200 on the very same authenticated route the negative case 401s.
    // Captured and asserted directly so this control cannot silently
    // degrade into exercising a 404 (or any other non-200) again.
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

    expect(screen.queryByTestId('password-login')).toBeNull();
    expect(screen.getByTestId('user-email').textContent).toBe('admin');
    expect(screen.getByTestId('history-row-row-under-test')).toBeTruthy();
    expect(screen.queryByTestId('session-expired')).toBeNull();
  });
});
