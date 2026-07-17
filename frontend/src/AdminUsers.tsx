/**
 * AdminUsers — allowlist UI, lifecycle actions, sync visibility (issue #92).
 *
 * Admin-only screen (RUNBOOK.md refers to this as "Admin UI -> Users"):
 *   - Lists every `users` row (GET /api/users): email, status
 *     (active/suspended/deprovisioned), admin flag, last_auth_at, and
 *     whether the row was JIT-created (issue #33's canonical admission
 *     path — see ARCHITECTURE.md -> Authentication).
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
 */

import { useCallback, useEffect, useState } from 'react';
import { authorizedFetch, friendlyErrorMessage, readErrorDetail } from './api';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/users.py's users row and sync_status shapes.
// ---------------------------------------------------------------------------

export type UserStatus = 'active' | 'suspended' | 'deprovisioned';

export interface UserRow {
  cognito_sub: string;
  email: string;
  status: UserStatus;
  is_admin: boolean;
  last_auth_at: number;
  created_at: number;
  admission?: string; // "jit" for pre-token-Lambda-created rows (#33)
}

export interface SyncStatus {
  sync_type: string;
  last_run_at: number | null;
  last_run_outcome: 'ok' | 'directory_unavailable' | null;
  users_deprovisioned_count: number;
  next_run_at: number | null;
}

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

// Status → chip variant: active reads as healthy, suspended as a warning,
// deprovisioned as a terminal/danger state. Exhaustive over UserStatus.
function statusChipClass(status: UserStatus): string {
  switch (status) {
    case 'active':
      return 'ct-chip ct-chip--ok';
    case 'suspended':
      return 'ct-chip ct-chip--warn';
    case 'deprovisioned':
      return 'ct-chip ct-chip--danger';
  }
}

export default function AdminUsers(): React.ReactElement | null {
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [syncStatus, setSyncStatus] = useState<SyncStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingSub, setPendingSub] = useState<string | null>(null);
  // Non-admins get HTTP 403 from every /api/users* call (server-enforced —
  // src/users.py). We use that response to hide the panel entirely rather
  // than trusting any client-side claim of admin status.
  const [isForbidden, setIsForbidden] = useState(false);

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
      setUsers(data.users);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't load the users list. Please try again."),
      );
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
      setSyncStatus((await response.json()) as SyncStatus);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't load the sync status. Please try again."),
      );
    }
  }, []);

  useEffect(() => {
    void loadUsers();
    void loadSyncStatus();
  }, [loadUsers, loadSyncStatus]);

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

  if (isForbidden) {
    return null;
  }

  return (
    <section data-testid="admin-users-panel" className="ct-section ct-stack">
      <h2 className="ct-section-title">Users</h2>

      {error && (
        <p data-testid="admin-users-error" role="alert" className="ct-error">
          {error}
        </p>
      )}

      {/* Sync-job visibility panel */}
      <div data-testid="sync-status-panel" className="ct-note">
        <strong>Workspace sync status</strong>
        {syncStatus ? (
          <ul>
            <li data-testid="sync-last-run">Last run: {formatTimestamp(syncStatus.last_run_at)}</li>
            <li data-testid="sync-outcome">
              Outcome:{' '}
              {syncStatus.last_run_outcome === 'directory_unavailable' ? (
                <span className="ct-chip ct-chip--danger">
                  directory unavailable — fail-closed, no changes made
                </span>
              ) : syncStatus.last_run_outcome ? (
                <span className="ct-chip ct-chip--ok">{syncStatus.last_run_outcome}</span>
              ) : (
                'not yet run'
              )}
            </li>
            <li data-testid="sync-deprovisioned-count">
              Users deprovisioned on last run: {syncStatus.users_deprovisioned_count}
            </li>
          </ul>
        ) : (
          <p data-testid="sync-status-loading">Loading sync status…</p>
        )}
      </div>

      {/* Break-glass procedure — surfaced read-only, no action button here.
          Break-glass stays IAM-side per #53; see RUNBOOK.md for the procedure. */}
      <details data-testid="break-glass-note" className="ct-note">
        <summary>Break-glass admin recovery (read-only)</summary>
        <p>
          If the last admin is locked out, recovery does not go through this screen. A
          dedicated, normally-unused break-glass IAM role (SSO + MFA, CloudTrail-logged)
          can restore admin access directly. Every use is recorded in the audit log with
          <code> reason=emergency-override</code>. See RUNBOOK.md → &quot;Break-glass:
          restoring admin access&quot; for the procedure. This UI cannot invoke break-glass.
        </p>
      </details>

      {actionError && (
        <p data-testid="admin-users-action-error" role="alert" className="ct-error">
          {actionError}
        </p>
      )}

      {users === null ? (
        <p data-testid="admin-users-loading">Loading users…</p>
      ) : (
        <div className="ct-table-scroll">
          <table data-testid="users-table">
            <thead>
              <tr>
                <th>Email</th>
                <th>Status</th>
                <th>Admin</th>
                <th>Admission</th>
                <th>Last sign-in</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.cognito_sub} data-testid={`user-row-${u.cognito_sub}`}>
                  <td>{u.email}</td>
                  <td data-testid={`user-status-${u.cognito_sub}`}>
                    <span className={statusChipClass(u.status)}>{u.status}</span>
                  </td>
                  <td>{u.is_admin ? 'admin' : 'reviewer'}</td>
                  <td>{u.admission === 'jit' ? 'JIT (group sign-in)' : (u.admission ?? '—')}</td>
                  <td>{formatTimestamp(u.last_auth_at)}</td>
                  <td>
                    <div className="ct-actions" role="group">
                      <button
                        className="ct-icon-button"
                        disabled={pendingSub === u.cognito_sub || u.status === 'suspended'}
                        onClick={() => void applyUpdate(u.cognito_sub, { status: 'suspended' })}
                      >
                        Suspend
                      </button>
                      <button
                        className="ct-icon-button"
                        disabled={pendingSub === u.cognito_sub || u.status === 'deprovisioned'}
                        onClick={() => void applyUpdate(u.cognito_sub, { status: 'deprovisioned' })}
                      >
                        Deprovision
                      </button>
                      <button
                        className="ct-icon-button"
                        disabled={pendingSub === u.cognito_sub || u.status === 'active'}
                        onClick={() => void applyUpdate(u.cognito_sub, { status: 'active' })}
                      >
                        Reactivate
                      </button>
                      <button
                        className="ct-icon-button"
                        disabled={pendingSub === u.cognito_sub}
                        onClick={() => void applyUpdate(u.cognito_sub, { is_admin: !u.is_admin })}
                      >
                        {u.is_admin ? 'Revoke admin' : 'Grant admin'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
