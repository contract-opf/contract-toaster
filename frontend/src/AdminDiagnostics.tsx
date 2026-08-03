/**
 * AdminDiagnostics — "why did recent reviews fail?", inside the app (issue #443).
 *
 * ## The problem this exists for
 *
 * On 2026-08-01 a review failed and the UI could only offer a three-way
 * guess. The true cause — the model account was out of credits — existed
 * only in the backend container's log, reachable solely by driving the
 * deployment console past an access proxy. Every operator-fixable failure
 * class has that shape: the system knows, the operator cannot see. Issue #442
 * gave the backend a controlled `reason` token vocabulary; this screen is
 * where an admin reads it, instance-wide, without shell access.
 *
 * ## What it talks to
 *
 *   GET /api/admin/diagnostics/recent-failures?limit=N
 *
 * which returns a bounded, newest-first list of recent non-OK terminal
 * reviews, each carrying exactly five fields: `review_id`, `created_at`,
 * `failing_stage`, `reason`, `status`.
 *
 * ## What this screen is NOT, deliberately
 *
 * **It is not a log viewer.** It never streams, proxies, or renders raw
 * application logs, stack traces, or exception messages. Those carry prompt
 * substance, document text, model output, and potentially key material. The
 * whole point of the #442 token vocabulary is that it is a *controlled, safe*
 * projection of the failure; this screen renders that projection and nothing
 * else. The guarantee is enforced server-side by an explicit field allowlist
 * (`backend/src/reviews.py`'s `_RECENT_FAILURE_FIELDS`) — there is nothing
 * here to redact because nothing else ever arrives.
 *
 * It also offers **no "retry this review" action**: re-running spends money
 * and belongs in a deliberate, separately-designed flow.
 *
 * ## One token→prose table, not two
 *
 * The cause/fix copy is `ReviewSubmission.tsx`'s `REASON_EXPLANATIONS`,
 * resolved through its `explainFailure` — imported, never re-declared. Two
 * copies would drift apart the moment one surface learned a token the other
 * lacked, and the reader and the admin would be told different things about
 * the same failure.
 *
 * Admin-only. The server 403s a non-admin caller; that 403 is this
 * component's sole signal to hide itself, the same defense-in-depth posture
 * as AdminUsers/AdminRetention/AdminModel/AdminPlaybooks (App.tsx's /api/me
 * probe decides whether it mounts at all; the server stays authoritative).
 *
 * The load is an explicit `LoadState`, so a failed load is TERMINAL and
 * renders an error plus a working retry — never an error and a spinner at
 * once (issue #439).
 */

import { useCallback, useEffect, useState } from 'react';
import { authorizedFetch, friendlyErrorMessage } from './api';
import { explainFailure } from './ReviewSubmission';
import { CtBanner, CtButton, CtCard, CtChip, CtProgress, CtTable, CtToolbar } from './ui/react';
import type { CtChipVariant } from './ui/react';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/reviews.py's `_RECENT_FAILURE_FIELDS` exactly.
// If a field is not in this interface, the route does not serve it.
// ---------------------------------------------------------------------------

export interface RecentFailure {
  review_id: string;
  /** Epoch seconds. Written as a string by `_create_review_row`; a number if
   *  the row was stored numerically (boto3 Decimals are coerced server-side). */
  created_at?: string | number | null;
  /** The pipeline stage that failed, e.g. `run_review`. Null on a row that
   *  predates the stage taxonomy. */
  failing_stage?: string | null;
  /** The issue-#442 reason TOKEN. Never a status code, endpoint, or message. */
  reason?: string | null;
  /** The terminal status the taxonomy resolved, e.g. `ERROR`. */
  status: string;
}

type LoadState<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  | { status: 'failed'; message: string };

// How many rows to ask for. The backend clamps this into its own hard range
// (`reviews.RECENT_FAILURES_MAX_LIMIT`) — the value here is a display choice,
// never the bound that matters.
const REQUESTED_LIMIT = 50;

function jsonFetch(path: string, init?: RequestInit): Promise<Response> {
  return authorizedFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
}

/**
 * Render an epoch-second timestamp. Accepts the string form the reviews row
 * actually stores as well as a number, and degrades to an em dash rather than
 * rendering "Invalid Date" for a row with no usable timestamp.
 */
export function formatFailureTime(createdAt: string | number | null | undefined): string {
  if (createdAt === null || createdAt === undefined || createdAt === '') {
    return '—';
  }
  const epochSeconds = typeof createdAt === 'number' ? createdAt : Number(createdAt);
  if (!Number.isFinite(epochSeconds)) {
    return '—';
  }
  return new Date(epochSeconds * 1000).toLocaleString();
}

/**
 * Terminal status → chip variant.
 *
 * `MANUAL_REVIEW_REQUIRED` / `ERROR_MANUAL_REVIEW_REQUIRED` are the two
 * DOCUMENTED manual-review outcomes: work is queued for a person, which is a
 * warning, not a fault. Everything else that reaches this table (`ERROR`,
 * `QUARANTINED`) is a fault. An unrecognised status is treated as a fault
 * rather than quietly downgraded — this table only ever contains failures.
 */
export function failureStatusVariant(status: string): CtChipVariant {
  return status.includes('MANUAL_REVIEW_REQUIRED') ? 'warn' : 'danger';
}

export default function AdminDiagnostics(): React.ReactElement | null {
  const [load, setLoad] = useState<LoadState<RecentFailure[]>>({ status: 'loading' });
  // A 403 from the route is the sole signal to hide this panel — no
  // client-side "am I an admin" claim to keep in sync or spoof.
  const [isForbidden, setIsForbidden] = useState(false);

  const loadFailures = useCallback(async () => {
    try {
      const response = await jsonFetch(
        `/api/admin/diagnostics/recent-failures?limit=${REQUESTED_LIMIT}`,
      );
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/admin/diagnostics/recent-failures returned HTTP ${response.status}`,
            "We couldn't load recent failures. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as { failures?: RecentFailure[] };
      setLoad({ status: 'ready', data: data.failures ?? [] });
    } catch (err) {
      setLoad({
        status: 'failed',
        message:
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't load recent failures. Please try again."),
      });
    }
  }, []);

  useEffect(() => {
    void loadFailures();
  }, [loadFailures]);

  const retry = useCallback(() => {
    setLoad({ status: 'loading' });
    void loadFailures();
  }, [loadFailures]);

  if (isForbidden) {
    return null;
  }

  return (
    <section data-testid="admin-diagnostics-panel" className="ct-section ct-stack">
      <CtToolbar title="Diagnostics">
        <div slot="actions">
          <CtButton
            type="button"
            variant="secondary"
            size="sm"
            data-testid="admin-diagnostics-refresh"
            disabled={load.status === 'loading'}
            onClick={retry}
          >
            Refresh
          </CtButton>
        </div>
      </CtToolbar>

      {/* Permanent scope note. Says what this surface is and — just as
          importantly — what it is not, so nobody reads a short list as
          "nothing else ever went wrong" or comes here looking for logs. */}
      <CtBanner variant="muted" data-testid="admin-diagnostics-scope-note">
        The most recent failed reviews across this deployment, newest first, up to{' '}
        {REQUESTED_LIMIT}. Each row says what went wrong and who can fix it. This is not a log
        view: no document text, review content, or diagnostic output is shown here, and nothing
        on this screen re-runs a review.
      </CtBanner>

      {load.status === 'failed' && (
        <div className="ct-stack">
          <CtBanner variant="danger" data-testid="admin-diagnostics-error">
            {load.message}
          </CtBanner>
          <div className="ct-actions" role="group">
            <CtButton
              type="button"
              variant="secondary"
              size="sm"
              data-testid="admin-diagnostics-retry"
              onClick={retry}
            >
              Try again
            </CtButton>
          </div>
        </div>
      )}

      {load.status === 'failed' ? null : load.status === 'loading' ? (
        <CtProgress data-testid="admin-diagnostics-loading" label="Loading recent failures…" />
      ) : (
        <CtCard data-testid="admin-diagnostics-table-panel">
          <CtTable>
            <table data-testid="diagnostics-table">
              <thead>
                <tr>
                  <th>Review</th>
                  <th>Failed at</th>
                  <th>Outcome</th>
                  <th>Stage</th>
                  <th>Cause</th>
                  <th>What to do</th>
                </tr>
              </thead>
              <tbody>
                {load.data.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="ct-table__empty" data-testid="admin-diagnostics-empty">
                      No recent failures.
                    </td>
                  </tr>
                ) : (
                  load.data.map((failure) => {
                    // The SAME resolution the Review tab runs: the #442 token
                    // first, the failing stage as fallback. Never re-derived
                    // here (see this file's header).
                    const explanation = explainFailure(failure);
                    return (
                      <tr
                        key={failure.review_id}
                        data-testid={`failure-row-${failure.review_id}`}
                      >
                        <td className="ct-table__mono">{failure.review_id}</td>
                        <td className="ct-table__mono">{formatFailureTime(failure.created_at)}</td>
                        <td>
                          <CtChip variant={failureStatusVariant(failure.status)}>
                            {failure.status}
                          </CtChip>
                        </td>
                        <td className="ct-table__mono">{failure.failing_stage || '—'}</td>
                        <td data-testid={`failure-cause-${failure.review_id}`}>
                          {explanation
                            ? explanation.cause
                            : 'The review stopped before it could finish, and no cause was recorded.'}
                        </td>
                        <td data-testid={`failure-fix-${failure.review_id}`}>
                          {explanation
                            ? explanation.fix
                            : 'Ask the person who submitted it to try again, and check the model account and key under “Model & API key” if it keeps happening.'}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </CtTable>
        </CtCard>
      )}
    </section>
  );
}
