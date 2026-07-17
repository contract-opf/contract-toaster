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

import { useCallback, useEffect, useRef, useState } from 'react';
import { Authenticator, useAuthenticator } from '@aws-amplify/ui-react';
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

// ---------------------------------------------------------------------------
// Tabbed shell (issue: Pico tabbed layout). One app shell + one Review
// experience shared by both roles; the two admin tabs are appended only for
// an admin caller.
// ---------------------------------------------------------------------------
type TabId = 'review' | 'users' | 'retention';

interface TabDef {
  id: TabId;
  label: string;
}

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

  const isAdmin = adminCapability === 'admin';

  // Tab set: Review is always present; the two admin tabs are appended only
  // for an admin caller. `useAdminCapability` decides ONLY this — it never
  // branches the header, the Review tab, or <ReviewSubmission /> itself.
  const tabs: TabDef[] = [
    { id: 'review', label: 'Review' },
    ...(isAdmin
      ? ([
          { id: 'users', label: 'Users & access' },
          { id: 'retention', label: 'Retention & legal hold' },
        ] as TabDef[])
      : []),
  ];

  const [activeTab, setActiveTab] = useState<TabId>('review');
  const tablistRef = useRef<HTMLDivElement | null>(null);

  // Roving-tabindex + arrow-key navigation across the tablist, mirroring the
  // radiogroup idiom in Toaster.tsx's ContractTypeDial: selection and focus
  // move together, wrapping at the ends.
  const focusTab = useCallback((id: TabId) => {
    const buttons = tablistRef.current?.querySelectorAll<HTMLButtonElement>('button[data-tab-id]');
    const target = buttons
      ? Array.from(buttons).find((btn) => btn.dataset.tabId === id)
      : undefined;
    target?.focus();
  }, []);

  const activateAt = useCallback(
    (index: number) => {
      const next = tabs[(index + tabs.length) % tabs.length];
      if (next) {
        setActiveTab(next.id);
        focusTab(next.id);
      }
    },
    [tabs, focusTab],
  );

  const handleTabKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      const currentIndex = Math.max(
        0,
        tabs.findIndex((t) => t.id === activeTab),
      );
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        event.preventDefault();
        activateAt(currentIndex + 1);
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        event.preventDefault();
        activateAt(currentIndex - 1);
      } else if (event.key === 'Home') {
        event.preventDefault();
        activateAt(0);
      } else if (event.key === 'End') {
        event.preventDefault();
        activateAt(tabs.length - 1);
      }
    },
    [tabs, activeTab, activateAt],
  );

  return (
    <div className="ct-app">
      {/* Header — one shell shared by both roles. The admin badge is the only
          role-conditional element here. */}
      <header className="ct-header">
        <span className="ct-header__brand">
          <strong>{PRODUCT_NAME} Review Tool</strong>
        </span>
        <span className="ct-identity">
          Signed in as <strong data-testid="user-email">{userEmail}</strong>
          {isAdmin && <span className="ct-role-badge">admin</span>}
          <button type="button" className="ct-linkbutton" onClick={signOut}>
            Sign out
          </button>
        </span>
      </header>

      {/* Tabs — an accessible tablist. For a single-tab (non-admin) user we
          drop the tab bar entirely and show the Review panel on its own. */}
      {tabs.length > 1 && (
        <div
          role="tablist"
          aria-label="Sections"
          className="ct-tabs"
          ref={tablistRef}
          onKeyDown={handleTabKeyDown}
        >
          {tabs.map((tab) => {
            const selected = tab.id === activeTab;
            return (
              <button
                key={tab.id}
                role="tab"
                type="button"
                id={`tab-${tab.id}`}
                data-tab-id={tab.id}
                className="ct-tab"
                aria-selected={selected}
                aria-controls={`panel-${tab.id}`}
                tabIndex={selected ? 0 : -1}
                onClick={() => setActiveTab(tab.id)}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
      )}

      {/* Tabpanels. CRITICAL: every panel stays MOUNTED at once; visibility is
          toggled via the `hidden` attribute so ReviewSubmission's polling and
          the admin panels' state persist across tab switches (and tests can
          find hidden testids). Admin panels are still only *rendered* for an
          admin caller (#234/#235) — a non-admin never mounts AdminUsers/
          AdminRetention at all. The server stays authoritative; each panel
          also keeps its own 403 gate as defense in depth. */}
      {tabs.length > 1 ? (
        <section
          role="tabpanel"
          id="panel-review"
          aria-labelledby="tab-review"
          className="ct-tabpanel"
          hidden={activeTab !== 'review'}
        >
          <ReviewSubmission />
        </section>
      ) : (
        <section className="ct-tabpanel">
          <ReviewSubmission />
        </section>
      )}

      {isAdmin && (
        <>
          <section
            role="tabpanel"
            id="panel-users"
            aria-labelledby="tab-users"
            className="ct-tabpanel"
            hidden={activeTab !== 'users'}
          >
            <AdminUsers />
          </section>
          <section
            role="tabpanel"
            id="panel-retention"
            aria-labelledby="tab-retention"
            className="ct-tabpanel"
            hidden={activeTab !== 'retention'}
          >
            <AdminRetention />
          </section>
        </>
      )}

      {/* Footer — version from authenticated /version endpoint (unchanged). */}
      <footer className="ct-footer">
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

// Password (Docker Compose) target: gate on PasswordLogin; once signed in, render the app
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
