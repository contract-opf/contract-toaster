/**
 * SsoShell — the AWS-target (Cognito/SSO) auth shell.
 *
 * `SsoApp` and `SsoShell` lived in App.tsx until issue #56. They are here
 * now for ONE reason: they are the only shipping code that imports
 * `@aws-amplify/ui-react`, and App.tsx `React.lazy`s this module so that
 * neither the Authenticator nor the `aws-amplify` runtime it drags in lands
 * in the entry chunk. A password-mode (Docker Compose) deployment never
 * calls `renderAuthenticatedApp`'s SSO branch at all, so it never fetches
 * this chunk — which is the whole point.
 *
 * The import of `Authenticator` below is deliberately STATIC, not dynamic:
 * `tests/test_infra_frontend_stack.py` Check C demands a real
 * `import { Authenticator } from '@aws-amplify/ui-react'` and a real
 * rendered `<Authenticator …>` element in the same non-test file under
 * frontend/src/ (see #451 — a commented-out or `vi.mock`'d form used to
 * satisfy it on a codebase with no Amplify auth at all). The splitting is
 * done by the lazy boundary in App.tsx; doing it a second time here would
 * buy nothing and break that check.
 *
 * Two Authenticator props make the AWS-target sign-in screen match the
 * product's actual access model (issue #426):
 *
 *   socialProviders={['google']} — renders a "Sign In with Google" button
 *     ABOVE the standard form. It ADDS the federated path; it does not
 *     replace the username/password form. Both are supported sign-in paths.
 *   hideSignUp — removes the "Create Account" tab. This product has zero
 *     self-registration: admission is Google SSO + Cognito JIT provisioning
 *     behind an application allowlist (ARCHITECTURE.md → Authentication), so
 *     a sign-up tab is an affordance that can only ever dead-end.
 *
 * Both are verified present on the installed @aws-amplify/ui-react@6.15.4
 * (AuthenticatorProps → RouterProps/SignInBaseProps; both are destructured
 * by AuthenticatorInternal). See sso-signin-surface.test.tsx, which renders
 * the REAL Authenticator — no vi.mock — and asserts the resulting surface.
 */
import { useEffect, useState } from 'react';
import { Authenticator, useAuthenticator } from '@aws-amplify/ui-react';
import { AppContent } from './App';
import { onSessionExpired } from './api';
import { CtBanner } from './ui/react';

// SSO (AWS) target: the Cognito Authenticator provides the identity; derive the
// email and sign-out from the Amplify session, exactly as before.
//
// Issue #587. Before this, a 401 from an authenticated call in SSO mode fired
// the central notifier (api.ts, issue #487) into an empty listener set —
// `PasswordApp` was the only subscriber in the whole codebase — so the
// authenticated shell and stale History rows stayed on screen with no
// visible signal that the session had died. Calling Amplify's own `signOut`
// here forces the Authenticator back to its signed-out (hosted UI) surface,
// the same mechanism the user's own sign-out button already uses.
// `onExpired`/`onReauthenticated` (from `SsoShell`, which survives the
// sign-out unlike this component, since `signOut` unmounts it) are what
// actually drive the "session expired" banner on the resulting hosted-UI
// surface, and clear it again once the user is back — see `SsoShell`.
function SsoApp({
  onExpired,
  onReauthenticated,
}: {
  onExpired: () => void;
  onReauthenticated: () => void;
}): React.ReactElement {
  const { user, signOut } = useAuthenticator((ctx) => [ctx.user]);
  const userEmail: string =
    (user as { signInDetails?: { loginId?: string } }).signInDetails?.loginId ??
    (user as { username?: string }).username ??
    'unknown';

  useEffect(
    () =>
      onSessionExpired(() => {
        onExpired();
        signOut?.();
      }),
    [signOut, onExpired],
  );

  // `SsoApp` only ever renders while the Authenticator considers the user
  // signed in — including immediately after a re-sign-in following an
  // expiry — so mounting it is exactly the "reauthenticated" event. Without
  // this, the banner set by `onExpired` above would never clear: it renders
  // outside the Authenticator (see `SsoShell`), so nothing else tells it the
  // user signed back in. Mirrors `PasswordApp`, which clears its own
  // `sessionExpired` flag from `PasswordLogin`'s `onAuthenticated`.
  useEffect(() => {
    onReauthenticated();
    // Run once per mount only — `onReauthenticated` is a fresh closure each
    // render, and re-running this on every render would still be correct
    // (it's idempotent) but noisier than necessary.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <AppContent userEmail={userEmail} signOut={signOut ?? (() => {})} />;
}

// Issue #587. `SsoApp` unmounts the instant `signOut()` runs (the
// Authenticator swaps it for its own hosted-UI sign-in surface), so any
// "why are you seeing this" state has to live above the Authenticator,
// where it survives that unmount. `SsoShell` is that place: it owns the
// `sessionExpired` flag, passes `onExpired`/`onReauthenticated` down so
// `SsoApp` can flip it on a 401 and clear it again once signed back in, and
// renders the same `session-expired` banner password mode already shows
// (App.tsx's `PasswordApp`) around whatever the Authenticator is currently
// rendering — the hosted sign-in form on expiry, `SsoApp` once signed in
// again.
export default function SsoShell(): React.ReactElement {
  const [sessionExpired, setSessionExpired] = useState(false);

  return (
    <>
      {sessionExpired && (
        <CtBanner variant="warn" data-testid="session-expired">
          Your session expired — sign in to continue.
        </CtBanner>
      )}
      <Authenticator hideSignUp socialProviders={['google']}>
        {() => (
          <SsoApp
            onExpired={() => setSessionExpired(true)}
            onReauthenticated={() => setSessionExpired(false)}
          />
        )}
      </Authenticator>
    </>
  );
}
