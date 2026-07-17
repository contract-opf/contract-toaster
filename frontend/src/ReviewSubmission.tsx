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
import { ToasterHero, ToasterStyles, type ToasterPhase } from './toaster/Toaster';
import {
  primeAudio,
  playLever,
  startTicking,
  stopTicking,
  playPop,
  useSoundMuted,
} from './toaster/sounds';

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
  // Failure diagnosis. backend/src/reviews.py's record_stage_failure records
  // the REAL per-stage name that failed (never a hardcoded 'pipeline'), and
  // get_review_detail has always returned both of these — this UI just used
  // to drop them on the floor and render a bare "ERROR", which told an
  // operator nothing about whether the cause was a missing API key, the
  // playbook, or their document. Null on a review that didn't fail.
  failing_stage?: string | null;
  reason?: string | null;
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

// Human-readable failure explanations, keyed by the `failing_stage` that
// backend/src/pipeline_runner.py's run_real_pipeline records. A bare "ERROR"
// is useless to the person who has to fix it: every entry here says what
// broke AND what to do about it. Keep the keys in step with the `stage = "…"`
// assignments in run_real_pipeline.
const STAGE_EXPLANATIONS: Record<string, { cause: string; fix: string }> = {
  build_model_client: {
    cause: 'No usable model API key was found, so the review never reached the model.',
    fix: 'An admin can add one under “Model & API key”. Until then every review will fail here.',
  },
  load_playbook: {
    cause: "This contract type isn't set up for review yet.",
    fix: 'Pick a different contract type, or ask an admin to activate this one.',
  },
  fetch_upload: {
    cause: "Your document was uploaded, but couldn't be read back for review.",
    fix: 'This is usually temporary — try submitting it again.',
  },
  run_review: {
    cause: 'The model could not complete the review.',
    fix:
      'Most often the API key was rejected, the selected model is unavailable, or the ' +
      'document is longer than the model can read at once. An admin can check the key ' +
      'and model under “Model & API key”.',
  },
  persist_result: {
    cause: 'The review finished, but the result could not be saved.',
    fix: 'Please try again — the review will need to be re-run.',
  },
  mark_running: {
    cause: "The review couldn't be started.",
    fix: 'Please try again.',
  },
};

function explainFailure(detail: ReviewDetail): { cause: string; fix: string } | null {
  if (!detail.failing_stage) {
    return null;
  }
  return (
    STAGE_EXPLANATIONS[detail.failing_stage] ?? {
      cause: 'The review stopped before it could finish.',
      fix: 'Please try again, or contact an admin if it keeps happening.',
    }
  );
}

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
  // Distinguishes "catalog hasn't arrived yet" from "catalog arrived and
  // nothing is loaded" — only the latter warrants the empty-state message.
  const [catalogLoaded, setCatalogLoaded] = useState(false);
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
        // Every registered playbook reaches the dial, which renders the
        // unactivated ones as de-emphasized, NON-selectable "(coming soon)"
        // stops (see ContractTypeDial). Two things are true at once: a
        // registered-but-unactivated playbook can't be reviewed against
        // (run_real_pipeline fails closed at load_playbook), so offering it as
        // a *choice* only invites a guaranteed 503 — but it is still real,
        // published intent, and the dial is the product's roadmap as much as
        // its control. So: visible, not selectable. The catalog endpoint
        // remains the authority on `status`; this is presentation only.
        const entries = data.playbooks ?? [];
        setPlaybooks(entries);
        setCatalogLoaded(true);
        setCatalogError(null);
        // Default to the first LOADED type — never park the selection on a
        // stop the user isn't allowed to pick.
        const firstActive = entries.find((entry) => entry.status === 'active');
        setPlaybookId((current) => current || firstActive?.playbook_id || '');
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

      // Prime + play inside the user's submit gesture so the browser's audio
      // autoplay policy is satisfied (primeAudio must run in a user gesture).
      primeAudio();
      playLever();

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

  // Sound mute state (persisted by the sounds module; no localStorage here).
  const { muted, toggle } = useSoundMuted();

  // A single derived phase drives the whole photoreal toaster (ToasterHero):
  // idle before a review is in flight; working while the pipeline is
  // non-terminal (or the first poll hasn't landed); done on DONE; error on any
  // other terminal status.
  const phase: ToasterPhase = !reviewId
    ? 'idle'
    : !detail || NON_TERMINAL_STATUSES.has(detail.status)
      ? 'working'
      : detail.status === 'DONE'
        ? 'done'
        : 'error';

  // "Is anything actually reviewable?" — distinct from "is the catalog empty?".
  // A registry of only unactivated types yields coming-soon stops the user
  // can't pick, which must still read as "nothing loaded".
  const hasLoadedPlaybook = playbooks.some((entry) => entry.status === 'active');

  // Ticking sound tracks the working phase; a single pop fires on the
  // transition into done. startTicking/stopTicking are idempotent, and playPop
  // fires once per entry into 'done' because deps are just [phase].
  useEffect(() => {
    if (phase === 'working') {
      startTicking();
    } else {
      stopTicking();
    }
    if (phase === 'done') {
      playPop();
    }
    return () => stopTicking();
  }, [phase]);

  // ACCEPT never reads "approved" / "no action needed" (ARCHITECTURE.md's
  // Wrong-format rejection UX / accept framing) — always "no requested
  // changes identified by tool", with the same watermark every other
  // terminal state carries.
  const decisionCopy: string | null =
    detail?.decision === 'ACCEPT'
      ? 'No requested changes identified by tool.'
      : (detail?.message ?? (detail?.decision === 'REQUEST_CHANGE' ? 'Changes requested.' : null));

  const failureExplanation = detail ? explainFailure(detail) : null;

  return (
    <section data-testid="review-submission" className="ct-section ct-stack">
      <h2 className="ct-section-title">Submit a contract for review</h2>

      <ToasterStyles />

      {/*
        One photoreal toaster drives every visual state via `phase`. It renders
        the accessible contract-type dial itself when `entries.length > 0`
        (data-testid review-playbook-dial + review-playbook-option-{id}),
        rotates the pointer to `value`, and provides the progress / done / sober
        state visuals (toaster-state-progress / -done / -sober) that used to be
        three separate illustrations. When output is ready, the "done" toast is
        a real download button wired to handleDownload.
      */}
      <ToasterHero
        entries={playbooks}
        value={playbookId}
        onChange={setPlaybookId}
        phase={phase}
        onDownload={detail?.has_output ? () => void handleDownload() : undefined}
        downloadDisabled={downloading}
      />

      {/*
        No LOADED playbook == nothing is reviewable, so say so explicitly
        rather than leave a toaster whose only stops are ones you can't pick.
        Keyed on the absence of an *active* type, not on an empty catalog: a
        registry holding only unactivated types still renders (coming-soon)
        stops, and that must not read as a working dial. Only shown once the
        catalog has actually loaded (a catalog FETCH failure has its own
        message below).
      */}
      {catalogLoaded && !hasLoadedPlaybook && !catalogError && (
        <p data-testid="review-no-playbooks" className="ct-note" role="status">
          No contract types are loaded yet, so there&apos;s nothing to review against. An
          admin needs to activate a playbook first.
        </p>
      )}

      <div className="ct-toolbar">
        <button
          type="button"
          className="ct-icon-button"
          aria-pressed={muted}
          onClick={toggle}
          data-testid="sound-toggle"
        >
          {muted ? '🔇 Sound off' : '🔊 Sound on'}
        </button>
      </div>

      <form onSubmit={(event) => void handleSubmit(event)}>
        {catalogError && (
          <p data-testid="review-catalog-error" className="ct-error" role="alert">
            {catalogError}
          </p>
        )}

        <input
          type="file"
          accept=".docx"
          data-testid="review-file-input"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        <div className="ct-actions">
          <button type="submit" disabled={submitting || !file} data-testid="review-submit-button">
            {submitting ? 'Uploading…' : 'Upload for review'}
          </button>
        </div>
      </form>

      {submitError && (
        <p data-testid="review-submit-error" className="ct-error" role="alert">
          {submitError}
        </p>
      )}

      {reviewId && (
        <div data-testid="review-status" className="ct-stack" aria-live="polite">
          <p className="ct-status">
            Review <code>{reviewId}</code>: <strong>{detail?.status ?? 'submitting…'}</strong>
          </p>

          {submittedPlaybookLabel && (
            <p data-testid="review-submitted-playbook">
              Contract type: <strong>{submittedPlaybookLabel}</strong>
            </p>
          )}

          {pollError && (
            <p data-testid="review-poll-error" className="ct-error" role="alert">
              {pollError}
            </p>
          )}

          {/*
            Failure diagnosis. The server already knows exactly which stage
            failed; showing it (with the technical stage name kept visible for
            an admin to act on or quote in a bug report) is the difference
            between "ERROR" and an operator knowing to go add an API key.
          */}
          {detail && failureExplanation && (
            <div data-testid="review-failure" className="ct-error" role="alert">
              <p>
                <strong>{failureExplanation.cause}</strong>
              </p>
              <p>{failureExplanation.fix}</p>
              <p className="ct-muted">
                <small>
                  Failed at stage <code data-testid="review-failing-stage">{detail.failing_stage}</code>
                  {detail.reason && detail.reason !== 'unhandled_exception' && (
                    <> · {detail.reason}</>
                  )}
                </small>
              </p>
            </div>
          )}

          {detail && !NON_TERMINAL_STATUSES.has(detail.status) && (
            <div data-testid="review-result" className="ct-stack">
              {decisionCopy && <p>{decisionCopy}</p>}
              <p className="ct-muted">
                <em>Tool recommendation only — attorney approval required.</em>
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
                <div data-testid="review-confidence-band" className="ct-note">
                  <strong>System status:</strong> {detail.confidence_band}
                </div>
              )}

              {criticDeltaHasContent(detail.critic_delta) && (
                <div data-testid="review-critic-delta" className="ct-note">
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
                <div className="ct-actions">
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
                <p data-testid="review-download-error" className="ct-error" role="alert">
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
