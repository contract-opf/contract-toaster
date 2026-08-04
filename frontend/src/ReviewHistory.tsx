/**
 * ReviewHistory — "what did you toast for me, and how?" (issue #449).
 *
 * ## The problem this exists for
 *
 * `GET /api/reviews` had been implemented since #84 and had no UI at all, so
 * the app exposed **no review history anywhere**: once the Review tab moved on
 * from a finished review, the redline was unreachable and the way it was
 * produced was unrecorded. This screen is that missing surface — past reviews,
 * newest first, each carrying the provenance that makes it auditable: when it
 * ran, which playbook and version governed it, which model ran each step, the
 * instructions the chef applied, and links back to both documents.
 *
 * ## Not admin-only
 *
 * A reviewer's history is their own work. The tab mounts for every signed-in
 * user (`App.tsx`), and the screen asks for `?scope=mine`, which pins the
 * listing to the caller's own rows **even for an admin** — an admin's History
 * is their history, not a table of every user's contract activity. (The
 * server is authoritative either way: `reviews.list_reviews` decides what a
 * caller may see; the query parameter can only narrow it.)
 *
 * ## "Not recorded" is never a guess
 *
 * Per-step model ids and the playbook version only started being persisted
 * with this issue, so most existing rows carry none. Those rows render an
 * explicit **"Not recorded"** — never the model configured today. Re-labelling
 * a review that ran last week with today's model would be a fabricated audit
 * record, and the lie gets worse the moment an admin can change models (#445).
 * The backend applies the same rule (`reviews.get_review_detail`'s
 * faithful-projection convention); this screen just refuses to paper over it.
 *
 * ## A purged document is a dead end, not a dead link
 *
 * Retention deletes the S3 objects and leaves the review row's pointers in
 * place, so `has_output` / `has_input` mean "a pointer was recorded", not "the
 * file is still there". The download routes answer **410 Gone** for a purged
 * object; this screen turns that into a persistent, explicit per-row message
 * rather than handing the browser a URL that 404s.
 *
 * ## What this screen deliberately does NOT do
 *
 * **Re-run a past review.** That spends money and needs its own deliberate
 * design — same reasoning as `AdminDiagnostics.tsx`'s missing retry.
 *
 * Conventions follow the existing screens exactly (docs/frontend-design-system
 * .md §15.1): `CtToolbar` header, `CtCard` body, `CtTable`, `CtProgress` while
 * loading, an in-table `.ct-table__empty` empty state, and a local
 * `authorizedFetch`-wrapping helper. The load is an explicit `LoadState`, so a
 * failed load is TERMINAL — an error plus a working retry, never an error and
 * a spinner at once (issue #439).
 */

import { Fragment, useCallback, useEffect, useState } from 'react';
import {
  authorizedFetch,
  friendlyDownloadError,
  friendlyErrorMessage,
  readErrorDetail,
  triggerBrowserDownload,
} from './api';
// Imported, never re-declared: the epoch-string→locale formatter is the same
// one the Diagnostics table already uses (reviews rows store `created_at` as a
// string of epoch seconds, and boto3 can hand back a number). A second copy
// would drift the moment one screen learned to handle a shape the other did
// not — the same "one table, not two" rule AdminDiagnostics.tsx applies to
// `REASON_EXPLANATIONS`.
import { formatFailureTime as formatEpochSeconds } from './AdminDiagnostics';
// Same hash-shortening convention AdminPlaybooks.tsx's version trail table
// already uses (full value never dropped -- carried on the cell's `title`
// so it stays readable/copyable from the tooltip, never truncated away).
import { shortenHash } from './AdminPlaybooks';
// The outcome chip's label AND variant — imported, never re-derived. Before
// issue #470 this screen read the label off `row.decision || row.status`
// and the variant off a separate `historyStatusVariant(row.status)`: two
// independent reads of overlapping data that could (and did) disagree for
// the same outcome. See outcome.ts's module docstring for the full history.
import { describeOutcome } from './outcome';
import { CtBanner, CtButton, CtCard, CtChip, CtProgress, CtTable, CtToolbar } from './ui/react';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/reviews.py's `_review_list_item` exactly.
// If a field is not in this interface, the list route does not serve it.
// Note what is absent by design: the S3 object keys. The route projects
// availability as booleans; storage layout stays server-side.
// ---------------------------------------------------------------------------

export interface HistoryRow {
  review_id: string;
  playbook_id?: string | null;
  status: string;
  decision?: string | null;
  confidence_band?: string | null;
  /** Epoch seconds — a string as `_create_review_row` writes it, or a number. */
  created_at?: string | number | null;
  updated_at?: string | number | null;
  /** OPF 0.3 review-policy version. Null on a row that recorded none. */
  policy_version?: number | null;
  /** Posture-override version (issue #294). Null when the bundle had none. */
  posture_version?: number | null;
  /**
   * The `playbook_versions` admin-facing version (e.g. "1.0.0") that gated
   * this submission (issue #471) -- populated for the live, non-OPF
   * playbook too, unlike `policy_version`/`posture_version` above (both
   * OPF-v2-bundle-only). Null on a row with no matching `playbook_versions`
   * row (a pre-#471 row, or a demo/dev environment seeded without one).
   */
  playbook_version?: string | null;
  /** The content hash behind `playbook_version` above. Same null rule. */
  playbook_content_hash?: string | null;
  /** The model that ran the primary pass. Null = not recorded, never a guess. */
  primary_model_id?: string | null;
  /** The model that ran the critic pass. Null = not recorded, never a guess. */
  critic_model_id?: string | null;
  /** A redline pointer is recorded (NOT proof the object still exists). */
  has_output?: boolean;
  /** An input-document pointer is recorded (same caveat). */
  has_input?: boolean;
}

/** The slice of `GET /api/reviews/{id}` this screen reads on expand. */
interface ReviewGuidanceDetail {
  toaster_guidance?: string | null;
}

type LoadState<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  | { status: 'failed'; message: string };

type GuidanceState =
  | { status: 'loading' }
  | { status: 'ready'; guidance: string | null }
  | { status: 'failed'; message: string };

const NOT_RECORDED = 'Not recorded';

const PURGED_MESSAGE =
  'This document is no longer available — it was removed once its retention window passed.';

function jsonFetch(path: string, init?: RequestInit): Promise<Response> {
  return authorizedFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
}

/**
 * The governing-rules version for a row, as prose.
 *
 * Three independent version signals can govern a review: the playbook
 * itself (`playbook_version`, issue #471 — the one every review carries,
 * OPF-bound or not), the OPF 0.3 review POLICY, and the Posture override
 * (both OPF-v2-bundle-only, and absent for the live playbook). Most
 * historic rows (everything before #471) carry none of the three —
 * hence an explicit "Version not recorded" rather than an empty cell, which
 * would read as "version 0" or "no rules".
 */
export function describePlaybookVersion(row: HistoryRow): string {
  const parts: string[] = [];
  if (row.playbook_version !== null && row.playbook_version !== undefined) {
    parts.push(`v${row.playbook_version}`);
  }
  if (row.policy_version !== null && row.policy_version !== undefined) {
    parts.push(`Policy v${row.policy_version}`);
  }
  if (row.posture_version !== null && row.posture_version !== undefined) {
    parts.push(`Posture v${row.posture_version}`);
  }
  return parts.length > 0 ? parts.join(' · ') : `Version ${NOT_RECORDED.toLowerCase()}`;
}

export default function ReviewHistory(): React.ReactElement {
  const [load, setLoad] = useState<LoadState<HistoryRow[]>>({ status: 'loading' });
  // Per-row UI state, keyed by review_id. Kept out of the row objects so a
  // refresh replaces the data without discarding what the user has open.
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [guidance, setGuidance] = useState<Record<string, GuidanceState>>({});
  const [actionMessage, setActionMessage] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  const loadHistory = useCallback(async () => {
    try {
      // scope=mine: this is a personal surface, not a cross-user one.
      const response = await jsonFetch('/api/reviews?scope=mine');
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/reviews?scope=mine returned HTTP ${response.status}`,
            "We couldn't load your history. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as { reviews?: HistoryRow[] };
      // Rendered in the order the server sent them (newest first). No
      // client-side sort: `created_at` is a string epoch, and sorting it here
      // would silently disagree with the server for rows of differing width.
      setLoad({ status: 'ready', data: data.reviews ?? [] });
    } catch (err) {
      setLoad({
        status: 'failed',
        message:
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't load your history. Please try again."),
      });
    }
  }, []);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  const retry = useCallback(() => {
    setLoad({ status: 'loading' });
    void loadHistory();
  }, [loadHistory]);

  /** Fetch the review's own detail record for its instructions, once. */
  const loadGuidance = useCallback(async (reviewId: string) => {
    setGuidance((current) => ({ ...current, [reviewId]: { status: 'loading' } }));
    try {
      const response = await jsonFetch(`/api/reviews/${reviewId}`);
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/reviews/${reviewId} returned HTTP ${response.status}`,
            "We couldn't load the instructions for this review.",
          ),
        );
      }
      const detail = (await response.json()) as ReviewGuidanceDetail;
      setGuidance((current) => ({
        ...current,
        [reviewId]: { status: 'ready', guidance: detail.toaster_guidance ?? null },
      }));
    } catch (err) {
      setGuidance((current) => ({
        ...current,
        [reviewId]: {
          status: 'failed',
          message:
            err instanceof Error
              ? err.message
              : friendlyErrorMessage(err, "We couldn't load the instructions for this review."),
        },
      }));
    }
  }, []);

  const toggleGuidance = useCallback(
    (reviewId: string) => {
      setExpanded((current) => ({ ...current, [reviewId]: !current[reviewId] }));
      // Fetched once per review and then kept — reopening the expander does
      // not re-hit the API for a record that cannot change.
      //
      // A FAILED attempt is not such a record. Caching it would make one
      // transient 500 permanent: the instructions for that row would be
      // unreachable for the life of the page no matter how often the user
      // collapsed and reopened it — precisely the terminal dead end the main
      // load avoids with `retry`, and the failure mode #439 flags on sibling
      // screens. So a failure is retried on the next open (and, for a user
      // who leaves the expander open, by the button in the failed branch).
      const cached = guidance[reviewId];
      if (!cached || cached.status === 'failed') {
        void loadGuidance(reviewId);
      }
    },
    [guidance, loadGuidance],
  );

  /**
   * Mint a presigned URL for one of the review's two documents and hand it to
   * the browser. HTTP 410 is the retention case and gets its own persistent
   * per-row copy — nothing is handed to the browser, so there is no dead link.
   */
  const downloadDocument = useCallback(async (reviewId: string, kind: 'output' | 'input') => {
    setBusy((current) => ({ ...current, [reviewId]: true }));
    setActionMessage((current) => {
      const next = { ...current };
      delete next[reviewId];
      return next;
    });
    try {
      const response = await authorizedFetch(`/api/reviews/${reviewId}/${kind}`);
      if (response.status === 410) {
        setActionMessage((current) => ({ ...current, [reviewId]: PURGED_MESSAGE }));
        return;
      }
      if (!response.ok) {
        // Same failure family as ReviewSubmission.tsx's fetchOutputUrl
        // (issue #466): a 503 here carries server configuration in `detail`
        // (an unset storage env var — #465's own failure mode), never
        // something to show a reviewer, and this is a deterministic error a
        // retry cannot fix — so route through the shared friendlyDownloadError
        // rather than a raw detail or a "Please try again" that would be
        // false for exactly this shape of failure.
        const errorDetail = await readErrorDetail(response);
        throw new Error(
          friendlyDownloadError(
            errorDetail ??
              `GET /api/reviews/${reviewId}/${kind} returned HTTP ${response.status}`,
          ),
        );
      }
      const data = (await response.json()) as { url?: string };
      if (!data.url) {
        throw new Error(friendlyDownloadError(`GET /api/reviews/${reviewId}/${kind} returned no url`));
      }
      triggerBrowserDownload(data.url);
    } catch (err) {
      setActionMessage((current) => ({
        ...current,
        [reviewId]: err instanceof Error ? err.message : friendlyDownloadError(err),
      }));
    } finally {
      setBusy((current) => ({ ...current, [reviewId]: false }));
    }
  }, []);

  return (
    <section data-testid="review-history-panel" className="ct-section ct-stack">
      <CtToolbar title="History">
        <div slot="actions">
          <CtButton
            type="button"
            variant="secondary"
            size="sm"
            data-testid="review-history-refresh"
            disabled={load.status === 'loading'}
            onClick={retry}
          >
            Refresh
          </CtButton>
        </div>
      </CtToolbar>

      <CtBanner variant="muted" data-testid="review-history-scope-note">
        Your own past reviews, newest first — what was toasted, what governed it, and which model
        ran each step. Documents are removed once their retention window has passed, so an older
        review may no longer have a file to download.
      </CtBanner>

      {load.status === 'failed' && (
        <div className="ct-stack">
          <CtBanner variant="danger" data-testid="review-history-error">
            {load.message}
          </CtBanner>
          <div className="ct-actions" role="group">
            <CtButton
              type="button"
              variant="secondary"
              size="sm"
              data-testid="review-history-retry"
              onClick={retry}
            >
              Try again
            </CtButton>
          </div>
        </div>
      )}

      {load.status === 'failed' ? null : load.status === 'loading' ? (
        <CtProgress data-testid="review-history-loading" label="Loading your history…" />
      ) : (
        <CtCard data-testid="review-history-table-panel">
          <CtTable>
            <table data-testid="history-table">
              <thead>
                <tr>
                  <th>Toasted</th>
                  <th>Playbook &amp; version</th>
                  <th>Outcome</th>
                  <th>Models used</th>
                  <th>Instructions</th>
                  <th>Documents</th>
                </tr>
              </thead>
              <tbody>
                {load.data.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="ct-table__empty" data-testid="review-history-empty">
                      Nothing toasted yet. Reviews you run will appear here.
                    </td>
                  </tr>
                ) : (
                  load.data.map((row) => {
                    const isExpanded = Boolean(expanded[row.review_id]);
                    const guidanceState = guidance[row.review_id];
                    const message = actionMessage[row.review_id];
                    const isBusy = Boolean(busy[row.review_id]);
                    const outcomeChip = describeOutcome(row.status, row.decision);
                    return (
                      <Fragment key={row.review_id}>
                        <tr data-testid={`history-row-${row.review_id}`}>
                          <td
                            className="ct-table__mono"
                            data-testid={`history-toasted-${row.review_id}`}
                          >
                            {formatEpochSeconds(row.created_at)}
                          </td>
                          <td data-testid={`history-playbook-${row.review_id}`}>
                            <span className="ct-table__mono">{row.playbook_id || '—'}</span>
                            <br />
                            <small
                              className="ct-muted"
                              title={
                                row.playbook_content_hash
                                  ? `Content hash: ${row.playbook_content_hash}`
                                  : undefined
                              }
                            >
                              {describePlaybookVersion(row)}
                              {row.playbook_content_hash ? (
                                <>
                                  {' '}
                                  <span className="ct-table__mono">
                                    ({shortenHash(row.playbook_content_hash)})
                                  </span>
                                </>
                              ) : null}
                            </small>
                          </td>
                          <td data-testid={`history-outcome-${row.review_id}`}>
                            <CtChip variant={outcomeChip.variant}>{outcomeChip.label}</CtChip>
                          </td>
                          {/*
                            Per-step model provenance. A row that predates the
                            field says so explicitly — the one thing this cell
                            must never do is show today's configured model.
                          */}
                          <td data-testid={`history-models-${row.review_id}`}>
                            <small>
                              Primary:{' '}
                              <span className="ct-table__mono">
                                {row.primary_model_id || NOT_RECORDED}
                              </span>
                              <br />
                              Critic:{' '}
                              <span className="ct-table__mono">
                                {row.critic_model_id || NOT_RECORDED}
                              </span>
                            </small>
                          </td>
                          <td>
                            <CtButton
                              type="button"
                              variant="ghost"
                              size="sm"
                              data-testid={`history-guidance-toggle-${row.review_id}`}
                              onClick={() => toggleGuidance(row.review_id)}
                            >
                              {isExpanded ? 'Hide instructions' : 'Show instructions'}
                            </CtButton>
                          </td>
                          <td data-testid={`history-actions-${row.review_id}`}>
                            <div className="ct-stack">
                              {row.has_output ? (
                                <CtButton
                                  type="button"
                                  variant="secondary"
                                  size="sm"
                                  disabled={isBusy}
                                  data-testid={`history-download-output-${row.review_id}`}
                                  onClick={() => void downloadDocument(row.review_id, 'output')}
                                >
                                  Redline
                                </CtButton>
                              ) : (
                                <small className="ct-muted">No redline was produced.</small>
                              )}
                              {row.has_input ? (
                                <CtButton
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  disabled={isBusy}
                                  data-testid={`history-download-input-${row.review_id}`}
                                  onClick={() => void downloadDocument(row.review_id, 'input')}
                                >
                                  Input document
                                </CtButton>
                              ) : (
                                <small className="ct-muted">
                                  Input document {NOT_RECORDED.toLowerCase()}.
                                </small>
                              )}
                              {message && (
                                <small
                                  className="ct-muted"
                                  data-testid={`history-action-message-${row.review_id}`}
                                >
                                  {message}
                                </small>
                              )}
                            </div>
                          </td>
                        </tr>
                        {isExpanded && (
                          <tr data-testid={`history-guidance-row-${row.review_id}`}>
                            {/*
                              Guidance is free text and can be long, so it gets
                              a full-width row of its own rather than a table
                              cell that would either truncate or wreck the
                              column widths.
                            */}
                            <td colSpan={6} data-testid={`history-guidance-${row.review_id}`}>
                              {!guidanceState || guidanceState.status === 'loading' ? (
                                <CtProgress label="Loading instructions…" />
                              ) : guidanceState.status === 'failed' ? (
                                // Terminal error + a working way out, exactly
                                // as the main load does it (#439). Without the
                                // button, a user who leaves the expander open
                                // has no route back to these instructions.
                                <div className="ct-stack">
                                  <CtBanner variant="danger">{guidanceState.message}</CtBanner>
                                  <div className="ct-actions" role="group">
                                    <CtButton
                                      type="button"
                                      variant="secondary"
                                      size="sm"
                                      data-testid={`history-guidance-retry-${row.review_id}`}
                                      onClick={() => void loadGuidance(row.review_id)}
                                    >
                                      Try again
                                    </CtButton>
                                  </div>
                                </div>
                              ) : guidanceState.guidance ? (
                                <>
                                  <p style={{ margin: '0 0 0.25rem' }}>
                                    <strong>Instructions applied to this review</strong>
                                  </p>
                                  <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                                    {guidanceState.guidance}
                                  </p>
                                </>
                              ) : (
                                <small className="ct-muted">
                                  No instructions were given for this review — it ran on the
                                  playbook alone.
                                </small>
                              )}
                            </td>
                          </tr>
                        )}
                      </Fragment>
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
