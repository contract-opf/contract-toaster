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
 * ACCEPT/REQUEST_CHANGE framing: ACCEPT reads "no requested changes
 * identified by tool" — never "no action needed" or "approved"
 * (ARCHITECTURE.md § Wrong-format rejection UX) — see ReviewSubmission.
 *
 * Issue #492 (owner direction): attorney/legal review is a policy the
 * deploying organization owns entirely outside this product — the result
 * panel no longer asserts or nags about it. See ReviewSubmission.tsx's own
 * module docstring for what replaced the removed disclaimer, and
 * docs/threat-model.md for where that framing still lives outside the SPA
 * (the generated `.docx` itself).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Authenticator, useAuthenticator } from '@aws-amplify/ui-react';
import AdminUsers from './AdminUsers';
import AdminRetention from './AdminRetention';
import AdminModel from './AdminModel';
import AdminInstructions from './AdminInstructions';
import AdminPlaybooks from './AdminPlaybooks';
import AdminDiagnostics from './AdminDiagnostics';
import ReviewSubmission from './ReviewSubmission';
import ReviewHistory from './ReviewHistory';
import PasswordLogin, { DemoIdentity } from './PasswordLogin';
import ChangePassword from './ChangePassword';
import { isPasswordMode } from './auth';
import { authorizedFetch, onSessionExpired } from './api';
import { ErrorBoundary } from './ErrorBoundary';
import { CtAppShell, CtBanner, CtButton, CtChip, CtTabBar } from './ui/react';

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
type TabId =
  | 'review'
  // Issue #449 — past redlines and how they were produced. Deliberately NOT
  // in the admin block below: a reviewer's history is their own work, and the
  // screen asks the API for `?scope=mine` so an admin's History is theirs too.
  | 'history'
  | 'users'
  | 'retention'
  | 'model'
  | 'instructions'
  | 'playbooks'
  | 'diagnostics';

interface TabDef {
  id: TabId;
  label: string;
}

// ---------------------------------------------------------------------------
// Hash-based tab routing (issue #489, item 1).
//
// Before this, `activeTab` was plain in-memory state: no tab was linkable,
// browser back/forward did nothing, and every reload landed on Review. No
// router dependency is warranted at eight tabs — this is a bidirectional
// mapping between a `TabId` and a `location.hash` fragment, plus the
// `hashchange` listener wired into `AppContent` below.
//
// Primary tabs (every signed-in user's) route as `#/<id>` (`#/review`,
// `#/history`); the six admin-only tabs route as `#/admin/<id>`
// (`#/admin/playbooks`, …) so the URL itself carries the same two-tier shape
// issue #477 gave the tab bar. PRIMARY_TAB_IDS/ADMIN_TAB_IDS are the sole
// authority on which prefix a given id gets — kept as `Set`s (not re-derived
// from `primaryTabs`/`adminTabs` below, which depend on `isAdmin` and would
// make the admin-tab set empty for a non-admin caller, exactly the caller
// `tabFromHash` most needs to recognise so it can refuse the hash).
const PRIMARY_TAB_IDS = new Set<TabId>(['review', 'history']);
const ADMIN_TAB_IDS = new Set<TabId>([
  'users',
  'retention',
  'model',
  'instructions',
  'playbooks',
  'diagnostics',
]);

function hashForTab(id: TabId): string {
  return ADMIN_TAB_IDS.has(id) ? `#/admin/${id}` : `#/${id}`;
}

/**
 * The inverse of `hashForTab`. Returns `null` for anything that isn't a
 * recognised tab id in the shape this app produces — an empty hash (first
 * visit, no tab hash written yet), a stale/foreign hash, or a hand-typed
 * `#/admin/nonsense`. `null` always means "fall back to Review" to the one
 * caller (below); it does NOT by itself mean "unauthorized" — a non-admin
 * caller deep-linking to a real admin tab id gets a non-null result here and
 * is caught instead by the admin-capability effect in `AppContent`, which is
 * the only place that knows the caller's resolved role.
 */
function tabFromHash(hash: string): TabId | null {
  const path = hash.replace(/^#\/?/, '');
  if (!path) return null;
  const adminMatch = /^admin\/(.+)$/.exec(path);
  if (adminMatch) {
    const candidate = adminMatch[1] as TabId;
    return ADMIN_TAB_IDS.has(candidate) ? candidate : null;
  }
  return PRIMARY_TAB_IDS.has(path as TabId) ? (path as TabId) : null;
}

/** The tab to render on first mount: whatever `location.hash` already names
 *  (a fresh page load with a hash from a previous visit, a pasted deep
 *  link, or — since `AppContent` can remount without a navigation, e.g. the
 *  #487 expired-session/relogin cycle — a hash this same tab already wrote),
 *  or Review when there is none/it's unrecognised. `TabId`, not just the
 *  admin subset, so an admin-tab hash survives long enough for the
 *  admin-capability effect below to either confirm it or reject it — it must
 *  never resolve straight to 'review' on the strength of a probe that has
 *  not even started. */
function resolveInitialTab(): TabId {
  if (typeof window === 'undefined') return 'review';
  return tabFromHash(window.location.hash) ?? 'review';
}

function useAdminCapability(): AdminCapability {
  const [capability, setCapability] = useState<AdminCapability>('loading');

  useEffect(() => {
    let cancelled = false;

    async function probeCapability(): Promise<void> {
      try {
        // authorizedFetch (api.ts) adds the Authorization header for the
        // sso/Cognito path and sends `credentials: 'same-origin'` so the
        // password-mode session cookie (issue #468) rides along — neither
        // this probe nor the version fetch below needs to know which mode
        // it's running under.
        const response = await authorizedFetch('/api/me');

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

// ---------------------------------------------------------------------------
// Default-credentials warning (issue #469). Password-mode only (an SSO row
// has no password to warn about) — a second, independent GET /api/me probe
// rather than folding into useAdminCapability above, so a password rotation
// can re-probe on its own (`refreshKey`) without also re-running the admin
// gate. Fails closed to "no warning shown": this banner is advisory copy,
// not a security boundary — the server enforces everything else (throttle,
// change-password verification) independently of whether this renders.
// ---------------------------------------------------------------------------
function useDefaultCredentialsWarning(refreshKey: number): boolean {
  const [warning, setWarning] = useState(false);

  useEffect(() => {
    if (!isPasswordMode()) {
      return undefined;
    }
    let cancelled = false;

    async function probeWarning(): Promise<void> {
      try {
        const response = await authorizedFetch('/api/me');
        if (!response.ok) {
          throw new Error(`/api/me returned HTTP ${response.status}`);
        }
        const data = (await response.json()) as { default_credentials_warning?: boolean };
        if (!cancelled) {
          setWarning(Boolean(data.default_credentials_warning));
        }
      } catch {
        if (!cancelled) {
          setWarning(false);
        }
      }
    }

    void probeWarning();
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  return warning;
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
  // Bumped after a successful password change (issue #469) so the warning
  // banner below clears immediately instead of waiting for a reload.
  const [credentialsRefreshKey, setCredentialsRefreshKey] = useState(0);
  const defaultCredentialsWarning = useDefaultCredentialsWarning(credentialsRefreshKey);

  // Fetch version from the authenticated /version endpoint via authorizedFetch
  // (sso: Amplify session Bearer token, unchanged; password mode: the
  // httpOnly session cookie, issue #468 — see useAdminCapability above).
  // /health is public/liveness-only; /version requires authentication.
  useEffect(() => {
    let cancelled = false;

    async function fetchVersion(): Promise<void> {
      try {
        const response = await authorizedFetch('/version');

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

  // Contract-type catalog sync (issue #464). ReviewSubmission's dial and
  // AdminPlaybooks' table each fetch GET /api/playbooks independently and
  // keep their own copy; nothing signalled the dial to refetch after an
  // admin mutation (rename/remove/activate/rollback), so a renamed playbook
  // kept its old label — or, after removing the last one, stayed selectable
  // — in the Review tab until a full reload. `catalogVersion` is a plain
  // refresh signal (not the catalog data itself, per this issue's own
  // "not prescriptive" note — a full state lift touches both components'
  // props contracts for no behavioral gain over this): AdminPlaybooks calls
  // `bumpCatalogVersion` after every mutation that can change what
  // GET /api/playbooks returns, and ReviewSubmission's catalog-fetch effect
  // depends on it, so both panels stay mounted (per the tabpanel comment
  // below) and the dial re-fetches the instant the admin table does.
  const [catalogVersion, setCatalogVersion] = useState(0);
  const bumpCatalogVersion = useCallback(() => {
    setCatalogVersion((version) => version + 1);
  }, []);

  // Tab set (issue #477): split into two independent tablists instead of one
  // flat row of up to eight peers. Review and History are every signed-in
  // user's tabs; the six admin-only panels render as their OWN labeled
  // "Admin" tablist beneath it, allowed to wrap on its own without reading
  // as an accident (see the DECISION comment on issue #477 for why this beat
  // a horizontally-scrolling strip). `useAdminCapability` decides whether
  // the admin group exists at all and the header admin badge; it never
  // branches which panel renders or the rest of the Review flow.
  // <ReviewSubmission /> takes no admin gate of its own (issue #433 removed
  // the one bespoke admin action it used to offer) — playbook administration
  // lives in the admin tabs, and the server stays authoritative for every
  // action there.
  const primaryTabs: TabDef[] = [
    { id: 'review', label: 'Review' },
    // History (issue #449) — every signed-in user, not gated on isAdmin.
    { id: 'history', label: 'History' },
  ];

  // Empty (not just hidden) for a non-admin caller — the admin group must
  // not render at all, matching how every admin panel already 403-hides
  // itself (issue #477 DECISION comment). Kept as the pre-#477
  // `...(isAdmin ? ([...] as TabDef[]) : [])` shape (rather than a plain
  // ternary) — tests/test_review_history_449.py's
  // test_history_tab_is_not_admin_gated source-scrapes App.tsx for exactly
  // this pattern to confirm 'history' never lands inside it.
  const adminTabs: TabDef[] = [
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
          // Playbook instructions (issue #484, epic #481). Replaces the old
          // "Pen rules & posture" tab: a live, per-playbook plain-English
          // standing-instructions box, versioned and compare-and-set saved.
          // "Playbook instructions" reads better as a tab label than
          // "Standing instructions" (the page heading inside says that
          // instead) — see AdminInstructions.tsx's docstring.
          { id: 'instructions', label: 'Playbook instructions' },
          // Diagnostics (issue #443) — why recent reviews failed, read from
          // the #442 reason vocabulary. Last in the admin set on purpose: it
          // is where you go when something is wrong, not part of the
          // routine configuration flow above.
          { id: 'diagnostics', label: 'Diagnostics' },
        ] as TabDef[])
      : []),
  ];

  // Issue #487/#489: which tab you were on has to survive both an expired
  // session AND a browser reload.
  //
  // #487 alone used to solve this with a module-level `lastActiveTab`
  // variable, deliberately in-memory and reset on reload ("a reload IS the
  // user asking for a fresh start"). #489 overturns that last part: a reload
  // must land back on the same tab, a pasted deep link must open directly to
  // it, and back/forward must walk tab history — none of which an in-memory
  // variable can do. `location.hash` replaces it and, as a side effect,
  // subsumes the #487 case for free: a 401 drops `identity` and unmounts this
  // component (taking any in-memory state with it), but never touches
  // `location.hash`, so `resolveInitialTab` reads the very same hash back on
  // re-login without this component needing to remember anything itself.
  const [activeTab, setActiveTab] = useState<TabId>(resolveInitialTab);

  // Reflect `activeTab` into `location.hash` on every change, INCLUDING the
  // very first render — a fresh visit with no hash yet must still leave one
  // behind (`#/review`) so the very next reload has something to restore.
  // Guarded on the hash already matching so a `hashchange`-driven update
  // (the effect below) doesn't bounce straight back into a second history
  // entry for the tab it just navigated to.
  //
  // The very first reflect is special-cased: when the URL arrived with NO
  // hash at all, writing one via `location.hash = next` PUSHES a history
  // entry (browser back would then return to the same page with the hash
  // stripped, taking two presses to actually leave). `history.replaceState`
  // leaves the same hash behind without stacking that spurious entry. Once
  // a hash exists, every subsequent reflect is a genuine tab change and
  // should keep pushing so back/forward can walk tab history.
  const didInitialReflect = useRef(false);
  useEffect(() => {
    const next = hashForTab(activeTab);
    if (window.location.hash !== next) {
      if (!didInitialReflect.current && !window.location.hash) {
        history.replaceState(null, '', next);
      } else {
        window.location.hash = next;
      }
    }
    didInitialReflect.current = true;
  }, [activeTab]);

  // Back/forward (and any other out-of-band hash edit) walks tab history.
  // Mount-only listener; `tabFromHash` alone decides the fallback here since
  // this effect has no opinion on admin authorization — that is the
  // capability-gate effect below's job, and it runs on `activeTab` too.
  useEffect(() => {
    function onHashChange(): void {
      setActiveTab(tabFromHash(window.location.hash) ?? 'review');
    }
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  // Admin gate on the RESOLVED tab (issue #489's "unauthorized hashes fall
  // back to review"): a non-admin caller who deep-links to `#/admin/users`
  // must land on Review, exactly like every admin panel's own 403-hide-itself
  // posture. Deliberately keyed on `adminCapability === 'non-admin'` and NOT
  // `!== 'admin'` — the probe starts 'loading' on every mount, and an actual
  // admin's own deep link into an admin tab must survive that window rather
  // than being bounced to Review before the probe even answers.
  useEffect(() => {
    if (adminCapability !== 'non-admin') return;
    if (ADMIN_TAB_IDS.has(activeTab)) {
      // REPLACE the rejected admin hash, don't push a new entry on top of
      // it. The reflect effect above only pushes when the hash doesn't
      // already match `activeTab` — pre-correcting the URL here first means
      // that by the time it runs, `location.hash` already reads `#/review`,
      // so it sees nothing to do. Without this, Back would return to the
      // very admin hash that was just rejected, re-triggering this same
      // gate and pushing `#/review` again: an escape-proof loop.
      history.replaceState(null, '', hashForTab('review'));
      setActiveTab('review');
    }
  }, [adminCapability, activeTab]);

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
        {/* Password changes only make sense for the username/password
            (Docker Compose) target — an SSO row's password lives with
            Google, not here (issue #469). */}
        {isPasswordMode() && (
          <ChangePassword
            onChanged={() => setCredentialsRefreshKey((key) => key + 1)}
          />
        )}
        <CtButton type="button" variant="ghost" onClick={signOut}>
          Sign out
        </CtButton>
      </div>

      {/* Account notices live in their OWN full-width row, never inside the
          identity cluster above. A block-level banner rendered as a flex item
          between "Sign out" and the page edge is both visually wrong and
          structurally destructive: the identity track is sized to its
          content, so a full-width alert in it starves the brand nameplate
          until it stacks one word per line. See ct-app-shell.css's `notice`
          area. Nothing renders (and the row collapses) when there is no
          warning to show. */}
      {defaultCredentialsWarning && (
        <div slot="notice">
          <CtBanner variant="warn" data-testid="default-credentials-warning">
            This account still uses the shipped default password — change it now.
          </CtBanner>
        </div>
      )}

      {/* Tabs — two independent accessible tablists (issue #477), not one
          flat row. Every signed-in user has at least Review + History
          (issue #449); the primary tab bar used to be dropped for the
          single-tab case, which no longer exists. The Admin group only
          renders for an admin caller — it must never appear empty or
          disabled for a reviewer. `active` is one id shared by both
          instances: keyboard Home/End/arrow cycling stays PER GROUP
          (ct-tab-bar.ts's roving tabindex is per-instance), and the native
          Tab key moves between the two `<ct-tab-bar>` elements exactly as
          it would between any two sibling widgets. */}
      <div slot="tabs">
        <CtTabBar tabs={primaryTabs} active={activeTab} onSelect={handleTabSelect} />
        {isAdmin && (
          <div className="ct-tab-group">
            <span className="ct-tab-group__label" aria-hidden="true">
              Admin
            </span>
            <CtTabBar tabs={adminTabs} active={activeTab} onSelect={handleTabSelect} label="Admin" />
          </div>
        )}
      </div>

      {/* Tabpanels. CRITICAL: every panel stays MOUNTED at once; visibility is
          toggled via the `hidden` attribute so ReviewSubmission's polling and
          the admin panels' state persist across tab switches (and tests can
          find hidden testids). Admin panels are still only *rendered* for an
          admin caller (#234/#235) — a non-admin never mounts AdminUsers/
          AdminRetention at all. The server stays authoritative; each panel
          also keeps its own 403 gate as defense in depth. */}
      <section
        role="tabpanel"
        id="panel-review"
        aria-labelledby="tab-review"
        className="ct-tabpanel"
        hidden={activeTab !== 'review'}
      >
        <ErrorBoundary name="review"><ReviewSubmission catalogVersion={catalogVersion} /></ErrorBoundary>
      </section>

      {/* History — mounted for every signed-in user, not inside the isAdmin
          block below. Always mounted like every other panel, so the list it
          has loaded survives a tab switch. */}
      <section
        role="tabpanel"
        id="panel-history"
        aria-labelledby="tab-history"
        className="ct-tabpanel"
        hidden={activeTab !== 'history'}
      >
        <ErrorBoundary name="history"><ReviewHistory /></ErrorBoundary>
      </section>

      {isAdmin && (
        <>
          <section
            role="tabpanel"
            id="panel-users"
            aria-labelledby="tab-users"
            className="ct-tabpanel"
            hidden={activeTab !== 'users'}
          >
            <ErrorBoundary name="users"><AdminUsers /></ErrorBoundary>
          </section>
          <section
            role="tabpanel"
            id="panel-retention"
            aria-labelledby="tab-retention"
            className="ct-tabpanel"
            hidden={activeTab !== 'retention'}
          >
            <ErrorBoundary name="retention"><AdminRetention /></ErrorBoundary>
          </section>
          <section
            role="tabpanel"
            id="panel-model"
            aria-labelledby="tab-model"
            className="ct-tabpanel"
            hidden={activeTab !== 'model'}
          >
            <ErrorBoundary name="model"><AdminModel /></ErrorBoundary>
          </section>
          <section
            role="tabpanel"
            id="panel-playbooks"
            aria-labelledby="tab-playbooks"
            className="ct-tabpanel"
            hidden={activeTab !== 'playbooks'}
          >
            <ErrorBoundary name="playbooks"><AdminPlaybooks onCatalogChange={bumpCatalogVersion} /></ErrorBoundary>
          </section>
          <section
            role="tabpanel"
            id="panel-instructions"
            aria-labelledby="tab-instructions"
            className="ct-tabpanel"
            hidden={activeTab !== 'instructions'}
          >
            <ErrorBoundary name="instructions"><AdminInstructions /></ErrorBoundary>
          </section>
          <section
            role="tabpanel"
            id="panel-diagnostics"
            aria-labelledby="tab-diagnostics"
            className="ct-tabpanel"
            hidden={activeTab !== 'diagnostics'}
          >
            <ErrorBoundary name="diagnostics"><AdminDiagnostics /></ErrorBoundary>
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
// with the demo identity. Sign-out (issue #468) POSTs /api/auth/logout so the
// server clears the httpOnly session cookie; `identity` is only dropped once
// that response confirms the cookie is gone (AC #2 — the SPA must never
// render itself as signed out while the session cookie is still valid, since
// the restore-on-mount probe below would just sign it straight back in on
// the next reload). A failed logout (network error or non-2xx) leaves
// `identity` untouched and surfaces a sign-out-failed banner instead.
function PasswordApp(): React.ReactElement {
  const [identity, setIdentity] = useState<DemoIdentity | null>(null);
  const [signOutError, setSignOutError] = useState<string | null>(null);
  // Issue #487. Before this, an expired session showed up as every panel
  // independently failing with its own "We couldn't load…" copy, and none of
  // them said the one true, actionable thing: you are signed out. `api.ts`
  // notices the 401 once, centrally; this drops `identity` so the login gate
  // renders, with copy that says what happened.
  const [sessionExpired, setSessionExpired] = useState(false);

  useEffect(
    () =>
      onSessionExpired(() => {
        setSessionExpired(true);
        setIdentity(null);
      }),
    [],
  );
  // Issue #468's whole point: a page reload must NOT force a re-login when
  // the httpOnly session cookie is still valid. `identity` is in-memory
  // React state, so it is always null on the very first render after a
  // reload regardless of the cookie — this probe is what restores it. GET
  // /api/me is already the authenticated capability route every caller
  // hits post-login (useAdminCapability above); reusing it here (rather
  // than adding a bespoke "am I signed in" route) means restoring a
  // session and confirming one both go through the exact same
  // server-authoritative check. `restoring` gates rendering PasswordLogin
  // so a valid session never flashes the login form first.
  const [restoring, setRestoring] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function restoreSession(): Promise<void> {
      try {
        const response = await authorizedFetch('/api/me');
        if (!response.ok) {
          throw new Error(`GET /api/me returned HTTP ${response.status}`);
        }
        const data = (await response.json()) as { username?: string | null; is_admin: boolean };
        if (!cancelled && data.username) {
          setIdentity({ username: data.username, isAdmin: Boolean(data.is_admin) });
        }
      } catch {
        // No valid session (never logged in, or the cookie is missing/
        // expired/cleared) — fall through to the login gate. The server
        // stays the sole authority here; this catch only decides whether
        // the SPA *attempts* to skip the login form.
      } finally {
        if (!cancelled) {
          setRestoring(false);
        }
      }
    }

    void restoreSession();
    return () => {
      cancelled = true;
    };
  }, []);

  if (restoring) {
    return (
      <main className="ct-login-shell" data-testid="password-session-restoring">
        <div className="ct-login-brand">{PRODUCT_NAME}</div>
      </main>
    );
  }

  if (!identity) {
    return (
      <>
        {sessionExpired && (
          <CtBanner variant="warn" data-testid="session-expired">
            Your session expired — sign in to continue.
          </CtBanner>
        )}
        <PasswordLogin
          onAuthenticated={(next) => {
            setSessionExpired(false);
            setIdentity(next);
          }}
        />
      </>
    );
  }

  function handleSignOut(): void {
    setSignOutError(null);
    void (async () => {
      try {
        const response = await authorizedFetch('/api/auth/logout', { method: 'POST' });
        if (!response.ok) {
          throw new Error(`POST /api/auth/logout returned HTTP ${response.status}`);
        }
        setIdentity(null);
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error(err);
        // The cookie may still be valid server-side — do NOT drop `identity`
        // here, or the restore-on-mount probe would just sign the session
        // back in on the next reload while the SPA shows a signed-out UI.
        setSignOutError('Sign out failed. Your session is still active — please try again.');
      }
    })();
  }

  return (
    <>
      {signOutError && (
        <CtBanner variant="danger" data-testid="sign-out-error">
          {signOutError}
        </CtBanner>
      )}
      <AppContent userEmail={identity.username} signOut={handleSignOut} />
    </>
  );
}

export default function App(): React.ReactElement {
  // The last-resort boundary (issue #487). Per-panel boundaries are the
  // primary defence -- they keep a broken screen from taking the other seven
  // with it -- and this only catches a throw OUTSIDE any panel: the header,
  // the tab bar, the auth shell. Without it those still white-screen, which
  // is the failure mode being fixed, just from a rarer origin.
  return <ErrorBoundary name="app">{renderAuthenticatedApp()}</ErrorBoundary>;
}

function renderAuthenticatedApp(): React.ReactElement {
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
