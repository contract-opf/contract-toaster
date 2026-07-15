/**
 * ReviewSubmission — minimal upload/poll/download UI (issue #186).
 *
 * #186 ("No user-facing review flow exists") mounted the already-tested
 * #84 handlers onto `src.main.app` (`POST /api/reviews`, `GET
 * /api/reviews/{review_id}`, `GET /api/reviews/{review_id}/output`) but,
 * per that ticket's Dependencies note, also owns "the minimal
 * upload/poll/download UI" -- this component is that UI:
 *
 *   1. Upload: a multipart POST /api/reviews with the chosen .docx file.
 *      A 202 response carries `{review_id, resumed}`.
 *   2. Poll: GET /api/reviews/{review_id} every few seconds while `status`
 *      is a non-terminal pipeline status (`PENDING` / `RUNNING` --
 *      src/reviews.py's `REVIEW_STATUSES_NON_TERMINAL`); stop once it
 *      reaches a terminal status.
 *   3. Download: once the polled detail reports `has_output`, fetch a
 *      short-lived presigned URL via GET /api/reviews/{review_id}/output
 *      and hand it to the browser.
 *
 * CONTRACT-TYPE SELECTOR (issue #272): on mount, GET /api/playbooks
 * fetches the catalog of registered playbook ids (`{playbook_id,
 * display_name, status}` — backend/src/review_routes.py's `get_playbooks`).
 * The selector renders entirely from that response — no playbook id or
 * display name is ever hardcoded here. The chosen `playbook_id` is
 * appended to the upload FormData; the type submitted for the in-flight
 * review is also shown in the status/result view. Choosing a
 * "coming_soon" type and submitting anyway reaches the backend's existing
 * "no active playbook" 503, which renders through the same submitError
 * path as any other submission failure (no special-cased copy, no
 * crash).
 *
 * ATTORNEY-APPROVAL WATERMARK (ARCHITECTURE.md -> "Every output and UI
 * state is watermarked..."): the terminal-status panel below always
 * carries "tool recommendation only — attorney approval required", and an
 * ACCEPT decision reads "no requested changes identified by tool" (never
 * "approved" / "no action needed") -- this is the "future output states"
 * requirement flagged in App.tsx's module docstring, now that this is the
 * component adding output states.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { authorizedFetch, friendlyErrorMessage, readErrorDetail, triggerBrowserDownload } from './api';
import {
  ContractTypeDial,
  ProgressToaster,
  SoberToaster,
  ToastUpToaster,
  ToasterStyles,
} from './toaster/Toaster';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/review_routes.py + backend/src/reviews.py's
// get_review_detail shape (only the fields this minimal UI renders).
// ---------------------------------------------------------------------------

interface SubmitResponse {
  review_id: string;
  resumed: boolean;
}

// Critic-delta shape — mirrors the fields backend/src/reviews.py's
// get_review_detail surfaces from the adversarial critic pass (see
// scripts/leakage_scan.py's `_scan_critic_delta_fields`, the authoritative
// enumeration of these field names). Only the fields this pre-download
// indicator renders are typed here.
interface ContestedReplacement {
  section?: string | null;
  critic_objection?: string | null;
  critic_suggested_replacement?: string | null;
}

interface CriticDelta {
  contested_replacements?: ContestedReplacement[] | null;
  added_issues?: unknown[] | null;
}

interface ReviewDetail {
  review_id: string;
  status: string;
  decision: string | null;
  message: string | null;
  has_output: boolean;
  // Trust-calibration signals the attorney must see BEFORE downloading
  // (docs/output-contract.md -> "Confidence band" / "Critic-delta
  // presentation" / "Download gate"). Absent/null on a review with no band
  // or no critic delta.
  confidence_band?: string | null;
  critic_delta?: CriticDelta | null;
}

interface OutputResponse {
  url: string;
  expires_in: number;
}

// Contract-type catalog entry — mirrors backend/src/review_routes.py's
// `get_playbooks` response shape (issue #272). `status` distinguishes an
// activated playbook ("active") from one that is registered but not yet
// activated ("coming_soon").
interface PlaybookCatalogEntry {
  playbook_id: string;
  display_name: string;
  status: string;
}

interface PlaybookCatalogResponse {
  playbooks: PlaybookCatalogEntry[];
}

// Non-terminal pipeline statuses — keep in sync with
// backend/src/reviews.py's REVIEW_STATUSES_NON_TERMINAL. Polling continues
// while the detail's status is one of these.
const NON_TERMINAL_STATUSES = new Set(['PENDING', 'RUNNING']);

const POLL_INTERVAL_MS = 3000;

// Capped exponential backoff for retrying a transient poll failure — a
// rejected/errored GET no longer stops polling for good (issue #271 item
// 1); it retries with growing delay, capped, until a response (success or
// terminal status) arrives.
const POLL_BACKOFF_MAX_MS = 30000;

const STILL_CHECKING_COPY = "Still checking on your review's status — reconnecting…";

// A critic delta is "present" (and must gate the download) when it carries at
// least one contested replacement or one critic-added issue
// (docs/output-contract.md -> "Download gate — delta indicator must be visible
// before download"). A null critic_delta, or one with empty lists, does not
// gate.
function criticDeltaHasContent(delta: CriticDelta | null | undefined): boolean {
  if (!delta) {
    return false;
  }
  const contested = delta.contested_replacements ?? [];
  const added = delta.added_issues ?? [];
  return contested.length > 0 || added.length > 0;
}

export default function ReviewSubmission(): React.ReactElement {
  const [file, setFile] = useState<File | null>(null);
  const [reviewId, setReviewId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [downloading, setDownloading] = useState(false);

  // Contract-type catalog + selection (issue #272). `playbooks` renders the
  // picker entirely — never a hardcoded id/name list. `playbookId` is the
  // current selection; `submittedPlaybookLabel` freezes the label for the
  // review actually in flight, so it keeps showing correctly even if the
  // attorney changes the selector afterward.
  const [playbooks, setPlaybooks] = useState<PlaybookCatalogEntry[]>([]);
  const [playbookId, setPlaybookId] = useState<string>('');
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [submittedPlaybookLabel, setSubmittedPlaybookLabel] = useState<string | null>(null);

  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Fetch the contract-type catalog once on mount. A failure here degrades
  // gracefully (no selector renders; the submission FormData simply omits
  // playbook_id and the backend's own default applies) rather than
  // blocking upload.
  useEffect(() => {
    let cancelled = false;

    async function fetchCatalog(): Promise<void> {
      try {
        const response = await authorizedFetch('/api/playbooks');
        if (!response.ok) {
          throw new Error(`GET /api/playbooks returned HTTP ${response.status}`);
        }
        const data = (await response.json()) as PlaybookCatalogResponse;
        if (cancelled) {
          return;
        }
        const entries = data.playbooks ?? [];
        setPlaybooks(entries);
        setCatalogError(null);
        const firstActive = entries.find((entry) => entry.status === 'active');
        setPlaybookId((current) => current || (firstActive ?? entries[0])?.playbook_id || '');
      } catch (err) {
        if (!cancelled) {
          setCatalogError(
            friendlyErrorMessage(err, "We couldn't load the list of contract types right now."),
          );
        }
      }
    }

    void fetchCatalog();
    return () => {
      cancelled = true;
    };
  }, []);

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) {
      clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  // Poll GET /api/reviews/{review_id} on an interval while the review is
  // non-terminal (PENDING/RUNNING); stop once a terminal status arrives.
  useEffect(() => {
    if (!reviewId) {
      return undefined;
    }

    let cancelled = false;
    let attempt = 0;

    async function poll(): Promise<void> {
      try {
        const response = await authorizedFetch(`/api/reviews/${reviewId}`);
        if (!response.ok) {
          throw new Error(`GET /api/reviews/${reviewId} returned HTTP ${response.status}`);
        }
        const data = (await response.json()) as ReviewDetail;
        if (cancelled) {
          return;
        }
        attempt = 0;
        setDetail(data);
        setPollError(null);
        if (NON_TERMINAL_STATUSES.has(data.status)) {
          pollTimer.current = setTimeout(() => {
            void poll();
          }, POLL_INTERVAL_MS);
        }
      } catch (err) {
        if (cancelled) {
          return;
        }
        // Transient failure — distinguish "still checking" from a terminal
        // stop: log the technical detail and reschedule with capped
        // exponential backoff instead of giving up on polling for good.
        attempt += 1;
        setPollError(friendlyErrorMessage(err, STILL_CHECKING_COPY));
        const backoff = Math.min(POLL_INTERVAL_MS * 2 ** (attempt - 1), POLL_BACKOFF_MAX_MS);
        pollTimer.current = setTimeout(() => {
          void poll();
        }, backoff);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [reviewId, stopPolling]);

  const handleSubmit = useCallback(
    async (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!file) {
        setSubmitError('Choose a .docx file first.');
        return;
      }

      setSubmitting(true);
      setSubmitError(null);
      setDownloadError(null);
      stopPolling();
      setDetail(null);
      setReviewId(null);

      try {
        const formData = new FormData();
        formData.append('file', file);
        if (playbookId) {
          formData.append('playbook_id', playbookId);
        }

        const response = await authorizedFetch('/api/reviews', {
          method: 'POST',
          body: formData,
        });

        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `POST /api/reviews returned HTTP ${response.status}`,
                "We couldn't submit your file for review. Please try again.",
              ),
          );
        }

        const data = (await response.json()) as SubmitResponse;
        const selected = playbooks.find((entry) => entry.playbook_id === playbookId);
        setSubmittedPlaybookLabel(selected?.display_name ?? (playbookId || null));
        setReviewId(data.review_id);
      } catch (err) {
        setSubmitError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't submit your file for review. Please try again."),
        );
      } finally {
        setSubmitting(false);
      }
    },
    [file, playbookId, playbooks, stopPolling],
  );

  const handleDownload = useCallback(async () => {
    if (!reviewId) {
      return;
    }
    setDownloading(true);
    setDownloadError(null);
    try {
      const response = await authorizedFetch(`/api/reviews/${reviewId}/output`);
      if (!response.ok) {
        const detail = await readErrorDetail(response);
        throw new Error(
          detail ??
            friendlyErrorMessage(
              `GET /api/reviews/${reviewId}/output returned HTTP ${response.status}`,
              "We couldn't prepare your download. Please try again.",
            ),
        );
      }
      const data = (await response.json()) as OutputResponse;
      // Hand the URL to the browser via a temporary anchor rather than
      // window.location.assign — the SPA (and its in-memory app state)
      // never navigates away (issue #271 item 5).
      triggerBrowserDownload(data.url);
    } catch (err) {
      setDownloadError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't prepare your download. Please try again."),
      );
    } finally {
      setDownloading(false);
    }
  }, [reviewId]);

  // ACCEPT never reads "approved" / "no action needed" (ARCHITECTURE.md's
  // Wrong-format rejection UX / accept framing) — always "no requested
  // changes identified by tool", with the same watermark every other
  // terminal state carries.
  const decisionCopy: string | null =
    detail?.decision === 'ACCEPT'
      ? 'No requested changes identified by tool.'
      : (detail?.message ?? (detail?.decision === 'REQUEST_CHANGE' ? 'Changes requested.' : null));

  return (
    <section data-testid="review-submission" style={{ marginTop: '1.5rem' }}>
      <h2 style={{ fontSize: '1.1rem' }}>Submit a contract for review</h2>

      <ToasterStyles />

      <form onSubmit={(event) => void handleSubmit(event)}>
        {playbooks.length > 0 && (
          <ContractTypeDial entries={playbooks} value={playbookId} onChange={setPlaybookId} />
        )}

        {catalogError && (
          <p data-testid="review-catalog-error" role="alert">
            {catalogError}
          </p>
        )}

        <input
          type="file"
          accept=".docx"
          data-testid="review-file-input"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        <button type="submit" disabled={submitting || !file} data-testid="review-submit-button">
          {submitting ? 'Uploading…' : 'Upload for review'}
        </button>
      </form>

      {submitError && (
        <p data-testid="review-submit-error" role="alert">
          {submitError}
        </p>
      )}

      {reviewId && (
        <div data-testid="review-status" style={{ marginTop: '1rem' }} aria-live="polite">
          {/*
            Doneness-style progress illustration (issue #280) — reuses
            ReviewStatus as-is (no new states): shown while the pipeline is
            non-terminal (PENDING/RUNNING), including the brief window before
            the first poll response arrives (`detail` still null). Purely
            decorative; the text status above/below still carries the
            information for assistive tech.
          */}
          {(!detail || NON_TERMINAL_STATUSES.has(detail.status)) && <ProgressToaster />}

          <p>
            Review <code>{reviewId}</code>: <strong>{detail?.status ?? 'submitting…'}</strong>
          </p>

          {submittedPlaybookLabel && (
            <p data-testid="review-submitted-playbook">
              Contract type: <strong>{submittedPlaybookLabel}</strong>
            </p>
          )}

          {pollError && (
            <p data-testid="review-poll-error" role="alert">
              {pollError}
            </p>
          )}

          {detail && !NON_TERMINAL_STATUSES.has(detail.status) && (
            <div data-testid="review-result">
              {/*
                State mapping (issue #280): DONE gets the toast-up treatment;
                every other terminal status (ERROR, MANUAL_REVIEW_REQUIRED,
                ERROR_MANUAL_REVIEW_REQUIRED) gets a distinct, sober,
                non-cute treatment — this is legal software, and failure
                states must read as serious. Purely decorative; the copy
                below is unchanged from before this ticket.
              */}
              {detail.status === 'DONE' ? <ToastUpToaster /> : <SoberToaster />}

              {decisionCopy && <p>{decisionCopy}</p>}
              <p style={{ fontSize: '0.8rem', fontStyle: 'italic' }}>
                Tool recommendation only — attorney approval required.
              </p>

              {/*
                Pre-download trust-calibration signals. These render ABOVE the
                download affordance, in normal document flow, so the attorney
                sees them before acting on the result
                (docs/output-contract.md -> "Confidence band" is shown
                pre-download; "Download gate — delta indicator must be visible
                before download"). They are distinct SYSTEM signals, visually
                separate from the binary ACCEPT | REQUEST_CHANGE decision and
                from the attorney-approval watermark — never a legal category.
              */}
              {detail.confidence_band && (
                <div
                  data-testid="review-confidence-band"
                  style={{
                    marginTop: '0.75rem',
                    padding: '0.5rem',
                    border: '1px solid currentColor',
                    fontSize: '0.85rem',
                  }}
                >
                  <strong>System status:</strong> {detail.confidence_band}
                </div>
              )}

              {criticDeltaHasContent(detail.critic_delta) && (
                <div
                  data-testid="review-critic-delta"
                  style={{
                    marginTop: '0.75rem',
                    padding: '0.5rem',
                    border: '1px solid currentColor',
                    fontSize: '0.85rem',
                  }}
                >
                  <p style={{ margin: 0 }}>
                    <strong>Adversarial critic flagged this review.</strong> Review the
                    points below before downloading.
                  </p>

                  {(detail.critic_delta?.contested_replacements ?? []).map((contested, i) => (
                    <div
                      key={`contested-${i}`}
                      data-testid={`critic-contested-${i}`}
                      style={{ marginTop: '0.5rem' }}
                    >
                      {contested.critic_objection && (
                        <p style={{ margin: 0 }}>
                          <em>Critic flagged this replacement:</em> {contested.critic_objection}
                        </p>
                      )}
                      {contested.critic_suggested_replacement && (
                        <p style={{ margin: '0.25rem 0 0' }}>
                          <em>Critic suggestion:</em> {contested.critic_suggested_replacement}
                        </p>
                      )}
                    </div>
                  ))}

                  {(detail.critic_delta?.added_issues ?? []).length > 0 && (
                    <p data-testid="critic-added-issues" style={{ marginTop: '0.5rem' }}>
                      The critic added{' '}
                      {(detail.critic_delta?.added_issues ?? []).length} issue(s) the primary
                      review missed.
                    </p>
                  )}
                </div>
              )}

              {detail.has_output && (
                <div style={{ marginTop: '0.75rem' }}>
                  <button
                    type="button"
                    onClick={() => void handleDownload()}
                    disabled={downloading}
                    data-testid="review-download-button"
                  >
                    {downloading ? 'Preparing download…' : 'Download result'}
                  </button>
                </div>
              )}

              {downloadError && (
                <p data-testid="review-download-error" role="alert">
                  {downloadError}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
