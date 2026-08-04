/**
 * AdminUsers — allowlist UI, lifecycle actions, sync visibility (issue #92).
 *
 * Admin-only screen (RUNBOOK.md refers to this as "Admin UI -> Users"):
 *   - Lists every `users` row (GET /api/users): identity, status
 *     (active/suspended/deprovisioned), admin flag, last_auth_at, and
 *     whether the row was JIT-created (issue #33's canonical admission
 *     path — see ARCHITECTURE.md -> Authentication). "Identity" is
 *     email-or-username, because the two auth targets name people
 *     differently and neither field is universal (issue #441).
 *   - Suspend / deprovision / reactivate actions (PATCH /api/users/{sub}),
 *     and toggling the admin flag.
 *   - A read-only sync-status panel (GET /api/users/sync-status): last
 *     run, users deprovisioned on that run, and whether the run failed
 *     closed (directory unavailable -> no changes made).
 *   - A read-only summary of the break-glass procedure. This UI
 *     deliberately does NOT expose a break-glass action — break-glass
 *     "stays IAM-side per #53" (issue #92); the full procedure lives in
 *     RUNBOOK.md -> "Break-glass: restoring admin access".
 *
 * This screen itself is gated server-side: every request 403s for a
 * non-admin caller (backend/src/users.py). The component treats that 403
 * as the sole signal to hide itself — there is no separate client-side
 * "am I an admin" claim to keep in sync or that could be spoofed.
 *
 * Every mutation here is misuse-adjacent (it changes who can access a
 * legal-document tool), so no optimistic UI: the table only reflects a
 * change after the PATCH response confirms it, and any error is shown
 * inline rather than silently retried.
 *
 * Both reads are modelled as an explicit `LoadState` (see below) rather than
 * a `data | null` sentinel, so a failed load is a TERMINAL state that renders
 * an error plus a working retry — never an error and a spinner at the same
 * time (issue #439).
 */

import { useCallback, useEffect, useState } from 'react';
import { authorizedFetch, friendlyErrorMessage, readErrorDetail } from './api';
import { CtBanner, CtButton, CtCard, CtChip, CtField, CtProgress, CtTable, CtToolbar } from './ui/react';
import type { CtChipVariant } from './ui/react';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/users.py's users row and sync_status shapes.
// ---------------------------------------------------------------------------

export type UserStatus = 'active' | 'suspended' | 'deprovisioned';

/**
 * A `users` row. BOTH identity fields are optional, because the two auth
 * targets legitimately identify people differently (issue #441):
 *
 *   - SSO rows (JIT-created by the pre-token Lambda, #33) carry `email`.
 *   - Password rows (`backend/src/demo_auth.py`) carry `username` and have no
 *     `email` key at all.
 *
 * `email` used to be declared required, which made every password-mode row
 * render a blank identity cell above its Suspend/Deprovision buttons. Neither
 * field is guaranteed, so nothing here may be read without a fallback — see
 * `userIdentity`.
 */
export interface UserRow {
  cognito_sub: string;
  email?: string;
  username?: string;
  status: UserStatus;
  is_admin: boolean;
  /**
   * `null` for a user who has never signed in — `backend/src/demo_auth.py`
   * writes `last_auth_at: None` when it seeds the demo rows and when an admin
   * adds a user, so the wire value really is JSON null (issue #452). This was
   * declared plain `number`, which was a type lie about the response; the
   * runtime was already correct, since `formatTimestamp` renders 'never' for
   * null. Do not "simplify" that guard away on the strength of the type.
   */
  last_auth_at: number | null;
  created_at: number;
  admission?: string; // "jit" for pre-token-Lambda-created rows (#33)
  /**
   * True for a username/password row whose CURRENT password still verifies
   * against the shipped seed default (admin/admin, user/user) — issue #469.
   * Absent (falsy) for an SSO row, which never carries it at all
   * (backend/src/users.py::public_user_view only synthesizes it for
   * `user_type == "password"` rows).
   */
  default_credentials_warning?: boolean;
}

export interface SyncStatus {
  sync_type: string;
  last_run_at: number | null;
  last_run_outcome: 'ok' | 'directory_unavailable' | null;
  users_deprovisioned_count: number;
  next_run_at: number | null;
}

/** The subset of `GET /api/admin/auth-mode`'s response this screen reads
 * (issue #474) — mirrors `backend/src/demo_auth.py::get_auth_mode_settings`.
 * The toggle UI itself is #246; this screen only reads the stored mode to
 * decide what to show, never writes it. */
type AuthMode = 'sso' | 'password' | 'both';

/** A user-creation type this screen can add (mirrors `demo_auth.USER_TYPE_*`). */
type AddUserType = 'sso' | 'password';

/**
 * An explicit three-state load, replacing the `T | null` sentinel this screen
 * used to key its loading branch off (issue #439).
 *
 * The shipped bug: a failed load set an error string and left the data at
 * `null`, so "has an error" and "is still loading" were both true at once and
 * the screen rendered a danger banner AND a permanent "Loading users…". This
 * shape makes that state unrepresentable — the failure message lives INSIDE
 * the failed state, so there is nowhere for an error to sit while the status
 * is still `loading`.
 *
 * `failed` is deliberately distinct from `ready` with empty data: an empty
 * workspace and a failed load are different claims, and collapsing them would
 * make a 500 render as "No users yet."
 */
type LoadState<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  | { status: 'failed'; message: string };

function jsonFetch(path: string, init?: RequestInit): Promise<Response> {
  return authorizedFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
}

function formatTimestamp(epochSeconds: number | null): string {
  if (epochSeconds === null || epochSeconds === undefined) {
    return 'never';
  }
  return new Date(epochSeconds * 1000).toLocaleString();
}

/**
 * The single identity string for a row — never empty (issue #441).
 *
 * Prefers `email` (SSO), falls back to `username` (password mode), and last
 * resorts to `cognito_sub`, which is the primary key and therefore always
 * present. A blank cell is not an acceptable outcome here: every row carries
 * Suspend / Deprovision / Revoke admin, so an unidentifiable row is an
 * irreversible access decision taken against an unknown person. Showing the
 * subject is ugly; showing nothing is dangerous.
 *
 * Whitespace-only values count as absent, so a row with `email: ""` degrades
 * to its username rather than rendering the empty string it literally holds.
 * The `Admin` column is deliberately NOT part of this: `admin`/`reviewer` is a
 * role, identical across every admin, and it only looks like a name by
 * coincidence.
 */
export function userIdentity(user: UserRow): string {
  const candidates = [user.email, user.username, user.cognito_sub];
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim() !== '') {
      return candidate;
    }
  }
  return '—';
}

// Status → chip variant: active reads as healthy, suspended as a warning,
// deprovisioned as a terminal/danger state. Exhaustive over UserStatus.
function statusChipVariant(status: UserStatus): CtChipVariant {
  switch (status) {
    case 'active':
      return 'ok';
    case 'suspended':
      return 'warn';
    case 'deprovisioned':
      return 'danger';
  }
}

// Mirrors backend/src/users.py::update_user's 409 detail copy exactly
// (issue #473) — the frontend guard below is cosmetic (disables the button
// before the request is even sent), the server-side check is the real
// gate, and the two should read as the same rule, not two different ones.
const LAST_ADMIN_TITLE = 'This is the only admin account — add another admin first.';

// Mirrors backend/src/demo_auth.py::remove_user's 409 detail verbatim
// (issue #474) — that guard is UNCONDITIONAL (not tied to the active-admin
// count the way update_user's is), so the frontend disables Remove on the
// caller's own row regardless of how many other admins exist.
const REMOVE_SELF_TITLE = 'An admin cannot remove their own user row. Ask another admin.';

// Hover copy distinguishing hard Remove from Deprovision (issue #474 —
// "hard Remove distinct from Deprovision, with confirm copy explaining the
// difference"). Deprovision is reversible (Reactivate) and keeps the row
// and its audit history; Remove deletes the row outright.
const REMOVE_HELP_TITLE =
  'Removing permanently deletes this user and their row. Use Deprovision instead to ' +
  'suspend access while keeping their history and the ability to reactivate them later.';

// The ct-button confirm-step label (docs/frontend-design-system.md §14) IS
// the button's own text while armed, so this doubles as the "confirm copy
// explaining the difference" itself — REMOVE_HELP_TITLE above is the fuller
// hover explanation, this is what the button says out loud.
const REMOVE_CONFIRM_TEXT = 'Click again to permanently delete (not reversible — use Deprovision to keep history)';

/** Characters used by the "Generate" password button (issue #474). Excludes
 * visually-ambiguous characters (0/O, 1/l/I) — this is typed/read by a human
 * once, not machine-parsed. Client-side only: nothing here is persisted or
 * sent anywhere until the admin submits the Add-user form. */
const GENERATED_PASSWORD_ALPHABET =
  'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!@#$%^&*-_';

function generatePassword(length = 16): string {
  const values = new Uint32Array(length);
  crypto.getRandomValues(values);
  return Array.from(values, (n) => GENERATED_PASSWORD_ALPHABET[n % GENERATED_PASSWORD_ALPHABET.length]).join(
    '',
  );
}

export default function AdminUsers(): React.ReactElement | null {
  const [usersLoad, setUsersLoad] = useState<LoadState<UserRow[]>>({ status: 'loading' });
  const [syncLoad, setSyncLoad] = useState<LoadState<SyncStatus>>({ status: 'loading' });
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingSub, setPendingSub] = useState<string | null>(null);
  // The caller's own cognito_sub (GET /api/me, issue #473), used only to
  // recognize "this row is me" for self-aware confirm copy. Stays `null` if
  // the probe fails or hasn't resolved yet — that degrades to "self unknown"
  // rather than breaking anything, because the actual gate below is the
  // active-admin COUNT (computed from the users list itself), which needs no
  // self-identification at all: an admin can only ever be the sole caller
  // able to hit this API when they are also the sole active admin.
  const [mySub, setMySub] = useState<string | null>(null);
  // Non-admins get HTTP 403 from every /api/users* call (server-enforced —
  // src/users.py). We use that response to hide the panel entirely rather
  // than trusting any client-side claim of admin status.
  const [isForbidden, setIsForbidden] = useState(false);

  // The stored auth-mode setting (GET /api/admin/auth-mode, issue #474) —
  // read-only here; the toggle UI itself is #246. `null` covers both "still
  // loading" and "the probe failed", and deliberately degrades the SAME way
  // in both cases: the Workspace-sync card stays visible (it is informational,
  // never a privilege decision, so failing open here just means an admin on
  // an unprobeable deployment sees one extra card rather than a broken
  // screen) and Add-user offers BOTH user types rather than guessing wrong
  // and hiding a legitimate one.
  const [authMode, setAuthMode] = useState<AuthMode | null>(null);

  const [addUserOpen, setAddUserOpen] = useState(false);
  const [addUserType, setAddUserType] = useState<AddUserType>('sso');
  const [addEmail, setAddEmail] = useState('');
  const [addUsername, setAddUsername] = useState('');
  const [addPassword, setAddPassword] = useState('');
  const [addIsAdmin, setAddIsAdmin] = useState(false);
  const [addSubmitting, setAddSubmitting] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  // Set once, right after a successful add — the one place a plaintext
  // password the admin typed or generated is shown, since the server never
  // returns it (backend/src/users.py's public_user_view strips
  // password_hash and the create response never carries the plaintext
  // either). Cleared the moment the form is reopened or closed.
  const [addResult, setAddResult] = useState<{ identity: string; password?: string } | null>(
    null,
  );

  const loadUsers = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/users');
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/users returned HTTP ${response.status}`,
            "We couldn't load the users list. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as { users: UserRow[] };
      setUsersLoad({ status: 'ready', data: data.users });
    } catch (err) {
      // Terminal, and it carries its own message — the loader branch below
      // cannot render alongside it (issue #439).
      setUsersLoad({
        status: 'failed',
        message:
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't load the users list. Please try again."),
      });
    }
  }, []);

  const loadSyncStatus = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/users/sync-status');
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/users/sync-status returned HTTP ${response.status}`,
            "We couldn't load the sync status. Please try again.",
          ),
        );
      }
      setSyncLoad({ status: 'ready', data: (await response.json()) as SyncStatus });
    } catch (err) {
      setSyncLoad({
        status: 'failed',
        message:
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't load the sync status. Please try again."),
      });
    }
  }, []);

  const loadMe = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/me');
      if (!response.ok) {
        return; // Self-recognition is a UX nicety; the server stays authoritative.
      }
      const data = (await response.json()) as { cognito_sub?: string };
      if (typeof data.cognito_sub === 'string' && data.cognito_sub !== '') {
        setMySub(data.cognito_sub);
      }
    } catch {
      // Leaves mySub null — every row just renders as "not (recognizably) me".
    }
  }, []);

  const loadAuthMode = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/admin/auth-mode');
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        return; // Degrades to "unknown" (see authMode's docstring above).
      }
      const data = (await response.json()) as { auth_mode?: string };
      if (data.auth_mode === 'sso' || data.auth_mode === 'password' || data.auth_mode === 'both') {
        setAuthMode(data.auth_mode);
      }
    } catch {
      // Leaves authMode null — same "unknown, degrade safely" contract as loadMe.
    }
  }, []);

  useEffect(() => {
    void loadUsers();
    void loadSyncStatus();
    void loadMe();
    void loadAuthMode();
  }, [loadUsers, loadSyncStatus, loadMe, loadAuthMode]);

  // Which user type(s) Add-user should offer for the current auth mode
  // (issue #474 — "mode-appropriate"): password-only deployments only make
  // sense to add password users to, sso-only deployments only sso, `both`
  // (or an unprobeable mode — see authMode's docstring) offers a choice.
  const availableAddUserTypes: AddUserType[] =
    authMode === 'password' ? ['password'] : authMode === 'sso' ? ['sso'] : ['sso', 'password'];
  const activeAddUserType: AddUserType =
    availableAddUserTypes.length === 1 ? availableAddUserTypes[0] : addUserType;
  // Workspace/SSO sync is meaningless on a password-only deployment (issue
  // #474) — it will read "never" forever there. `authMode === null` (still
  // loading, or the probe failed) fails OPEN and keeps the card, since it is
  // purely informational and an extra card is a far smaller cost than
  // wrongly hiding sync visibility on an sso/both deployment.
  const showSyncCard = authMode !== 'password';

  // Retry handlers (issue #439). Each one re-runs its own load IN PLACE —
  // no page reload, which would destroy the in-memory session token
  // (auth.ts) and sign the operator out, i.e. the only "try again" the old
  // copy left available. The status is flipped back to `loading` here rather
  // than at the top of the load function itself, so that the post-PATCH
  // re-fetch in applyUpdate keeps the current table on screen instead of
  // blanking it to a spinner on every mutation.
  const retryLoadUsers = useCallback(() => {
    setUsersLoad({ status: 'loading' });
    void loadUsers();
  }, [loadUsers]);

  const retryLoadSyncStatus = useCallback(() => {
    setSyncLoad({ status: 'loading' });
    void loadSyncStatus();
  }, [loadSyncStatus]);

  const applyUpdate = useCallback(
    async (sub: string, updates: Partial<Pick<UserRow, 'status' | 'is_admin'>>) => {
      setActionError(null);
      setPendingSub(sub);
      try {
        const response = await jsonFetch(`/api/users/${encodeURIComponent(sub)}`, {
          method: 'PATCH',
          body: JSON.stringify(updates),
        });
        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `PATCH /api/users/${sub} returned HTTP ${response.status}`,
                "We couldn't update that user. Please try again.",
              ),
          );
        }
        // Reflect the change only after the server confirms it — no
        // optimistic UI for an access-control mutation.
        await loadUsers();
      } catch (err) {
        setActionError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't update that user. Please try again."),
        );
      } finally {
        setPendingSub(null);
      }
    },
    [loadUsers],
  );

  // Hard remove (DELETE /api/users/{sub}, issue #474) — distinct from
  // applyUpdate's PATCH-based lifecycle actions: this deletes the row
  // outright rather than changing its status, and (like applyUpdate) is
  // never optimistic — the table only drops the row once loadUsers()
  // re-fetches and confirms it is actually gone.
  const applyRemove = useCallback(
    async (sub: string) => {
      setActionError(null);
      setPendingSub(sub);
      try {
        const response = await jsonFetch(`/api/users/${encodeURIComponent(sub)}`, {
          method: 'DELETE',
        });
        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `DELETE /api/users/${sub} returned HTTP ${response.status}`,
                "We couldn't remove that user. Please try again.",
              ),
          );
        }
        await loadUsers();
      } catch (err) {
        setActionError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't remove that user. Please try again."),
        );
      } finally {
        setPendingSub(null);
      }
    },
    [loadUsers],
  );

  const resetAddUserForm = useCallback(() => {
    setAddEmail('');
    setAddUsername('');
    setAddPassword('');
    setAddIsAdmin(false);
    setAddError(null);
  }, []);

  // POST /api/users (issue #474) — add either an SSO user (by email; they
  // are admitted the next time they sign in, same as a JIT row, just
  // pre-created instead of sync-created) or a password user (username +
  // an admin-typed or generated initial password). The server never
  // returns the password (backend/src/users.py::public_user_view strips
  // password_hash); this is why `addResult` below is populated from the
  // value THIS form held locally, shown exactly once, and never re-derived
  // from a server response.
  const submitAddUser = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setAddError(null);
      setAddResult(null);

      const grantAdmin = addIsAdmin;
      let body: Record<string, unknown>;
      let identity: string;
      let passwordUsed: string | undefined;

      if (activeAddUserType === 'sso') {
        const email = addEmail.trim();
        if (email === '') {
          setAddError('Enter an email address.');
          return;
        }
        body = { user_type: 'sso', email, is_admin: grantAdmin };
        identity = email;
      } else {
        const username = addUsername.trim();
        const password = addPassword;
        if (username === '') {
          setAddError('Enter a username.');
          return;
        }
        if (password === '') {
          setAddError('Enter an initial password, or click Generate.');
          return;
        }
        body = { user_type: 'password', username, password, is_admin: grantAdmin };
        identity = username;
        passwordUsed = password;
      }

      setAddSubmitting(true);
      try {
        const response = await jsonFetch('/api/users', {
          method: 'POST',
          body: JSON.stringify(body),
        });
        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `POST /api/users returned HTTP ${response.status}`,
                "We couldn't add that user. Please try again.",
              ),
          );
        }
        // Confirmed by the server before the table reflects it — no
        // optimistic UI, same posture as every other mutation on this screen.
        await loadUsers();
        resetAddUserForm();
        setAddResult({ identity, password: passwordUsed });
      } catch (err) {
        setAddError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't add that user. Please try again."),
        );
      } finally {
        setAddSubmitting(false);
      }
    },
    [activeAddUserType, addEmail, addIsAdmin, addPassword, addUsername, loadUsers, resetAddUserForm],
  );

  if (isForbidden) {
    return null;
  }

  return (
    <section data-testid="admin-users-panel" className="ct-section ct-stack">
      <CtToolbar title="Users">
        <div slot="actions">
          <CtButton
            type="button"
            variant="primary"
            data-testid="admin-users-add-toggle"
            onClick={() => {
              setAddResult(null);
              setAddError(null);
              setAddUserOpen((open) => !open);
            }}
          >
            Add user
          </CtButton>
        </div>
      </CtToolbar>

      {addUserOpen && (
        <CtCard data-testid="admin-users-add-panel">
          <form className="ct-stack" noValidate onSubmit={submitAddUser}>
            <CtToolbar title="Add user" />

            {addError && (
              <CtBanner variant="danger" data-testid="admin-users-add-error">
                {addError}
              </CtBanner>
            )}
            {addResult && (
              <CtBanner variant="ok" data-testid="admin-users-add-success">
                {addResult.password === undefined ? (
                  <p>
                    Added <strong>{addResult.identity}</strong>. They can sign in with single
                    sign-on next time they authenticate.
                  </p>
                ) : (
                  <div>
                    <p>
                      Added <strong>{addResult.identity}</strong>. This password is shown once —
                      it is never stored or returned by the server again, so share it now or
                      it's gone:
                    </p>
                    <p className="ct-mono" data-testid="admin-users-add-password-once">
                      {addResult.password}
                    </p>
                  </div>
                )}
              </CtBanner>
            )}

            {availableAddUserTypes.length > 1 && (
              <CtField label="User type" hint="Which kind of account this is.">
                <select
                  data-testid="admin-users-add-type"
                  value={addUserType}
                  onChange={(e) => setAddUserType(e.target.value as AddUserType)}
                >
                  <option value="sso">Single sign-on (by email)</option>
                  <option value="password">Username and password</option>
                </select>
              </CtField>
            )}

            {activeAddUserType === 'sso' ? (
              <CtField
                label="Email"
                hint="They are admitted the next time they sign in with single sign-on — same as a group-sync row, just created ahead of time."
              >
                <input
                  data-testid="admin-users-add-email"
                  type="email"
                  autoComplete="off"
                  value={addEmail}
                  onChange={(e) => setAddEmail(e.target.value)}
                />
              </CtField>
            ) : (
              <>
                <CtField label="Username">
                  <input
                    data-testid="admin-users-add-username"
                    type="text"
                    autoComplete="off"
                    spellCheck={false}
                    value={addUsername}
                    onChange={(e) => setAddUsername(e.target.value)}
                  />
                </CtField>
                <CtField
                  label="Initial password"
                  hint="Type one, or click Generate. The server never returns it after creation — this screen shows it exactly once."
                >
                  <input
                    data-testid="admin-users-add-password"
                    type="text"
                    autoComplete="off"
                    spellCheck={false}
                    className="ct-mono"
                    value={addPassword}
                    onChange={(e) => setAddPassword(e.target.value)}
                  />
                </CtField>
                <div className="ct-actions">
                  <CtButton
                    type="button"
                    variant="secondary"
                    size="sm"
                    data-testid="admin-users-add-generate"
                    onClick={() => setAddPassword(generatePassword())}
                  >
                    Generate
                  </CtButton>
                </div>
              </>
            )}

            <label className="ct-row" data-testid="admin-users-add-is-admin-row">
              <input
                type="checkbox"
                data-testid="admin-users-add-is-admin"
                checked={addIsAdmin}
                onChange={(e) => setAddIsAdmin(e.target.checked)}
              />
              Admin
            </label>

            <div className="ct-actions">
              <CtButton
                type="submit"
                variant="primary"
                data-testid="admin-users-add-submit"
                disabled={addSubmitting}
                loading={addSubmitting}
              >
                {addSubmitting ? 'Adding…' : 'Add user'}
              </CtButton>
            </div>
          </form>
        </CtCard>
      )}

      {/* A failed load is terminal: the banner carries the message and a
          working retry, and the loader below is unreachable while it shows. */}
      {usersLoad.status === 'failed' && (
        <div className="ct-stack">
          <CtBanner variant="danger" data-testid="admin-users-error">
            {usersLoad.message}
          </CtBanner>
          <div className="ct-actions" role="group">
            <CtButton
              type="button"
              variant="secondary"
              size="sm"
              data-testid="admin-users-retry"
              onClick={retryLoadUsers}
            >
              Try again
            </CtButton>
          </div>
        </div>
      )}

      {/* Sync-job visibility panel — hidden entirely on a password-mode
          deployment (issue #474): Workspace sync is the Google-Workspace/
          Cognito concept and reads "never" forever there, which trains an
          admin to ignore the page's most prominent card. See showSyncCard's
          definition above for the fail-open default while the mode is
          still unknown. */}
      {showSyncCard && (
      <CtBanner variant="muted" data-testid="sync-status-panel">
        <strong>Workspace sync status</strong>
        {syncLoad.status === 'ready' ? (
          <ul>
            <li data-testid="sync-last-run">Last run: {formatTimestamp(syncLoad.data.last_run_at)}</li>
            <li data-testid="sync-outcome">
              Outcome:{' '}
              {syncLoad.data.last_run_outcome === 'directory_unavailable' ? (
                <CtChip variant="danger">directory unavailable — fail-closed, no changes made</CtChip>
              ) : syncLoad.data.last_run_outcome ? (
                <CtChip variant="ok">{syncLoad.data.last_run_outcome}</CtChip>
              ) : (
                'not yet run'
              )}
            </li>
            <li data-testid="sync-deprovisioned-count">
              Users deprovisioned on last run: {syncLoad.data.users_deprovisioned_count}
            </li>
          </ul>
        ) : syncLoad.status === 'failed' ? (
          <div className="ct-stack">
            <CtBanner variant="danger" data-testid="sync-status-error">
              {syncLoad.message}
            </CtBanner>
            <div className="ct-actions" role="group">
              <CtButton
                type="button"
                variant="secondary"
                size="sm"
                data-testid="sync-status-retry"
                onClick={retryLoadSyncStatus}
              >
                Try again
              </CtButton>
            </div>
          </div>
        ) : (
          <p data-testid="sync-status-loading">Loading sync status…</p>
        )}
      </CtBanner>
      )}

      {/* Break-glass procedure — surfaced read-only, no action button here.
          Break-glass stays IAM-side per #53; see RUNBOOK.md for the procedure. */}
      <CtBanner variant="muted">
        {/* data-testid stays on <details> (not the CtBanner wrapper): the
            read-only break-glass guarantee is asserted by
            tests/test_admin_users_ui_92.py check E, which isolates the block
            via /<details[^>]*break-glass.*?<\/details>/ to prove no request
            call lives inside it. Hoisting the id off <details> blinds it. */}
        <details data-testid="break-glass-note">
          <summary>Break-glass admin recovery (read-only)</summary>
          <p>
            If the last admin is locked out, recovery does not go through this screen. A
            dedicated, normally-unused break-glass IAM role (SSO + MFA, CloudTrail-logged)
            can restore admin access directly. Every use is recorded in the audit log with
            <code> reason=emergency-override</code>. See RUNBOOK.md → &quot;Break-glass:
            restoring admin access&quot; for the procedure. This UI cannot invoke break-glass.
          </p>
        </details>
      </CtBanner>

      {actionError && (
        <CtBanner variant="danger" data-testid="admin-users-action-error">
          {actionError}
        </CtBanner>
      )}

      {usersLoad.status === 'failed' ? null : usersLoad.status === 'loading' ? (
        <CtProgress data-testid="admin-users-loading" label="Loading users…" />
      ) : (
        <CtCard data-testid="admin-users-table-panel">
          <CtTable>
            <table data-testid="users-table">
              <thead>
                <tr>
                  {/* Not "Email": a password-mode deployment has none, and a
                      header that names a field half the rows cannot have is
                      how the blank column went unnoticed (#441). */}
                  <th data-testid="users-identity-header">Email / username</th>
                  <th>Status</th>
                  <th>Admin</th>
                  <th>Admission</th>
                  <th>Last sign-in</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {usersLoad.data.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="ct-table__empty" data-testid="admin-users-empty">
                      No users yet.
                    </td>
                  </tr>
                ) : (
                  // The last-active-admin guard (issue #473) needs the CURRENT
                  // active-admin count, computed fresh from the just-loaded
                  // rows on every render — never cached, so it stays correct
                  // immediately after a PATCH re-fetch (applyUpdate → loadUsers).
                  (() => {
                    const activeAdminCount = usersLoad.data.filter(
                      (row) => row.is_admin && row.status === 'active',
                    ).length;
                    return usersLoad.data.map((u) => {
                      const isSelf = mySub !== null && u.cognito_sub === mySub;
                      // Mirrors backend/src/users.py::update_user's
                      // `target_is_active_admin` / `would_strip_admin_access`
                      // exactly: true only for the one row whose loss of admin
                      // access the server would refuse to allow.
                      const isLastActiveAdmin =
                        u.is_admin && u.status === 'active' && activeAdminCount === 1;
                      return (
                        <tr key={u.cognito_sub} data-testid={`user-row-${u.cognito_sub}`}>
                          <td data-testid={`user-identity-${u.cognito_sub}`}>
                            {userIdentity(u)}
                            {u.default_credentials_warning && (
                              <CtChip
                                variant="warn"
                                data-testid={`user-default-credentials-${u.cognito_sub}`}
                                title="Still uses the shipped default password."
                              >
                                default password
                              </CtChip>
                            )}
                          </td>
                          <td data-testid={`user-status-${u.cognito_sub}`}>
                            <CtChip variant={statusChipVariant(u.status)}>{u.status}</CtChip>
                          </td>
                          <td>{u.is_admin ? 'admin' : 'reviewer'}</td>
                          <td>
                            {u.admission === 'jit'
                              ? 'JIT (group sign-in)'
                              : u.admission === 'admin_added'
                                ? 'Added by admin'
                                : (u.admission ?? '—')}
                          </td>
                          <td>{formatTimestamp(u.last_auth_at)}</td>
                          <td>
                            <div className="ct-actions" role="group">
                              {/* State-appropriate actions only (#473): a row
                                  that is already suspended has no business
                                  offering Suspend again. */}
                              {u.status !== 'suspended' && (
                                <CtButton
                                  type="button"
                                  variant="secondary"
                                  size="sm"
                                  disabled={pendingSub === u.cognito_sub || isLastActiveAdmin}
                                  title={isLastActiveAdmin ? LAST_ADMIN_TITLE : undefined}
                                  onClick={() => void applyUpdate(u.cognito_sub, { status: 'suspended' })}
                                >
                                  Suspend
                                </CtButton>
                              )}
                              <CtButton
                                type="button"
                                variant="danger"
                                size="sm"
                                confirm={
                                  isSelf
                                    ? 'Click again to remove your own admin access'
                                    : 'Click again to deprovision'
                                }
                                disabled={
                                  pendingSub === u.cognito_sub ||
                                  u.status === 'deprovisioned' ||
                                  isLastActiveAdmin
                                }
                                title={isLastActiveAdmin ? LAST_ADMIN_TITLE : undefined}
                                onClick={() => void applyUpdate(u.cognito_sub, { status: 'deprovisioned' })}
                              >
                                Deprovision
                              </CtButton>
                              {/* Reactivate is a no-op on an already-active row
                                  (#473) — not merely disabled, but not offered. */}
                              {u.status !== 'active' && (
                                <CtButton
                                  type="button"
                                  variant="secondary"
                                  size="sm"
                                  disabled={pendingSub === u.cognito_sub}
                                  onClick={() => void applyUpdate(u.cognito_sub, { status: 'active' })}
                                >
                                  Reactivate
                                </CtButton>
                              )}
                              <CtButton
                                type="button"
                                variant="secondary"
                                size="sm"
                                confirm={
                                  u.is_admin
                                    ? isSelf
                                      ? 'Click again to remove your own admin access'
                                      : 'Click again to revoke admin'
                                    : undefined
                                }
                                disabled={
                                  pendingSub === u.cognito_sub || (u.is_admin && isLastActiveAdmin)
                                }
                                title={u.is_admin && isLastActiveAdmin ? LAST_ADMIN_TITLE : undefined}
                                onClick={() => void applyUpdate(u.cognito_sub, { is_admin: !u.is_admin })}
                              >
                                {u.is_admin ? 'Revoke admin' : 'Grant admin'}
                              </CtButton>
                              {/* Hard delete — distinct from Deprovision (issue #474):
                                  this removes the row outright rather than changing its
                                  lifecycle status, and has no Reactivate path back.
                                  Mirrors backend/src/demo_auth.py::remove_user's
                                  UNCONDITIONAL self-removal guard (stricter than the
                                  last-active-admin guard above, which only blocks
                                  self-targeting once no other active admin exists). */}
                              <CtButton
                                type="button"
                                variant="danger"
                                size="sm"
                                confirm={REMOVE_CONFIRM_TEXT}
                                data-testid={`user-remove-${u.cognito_sub}`}
                                disabled={pendingSub === u.cognito_sub || isSelf}
                                title={isSelf ? REMOVE_SELF_TITLE : REMOVE_HELP_TITLE}
                                onClick={() => void applyRemove(u.cognito_sub)}
                              >
                                Remove
                              </CtButton>
                            </div>
                          </td>
                        </tr>
                      );
                    });
                  })()
                )}
              </tbody>
            </table>
          </CtTable>
        </CtCard>
      )}
    </section>
  );
}
