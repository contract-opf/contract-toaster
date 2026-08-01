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

import { useCallback, useEffect, useState } from 'react';
import { Authenticator, useAuthenticator } from '@aws-amplify/ui-react';
import AdminUsers from './AdminUsers';
import AdminRetention from './AdminRetention';
import AdminModel from './AdminModel';
import AdminPenRules from './AdminPenRules';
import AdminPlaybooks from './AdminPlaybooks';
import ReviewSubmission from './ReviewSubmission';
import PasswordLogin, { DemoIdentity } from './PasswordLogin';
import { getToken, isPasswordMode, setDemoToken } from './auth';
import { CtAppShell, CtButton, CtChip, CtTabBar } from './ui/react';

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
// Tabbed shell. One app shell + one Review experience shared by both roles;
// the two admin tabs are appended only for an admin caller.
// ---------------------------------------------------------------------------
type TabId = 'review' | 'users' | 'retention' | 'model' | 'pen-rules' | 'playbooks';

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
  // for an admin caller. `useAdminCapability` decides the tab set and the
  // header admin badge; it never branches which panel renders or the rest
  // of the Review flow. <ReviewSubmission /> takes no admin gate of its own
  // (issue #433 removed the one bespoke admin action it used to offer) —
  // playbook administration lives in the admin tabs, and the server stays
  // authoritative for every action there.
  const tabs: TabDef[] = [
    { id: 'review', label: 'Review' },
    ...(isAdmin
      ? ([
          { id: 'users', label: 'Users & access' },
          { id: 'retention', label: 'Retention & legal hold' },
          { id: 'model', label: 'Model & API key' },
          // Playbook lifecycle (issue #434) — upload, activate, roll back,
          // rename, remove, per-version notes. Since #433 retired the
          // bundled-sample special case, this is the ONLY playbook-lifecycle
          // surface in the app.
          { id: 'playbooks', label: 'Playbooks' },
          // Pen rules & posture (issue #435). Still its own tab rather than a
          // sub-view of the Playbooks tab above — re-homing it into a
          // per-version view is a follow-up, not part of #434; see
          // AdminPenRules.tsx's docstring.
          { id: 'pen-rules', label: 'Pen rules & posture' },
        ] as TabDef[])
      : []),
  ];

  const [activeTab, setActiveTab] = useState<TabId>('review');

  // ct-tab-bar is a controlled component (docs/frontend-design-system.md
  // §9 Notes): it owns no state of its own, only the keyboard/roving-
  // tabindex behavior and the sliding indicator. Selecting a tab (click or
  // keyboard) dispatches `ct-select`, wired here through @lit/react's
  // events map (ui/react.ts) to this onSelect callback.
  const handleTabSelect = useCallback((event: CustomEvent<{ id: string }>) => {
    setActiveTab(event.detail.id as TabId);
  }, []);

  return (
    <CtAppShell brand={`${PRODUCT_NAME} Review Tool`}>
      {/* Identity cluster — the admin badge is the only role-conditional
          element here. */}
      <div slot="identity">
        Signed in as <strong data-testid="user-email">{userEmail}</strong>
        {isAdmin && <CtChip variant="info">admin</CtChip>}
        <CtButton type="button" variant="ghost" onClick={signOut}>
          Sign out
        </CtButton>
      </div>

      {/* Tabs — an accessible tablist (ct-tab-bar). For a single-tab
          (non-admin) user we drop the tab bar entirely and show the Review
          panel on its own. */}
      {tabs.length > 1 && (
        <div slot="tabs">
          <CtTabBar tabs={tabs} active={activeTab} onSelect={handleTabSelect} />
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
          <section
            role="tabpanel"
            id="panel-model"
            aria-labelledby="tab-model"
            className="ct-tabpanel"
            hidden={activeTab !== 'model'}
          >
            <AdminModel />
          </section>
          <section
            role="tabpanel"
            id="panel-playbooks"
            aria-labelledby="tab-playbooks"
            className="ct-tabpanel"
            hidden={activeTab !== 'playbooks'}
          >
            <AdminPlaybooks />
          </section>
          <section
            role="tabpanel"
            id="panel-pen-rules"
            aria-labelledby="tab-pen-rules"
            className="ct-tabpanel"
            hidden={activeTab !== 'pen-rules'}
          >
            <AdminPenRules />
          </section>
        </>
      )}

      {/* Footer — version from authenticated /version endpoint (unchanged). */}
      <footer slot="footer">
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
    </CtAppShell>
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
  // Two Authenticator props make the AWS-target sign-in screen match the
  // product's actual access model (issue #426):
  //
  //   socialProviders={['google']} — renders a "Sign In with Google" button
  //     ABOVE the standard form. It ADDS the federated path; it does not
  //     replace the username/password form. Both are supported sign-in paths.
  //   hideSignUp — removes the "Create Account" tab. This product has zero
  //     self-registration: admission is Google SSO + Cognito JIT provisioning
  //     behind an application allowlist (ARCHITECTURE.md → Authentication), so
  //     a sign-up tab is an affordance that can only ever dead-end.
  //
  // Both are verified present on the installed @aws-amplify/ui-react@6.15.4
  // (AuthenticatorProps → RouterProps/SignInBaseProps; both are destructured
  // by AuthenticatorInternal). See sso-signin-surface.test.tsx, which renders
  // the REAL Authenticator — no vi.mock — and asserts the resulting surface.
  return (
    <Authenticator hideSignUp socialProviders={['google']}>
      {() => <SsoApp />}
    </Authenticator>
  );
}
