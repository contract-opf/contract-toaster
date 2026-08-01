/**
 * sso-signin-surface.test.tsx — the AWS-target (Cognito/SSO) sign-in surface
 * (issue #426).
 *
 * Locks in what `<App/>` puts on screen with VITE_AUTH_MODE unset — the AWS
 * default, so `isPasswordMode()` is false and App renders the Amplify
 * `<Authenticator>`:
 *
 *   1. Both sign-in paths are offered: a "Sign In with Google" affordance
 *      (socialProviders={['google']}) AND the standard username/password
 *      form. The Google button ADDS a federated path; it does not replace
 *      the form.
 *   2. NO self-registration affordance: no "Create Account" tab, no sign-up
 *      route. Admission is Google SSO + Cognito JIT provisioning behind an
 *      application allowlist (ARCHITECTURE.md → Authentication); a sign-up
 *      tab could only ever dead-end.
 *
 * DELIBERATELY UNMOCKED: unlike password-auth / admin-gate / security-posture
 * / resilience-a11y, this file does NOT `vi.mock('@aws-amplify/ui-react')`.
 * Those mocks replace `Authenticator` with a pass-through that renders its
 * children, so an assertion about the sign-in surface made against them would
 * be an assertion about the mock — it would pass whatever props App passed,
 * including none. Here the real component renders and the real DOM is
 * asserted, which is the only way a wrong prop shape is caught (tsc accepts
 * anything assignable; only a real render proves the tab is gone and the
 * Google button is there). The `assertRealAuthenticator` guard below fails
 * loudly if a future mock ever creeps in via setupTests or a config default.
 *
 * Still fully offline: Amplify is never configured in tests (main.tsx is not
 * imported), so its auth calls reject locally with "Amplify has not been
 * configured" — logged to stderr, handled by the Authenticator's state
 * machine, and no network is touched. `fetchAuthSession` is stubbed anyway so
 * no session lookup can escape; `fetch` is stubbed to throw as a backstop.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import App from '../App';

// Keep the session lookup local. `importActual` is used rather than a bare
// factory because the real Authenticator imports many other symbols from this
// module (signIn, getCurrentUser, …) — replacing the whole module would break
// the component under test, which is exactly what this file exists to avoid.
vi.mock('aws-amplify/auth', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('aws-amplify/auth');
  return { ...actual, fetchAuthSession: vi.fn(async () => ({ tokens: {} })) };
});

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      throw new Error('no network in tests');
    }),
  );
});

/**
 * Render `<App/>` and wait for the Authenticator's state machine to settle on
 * the sign-in route. The machine boots asynchronously (it probes for an
 * existing session first), so a synchronous render sees an empty container —
 * every "is absent" assertion would pass vacuously against it.
 *
 * Also fails loudly if `<Authenticator>` has been mocked away: Amplify renders
 * its container with `data-amplify-authenticator` and its sign-in route as a
 * `data-amplify-authenticator-signin` form, and a pass-through mock emits
 * neither.
 */
async function renderSignInSurface(): Promise<HTMLElement> {
  render(<App />);
  return waitFor(() => {
    expect(
      document.querySelector('[data-amplify-authenticator]'),
      'the real Amplify Authenticator must render here — a vi.mock would make these assertions vacuous',
    ).not.toBeNull();
    const form = document.querySelector<HTMLElement>('[data-amplify-authenticator-signin]');
    expect(form, 'the Authenticator must settle on its sign-in route').not.toBeNull();
    return form as HTMLElement;
  });
}

describe('AWS-target sign-in surface (VITE_AUTH_MODE unset)', () => {
  it('offers Google SSO alongside the username/password form', async () => {
    const form = await renderSignInSurface();

    // Federated path (socialProviders={['google']}).
    expect(screen.getByRole('button', { name: /sign in with google/i })).toBeInTheDocument();

    // …which ADDS to, and does not replace, the standard form.
    expect(screen.getByLabelText(/username/i)).toBeInTheDocument();
    expect(form.querySelector('input[type="password"]')).not.toBeNull();
  });

  it('offers no self-registration affordance', async () => {
    await renderSignInSurface();

    // The "Create Account" tab (hideSignUp) — Amplify's only sign-up entry
    // point from the sign-in route. Without hideSignUp the Authenticator
    // renders it as a tab next to "Sign In".
    expect(screen.queryByText(/create account/i)).toBeNull();
    expect(screen.queryByRole('tab', { name: /create account|sign ?up/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /create account|sign ?up/i })).toBeNull();

    // …and no sign-up form rendered by any other route.
    expect(document.querySelector('[data-amplify-authenticator-signup]')).toBeNull();
  });
});
