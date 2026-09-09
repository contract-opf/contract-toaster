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
 * reviews, each carrying exactly ten fields: `review_id`, `created_at`,
 * `failed_at`, `failing_stage`, `reason`, `status`, — present only on a
 * leakage block (issue #616) — `leakage_category`, `leakage_rule_id`,
 * `leakage_field_name`, and — present only on a critic-pass failure (issue
 * #665) — `critic_attempts`.
 *
 * ## The detector line (issue #616)
 *
 * `reason: 'leakage_detected'` is written identically for all five of the
 * scanner's detection categories, so every leakage block rendered the same
 * cause prose and an admin could not tell a correct gate (the model really
 * did echo playbook text — fix the prompt) from an over-broad detector. The
 * three `leakage_*` fields say which detector fired, and this screen renders
 * them as a labelled operator line beneath the reader-facing cause.
 *
 * They carry no confidential text and cannot: the scanner reports "detection
 * category, and rule id — never the matched confidential text"
 * (`scripts/leakage_scan.py`), `leakage_field_name` is a model-output field
 * NAME rather than its contents, and the error object the pipeline catches
 * has nowhere to put a matched span. This is admin-only detail and stays
 * admin-only: `ReviewSubmission.tsx`'s reader-facing `leakage_detected` copy
 * is deliberately unchanged and non-technical — a reviewer submitting a
 * document is not shown detector internals.
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
 * The Review column deliberately does not LINK to the History tab (issue
 * #472 fix item 4): History is scoped `mine`, so a link to another user's
 * review would 404 for the admin viewing it here. One-click copy is the
 * fallback the ticket names instead — the id itself stays plain, selectable
 * text either way.
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
// The outcome chip's label AND variant — imported, never re-derived (issue
// #470's shared map). This screen used to render the raw `status` enum as
// the chip's own text while a local `failureStatusVariant` derived the
// color from the same field separately — one shared source now drives both.
import { describeOutcome } from './outcome';
import { explainFailure } from './ReviewSubmission';
import { CtBanner, CtButton, CtCard, CtChip, CtIconButton, CtProgress, CtTable, CtToolbar } from './ui/react';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/reviews.py's `_RECENT_FAILURE_FIELDS` exactly.
// If a field is not in this interface, the route does not serve it.
// ---------------------------------------------------------------------------

export interface RecentFailure {
  review_id: string;
  /** Epoch seconds. Written as a string by `_create_review_row`; a number if
   *  the row was stored numerically (boto3 Decimals are coerced server-side). */
  created_at?: string | number | null;
  /** Epoch seconds THE FAILURE was recorded (issue #472) — distinct from
   *  `created_at` (when the review was submitted). Written by
   *  `reviews.record_stage_failure` and the Docker Compose mock pipeline's
   *  own `_fail_review`. Null on a row that predates this field, or one
   *  whose terminal write never went through either path (e.g. QUARANTINED). */
  failed_at?: string | number | null;
  /** The pipeline stage that failed, e.g. `run_review`. Null on a row that
   *  predates the stage taxonomy. */
  failing_stage?: string | null;
  /** The issue-#442 reason TOKEN. Never a status code, endpoint, or message. */
  reason?: string | null;
  /** The terminal status the taxonomy resolved, e.g. `ERROR`. */
  status: string;
  /** Issue #616 — leakage-block diagnosis, all three present together and
   *  ONLY on a `reason: 'leakage_detected'` row. `leakage_category` is one of
   *  `scripts/leakage_scan.py`'s five `CATEGORY_*` constants;
   *  `leakage_rule_id` names the detector rule that fired;
   *  `leakage_field_name` is the model-output field it fired on — a field
   *  NAME, never its contents. No matched text ever reaches any of them. */
  leakage_category?: string | null;
  leakage_rule_id?: string | null;
  leakage_field_name?: string | null;
  /** Issue #665 — the size of the retry budget the second (critic) review
   *  pass spent, present only on a critic-pass failure row. An integer count
   *  computed by `scripts/critic_review_pass.py` itself: never prompt,
   *  document, or model-output substance.
   *
   *  It is the WHOLE budget the pass was allowed, not the attempt it stopped
   *  on, so it never reads as "failed once": the baseline is
   *  `1 + MAX_RETRIES_PER_PASS`, and the only thing that raises it is a
   *  widened retry granted after an output truncation. Above baseline
   *  therefore means the critic also ran out of output room on the way — an
   *  output-sizing lead the reason token alone does not carry.
   *
   *  `json_safe` (`backend/src/users.py`, applied by the diagnostics
   *  projection) coerces the stored Decimal to a plain `int` server-side, so
   *  this ordinarily arrives as a number; the `string` arm is the same
   *  defensive widening `created_at`/`failed_at` above carry, and
   *  `criticAttemptsDetail` normalizes either. */
  critic_attempts?: string | number | null;
}

/**
 * The operator-only detector line for a leakage-blocked row (issue #616):
 * `category · rule_id · field_name`, skipping whatever the row does not
 * carry, and `null` for every row that is not a leakage block (which is all
 * of them apart from that one reason token).
 *
 * Deliberately a join of the three ALLOWLISTED fields and nothing else — it
 * never reads, formats, or falls back to any other row attribute, so it
 * cannot become a route for document substance to reach the DOM.
 */
export function detectorDetail(failure: RecentFailure): string | null {
  const parts = [failure.leakage_category, failure.leakage_rule_id, failure.leakage_field_name]
    .map((part) => (typeof part === 'string' ? part.trim() : ''))
    .filter((part) => part.length > 0);
  return parts.length > 0 ? parts.join(' · ') : null;
}

/**
 * The operator-only attempt line for a critic-pass failure (issue #665):
 * the retry budget the second review pass spent, or `null` for every row
 * that does not carry a count.
 *
 * Same shape and same discipline as `detectorDetail` above — it reads ONE
 * allowlisted field, formats it as a number, and falls back to nothing, so
 * it cannot become a route for document substance to reach the DOM. A value
 * that is not a finite number is treated as absent rather than rendered raw.
 *
 * No singular form, deliberately. The producer reports the whole budget
 * (`attempts_allowed`), whose floor is `1 + MAX_RETRIES_PER_PASS` = 2 and
 * which only ever grows from there by a truncation grant — a count of 1 is
 * not a state the backend can write, so a `count === 1` arm here would be a
 * branch no production row takes and no test could honestly cover.
 */
export function criticAttemptsDetail(failure: RecentFailure): string | null {
  const raw = failure.critic_attempts;
  if (raw === null || raw === undefined || raw === '') {
    return null;
  }
  const count = Number(raw);
  if (!Number.isFinite(count)) {
    return null;
  }
  return `${count} attempts`;
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

export function detectConsecutiveIncidents(
  failures: RecentFailure[],
): { type: 'reason' | 'stage'; value: string; count: number } | null {
  if (failures.length < 3) return null;
  const firstReason = failures[0].reason;
  if (firstReason) {
    let count = 0;
    for (const f of failures) {
      if (f.reason === firstReason) {
        count++;
      } else {
        break;
      }
    }
    if (count >= 3) {
      return { type: 'reason', value: firstReason, count };
    }
  }
  const firstStage = failures[0].failing_stage;
  if (firstStage) {
    let count = 0;
    for (const f of failures) {
      if (f.failing_stage === firstStage) {
        count++;
      } else {
        break;
      }
    }
    if (count >= 3) {
      return { type: 'stage', value: firstStage, count };
    }
  }
  return null;
}

export default function AdminDiagnostics(): React.ReactElement | null {
  const [load, setLoad] = useState<LoadState<RecentFailure[]>>({ status: 'loading' });
  const [searchQuery, setSearchQuery] = useState('');
  // A 403 from the route is the sole signal to hide this panel — no
  // client-side "am I an admin" claim to keep in sync or spoof.
  const [isForbidden, setIsForbidden] = useState(false);
  // Issue #472 fix item 4: this screen deliberately does NOT link a review
  // id to the History tab — History is scoped `mine` (issue #443's own
  // module docstring), so an admin reading another user's failed review has
  // nowhere for that link to resolve to. One-click copy is the fallback the
  // ticket names: the id stays plain text, and this only changes what
  // pressing the button next to it does.
  const [copiedReviewId, setCopiedReviewId] = useState<string | null>(null);
  const [selectedFailure, setSelectedFailure] = useState<RecentFailure | null>(null);

  useEffect(() => {
    if (!selectedFailure) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setSelectedFailure(null);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedFailure]);

  const copyReviewId = useCallback((reviewId: string) => {
    // `navigator.clipboard` is a secure-context-only API: on a plain HTTP
    // origin that isn't localhost (the DTS Docker Compose target, reached at
    // `http://<lan-ip>:<port>`) it is `undefined`, and dereferencing
    // `.writeText` on it throws synchronously, before any promise exists.
    // Guard it so that case is a no-op rather than an uncaught throw in the
    // click handler.
    const clipboard = navigator.clipboard;
    if (!clipboard?.writeText) {
      // Clipboard unavailable (non-secure context, older browser). The id is
      // still selectable as plain text, so there is nothing further to do
      // here.
      return;
    }
    void clipboard
      .writeText(reviewId)
      .then(() => {
        setCopiedReviewId(reviewId);
        window.setTimeout(() => {
          setCopiedReviewId((current) => (current === reviewId ? null : current));
        }, 2000);
      })
      .catch(() => {
        // Clipboard permission denied. The id is still selectable as plain
        // text, so there is nothing further to do here.
      });
  }, []);

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
      <CtToolbar>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flex: 1 }}>
          <input
            type="search"
            data-testid="diagnostics-search-input"
            className="ct-input"
            style={{
              maxWidth: '320px',
              padding: '0.375rem 0.75rem',
              borderRadius: '4px',
              border: '1px solid var(--ct-border)',
            }}
            placeholder="Search by review ID, reason or stage…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
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
        view: no document text, review content, application logs, or exception output is shown
        here, and nothing on this screen re-runs a review. A “Detector” line names the safety
        check that stopped a review — the check’s own name, never anything it matched.
      </CtBanner>

      {load.status === 'ready' && (() => {
        const incident = detectConsecutiveIncidents(load.data);
        if (!incident) return null;
        return (
          <CtBanner variant="danger" data-testid="diagnostics-incident-banner">
            <div
              className="ct-row"
              style={{
                alignItems: 'center',
                justifyContent: 'space-between',
                width: '100%',
                flexWrap: 'wrap',
                gap: '0.5rem',
              }}
            >
              <span>
                <strong>Incident Alert:</strong> {incident.count} consecutive failures detected in{' '}
                {incident.type === 'reason' ? `reason "${incident.value}"` : `stage "${incident.value}"`}.
              </span>
              <CtButton
                type="button"
                variant="secondary"
                size="sm"
                data-testid="diagnostics-filter-incident-btn"
                onClick={() => setSearchQuery(incident.value)}
              >
                {`Filter to this ${incident.type}`}
              </CtButton>
            </div>
          </CtBanner>
        );
      })()}

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
          {(() => {
            const filteredFailures = load.data.filter((failure) => {
              if (!searchQuery.trim()) return true;
              const q = searchQuery.trim().toLowerCase();
              const matchId = failure.review_id.toLowerCase().includes(q);
              const matchReason = failure.reason ? failure.reason.toLowerCase().includes(q) : false;
              const matchStage = failure.failing_stage ? failure.failing_stage.toLowerCase().includes(q) : false;
              return matchId || matchReason || matchStage;
            });
            return (
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
                    ) : filteredFailures.length === 0 ? (
                      <tr>
                        <td colSpan={6} className="ct-table__empty" data-testid="admin-diagnostics-no-matches">
                          No failures match "{searchQuery}".
                        </td>
                      </tr>
                    ) : (
                      filteredFailures.map((failure) => {
                    // The SAME resolution the Review tab runs: the #442 token
                    // first, the failing stage as fallback. Never re-derived
                    // here (see this file's header).
                    const explanation = explainFailure(failure);
                    const outcomeChip = describeOutcome(failure.status);
                    // Issue #616: present only on a leakage block; null
                    // everywhere else, so no other row grows an empty label.
                    const detector = detectorDetail(failure);
                    // Issue #665: present only on a critic-pass failure;
                    // null everywhere else, same as `detector` above.
                    const criticAttempts = criticAttemptsDetail(failure);
                    return (
                      <tr
                        key={failure.review_id}
                        data-testid={`failure-row-${failure.review_id}`}
                      >
                        <td className="ct-table__mono">
                          <div className="ct-row" style={{ alignItems: 'center', gap: 'var(--ct-space-1)' }}>
                            <span data-testid={`review-id-text-${failure.review_id}`}>
                              {failure.review_id}
                            </span>{' '}
                            <CtIconButton
                              type="button"
                              label={`Copy review id ${failure.review_id}`}
                              data-testid={`copy-review-id-${failure.review_id}`}
                              onClick={() => copyReviewId(failure.review_id)}
                            >
                              {copiedReviewId === failure.review_id ? '✓' : '⧉'}
                            </CtIconButton>
                          </div>
                          <div style={{ marginTop: 'var(--ct-space-1)' }}>
                            <CtButton
                              type="button"
                              variant="ghost"
                              size="sm"
                              data-testid={`failure-details-btn-${failure.review_id}`}
                              onClick={() => setSelectedFailure(failure)}
                            >
                              Details
                            </CtButton>
                          </div>
                        </td>
                        {/* Issue #472: the real failure timestamp, when one was
                            recorded, falling back to created_at for a row
                            written before this field existed (or whose
                            terminal write never went through
                            record_stage_failure / _fail_review) rather than
                            regressing to a blank cell it didn't have before. */}
                        <td className="ct-table__mono">
                          {formatFailureTime(failure.failed_at ?? failure.created_at)}
                        </td>
                        <td>
                          <CtChip variant={outcomeChip.variant}>{outcomeChip.label}</CtChip>
                        </td>
                        <td className="ct-table__mono">{failure.failing_stage || '—'}</td>
                        <td data-testid={`failure-cause-${failure.review_id}`}>
                          {explanation
                            ? explanation.cause
                            : 'The review stopped before it could finish, and no cause was recorded.'}
                          {/* Issue #616: the leakage detector that actually
                              fired, under the reader-facing cause. Labelled
                              and set in the id/digest mono face so it reads
                              as operator diagnostic detail rather than as
                              more prose — the cause sentence above is the
                              same copy the submitter sees; this line is not.
                              Rendered only when the row carries it. */}
                          {detector !== null && (
                            <div
                              className="ct-table__mono"
                              data-testid={`failure-detector-${failure.review_id}`}
                            >
                              Detector: {detector}
                            </div>
                          )}
                          {/* Issue #665: the retry budget the critic pass
                              spent — above its baseline means it also ran
                              out of output room on the way. Same labelled,
                              mono, operator-only treatment as the detector
                              line. Rendered only when the row carries a
                              count. */}
                          {criticAttempts !== null && (
                            <div
                              className="ct-table__mono"
                              data-testid={`failure-critic-attempts-${failure.review_id}`}
                            >
                              Critic: {criticAttempts}
                            </div>
                          )}
                        </td>
                        <td data-testid={`failure-fix-${failure.review_id}`}>
                          {explanation
                            ? explanation.fix
                            : 'Ask the person who submitted it to try again, and check the model account and key under “Models” if it keeps happening.'}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </CtTable>
            );
          })()}
        </CtCard>
      )}

      {selectedFailure !== null && (
        <div
          className="ct-overlay"
          role="dialog"
          aria-modal="true"
          aria-label={`Failure details for ${selectedFailure.review_id}`}
          data-testid="admin-diagnostics-details-modal"
          onClick={(e) => {
            if (e.target === e.currentTarget) {
              setSelectedFailure(null);
            }
          }}
        >
          <div className="ct-overlay__panel">
            <CtCard>
              <CtToolbar title="Failure Details">
                <div slot="actions">
                  <CtButton
                    type="button"
                    variant="secondary"
                    size="sm"
                    data-testid="admin-diagnostics-details-close"
                    onClick={() => setSelectedFailure(null)}
                  >
                    Close
                  </CtButton>
                </div>
              </CtToolbar>
              <div className="ct-stack">
                <CtTable>
                  <table>
                    <tbody>
                      <tr>
                        <th style={{ width: '180px' }}>Review ID</th>
                        <td className="ct-table__mono">
                          <code>{selectedFailure.review_id}</code>
                        </td>
                      </tr>
                      <tr>
                        <th>Recorded failure</th>
                        <td className="ct-table__mono">
                          {formatFailureTime(selectedFailure.failed_at ?? selectedFailure.created_at)}
                        </td>
                      </tr>
                      <tr>
                        <th>Terminal outcome</th>
                        <td>
                          <CtChip variant={describeOutcome(selectedFailure.status).variant}>
                            {describeOutcome(selectedFailure.status).label}
                          </CtChip>
                        </td>
                      </tr>
                      <tr>
                        <th>Failing stage</th>
                        <td className="ct-table__mono">
                          <code>{selectedFailure.failing_stage || '—'}</code>
                        </td>
                      </tr>
                      <tr>
                        <th>Reason token</th>
                        <td className="ct-table__mono">
                          <code>{selectedFailure.reason || '—'}</code>
                        </td>
                      </tr>
                      {detectorDetail(selectedFailure) && (
                        <tr>
                          <th>Detector</th>
                          <td className="ct-table__mono">
                            {detectorDetail(selectedFailure)}
                          </td>
                        </tr>
                      )}
                      {criticAttemptsDetail(selectedFailure) && (
                        <tr>
                          <th>Critic attempts</th>
                          <td className="ct-table__mono">
                            {criticAttemptsDetail(selectedFailure)}
                          </td>
                        </tr>
                      )}
                      <tr>
                        <th>Cause</th>
                        <td>
                          {explainFailure(selectedFailure)?.cause ||
                            'The review stopped before it could finish, and no cause was recorded.'}
                        </td>
                      </tr>
                      <tr>
                        <th>Recommended fix</th>
                        <td>
                          {explainFailure(selectedFailure)?.fix ||
                            'Ask the person who submitted it to try again, and check the model account and key under “Models” if it keeps happening.'}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </CtTable>
              </div>
            </CtCard>
          </div>
        </div>
      )}
    </section>
  );
}
