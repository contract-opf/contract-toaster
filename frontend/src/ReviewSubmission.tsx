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
 * PER-REVIEW GUIDANCE (issue #431): an optional free-text field on the
 * submission form, appended to the upload FormData as `toaster_guidance` --
 * the field `POST /api/reviews` has accepted since issue #398 but that no
 * frontend surface ever sent. It governs over the playbook's positions on
 * conflict and never over the playbook's hard requirements; that precedence
 * is stated permanently beside the input itself (GUIDANCE_PRECEDENCE_COPY),
 * because a precedence rule only a doc knows about is not a rule the person
 * typing the instruction can act on. On a terminal review, whatever
 * guidance the review actually ran under is shown back read-only.
 *
 * COMPLETION HANDOFF (issue #448): the moment a review reaches DONE the
 * toaster dings (the already-bundled `pop` clip, governed by the existing
 * sound toggle), a persistent `aria-live` region announces that the redline
 * is ready, keyboard focus moves to the download control, and — ONLY when
 * the download gate below is already satisfied — a plain `<a download>`
 * click is fired so the file usually lands in the downloads folder with no
 * click at all.
 *
 * What is deliberately NOT done: `showSaveFilePicker()`. It requires
 * transient user activation and throws `SecurityError` without one, and a
 * review completes in a poll callback where no user gesture exists. A plain
 * anchor click is materially more permissive; when a browser suppresses it
 * anyway, nothing breaks — the focused button below is still the reliable
 * path, which is why focus and the announcement are not optional extras.
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
import {
  CtBanner,
  CtButton,
  CtCard,
  CtChip,
  CtField,
  CtFileDrop,
  CtIconButton,
  CtProgress,
} from './ui/react';
import type { CtChipVariant } from './ui/react';

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
  // Live progress (issue #447): which of the review spine's four sub-stages
  // (primary_pass / critic_pass / reconciliation / redline) is running RIGHT
  // NOW, written as each one starts and projected by get_review_detail. Null
  // or absent on a review that hasn't reached the spine, on a runner that
  // predates the seam, and on any target that reports no progress — the hero
  // then shows its indeterminate treatment rather than guessing a step.
  progress_stage?: string | null;
  // Trust-calibration signals the attorney must see BEFORE downloading
  // (docs/output-contract.md -> "Confidence band" / "Critic-delta
  // presentation" / "Download gate"). Absent/null on a review with no band
  // or no critic delta.
  confidence_band?: string | null;
  critic_delta?: CriticDelta | null;
  // The per-review free-text instructions this review was submitted with
  // (issue #431). Recorded on the reviews row at submission and projected
  // by backend/src/reviews.py's get_review_detail; null/absent on a review
  // submitted without any, and on every review created before that field
  // was recorded.
  toaster_guidance?: string | null;
}

interface OutputResponse {
  url: string;
  expires_in: number;
}

// Contract-type catalog entry — mirrors backend/src/review_routes.py's
// `get_playbooks` response shape (issue #272). `status` distinguishes an
// activated playbook ("active") from one that is registered but not yet
// activated ("coming_soon") — those are the only two, for every playbook
// alike (issue #433: the playbook the image ships with is installed by a
// deploy-time seed and carries no special marker).
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

// Permanent, non-dismissable precedence copy shown with the per-review
// guidance field (issue #431; docs/frontend-design-system.md §15.3). It is
// rendered as the field's own `hint`, so it is wired into the control's
// accessible description and cannot be dismissed or scrolled past
// independently of the input it qualifies.
//
// The wording is load-bearing. ARCHITECTURE.md's "Guidance-precedence model"
// is explicit that this precedence is enforced by INSTRUCTION to the model
// (scripts/primary_review_pass.py's TOASTER_GUIDANCE_INTRO — "THIS GUIDANCE
// GOVERNS"), never mechanically: the critic pass is the only check. So this
// says "govern", matching the prompt's own framing, and never "will
// override", which would promise a guarantee the system does not make. The
// hard-requirements carve-out is likewise not optional wording — guidance
// never reaches the judged-NL Floor the playbook's `hard_rejections`
// project, and copy that implied otherwise would misdescribe the tool.
const GUIDANCE_PRECEDENCE_COPY =
  "These instructions govern over the playbook's positions wherever the two conflict — " +
  'but never over rules the playbook marks as hard requirements, which nothing can ' +
  'override. A sentence or two is plenty.';

// Completion-handoff announcements (issue #448). Rendered into a persistent
// polite live region, so assistive tech is already watching it when the review
// lands rather than being handed a region that only appears at the same moment
// its text does. All three name the button by its visible label, because the
// announcement's whole job is to tell someone who cannot see the screen where
// the keyboard focus they just received now is.
const READY_SAVED_COPY =
  'Your redline is ready. Saving it to your downloads — the “Download result” button now has focus if you need it again.';
const READY_GATED_COPY =
  'Your redline is ready, but the adversarial critic flagged this review. Read the flagged points above, then use the “Download result” button to save it.';
const READY_NO_OUTPUT_COPY =
  'Your review has finished. There is no marked-up document to download.';

export interface FailureExplanation {
  cause: string;
  fix: string;
}

/**
 * The two failure facts a reviews row carries, and the only inputs
 * `explainFailure` reads. Declared narrowly (rather than taking a whole
 * `ReviewDetail`) so the ADMIN Diagnostics tab — whose rows carry five fields
 * and nothing else, by design (issue #443) — can call the same function the
 * reviewer-facing Review tab does, instead of growing a second copy of the
 * token→prose mapping that would drift the moment one surface learned a token
 * the other did not.
 */
export interface FailureFacts {
  reason?: string | null;
  failing_stage?: string | null;
}

// The generic reason token the backend records when it could not classify a
// failure any further (backend/src/pipeline_runner.py's
// FAILURE_REASON_UNCLASSIFIED). It carries no information, so it never wins
// over the stage-keyed copy below — it is precisely the "we don't know"
// value.
const UNCLASSIFIED_REASON = 'unhandled_exception';

// Human-readable failure explanations, keyed by the `reason` TOKEN the
// backend records on the review row (issue #442).
//
// The token→prose mapping lives HERE, on purpose. The backend knows the
// provider's HTTP status, the endpoint, the key, and the exception text; none
// of that may reach a user-facing string (issue #425, and model_client.py's
// deliberate response-body omission). So the backend ships a token that
// contains no such material, and this table turns it into copy. That is what
// buys comprehensibility and the leak guarantee at the same time.
//
// The bar for every entry: a reader who is not an engineer can tell whose
// problem it is — THEIRS (the document), the OPERATOR'S (the account, key or
// model), or the SYSTEM'S (something broke; nothing you can do) — and what
// happens next. Never a raw status number, endpoint, stack trace, or any
// substance from the prompt or the document.
//
// Exported (issue #443) so the admin Diagnostics tab renders the SAME prose
// for the same token. It deliberately stays declared in THIS file rather than
// moving to a module of its own: tests/test_review_failure_reason_442.py
// asserts every classifier token has copy by reading this table out of this
// file, so "the reader-facing copy lives where the reader-facing screen is"
// is a checked property, not a convention.
export const REASON_EXPLANATIONS: Record<string, FailureExplanation> = {
  // --- The operator's problem: the model account, key or model ------------
  model_account_out_of_credits: {
    cause: 'The model account has run out of credits, so the review was never run.',
    fix: 'An admin needs to add funds to the account used under “Model & API key”. Nothing is wrong with your document — resubmit it once that is done.',
  },
  model_key_rejected: {
    cause: 'The model provider rejected the key this deployment is using.',
    fix: 'An admin can replace the key under “Model & API key”. Until then every review will fail the same way.',
  },
  model_rate_limited: {
    cause: 'The model provider is temporarily refusing requests because too many were sent at once.',
    fix: 'Wait a few minutes and submit again. If it keeps happening, an admin should check the account’s limits under “Model & API key”.',
  },
  model_unavailable: {
    cause: 'The model this deployment is set to use is not available from the provider right now.',
    fix: 'Try again later, or ask an admin to select a different model under “Model & API key”.',
  },
  // --- Your problem: the document itself ----------------------------------
  model_context_length_exceeded: {
    cause: 'Your document is longer than the model can read in one go, so it was not reviewed.',
    fix: 'Split it into smaller documents and submit them separately, or have the long sections reviewed by hand.',
  },
  document_too_large: {
    cause: 'Your document is longer than the model can read in one go, so it was not reviewed.',
    fix: 'Split it into smaller documents and submit them separately, or have the long sections reviewed by hand.',
  },
  unnormalizable_input: {
    cause: 'Your file could not be read as a Word document.',
    fix: 'Upload a .docx file saved by Word — not a PDF, an older .doc, or a scan — and try again.',
  },
  // --- The operator's problem: which playbook is installed/active ---------
  unknown_playbook: {
    cause: 'The contract type this review was submitted for is not installed.',
    fix: 'Pick a different contract type, or ask an admin to install this one.',
  },
  playbook_coming_soon: {
    cause: 'This contract type is registered but not switched on for review yet.',
    fix: 'Pick a different contract type, or ask an admin when this one will be available.',
  },
  submission_time_bundle_retired: {
    cause: 'The playbook this review was submitted against was replaced or switched off before the review started, so it was stopped rather than run against different rules than you chose.',
    fix: 'Submit the document again — it will be reviewed against the playbook that is active now.',
  },
  // --- The system's problem: nothing the reader can do --------------------
  structured_output_retry_exhausted: {
    cause: 'The model kept returning a result the system could not read, so no review was produced.',
    fix: 'This has been recorded. Please try again; if it keeps happening, an admin should try a different model under “Model & API key”.',
  },
  quote_patches_not_applied: {
    cause: 'The review found changes to request, but none of them could be placed into your document, so no marked-up copy was produced.',
    fix: 'Try submitting the document again. If it keeps happening, the document may be formatted in a way the tool cannot mark up, and the changes will need to be made by hand.',
  },
  leakage_detected: {
    cause: 'A safety check stopped this review before any result was produced.',
    fix: 'There is nothing to fix on your side and nothing to download. It has been recorded — contact an admin if you still need this document reviewed.',
  },
  output_ooxml_scan_failed: {
    cause: 'The marked-up document failed the tool’s own safety check, so it was not released.',
    fix: 'This is a fault in the tool, not in your document. It has been recorded — please try again, or contact an admin if it keeps happening.',
  },
  round_trip_verification_failed: {
    cause: 'The marked-up document could not be verified as safe to open in Word, so it was not released.',
    fix: 'This is a fault in the tool, not in your document. It has been recorded — please try again, or contact an admin if it keeps happening.',
  },
};

// Human-readable failure explanations, keyed by the `failing_stage` that
// backend/src/pipeline_runner.py's run_real_pipeline records. A bare "ERROR"
// is useless to the person who has to fix it: every entry here says what
// broke AND what to do about it. Keep the keys in step with the `stage = "…"`
// assignments in run_real_pipeline.
//
// These are the FALLBACK: the stage says where the pipeline stopped, which is
// necessarily vaguer than why. Whenever the backend managed to classify the
// cause, REASON_EXPLANATIONS above wins.
const STAGE_EXPLANATIONS: Record<string, FailureExplanation> = {
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
      'The exact cause was not identified. An admin can check the account, key and ' +
      'model under “Model & API key”; it is also worth re-submitting in case it was ' +
      'a passing problem at the provider.',
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

/**
 * Explain a failed review, preferring the specific over the vague.
 *
 * Order is load-bearing (issue #442):
 *   1. the `reason` token, when the backend classified one — it names the
 *      actual cause (out of credits, key rejected, document too long);
 *   2. the `failing_stage`, which only says where the pipeline stopped;
 *   3. a generic "try again", for a stage this build has never heard of.
 *
 * `unhandled_exception` is skipped at step 1 by design: it is the backend's
 * "could not classify" value, so falling through to the stage copy is
 * strictly more informative — and is exactly today's behavior, which is why
 * no existing failure path regresses.
 *
 * Exported (issue #443): the admin Diagnostics tab resolves each of its rows
 * through this very function, so the two surfaces cannot disagree about what
 * a token means. It takes `FailureFacts`, not `ReviewDetail`, because the
 * diagnostics row deliberately carries nothing else.
 */
export function explainFailure(detail: FailureFacts): FailureExplanation | null {
  const reason = detail.reason;
  if (reason && reason !== UNCLASSIFIED_REASON) {
    const byReason = REASON_EXPLANATIONS[reason];
    if (byReason) {
      return byReason;
    }
  }
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

// Maps the confidence_band string to a chip variant. Unrecognised bands
// (the backend's vocabulary, not hardcoded here beyond this display hint)
// fall back to 'info', matching the banner treatment this chip replaces.
function confidenceChipVariant(band: string): CtChipVariant {
  const upper = band.toUpperCase();
  if (upper.includes('HIGH')) {
    return 'ok';
  }
  if (upper.includes('LOW')) {
    return 'warn';
  }
  return 'info';
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

  // Per-review free-text guidance (issue #431). `toasterGuidance` is what is
  // currently typed; `submittedGuidance` freezes what was actually sent with
  // the review now in flight — the same "freeze the submitted value" pattern
  // as submittedPlaybookLabel below, so editing the box afterwards can never
  // misreport what governed the running review. React state only: never
  // localStorage/sessionStorage, and never carried across a page load.
  const [toasterGuidance, setToasterGuidance] = useState('');
  const [submittedGuidance, setSubmittedGuidance] = useState<string | null>(null);
  // Whether the submit that produced the review now in flight was *resumed*
  // onto a pre-existing review (`SubmitResponse.resumed`). This matters only
  // for the guidance readback: the idempotency key
  // (backend/src/reviews.py's derive_idempotency_key) deliberately excludes
  // `toaster_guidance`, so re-dropping the same file inside the same time
  // bucket with instructions added returns the original review and leaves
  // its stored guidance untouched. On that path the locally held text never
  // governed anything, so it must never stand in for the server's record.
  const [submittedResumed, setSubmittedResumed] = useState(false);

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

  // Completion handoff (issue #448). `readyAnnouncement` populates a live
  // region that is mounted from first render; `saveControlRef` wraps the
  // download action row so focus can be moved onto the real <button> ct-button
  // renders into its light DOM; `handedOffReviewRef` remembers which review has
  // already been handed off so the announcement, the focus move and the
  // automatic save happen exactly once per review — never again on a re-render
  // or a late poll.
  const [readyAnnouncement, setReadyAnnouncement] = useState('');
  const saveControlRef = useRef<HTMLDivElement | null>(null);
  const handedOffReviewRef = useRef<string | null>(null);

  // Fetch the contract-type catalog on mount. A failure here degrades
  // gracefully (no selector renders; the submission FormData simply omits
  // playbook_id and the backend's own default applies) rather than
  // blocking upload.
  const fetchCatalog = useCallback(async (): Promise<void> => {
    try {
      const response = await authorizedFetch('/api/playbooks');
      if (!response.ok) {
        throw new Error(`GET /api/playbooks returned HTTP ${response.status}`);
      }
      const data = (await response.json()) as PlaybookCatalogResponse;
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
      setCatalogError(
        friendlyErrorMessage(err, "We couldn't load the list of contract types right now."),
      );
    }
  }, []);

  useEffect(() => {
    void fetchCatalog();
  }, [fetchCatalog]);

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
      setSubmittedGuidance(null);
      setSubmittedResumed(false);
      // Clear the previous completion handoff. The ref is keyed on review id
      // rather than simply "has run", and re-dropping the same file inside the
      // same idempotency bucket RESUMES the same review id — so without this
      // reset a resubmit that resolves straight back to an already-DONE review
      // would announce nothing, focus nothing and save nothing.
      setReadyAnnouncement('');
      handedOffReviewRef.current = null;

      try {
        const formData = new FormData();
        formData.append('file', file);
        if (playbookId) {
          formData.append('playbook_id', playbookId);
        }
        // Issue #431: the already-wired optional `toaster_guidance` form
        // field (backend/src/review_routes.py's post_review). Appended only
        // when it carries content — whitespace-only guidance is no guidance
        // at all (scripts/primary_review_pass.py's
        // render_toaster_guidance_block treats it that way too), and an
        // empty field must leave the request byte-identical to the one this
        // form sent before the field existed. No escaping/sanitization here:
        // the backend treats this as trusted first-party instruction text,
        // deliberately NOT wrapped in the pipeline's untrusted-input
        // delimiting (see that module's docstring).
        const guidance = toasterGuidance.trim();
        if (guidance) {
          formData.append('toaster_guidance', guidance);
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
        setSubmittedGuidance(guidance || null);
        setSubmittedResumed(Boolean(data.resumed));
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
    [file, playbookId, playbooks, stopPolling, toasterGuidance],
  );

  // Mint a short-lived presigned URL for this review's output. Shared by the
  // button the attorney clicks and by the automatic save on completion, so the
  // two can never drift on which endpoint they call or how they read a failure.
  const fetchOutputUrl = useCallback(async (): Promise<string> => {
    const response = await authorizedFetch(`/api/reviews/${reviewId}/output`);
    if (!response.ok) {
      const errorDetail = await readErrorDetail(response);
      throw new Error(
        errorDetail ??
          friendlyErrorMessage(
            `GET /api/reviews/${reviewId}/output returned HTTP ${response.status}`,
            "We couldn't prepare your download. Please try again.",
          ),
      );
    }
    const data = (await response.json()) as OutputResponse;
    return data.url;
  }, [reviewId]);

  const handleDownload = useCallback(async () => {
    if (!reviewId) {
      return;
    }
    setDownloading(true);
    setDownloadError(null);
    try {
      // Hand the URL to the browser via a temporary anchor rather than
      // window.location.assign — the SPA (and its in-memory app state)
      // never navigates away (issue #271 item 5).
      triggerBrowserDownload(await fetchOutputUrl());
    } catch (err) {
      setDownloadError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(err, "We couldn't prepare your download. Please try again."),
      );
    } finally {
      setDownloading(false);
    }
  }, [reviewId, fetchOutputUrl]);

  // The automatic save (issue #448) — the same anchor click the button
  // performs, fired once on completion without a user gesture.
  //
  // Deliberately NOT routed through handleDownload: this is a background
  // courtesy the attorney did not ask for, so it must not flip the visible
  // button into its disabled "Preparing download…" state (which would yank
  // away the focus we just placed there), and a failure must not paint a
  // download error over a result nobody tried to download yet. The button
  // below is the reliable path and will report any real failure when clicked,
  // so here the technical detail is logged and nothing else happens.
  const autoSaveOutput = useCallback(async (): Promise<void> => {
    if (!reviewId) {
      return;
    }
    try {
      triggerBrowserDownload(await fetchOutputUrl());
    } catch (err) {
      friendlyErrorMessage(err, 'The automatic save did not run; the download button still will.');
    }
  }, [reviewId, fetchOutputUrl]);

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

  // Completion handoff (issue #448): announce readiness, move focus to the
  // save control, and — only when the download gate is already satisfied —
  // fire the automatic save.
  //
  // THE GATE IS NOT BYPASSABLE HERE. docs/output-contract.md's "Download gate"
  // requires the critic-delta indicator to be surfaced before the download
  // affordance is acted on; it is a visual-surfacing rule, which an automatic
  // save performed while nobody is looking would silently defeat. So a review
  // whose critic delta carries content still dings, still announces, and still
  // focuses the button — but saves nothing until a human clicks, having passed
  // the indicator that now sits above that button. `criticDeltaHasContent` is
  // the same predicate that decides whether the indicator renders at all, so
  // the two can never disagree about whether the gate is in force.
  //
  // Ordering: focus BEFORE the save is attempted. The automatic save is best
  // effort (a browser may suppress an anchor click with no user activation),
  // and the focused button is the path that always works — it must not wait on
  // a network round trip that might fail.
  useEffect(() => {
    if (phase !== 'done' || !detail) {
      return;
    }
    if (handedOffReviewRef.current === detail.review_id) {
      return;
    }
    handedOffReviewRef.current = detail.review_id;

    const gateSatisfied = !criticDeltaHasContent(detail.critic_delta);
    const canSave = Boolean(detail.has_output);

    setReadyAnnouncement(
      !canSave ? READY_NO_OUTPUT_COPY : gateSatisfied ? READY_SAVED_COPY : READY_GATED_COPY,
    );

    // ct-button renders a real <button> into its light DOM (ui/components/
    // ct-button.ts), so the focusable node is a descendant of this wrapper,
    // not the wrapper itself.
    saveControlRef.current?.querySelector('button')?.focus();

    if (canSave && gateSatisfied) {
      void autoSaveOutput();
    }
  }, [phase, detail, autoSaveOutput]);

  // ACCEPT never reads "approved" / "no action needed" (ARCHITECTURE.md's
  // Wrong-format rejection UX / accept framing) — always "no requested
  // changes identified by tool", with the same watermark every other
  // terminal state carries.
  const decisionCopy: string | null =
    detail?.decision === 'ACCEPT'
      ? 'No requested changes identified by tool.'
      : (detail?.message ?? (detail?.decision === 'REQUEST_CHANGE' ? 'Changes requested.' : null));

  const failureExplanation = detail ? explainFailure(detail) : null;

  // Which per-review instructions actually governed this review (issue
  // #431). The server's own record of it wins — get_review_detail projects
  // the value stored on the reviews row at submission. The value frozen at
  // submit time is a fallback for backend/frontend version skew ONLY (a
  // deploy whose detail response predates the field), and only on a fresh
  // submission: on a resumed one the review already existed and its stored
  // guidance was left untouched (backend/src/reviews.py's submit_review), so
  // the text typed into *this* submit governed nothing and must not be
  // presented as though it had. Null/empty on both sides renders nothing at
  // all: a review submitted with no guidance must show no extra banner, not
  // an empty one.
  const appliedGuidance: string | null =
    detail?.toaster_guidance ?? (submittedResumed ? null : submittedGuidance);

  return (
    <section data-testid="review-submission" className="ct-section ct-stack">
      <h2 className="ct-section-title">Submit a contract for review</h2>

      <ToasterStyles />

      {/*
        The hero gets a stage — a ct-card "counter" (docs/frontend-design-
        system.md §7/§8) — with the submission row directly beneath the
        slot: ct-file-drop, primary submit button, sound toggle.
      */}
      <CtCard pad="lg" data-testid="review-counter">
        <div className="ct-stack">
          {/*
            One photoreal toaster drives every visual state via `phase`. It
            renders the accessible contract-type dial itself when
            `entries.length > 0` (data-testid review-playbook-dial +
            review-playbook-option-{id}), rotates the pointer to `value`, and
            provides the progress / done / sober state visuals
            (toaster-state-progress / -done / -sober) that used to be three
            separate illustrations. When output is ready, the "done" toast is
            a real download button wired to handleDownload.
          */}
          <ToasterHero
            entries={playbooks}
            value={playbookId}
            onChange={setPlaybookId}
            phase={phase}
            onDownload={detail?.has_output ? () => void handleDownload() : undefined}
            downloadDisabled={downloading}
            progressStage={detail?.progress_stage ?? null}
          />

          {/* Non-terminal states (submitting / polling). Issue #447 retired
              the indeterminate <CtProgress> bar that used to sit here ONCE
              the pipeline reports a real stage: an animated line that
              carries no information is strictly worse than the hero's
              staged toast, which says which of the four steps we are in.
              Until a stage lands (the first poll, or a runner that reports
              none) the bar stays — that period genuinely IS indeterminate,
              and the shimmer is the honest way to say so. */}
          {phase === 'working' && !detail?.progress_stage && (
            <CtProgress label="Reviewing your document…" data-testid="review-progress" />
          )}

          <form onSubmit={(event) => void handleSubmit(event)} className="ct-stack">
            {catalogError && (
              <CtBanner variant="danger" data-testid="review-catalog-error">
                {catalogError}
              </CtBanner>
            )}

            {/*
              Per-review instructions (issue #431). Optional and free-text:
              the backend already accepts it, defaults it to "", and omits
              the prompt block entirely when it is empty. The precedence copy
              rides along as the field's own hint so it is permanent,
              non-dismissable, and part of the control's accessible
              description — precedence is visible at the point of authoring,
              not only in a doc (docs/frontend-design-system.md §15.3).
            */}
            <div data-testid="review-guidance-field">
              <CtField
                label="Instructions for this review (optional)"
                hint={GUIDANCE_PRECEDENCE_COPY}
              >
                <textarea
                  data-testid="review-guidance-input"
                  rows={3}
                  value={toasterGuidance}
                  onChange={(event) => setToasterGuidance(event.target.value)}
                />
              </CtField>
            </div>

            <CtFileDrop
              accept=".docx"
              label="Drop your contract here or browse"
              data-testid="review-file-input"
              onFiles={(event) => setFile(event.detail.files[0] ?? null)}
            />

            <div className="ct-actions">
              <CtButton
                type="submit"
                variant="primary"
                disabled={submitting || !file}
                loading={submitting}
                data-testid="review-submit-button"
              >
                {submitting ? 'Uploading…' : 'Upload for review'}
              </CtButton>
              <CtIconButton
                type="button"
                label={muted ? 'Sound off' : 'Sound on'}
                aria-pressed={muted}
                onClick={toggle}
                data-testid="sound-toggle"
              >
                {muted ? '🔇 Sound off' : '🔊 Sound on'}
              </CtIconButton>
            </div>
          </form>
        </div>
      </CtCard>

      {/*
        Completion announcement (issue #448). Mounted from the very first
        render and left empty until a review lands, rather than appearing at
        the same moment its text does — a polite live region that is inserted
        already-populated is not reliably announced. It sits OUTSIDE the
        review-status block below (which is itself aria-live) so the two are
        never nested regions competing to narrate the same event.
      */}
      <p
        role="status"
        aria-live="polite"
        className="ct-muted"
        data-testid="review-ready-announcement"
      >
        {readyAnnouncement}
      </p>

      {/*
        No LOADED playbook == nothing is reviewable, so say so explicitly
        rather than leave a toaster whose only stops are ones you can't pick.
        Keyed on the absence of an *active* type, not on an empty catalog: a
        registry holding only unactivated types still renders (coming-soon)
        stops, and that must not read as a working dial. Only shown once the
        catalog has actually loaded (a catalog FETCH failure has its own
        message below).

        The empty-shell state (issue #401/#433): there is no bespoke
        activate-the-sample action here any more — every playbook, including
        the one the image ships with, is installed and activated from the
        Playbooks admin tab (or, on a fresh deployment, by the deploy-time
        seed). So this says who needs to act and where, plus a pointer to
        authoring your own.
      */}
      {catalogLoaded && !hasLoadedPlaybook && !catalogError && (
        <CtBanner variant="muted" data-testid="review-no-playbooks">
          <div className="ct-stack">
            <p>
              No contract types are loaded yet, so there&apos;s nothing to review against.
            </p>

            <p>An admin needs to install and activate a playbook first, from the Playbooks tab.</p>

            <p className="ct-muted">
              <small>
                Building your own? Author a playbook with the playbook-engine and upload it
                from the playbook admin panel once it&apos;s ready. Format reference:{' '}
                <a href="https://contract-opf.github.io/" target="_blank" rel="noreferrer">
                  contract-opf.github.io
                </a>
                .
              </small>
            </p>
          </div>
        </CtBanner>
      )}

      {submitError && (
        <CtBanner variant="danger" data-testid="review-submit-error">
          {submitError}
        </CtBanner>
      )}

      {reviewId && (
        <div data-testid="review-status" className="ct-stack" aria-live="polite">
          <div className="ct-row">
            <CtChip variant="muted">
              <span className="ct-mono">{reviewId}</span>
            </CtChip>
            <strong>{detail?.status ?? 'submitting…'}</strong>
          </div>

          {submittedPlaybookLabel && (
            <p data-testid="review-submitted-playbook">
              Contract type: <strong>{submittedPlaybookLabel}</strong>
            </p>
          )}

          {pollError && (
            <CtBanner variant="danger" data-testid="review-poll-error">
              {pollError}
            </CtBanner>
          )}

          {/*
            Failure diagnosis. The server already knows exactly why the review
            failed; showing it — in prose above, with the technical stage and
            reason tokens kept visible for an admin to act on or quote in a bug
            report — is the difference between "ERROR" and an operator knowing
            to go top up the model account.

            The tokens below are identifiers, never messages: everything the
            backend knew that must not be surfaced (status codes, endpoints,
            key material, exception text, prompt or document substance) was
            dropped on the backend side, and cannot reappear here.
          */}
          {detail && failureExplanation && (
            <CtBanner variant="danger" data-testid="review-failure">
              <p>
                <strong>{failureExplanation.cause}</strong>
              </p>
              <p>{failureExplanation.fix}</p>
              <p className="ct-muted">
                <small>
                  {detail.failing_stage && (
                    <>
                      Failed at stage{' '}
                      <code data-testid="review-failing-stage">{detail.failing_stage}</code>
                    </>
                  )}
                  {detail.reason && detail.reason !== UNCLASSIFIED_REASON && (
                    <>
                      {detail.failing_stage ? ' · ' : 'Recorded as '}
                      <code data-testid="review-failure-reason">{detail.reason}</code>
                    </>
                  )}
                </small>
              </p>
            </CtBanner>
          )}

          {detail && !NON_TERMINAL_STATUSES.has(detail.status) && (
            <div data-testid="review-result" className="ct-stack">
              {decisionCopy && <p>{decisionCopy}</p>}
              <p className="ct-muted">
                <em>Tool recommendation only — attorney approval required.</em>
              </p>

              {/*
                Read-only readback of the per-review instructions this review
                actually ran under (issue #431), so "which instructions
                applied to this review?" is answerable from the review itself
                rather than from memory. Rendered as text, never an editable
                control: a completed review's guidance is a record, not a
                setting. Only present when there was guidance — no empty
                banner on a review submitted without any.
              */}
              {appliedGuidance && (
                <CtBanner variant="info" data-testid="review-applied-guidance">
                  <p style={{ margin: '0 0 0.25rem' }}>
                    <strong>Instructions applied to this review</strong>
                  </p>
                  <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{appliedGuidance}</p>
                </CtBanner>
              )}

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
                <div className="ct-row" data-testid="review-confidence-band">
                  <span className="ct-muted">System status:</span>
                  <CtChip variant={confidenceChipVariant(detail.confidence_band)}>
                    {detail.confidence_band}
                  </CtChip>
                </div>
              )}

              {criticDeltaHasContent(detail.critic_delta) && (
                <CtBanner variant="warn" data-testid="review-critic-delta">
                  <p style={{ margin: '0 0 0.5rem' }}>
                    <CtChip variant="warn">
                      {(detail.critic_delta?.contested_replacements ?? []).length +
                        (detail.critic_delta?.added_issues ?? []).length}{' '}
                      flagged
                    </CtChip>
                  </p>
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
                </CtBanner>
              )}

              {detail.has_output && (
                // ref: the completion handoff moves keyboard focus onto the
                // real <button> inside this row (issue #448).
                <div className="ct-actions" ref={saveControlRef}>
                  <CtButton
                    type="button"
                    variant="primary"
                    onClick={() => void handleDownload()}
                    disabled={downloading}
                    loading={downloading}
                    data-testid="review-download-button"
                  >
                    {downloading ? 'Preparing download…' : 'Download result'}
                  </CtButton>
                </div>
              )}

              {downloadError && (
                <CtBanner variant="danger" data-testid="review-download-error">
                  {downloadError}
                </CtBanner>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
