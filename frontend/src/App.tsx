/**
 * App — Review Tool root component. The displayed product name comes from
 * VITE_PRODUCT_NAME (build-time config, default "Contract Toaster" —
 * issue #274; an adopter renames the product without touching source).
 *
 * Phase 0 skeleton: sign-in only.
 *
 * After sign-in via Google (Cognito hosted UI), the app shows:
 *   - Header: "Signed in as you@example.com" (the authenticated user's email)
 *   - Footer: version from the authenticated /version endpoint
 *
 * The Authenticator component from @aws-amplify/ui-react handles the full
 * sign-in flow (redirects to Cognito hosted UI, handles the OAuth callback,
 * and manages the session).
 *
 * ATTORNEY-APPROVAL WATERMARK: all output states carry the mandatory
 * watermark "tool recommendation only — attorney approval required" (see
 * ARCHITECTURE.md → Frontend) — implemented by ReviewSubmission, the
 * reviewer flow added for issue #186.
 *
 * ACCEPT/REQUEST_CHANGE framing: ACCEPT reads "no requested changes
 * identified by tool" — never "no action needed" or "approved"
 * (ARCHITECTURE.md § Wrong-format rejection UX) — see ReviewSubmission.
 */

import { useEffect, useState } from 'react';
import { Authenticator, useAuthenticator } from '@aws-amplify/ui-react';
import '@aws-amplify/ui-react/styles.css';
import AdminUsers from './AdminUsers';
import AdminRetention from './AdminRetention';
import ReviewSubmission from './ReviewSubmission';
import PasswordLogin, { DemoIdentity } from './PasswordLogin';
import { getToken, isPasswordMode, setDemoToken } from './auth';

// ---------------------------------------------------------------------------
// Product name (issue #274) — build-time config, no internal name baked in.
// index.html ships a static "Contract Toaster" <title> (matching this same
// default) so the tab has a name before this module evaluates; this line
// overrides it to the configured VITE_PRODUCT_NAME when one is set.
// ---------------------------------------------------------------------------
export const PRODUCT_NAME: string = import.meta.env.VITE_PRODUCT_NAME ?? 'Contract Toaster';
if (typeof document !== 'undefined') {
  document.title = PRODUCT_NAME;
}

// ---------------------------------------------------------------------------
// Version info fetched from the authenticated /version endpoint.
// The backend stub returns: { version, commit, image_digest, uptime_seconds }
// ---------------------------------------------------------------------------
interface VersionInfo {
  version: string;
  commit: string;
  image_digest: string;
  uptime_seconds: number;
}

// ---------------------------------------------------------------------------
// Admin-visibility gate (issue #234).
//
// AdminUsers/AdminRetention used to mount unconditionally and rely on their
// own HTTP 403 to hide themselves, which flashed admin chrome ("Loading
// users…", the break-glass note, etc.) for every reviewer on every load.
// The server is still authoritative — every admin endpoint still 403s a
// non-admin caller — but the SPA now waits to learn the caller's *resolved*
// role from GET /api/me (issue #235) before it decides whether to mount the
// admin panels at all. While that probe is in flight, and if it comes back
// non-admin (or fails), nothing admin-ish renders.
// ---------------------------------------------------------------------------
type AdminCapability = 'loading' | 'admin' | 'non-admin';

function useAdminCapability(): AdminCapability {
  const [capability, setCapability] = useState<AdminCapability>('loading');

  useEffect(() => {
    let cancelled = false;

    async function probeCapability(): Promise<void> {
      try {
        const token = await getToken();
        const apiBase = import.meta.env.VITE_API_BASE_URL ?? '';
        const response = await fetch(`${apiBase}/api/me`, {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        });

        if (!response.ok) {
          throw new Error(`/api/me returned HTTP ${response.status}`);
        }

        const data = (await response.json()) as { is_admin: boolean };
        if (!cancelled) {
          setCapability(data.is_admin ? 'admin' : 'non-admin');
        }
      } catch {
        // Fail closed: any probe failure (network error, non-2xx,
        // malformed body) is treated as non-admin. The server remains the
        // real authority for every admin endpoint — this probe only
        // decides whether the SPA attempts to render admin UI at all.
        if (!cancelled) {
          setCapability('non-admin');
        }
      }
    }

    void probeCapability();
    return () => {
      cancelled = true;
    };
  }, []);

  return capability;
}

// AppContent takes the identity (email) and sign-out handler as props, so it
// is independent of how the caller authenticated — Cognito (SsoApp) or
// username/password (PasswordApp).
function AppContent({
  userEmail,
  signOut,
}: {
  userEmail: string;
  signOut: () => void;
}): React.ReactElement {
  const [versionInfo, setVersionInfo] = useState<VersionInfo | null>(null);
  const [versionError, setVersionError] = useState<string | null>(null);
  const adminCapability = useAdminCapability();

  // Fetch version from the authenticated /version endpoint.
  // The JWT from the current Amplify session is sent as a Bearer token.
  // /health is public/liveness-only; /version requires authentication.
  useEffect(() => {
    let cancelled = false;

    async function fetchVersion(): Promise<void> {
      try {
        const token = await getToken();

        const apiBase = import.meta.env.VITE_API_BASE_URL ?? '';
        const response = await fetch(`${apiBase}/version`, {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        });

        if (!response.ok) {
          throw new Error(`/version returned HTTP ${response.status}`);
        }

        const data = (await response.json()) as VersionInfo;
        if (!cancelled) {
          setVersionInfo(data);
        }
      } catch (err) {
        if (!cancelled) {
          // eslint-disable-next-line no-console
          console.error(err);
          setVersionError('Version information is unavailable right now.');
        }
      }
    }

    void fetchVersion();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div style={{ fontFamily: 'sans-serif', maxWidth: '800px', margin: '0 auto', padding: '1rem' }}>
      {/* Header — signed-in user's email */}
      <header
        style={{
          borderBottom: '1px solid #ddd',
          paddingBottom: '0.5rem',
          marginBottom: '1rem',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <span>
          <strong>{PRODUCT_NAME} Review Tool</strong>
        </span>
        <span>
          Signed in as{' '}
          <span data-testid="user-email" style={{ fontWeight: 'bold' }}>
            {userEmail}
          </span>
          {' — '}
          <button
            onClick={signOut}
            style={{
              background: 'none',
              border: 'none',
              color: '#0073bb',
              cursor: 'pointer',
              textDecoration: 'underline',
              padding: 0,
            }}
          >
            Sign out
          </button>
        </span>
      </header>

      {/* Main area */}
      <main>
        {/* Upload/poll/download review flow (issue #186). */}
        <ReviewSubmission />

        {/* Admin Users (#92) + retention/legal-hold (#94) screens.
            Gated on the resolved role from GET /api/me (#234/#235): they
            don't mount at all until the probe resolves `is_admin: true`,
            so a non-admin reviewer never sees admin chrome — not even
            momentarily. The server remains authoritative; each panel also
            keeps its own 403-based gate as defense in depth. */}
        {adminCapability === 'admin' && (
          <>
            <AdminUsers />
            <AdminRetention />
          </>
        )}
      </main>

      {/* Footer — version from authenticated /version endpoint */}
      <footer
        style={{
          borderTop: '1px solid #ddd',
          marginTop: '2rem',
          paddingTop: '0.5rem',
          fontSize: '0.8rem',
          color: '#666',
        }}
      >
        {versionError ? (
          <span data-testid="version-error">{versionError}</span>
        ) : versionInfo ? (
          <span data-testid="version-display">
            Version {versionInfo.version} ({versionInfo.commit.slice(0, 8)})
          </span>
        ) : (
          <span data-testid="version-loading">Loading version…</span>
        )}
      </footer>
    </div>
  );
}

/**
 * App — wraps the content with the Amplify Authenticator.
 *
 * The Authenticator component handles the full Cognito hosted-UI sign-in flow.
 * When not signed in, it renders the Cognito hosted UI redirect.
 * When signed in, it renders the app content (AppContent).
 */
// SSO (AWS) target: the Cognito Authenticator provides the identity; derive the
// email and sign-out from the Amplify session, exactly as before.
function SsoApp(): React.ReactElement {
  const { user, signOut } = useAuthenticator((ctx) => [ctx.user]);
  const userEmail: string =
    (user as { signInDetails?: { loginId?: string } }).signInDetails?.loginId ??
    (user as { username?: string }).username ??
    'unknown';
  return <AppContent userEmail={userEmail} signOut={signOut ?? (() => {})} />;
}

// Password (DTS) target: gate on PasswordLogin; once signed in, render the app
// with the demo identity. Sign-out clears the in-memory token.
function PasswordApp(): React.ReactElement {
  const [identity, setIdentity] = useState<DemoIdentity | null>(null);
  if (!identity) {
    return <PasswordLogin onAuthenticated={setIdentity} />;
  }
  return (
    <AppContent
      userEmail={identity.username}
      signOut={() => {
        setDemoToken(null);
        setIdentity(null);
      }}
    />
  );
}

export default function App(): React.ReactElement {
  if (isPasswordMode()) {
    return <PasswordApp />;
  }
  return <Authenticator>{() => <SsoApp />}</Authenticator>;
}
