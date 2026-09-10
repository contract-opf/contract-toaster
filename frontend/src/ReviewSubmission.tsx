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
 * ISSUE #492 — the handoff copy above ("...the button now has focus...") is
 * for ASSISTIVE ANNOUNCEMENT ONLY: the live region it populates
 * (`review-ready-announcement`) is visually hidden (`.ct-sr-only`,
 * app.css) rather than plain muted text. It used to render on screen
 * verbatim, which is how focus-management narration ended up as visible
 * copy — an attorney reading the panel should never see prose about where
 * the keyboard focus went. What a sighted user sees instead is the
 * truthful save line below (`autoSaved`), never a promise about focus.
 *
 * ACCEPT decision framing (ARCHITECTURE.md -> Wrong-format rejection UX):
 * an ACCEPT decision always reads "no requested changes identified by
 * tool" (never "approved" / "no action needed"), rendered by
 * `decisionCopy` below. This is distinct from — and outlives — the
 * attorney-approval watermark that used to accompany it: issue #492 is
 * owner direction that attorney/legal review is a policy the deploying
 * organization owns entirely outside this product, so the panel no longer
 * asserts or nags about it. See ARCHITECTURE.md and docs/threat-model.md
 * for where that framing still lives (the generated `.docx` itself,
 * scripts/redline_generate.py — issue #513's separate scope).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  authorizedFetch,
  DOCUMENT_PURGED_COPY,
  DOWNLOAD_ERROR_COPY,
  friendlyDownloadError,
  friendlyErrorMessage,
  readErrorDetail,
  triggerBrowserDownload,
} from './api';
// The shared outcome→(label, variant) map (issue #470) — see outcome.ts's
// module docstring. `explainFailure`/`REASON_EXPLANATIONS` below stay
// separate: those explain WHY a failure happened (stage + reason token),
// this says WHAT the outcome is.
import { describeOutcome } from './outcome';
// Last-selected contract type, persisted across a reload (issue #489, item
// 4). See lastPlaybook.ts's module docstring for the storage shape and why
// this is safe to keep in localStorage.
import { readLastPlaybookId, writeLastPlaybookId } from './lastPlaybook';
import { readLastBrowning, writeLastBrowning } from './lastBrowning';
import { readLastNotesMode, writeLastNotesMode } from './lastNotesMode';
// The cheap, advisory upload-time preflight check (issue #491) — fired the
// moment a file is chosen, never gating the go button. See
// preflight.ts's module docstring for the full injection-defense posture;
// this component's own job is just rendering `PreflightResult` as inert
// text, never as markup or a link (see the render site below).
import { refreshMatchVerdict, runPreflight, type PreflightResult } from './preflight';
// The shared disposition capture (issue #486) — the POST call and the
// vocabulary it is typed against. The console renders the choices and their
// labels itself (issue #726), so only the transport and the type are needed
// here; History still imports the copy constants for its per-row control.
import { recordDisposition, type AttorneyDisposition } from './disposition';
// "Butter it" (issue #499) — the shared cover-note client + copy, so this
// panel and History's expanded row can never drift on wording or on how a
// failure is turned into copy. See coverNote.ts's module docstring.
import { butterIt } from './coverNote';
// toastedOn: the same epoch-seconds -> "YYYY-MM-DD  HH:MM UTC" formatter the
// receipt uses for its own date line (issue #492's meta line reuses it for
// `updated_at` rather than duplicating the formatting).
import {
  receiptFilename,
  receiptLines,
  receiptText,
  toastedOn,
  type ReceiptSource,
} from './toaster/receipt';
// The Orbit Diner console (issue #719, epic #729) — since #727 deleted the
// tree it replaced, this component's whole render. The console owns no state:
// `toReviewModel` projects what this component already holds, and
// `connectReviewSubmission` hands every action straight back to the guarded
// handlers here. See orbit-diner/projection.ts's module docstring and
// docs/planning/2026-09-07-orbit-diner-04p1-final-plan.md's "Standing
// invariants".
import { OrbitDiner } from './orbit-diner/OrbitDiner';
import { preflightPlaybookChoice } from './orbit-diner/autoPlaybook';
import {
  connectReviewSubmission,
  toReviewModel,
  type ReviewProjectionState,
  type ReviewSubmissionCallbacks,
} from './orbit-diner/projection';
import { copyReceipt as copyOrbitReceipt, saveReceipt as saveOrbitReceipt } from './orbit-diner/receipt';
import {
  composeGuidance,
  type BrowningLevel,
} from './toaster/browning';
import {
  DEFAULT_NOTES_MODE,
  isNotesMode,
  isNotesModeAvailable,
  type NotesMode,
} from './notesMode';
import {
  primeAudio,
  playLever,
  startTicking,
  stopTicking,
  playPop,
  playDetent,
  playClunk,
  playMotionEvent,
  useSoundMuted,
} from './toaster/sounds';
// Favicon browning + tab title (issue #497) — one hook, driven by the same
// `phase`/`progress_stage` pair the hero itself renders from, so the tab
// chrome can never disagree with what is on screen.
import { useTabTheater, type ToasterPhase } from './toaster/tabChrome';
// The opt-in "toast's ready" Notification (issue #497) — a second, optional
// layer on top of the ding above; see notify.ts's docstring for the
// permission rule.
import { useNotifyPreference, notifyToastDone, notificationsSupported } from './toaster/notify';

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
  // Whether a stop has been asked for but has not taken effect yet.
  // Cancellation is cooperative — the pipeline stops at its next checkpoint,
  // which may be on the far side of an in-flight model call — so this gap is
  // real and the UI shows it rather than leaving the reviewer pressing a
  // button that appears to have done nothing.
  cancel_requested?: boolean | null;
  // Lineage + timing the receipt prints (issue #498). All of it is already
  // projected by get_review_detail; nothing here is computed client-side, and
  // a field the row does not carry simply makes its receipt line disappear
  // rather than print a guess.
  created_at?: string | null;
  updated_at?: string | null;
  playbook_id?: string | null;
  playbook_version?: string | null;
  instructions_version?: string | number | null;
  primary_model_id?: string | null;
  critic_model_id?: string | null;
  issues?: unknown;
  // Issue #563: the free-text disclosure that stage 1 accepted one or more
  // of the counterparty's own pending tracked changes into the operative
  // draft before review ever ran. Absent (never null) on a review with
  // nothing to accept -- same convention `get_review_detail` uses for
  // every field on the row.
  normalization_notes?: string | null;
  // Issue #569: present only once the bounded re-quote repair pass has run
  // (the flag can be off, or the ticket unmerged, for a long time yet) --
  // this field is simply absent until then, and the receipt must render
  // correctly either way.
  requote?: { attempted?: number; recovered?: number; still_failed?: number } | null;
  // Issue #486: the reviewer's OPTIONAL disposition capture, projected by
  // get_review_detail. Null/absent on a review with nothing recorded yet —
  // the panel renders that as an unrecorded control, never a guess.
  attorney_disposition?: string | null;
  // Issue #499 ("Butter it"): whether a cover-note draft is already cached
  // on this row — a boolean pointer only, never the draft text itself
  // (same discipline as has_output/has_input above). Used only to label
  // the button; the draft itself is fetched by actually clicking it.
  has_cover_note_draft?: boolean;
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

/** App.tsx's own hash for the History tab (`hashForTab('history')`). Issue #719's
 *  console links there rather than inventing a second navigation mechanism. */
const HISTORY_TAB_HASH = '#/history';

/**
 * The canonical receipt, as the flat `string[]` the Orbit Diner console's
 * clipboard/image helpers take (issue #719). Identical composition to the one
 * `toReviewModel` puts on the model — `receiptText(receiptLines(detail))`,
 * split back into lines — so the printed slip, the copied text and the saved
 * image can never drift from each other or from `toaster/receipt.ts`.
 */
function orbitReceiptLines(
  source: ReceiptSource,
  playbookName: string | null,
): string[] {
  return receiptText(receiptLines(source, playbookName)).split('\n');
}

// Adaptive poll cadence (issue #50). A review that is going to finish fast
// deserves a fast answer, so the first two minutes poll every 3 s; after
// that the review is on a long run and 10 s is plenty. The worst 5-minute
// window is therefore 40 + 18 = 58 GETs, against a WAF budget of 300 per IP
// (`infra/lib/nested/waf-stack.ts`, RateLimitPollingEndpoint) — asserted by
// `poll-budget-waf.test.tsx` so neither side can drift alone.
export const POLL_INTERVAL_MS = 3000;
export const POLL_INTERVAL_SLOW_MS = 10000;
export const POLL_FAST_PHASE_MS = 120_000;

/** The poll delay to use `elapsedMs` after the poll loop started. */
export function pollIntervalFor(elapsedMs: number): number {
  return elapsedMs < POLL_FAST_PHASE_MS ? POLL_INTERVAL_MS : POLL_INTERVAL_SLOW_MS;
}

// Capped exponential backoff for retrying a transient poll failure — a
// rejected/errored GET no longer stops polling for good (issue #271 item
// 1); it retries with growing delay, capped, until a response (success or
// terminal status) arrives.
const POLL_BACKOFF_MAX_MS = 30000;

const STILL_CHECKING_COPY = "Still checking on your review's status — reconnecting…";

// Completion-handoff announcements (issue #448). Rendered into a persistent
// polite live region, so assistive tech is already watching it when the review
// lands rather than being handed a region that only appears at the same moment
// its text does. All name the button by its visible label, because the
// announcement's whole job is to tell someone who cannot see the screen where
// the keyboard focus they just received now is.
//
// Issue #492: this copy is now announced ONLY — the region it populates is
// visually hidden (`.ct-sr-only`). A sighted reader never sees "the button
// now has focus" as on-screen prose; that fact matters to someone who can't
// see the screen, and to no one else. What renders visibly instead is the
// outcome headline and the truthful `autoSaved` line below.
//
// READY_FOCUSED_COPY vs READY_SAVED_COPY (issue #466): the automatic save is a
// network round trip that can fail (e.g. a mis-configured outputs bucket —
// #465), so the moment the review lands can only truthfully promise that the
// button has focus, never that a save is already under way. READY_FOCUSED_COPY
// is what's announced immediately; autoSaveOutput below upgrades it to
// READY_SAVED_COPY *only once the fetch has actually resolved*, and leaves a
// visible failure banner (downloadError, DOWNLOAD_ERROR_COPY) instead if it
// hasn't. A UI that announced "saving" before knowing that was untrue was
// exactly the defect #466 fixed.
const READY_FOCUSED_COPY =
  'Your redline is ready — the “Download redline” button now has focus if you need it.';
const READY_SAVED_COPY =
  'Your redline is ready and has been saved to your downloads — the “Download redline” button still has focus if you need it again.';
const READY_GATED_COPY =
  'Your redline is ready, but the adversarial critic flagged this review. Read the flagged points above, then use the “Download redline” button to save it.';
const READY_NO_OUTPUT_COPY =
  'Your review has finished. There is no marked-up document to download.';
// Issue #510 note on the FAILURE path: this region deliberately stays
// DONE-only. A terminal failure already has an owner — the `review-failure`
// CtBanner, which is `variant="danger"` and therefore `role="alert"`, carrying
// cause-and-fix prose written for the specific reason code. Adding a second
// announcement here would recreate on the error path exactly the double
// narration this issue removes from the success path, and would do it worse:
// an assertive alert and a polite region competing over the same event.

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
    fix: 'An admin needs to add funds to the account used under “Models”. Nothing is wrong with your document — resubmit it once that is done.',
  },
  model_key_rejected: {
    cause: 'The model provider rejected the key this deployment is using.',
    fix: 'An admin can replace the key under “Models”. Until then every review will fail the same way.',
  },
  model_rate_limited: {
    cause: 'The model provider is temporarily refusing requests because too many were sent at once.',
    fix: 'Wait a few minutes and submit again. If it keeps happening, an admin should check the account’s limits under “Models”.',
  },
  model_unavailable: {
    cause: 'The model this deployment is set to use is not available from the provider right now.',
    fix: 'Try again later, or ask an admin to select a different model under “Models”.',
  },
  // Issue #472: the pre-call sibling of model_key_rejected above — no key
  // was configured at all, so the review never reached the model. This is
  // the single most likely first-run mistake (upload before setting a key).
  model_key_missing: {
    cause: 'No API key is configured for the model provider, so the review was never sent.',
    fix: 'An admin can add one under “Models”. Until then every review will fail here.',
  },
  model_timeout: {
    cause: 'The model provider did not respond in time, so the review was not completed.',
    fix: 'This is usually temporary — it is worth submitting again. If it keeps happening, an admin should check the account and model under “Models”.',
  },
  // Issue #527: the model returned no usable content at all -- distinct
  // from model_output_truncated below, which has a specific, actionable
  // cause (the output budget ran out) this one does not. Issue #662 checked
  // this copy against the same bar and left it as it stands: an empty
  // response has no budget ceiling behind it, so it is not deterministic the
  // way truncation is, and both levers this sentence offers are real ones --
  // resubmitting can genuinely come back different, and the model an admin
  // picks under "Models" is the model the next review asks for.
  model_empty_content: {
    cause: 'The model returned an empty response, so the review could not be completed.',
    fix: 'This is usually temporary — it is worth submitting again. If it keeps happening, an admin should try a different model under “Models”.',
  },
  // Issue #527 introduced this token when a reasoning-class model spending
  // its budget on internal reasoning was the observed cause, and its copy
  // sent the reader after that model's "reasoning allowance". Issue #662
  // corrected it: that allowance is not the lever here. Every ROLE pin in
  // model-policy/openrouter.json declares `reasoning_max_tokens: 0`, and the
  // allowance is only ever ADDED to `max_tokens` in model_client's request
  // payload -- so on the pinned primary and critic it contributes nothing.
  // That zero is a claim about the ROLE PINS, not about the whole matrix:
  // the `selectable` list carries two entries that DO declare an allowance
  // (google/gemini-3.1-pro-preview and moonshotai/kimi-k3) -- the same two
  // `model_client.openrouter_reasoning_max_tokens`'s docstring names as
  // spending budget on reasoning before content.
  //
  // What actually runs out is the CONTENT budget:
  // model_client.output_budget_for_document sizes it from the document, and
  // model_client.widen_output_budget then adds one widen step
  // (`OUTPUT_BUDGET_WIDEN_STEP_TOKENS`) on the single truncation retry
  // scripts/primary_review_pass.py grants (`MAX_TRUNCATION_RETRIES_PER_PASS
  // = 1`, issue #658). On the DEFAULT pins that document-sized budget plus
  // its one widen is what binds -- NOT the model's declared output cap: the
  // pinned primary and critic both declare `max_output_tokens: 128000`,
  // while the five-page agreement #658's derivation is sized against
  // (~4,100 body tokens) runs its budget from ~26k to ~42k and stops well
  // short of that ceiling.
  //
  // Two consequences for the copy below. A bare retry is still a wasted
  // round trip -- the budget is a deterministic function of the document,
  // so an unchanged resubmit is sized identically and widened identically,
  // and the pass has already spent that widen before this token is
  // recorded. But the fix must NOT promise a bigger model: no `selectable`
  // entry declares a cap above the default primary's, the Models screen
  // never renders `max_output_tokens` (AdminModel.tsx's `SelectableModel`
  // carries no such field), and on the shipped default the binding number
  // is a code constant with no operator-facing knob at all. So the honest
  // lever is whoever operates the deployment raising the output budget a
  // review is allowed to write. Still an operator/config problem rather
  // than a fault in the document, which is why it stays in this section.
  model_output_truncated: {
    cause:
      'Reviewing your document needed more room to write than the model was allowed, so the answer was cut off before it could be finished.',
    fix: 'Sending the same document again will run into the same limit, so a retry will not help — whoever operates this deployment has to raise the output budget a review is allowed to write.',
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
  // Issue #530: this used to say "Your file could not be read as a Word
  // document" / "Upload a .docx file saved by Word — not a PDF, an older
  // .doc, or a scan" — flatly wrong for the tracked-changes refusal this
  // token actually names most often: a genuine .docx that carries a
  // malformed (textless) revision record somewhere in it. Telling someone
  // to re-save a real .docx as a .docx is a dead end. The specific
  // paragraph and what the tool found (detail.normalization_notes, the
  // SAME per-paragraph disclosure the accepted-changes receipt line uses
  // on a successful review) is rendered alongside this copy, below.
  unnormalizable_input: {
    cause: 'A paragraph in your document has a tracked change the tool could not safely read.',
    fix: 'See the paragraph named below. In Word, review that tracked change directly — accept or reject it so it carries real text — then upload the document again. If the paragraph looks fine to you, contact an admin.',
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
    fix: 'This has been recorded. Please try again; if it keeps happening, an admin should try a different model under “Models”.',
  },
  // Issue #670: the first review pass answered in the right shape but kept
  // mis-copying your document's own wording into its proposed changes, so
  // its whole retry budget went without a usable answer. Deliberately NOT
  // the same token as `block_transcript_rejected` below, which is the
  // marked-up-document stage failing the same proof AFTER a review was
  // produced: there the findings exist and only the file is missing; here
  // there is no review at all. Before this token both of those, and the
  // structured-output failure above, stored a null reason and rendered the
  // vague `STAGE_EXPLANATIONS.run_review` fallback.
  primary_block_transcript_rejected: {
    cause: 'The review kept quoting your document inaccurately when drafting its changes, so it was stopped rather than finished against wording that does not match your file.',
    fix: 'Nothing is wrong with your document. This has been recorded — please try again; if it keeps happening, an admin should try a different model under “Models”.',
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
  // --- The operator's problem: the activated playbook itself (issue #479) -
  // scripts/review_spine.py's REASON_OPF_KNOWLEDGE_REFUSED /
  // REASON_OPF_DIGEST_MISSING / REASON_FLOOR_INVARIANT_UNJUDGED /
  // REASON_FLOOR_INVARIANT_TRUNCATED -- all four are fail-closed outcomes of
  // trying to compose the activated OPF playbook into a review, never
  // something wrong with the submitted document.
  opf_knowledge_refused: {
    cause: 'The contract type you submitted for is set up in a way the tool cannot honestly turn into review instructions.',
    fix: 'Nothing is wrong with your document. An admin needs to check how this contract type is configured before it can be reviewed.',
  },
  opf_digest_missing: {
    cause: 'The contract type you submitted for is missing the reference material the review needs, so it was not reviewed.',
    fix: 'Nothing is wrong with your document. An admin needs to fix or re-upload this contract type before it can be reviewed.',
  },
  floor_invariant_unjudged: {
    cause: 'One of this contract type’s required rules could not be checked, so the review was stopped rather than finish with a rule unverified.',
    fix: 'This has been recorded. It is worth submitting again; if it keeps happening, an admin should check the account and model under “Models”.',
  },
  // Issue #682: the SAME stopped-with-a-rule-unverified outcome as
  // `floor_invariant_unjudged` above, split off because its fix is the
  // opposite one. That token's copy says to submit again, which is right for
  // a model that answered unreadably and wrong here: the rule check ran out
  // of room to write before it could answer, and the room it is given is a
  // fixed budget, so the same document re-sent is cut off at the same place.
  // The lever is the operator's, exactly as in `model_output_truncated`
  // above — deliberately the same two sentences, because it is the same
  // deterministic limit, just reached by the rule check rather than by the
  // review itself.
  floor_invariant_truncated: {
    cause: 'Checking one of this contract type’s required rules needed more room to write than the model was allowed, so the answer was cut off and the review was stopped rather than finish with a rule unverified.',
    fix: 'Nothing is wrong with your document, and sending it again will run into the same limit — whoever operates this deployment has to raise the output budget the rule check is allowed to write.',
  },
  // Issue #584: the review found changes to request but produced no
  // marked-up document to deliver them in (every proposed change came back
  // flag-only, with nothing to place into the file). Distinct from
  // quote_patches_not_applied above: that token means changes WERE
  // attempted and failed to place; this one means nothing was ever
  // attempted because there was no located text to change.
  redline_not_persisted: {
    cause: 'The review found changes to request, but no marked-up document was produced to deliver them in.',
    fix: 'This has been recorded for an admin to investigate. Please try again; if it keeps happening, contact an admin.',
  },
  // Issue #665: the second review pass — the one that checks the first
  // pass's work — spent its whole retry budget without returning a usable
  // result. `scripts/review_spine.py` splits that terminal into three tokens
  // by the fixed-vocabulary token half of the pass's own `last_error`, so an
  // admin reading the Diagnostics tab gets a lead rather than a shrug.
  //
  // Before these tokens existed the spine stored the STAGE NAME "critic" in
  // `reason`; no key here matched it, so every critic failure fell through
  // to the `run_review` stage copy — "the exact cause was not identified" —
  // and a real paid production review could not be diagnosed from the
  // Diagnostics tab that exists to answer exactly that question.
  //
  // The copy deliberately never fails the pass by name to the reader:
  // "critic" is internal architecture, and a submitter has no lever on which
  // pass broke. Distinct from `structured_output_retry_exhausted` above
  // (which the backend classifier records when the failure is not attributed
  // to a pass) only in that these name WHICH pass — the point of the tokens.

  // The critic's answers parsed, but the output contract rejected them.
  // Issue #673 measured this class against live traffic: with structured
  // output OFF the critic invented a property the schema forbids, on two
  // consecutive attempts, and the review terminated here. The admin lead is
  // therefore schema enforcement and the model behind it, not the document.
  critic_schema_invalid: {
    cause: 'The second review pass, which checks the first one’s work, kept answering in the wrong shape — so the review was stopped rather than finished on one unchecked pass.',
    fix: 'This has been recorded. It is worth submitting again; if it keeps happening, an admin should check under “Models” that the checking pass is on a model that can be held to the required answer format.',
  },
  // The critic's answers were not readable as an answer at all — the
  // prose-preamble / markdown-fence class. A different lead from the shape
  // failure above, which is why it is a different token.
  critic_invalid_json: {
    cause: 'The second review pass, which checks the first one’s work, kept answering with something the system could not read at all — so the review was stopped rather than finished on one unchecked pass.',
    fix: 'This has been recorded. It is worth submitting again; if it keeps happening, an admin should try a different model for the checking pass under “Models”.',
  },
  // The residual: the pass gave up, and its recorded error was not one of
  // the two classes above (or none was recorded). Says only what is known.
  critic_retry_exhausted: {
    cause: 'The second review pass, which checks the first one’s work, kept returning a result the system could not use — so the review was stopped rather than finished on one unchecked pass.',
    fix: 'This has been recorded. It is worth submitting again; if it keeps happening, an admin should try a different model for the checking pass under “Models”.',
  },
  // Issue #665 item 4: three more tokens `scripts/review_spine.py` can put
  // on a result that had no copy here at all, found by the guard test this
  // issue added (tests/test_critic_failure_reason_665.py) rather than by
  // another production incident. All three are block-mode redline failures
  // (`scripts/redline_generate.py` / `scripts/redline_block_apply.py`), and
  // all three left the reader on the same "cause not identified" fallback
  // this issue is about.
  block_transcript_rejected: {
    cause: 'The changes the review proposed did not line up with the document they were meant for, so none of them were written into it.',
    fix: 'This is a fault in the tool, not in your document. It has been recorded — please try again, and contact an admin if it keeps happening.',
  },
  block_edits_not_applied: {
    cause: 'The review found changes to request, but none of them could be written into your document, so no marked-up copy was produced.',
    fix: 'The changes themselves are listed for you to apply by hand. It is worth submitting again; if it keeps happening, contact an admin.',
  },
  projection_verification_failed: {
    cause: 'The marked-up document failed the tool’s own check that it changes only what the review asked for, so it was not released.',
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
    fix: 'An admin can add one under “Models”. Until then every review will fail here.',
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
      'model under “Models”; it is also worth re-submitting in case it was ' +
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

// Issue #592: "a"/"an" for the classifier's type guess. NOT a fixed
// vocabulary: known_agreement_types() (scripts/preflight_pass.py) is built
// per request from the ACTIVE version of every installed playbook the
// catalog shows -- as of issue #659 there is no shipped list of contract
// types at all -- so an admin can put an arbitrary label here at any time
// by uploading a playbook, and this first-letter check gets a
// spelled-as-it-sounds label right
// ("Employment Agreement" -> "an") but still mis-articles an acronym like
// "NDA" (pronounced with a leading vowel sound despite the consonant
// letter); that gap is untracked and out of this ticket's scope. The one
// label this file DOES need to get exactly right, the classifier's own
// unclassified fallback ("Other" — matches
// scripts/preflight_pass.py::UNCLASSIFIED_AGREEMENT_TYPE), is special-cased
// in describeAgreementTypeGuess below instead of routed through here at
// all, because "an Other" is not idiomatic English no matter which article
// precedes it.
function withIndefiniteArticle(label: string): string {
  const article = /^[aeiou]/i.test(label) ? 'an' : 'a';
  return `${article} ${label}`;
}

// Mirrors scripts/preflight_pass.py::UNCLASSIFIED_AGREEMENT_TYPE — the
// classifier's fallback label when nothing in the vocabulary fits.
const UNCLASSIFIED_AGREEMENT_TYPE = 'Other';

// The noun phrase used everywhere a type guess reads like "This reads like
// ___". Every call site routes through here (not through
// withIndefiniteArticle directly) so the "Other" special case in the
// warning banners, the neutral type/side line, and any future caller of
// this phrase can never drift out of sync with each other again.
function describeAgreementTypeGuess(label: string): string {
  if (label === UNCLASSIFIED_AGREEMENT_TYPE) {
    return 'an unrecognized type';
  }
  return withIndefiniteArticle(label);
}

// Which side's paper this reads like, in the neutral, factual phrasing the
// issue's Context insists on: "the toaster reviews both first- and
// third-party paper -- 'this isn't our template' is never a mismatch
// signal." Neither answer is flagged as better or worse; "unclear" says
// nothing at all rather than guessing.
function describePaperSide(paperSide: PreflightResult['paperSide']): string {
  if (paperSide === 'ours') {
    return 'on your own paper';
  }
  if (paperSide === 'counterparty') {
    return "on the counterparty's paper";
  }
  return '';
}

// Helper to find an installed, active playbook matching an agreement type guess
function findMatchingPlaybook(
  typeGuess: string | null | undefined,
  playbooks: PlaybookCatalogEntry[],
  currentPlaybookId: string | null,
): PlaybookCatalogEntry | null {
  if (!typeGuess || typeGuess === UNCLASSIFIED_AGREEMENT_TYPE) return null;
  const normalizedGuess = typeGuess.toLowerCase().trim();
  return (
    playbooks.find((p) => {
      if (p.status !== 'active' || p.playbook_id === currentPlaybookId) return false;
      const normName = p.display_name.toLowerCase();
      const normId = p.playbook_id.toLowerCase();
      return (
        normName === normalizedGuess ||
        normId === normalizedGuess ||
        normName.includes(normalizedGuess) ||
        normalizedGuess.includes(normName) ||
        (normalizedGuess.includes('nda') && (normId.includes('nda') || normName.includes('nda'))) ||
        (normalizedGuess.includes('non-disclosure') && (normId.includes('nda') || normName.includes('nda'))) ||
        (normalizedGuess.includes('dpa') && (normId.includes('dpa') || normName.includes('dpa'))) ||
        (normalizedGuess.includes('data protection') && (normId.includes('dpa') || normName.includes('dpa'))) ||
        (normalizedGuess.includes('msa') && (normId.includes('msa') || normName.includes('msa'))) ||
        (normalizedGuess.includes('master services') && (normId.includes('msa') || normName.includes('msa')))
      );
    }) ?? null
  );
}

export interface ReviewSubmissionProps {
  /**
   * Issue #464: a plain refresh signal (not the catalog itself — see
   * App.tsx's `catalogVersion` comment for why a full state lift wasn't
   * worth the props-contract churn). App.tsx bumps this after an admin
   * rename/remove/activate/rollback lands in AdminPlaybooks, so this
   * component's own `fetchCatalog` (below) re-runs and the dial reflects it
   * without a reload. Optional and defaulted so every existing render of
   * this component with no props (all current tests) is unaffected.
   */
  catalogVersion?: number;
}

export default function ReviewSubmission({
  catalogVersion = 0,
}: ReviewSubmissionProps = {}): React.ReactElement {
  const [file, setFile] = useState<File | null>(null);
  const [reviewId, setReviewId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  /**
   * "Check now" (issue #726). The poller already retries on capped
   * exponential backoff, so this is not a second polling implementation — it
   * is a nonce in the poll effect's dependency list. Bumping it tears the
   * effect down (cancelling the in-flight attempt and clearing the pending
   * timer through the existing cleanup) and starts it again, which polls
   * immediately and resets the backoff. Its VALUE carries no data, in the
   * same idiom as `catalogVersion`.
   */
  const [pollNonce, setPollNonce] = useState(0);
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
  // Markup intensity (issue #495). Automatically persisted to localStorage
  // across sessions so whatever the reviewer picks stays changed.
  const [browning, setBrowning] = useState<BrowningLevel>(readLastBrowning);

  // Footnote audience (issue #523, epic #519 item F).
  // Automatically persisted across sessions: saved locally and synced to
  // account preferences in the background so the user's choice stays changed.
  const userChangedNotesModeRef = useRef(false);
  const [notesMode, setNotesMode] = useState<NotesMode>(readLastNotesMode);
  const [notesModeInternalAvailable, setNotesModeInternalAvailable] = useState(false);
  const [notesModeSaveError, setNotesModeSaveError] = useState<string | null>(null);
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
  // Issue #489: seeded from the last-selected playbook id (if any was ever
  // stored), not an empty string. `fetchCatalog` below already keeps the
  // CURRENT selection when it is still a loaded, active entry and otherwise
  // falls back to the first active one (issue #464) — seeding from storage
  // here, rather than special-casing it in fetchCatalog, means that exact
  // validate-or-fall-back logic does the work for a remembered id too: a
  // playbook an admin removed since the last visit degrades silently to the
  // default, never an error.
  const [playbookId, setPlaybookId] = useState<string>(() => readLastPlaybookId() ?? '');
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [submittedPlaybookLabel, setSubmittedPlaybookLabel] = useState<string | null>(null);
  // Issue #492: the quiet meta line's filename, frozen at submit time the
  // same way `submittedPlaybookLabel` is. `file` itself cannot be read at
  // render time instead — a reviewer can pick a NEW file while a previous
  // review's result panel is still on screen (nothing here clears the file
  // picker on success), and the meta line must keep naming the document that
  // review was actually about, not whatever is currently sitting in the
  // drop zone.
  const [submittedFilename, setSubmittedFilename] = useState<string | null>(null);

  // Issue #491: the "What we're looking at" preflight card. `preflightFor`
  // records which file (name+size+lastModified) the IN-FLIGHT full request
  // was fired for, so a slow response for a file the reviewer already
  // replaced is dropped on arrival instead of rendering as if it described
  // the current selection. `preflightMatchPlaybookId` records which
  // playbookId the CURRENT `preflight.match` was actually computed
  // against — fix round 1 (issue #491): a dial change no longer re-fires
  // the full request (which re-uploads the whole file and re-pays for the
  // cheap-model call); it only refreshes the match verdict via the cheap
  // `/api/reviews/preflight/match` route, and this ref is how that second
  // effect knows a refresh is actually owed rather than firing on every
  // render `preflight` happens to change on (including its own update).
  const [preflight, setPreflight] = useState<PreflightResult | null>(null);
  const preflightFor = useRef<string | null>(null);
  const preflightMatchPlaybookId = useRef<string | null>(null);

  // Issue #730 (owner decision H5): the preflight recommendation selects the
  // playbook automatically, SILENTLY — no banner, no confirmation, no Undo.
  // The dial and the selected name simply reflect the choice.
  //
  // `playbookSelection` is the record of WHO chose the playbook currently on
  // the dial, and it is also the `userOverride` input the vendored helper
  // (`orbit-diner/autoPlaybook.ts`) reads: `'user'` means the reviewer picked
  // it by hand for THIS document, which no recommendation may reverse.
  // `undefined` is the honest starting value — an initial, default or
  // remembered choice (`readLastPlaybookId`, or fetchCatalog's fall back to
  // the first active entry) is NOT a manual override for a newly selected
  // file, so neither of those paths writes here.
  const [playbookSelection, setPlaybookSelection] = useState<'automatic' | 'user' | undefined>(
    undefined,
  );
  // A unique in-memory key per file SELECTION, not per file identity: two
  // different File objects with the same name/size/lastModified are two
  // selections, and the second must not inherit the first's in-flight
  // response. `preflightFor` (the current file's key) and
  // `preflightResponseFor` (the key the landed `preflight` actually belongs
  // to) are the helper's `currentFileKey`/`responseFileKey` pair.
  const fileSelectionSeq = useRef(0);
  const preflightResponseFor = useRef<string | null>(null);
  // Which file key has already had a recommendation applied. One automatic
  // choice per document: without this, `findMatchingPlaybook` — which
  // excludes the CURRENT selection — could hand back the entry we just moved
  // away from on the next render and ping-pong between two playbooks that
  // both match one guess.
  const autoPlaybookAppliedFor = useRef<string | null>(null);

  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Completion handoff (issue #448). `readyAnnouncement` populates a live
  // region that is mounted from first render; `handedOffReviewRef` remembers
  // which review has already been handed off so the announcement, the focus
  // move and the automatic save happen exactly once per review — never again
  // on a re-render or a late poll. (The focus move reaches the console's save
  // key by its testid — see the handoff effect below; the `saveControlRef`
  // wrapper it used to hold was deleted with the old tree in #727.)
  const [readyAnnouncement, setReadyAnnouncement] = useState('');
  // Issue #492: the ONE truthful, visible save line (REDLINE_SAVED_COPY) is
  // gated on this, set true only once autoSaveOutput's fetch has actually
  // resolved for the review currently on screen — never optimistically, and
  // never for a stale save that resolves after a resubmit (same
  // savingReviewId-vs-handedOffReviewRef guard autoSaveOutput already uses
  // for the announcement and downloadError below).
  const [autoSaved, setAutoSaved] = useState(false);
  const handedOffReviewRef = useRef<string | null>(null);

  // Issue #492: "Copy review ID" replaces the raw UUID this panel used to
  // print in every state (RUNNING and finished alike) — the id is useful for
  // support/diagnostics correlation, not something a reviewer needs to read
  // off the screen. Mirrors AdminDiagnostics.tsx's copyReviewId: guard
  // `navigator.clipboard` being absent (a non-secure-context origin, e.g. the
  // DTS Docker Compose target reached over plain HTTP) rather than letting a
  // missing `.writeText` throw synchronously in the click handler.
  const [copiedReviewId, setCopiedReviewId] = useState(false);
  const copyReviewId = useCallback((id: string) => {
    const clipboard = navigator.clipboard;
    if (!clipboard?.writeText) {
      return;
    }
    void clipboard
      .writeText(id)
      .then(() => {
        setCopiedReviewId(true);
        window.setTimeout(() => setCopiedReviewId(false), 2000);
      })
      .catch(() => {
        // Clipboard permission denied. Nothing further to do — there is no
        // visible id text for the reviewer to select manually instead.
      });
  }, []);

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
      // Every registered playbook reaches the console's playbook control,
      // which renders the unactivated ones as `disabled` "· coming soon"
      // options (`orbit-diner/OrbitDiner.tsx`; `projection.ts` applies the
      // same `status === 'active'` gate to the recommendation). Two things
      // are true at once: a
      // registered-but-unactivated playbook can't be reviewed against
      // (run_real_pipeline fails closed at load_playbook), so offering it as
      // a *choice* only invites a guaranteed 503 — but it is still real,
      // published intent, and the dial is the product's roadmap as much as
      // its control. So: visible, not selectable. The catalog endpoint
      // remains the authority on `status`; this is presentation only.
      const entries = data.playbooks ?? [];
      setPlaybooks(entries);
      setCatalogError(null);
      // Default to the first LOADED type — never park the selection on a
      // stop the user isn't allowed to pick. This also re-runs on a refetch
      // (issue #464, catalogVersion above): keep the current selection when
      // it is still a loaded type (e.g. a rename left the same playbook_id
      // selected), but fall back to the new first-loaded type when it isn't
      // any more (e.g. an admin removed the selected playbook) — never leave
      // `playbookId` pointing at an entry that no longer exists or is no
      // longer active, which the dial would render as nothing checked.
      const firstActive = entries.find((entry) => entry.status === 'active');
      setPlaybookId((current) =>
        entries.some((entry) => entry.playbook_id === current && entry.status === 'active')
          ? current
          : firstActive?.playbook_id || '',
      );
    } catch (err) {
      setCatalogError(
        friendlyErrorMessage(err, "We couldn't load the list of contract types right now."),
      );
    }
  }, []);

  // catalogVersion (issue #464) is a plain counter bumped by App.tsx after
  // an admin mutation — its VALUE carries no data, only "refetch now".
  useEffect(() => {
    void fetchCatalog();
  }, [fetchCatalog, catalogVersion]);

  // Issue #489: persist every change to the selection — a direct pick on the
  // dial, or fetchCatalog's own fallback above when the stored/current id is
  // no longer loaded and active. Writing on the fallback too (rather than
  // only on a user-driven change) is what makes "remove B, reload -> default,
  // no error" actually stick: without this, the next reload would seed
  // straight back from the now-stale 'B' still sitting in storage. A blank
  // id (nothing loaded yet, or no active playbook at all) is never written —
  // there is nothing worth remembering yet.
  useEffect(() => {
    if (playbookId) {
      writeLastPlaybookId(playbookId);
    }
  }, [playbookId]);

  // Issue #523: seed the footnote-audience control from this user's stored
  // preference. Server-side and not localStorage on purpose — a client-only
  // preference cannot survive a sign-out, and this repo persists nothing
  // security-relevant in the browser anyway (see notesMode.ts / the epic).
  //
  // Failure posture matches `fetchCatalog`'s neighbours: a preferences read
  // that fails leaves the documented default in place and renders no error.
  // The control still works for this review; only the remembered default is
  // missing, and an error banner for that would be noise on a panel whose
  // job is toasting a contract.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await authorizedFetch('/api/me/preferences');
        if (!response.ok) return;
        const body = (await response.json()) as {
          preferences?: { notes_mode?: unknown };
          notes_mode_available?: unknown;
        };
        if (cancelled) return;
        const available = body.notes_mode_available === true;
        setNotesModeInternalAvailable(available);
        const stored = body.preferences?.notes_mode;
        // A stored mode this deployment can no longer offer falls back
        // to the default rather than preselecting an unavailable stop.
        if (isNotesMode(stored) && isNotesModeAvailable(stored, available)) {
          if (!userChangedNotesModeRef.current) {
            setNotesMode(stored);
          }
        }
      } catch {
        /* preferences are a nicety; the control's default already works */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // What the next review costs (issue #653, epic #649).
  //
  // Epic #649's through-line is that the UI should answer "why is the product
  // behaving this way?" without reading source. The cost of pressing this
  // button was not answerable at all: it is a function of the admin's model
  // selection and the pricing artifact, both of which live server-side, and on
  // 2026-09-01 the number had to be computed by hand after the fact.
  //
  // Read from `/api/review-cost-estimate`, whose projection is an allowlist of
  // two cost figures: it carries no instance-wide totals, no cap and no model
  // ids — those stay admin-only on the Settings tab. A reviewer needs the price
  // of the thing they are about to do; they do not need the deployment's
  // ledger.
  //
  // Silent on failure, like the preferences load above: this panel's job is
  // toasting a contract, and an error banner because a price line could not be
  // fetched would be noise. The line simply does not render.
  const [reviewCostUsdCents, setReviewCostUsdCents] = useState<number | null>(null);
  // The estimate CAPTURED for the review that was actually submitted (issue
  // #735, owner decision H2) — the same "freeze the submitted value" pattern as
  // `submittedPlaybookLabel` and `submittedFilename` above. The route this
  // panel reads answers for the NEXT review, so a finished review has to keep
  // showing what IT was quoted at rather than a number fetched afterwards for a
  // different document. `undefined` means nothing has been submitted yet;
  // `null` records a submit made with no estimate available, which is not the
  // same thing and must not fall back to the live figure.
  const [submittedEstimateCents, setSubmittedEstimateCents] = useState<
    number | null | undefined
  >(undefined);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await authorizedFetch('/api/review-cost-estimate');
        if (!response.ok) return;
        const body = (await response.json()) as { estimated_usd_cents?: unknown };
        if (cancelled) return;
        // Strict number check AND a positivity check. A backend older than
        // this bundle answers this route with a 404 body, so
        // `Number(undefined)` would render "$NaN" on a spend line; and the
        // field is legitimately `null` on a provider carrying no per-review
        // basis (the Bedrock path — see `reviews.estimate_review_usd_cents`),
        // where "$0.00" would claim a review is free. Anything that is not a
        // positive number of cents renders nothing at all, which is the
        // honest answer and the one this line degrades to anyway.
        if (typeof body.estimated_usd_cents === 'number' && body.estimated_usd_cents > 0) {
          setReviewCostUsdCents(body.estimated_usd_cents);
        }
      } catch {
        /* the price is a courtesy; the button works without it */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // The browning change WITHOUT its sound. Split out for issue #722: the
  // console's markup-intensity radio reports the same click as a `key` motion
  // event, which the one audio owner already sounds, so the caller that goes
  // through the console must be able to move the setting silently. Every other
  // caller wants the detent and uses `handleBrowningChange` below.
  const applyBrowningChange = useCallback((level: BrowningLevel): boolean => {
    if (level === browning) return false;
    setBrowning(level);
    writeLastBrowning(level);
    return true;
  }, [browning]);

  const handleBrowningChange = useCallback((level: BrowningLevel) => {
    if (applyBrowningChange(level)) playDetent();
  }, [applyBrowningChange, playDetent]);

  // Issue #523 / Review Tab Defaults: auto-save footnote preference in the
  // background and persist to localStorage whenever the control moves.
  const saveNotesModePreference = useCallback(async (modeToSave: NotesMode): Promise<void> => {
    setNotesModeSaveError(null);
    try {
      const response = await authorizedFetch('/api/me/preferences', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preferences: { notes_mode: modeToSave } }),
      });
      if (!response.ok) {
        const detail = await readErrorDetail(response);
        throw new Error(detail ?? 'preferences save rejected');
      }
    } catch (err) {
      setNotesModeSaveError(
        friendlyErrorMessage(err, "We couldn't save that as your default just now."),
      );
    }
  }, []);

  const handleNotesModeChange = useCallback(
    (mode: NotesMode) => {
      // Issue #572's kill switch, enforced HERE and not only by whatever the
      // rendered control does with it (issue #733). The legacy dial refuses an
      // unavailable stop in its own click handler; the console expresses the
      // same refusal as a `disabled` native radio, which is one attribute away
      // from a mode this deployment does not support being written into a
      // preference and submitted. The rule belongs with the state it guards.
      if (!notesModeInternalAvailable && (mode === 'internal' || mode === 'both')) {
        return;
      }
      userChangedNotesModeRef.current = true;
      setNotesMode(mode);
      setNotesModeSaveError(null);
      writeLastNotesMode(mode);
      void saveNotesModePreference(mode);
    },
    [notesModeInternalAvailable, saveNotesModePreference]
  );

  // Issue #491: fire the cheap preflight check the moment a file is chosen.
  // Deliberately NOT part of `submitReview` and never awaited by anything
  // that gates the Upload button: "the card must not delay anything ... the
  // Upload button is never gated on preflight" (issue's own words).
  // `runPreflight` never throws (see preflight.ts's docstring) and resolves
  // to `null` on any failure, so there is no error path here to wire up — a
  // `null` result simply renders no card, same as "no file chosen yet".
  //
  // Fix round 1 (issue #491): keyed and depended on `file` ONLY —
  // `render_preflight_user_prompt` (backend/src/review_routes.py) receives
  // just the document excerpt, never `playbookId`, so re-running this full,
  // whole-file-uploading request on every dial change bought nothing but
  // repeated spend and bandwidth. `playbookId` is still read here (the
  // guess needs SOME playbook to verdict against on first load), just not
  // watched — the effect below owns every LATER playbookId change.
  useEffect(() => {
    if (!file) {
      setPreflight(null);
      preflightFor.current = null;
      preflightResponseFor.current = null;
      preflightMatchPlaybookId.current = null;
      // Issue #730: a document leaving the screen ends its automatic choice
      // too. `resetForRetry` clears the file, so "Toast another slice" starts
      // the next document with no override and no applied recommendation
      // carried over from the last one.
      autoPlaybookAppliedFor.current = null;
      setPlaybookSelection(undefined);
      return;
    }
    // Issue #730: the sequence number is what makes this key unique per
    // SELECTION rather than per file identity — replacing a file with an
    // identical-looking one is a new document, and its recommendation must
    // not be satisfied by the previous request's response.
    fileSelectionSeq.current += 1;
    const key = `${fileSelectionSeq.current}:${file.name}:${file.size}:${file.lastModified}`;
    preflightFor.current = key;
    preflightResponseFor.current = null;
    preflightMatchPlaybookId.current = playbookId;
    // A genuinely new document: whatever the reviewer chose for the PREVIOUS
    // one is not an override for this one (issue #730, Scope).
    autoPlaybookAppliedFor.current = null;
    setPlaybookSelection(undefined);
    setPreflight(null);
    void runPreflight(file, playbookId).then((result) => {
      if (preflightFor.current === key) {
        preflightResponseFor.current = key;
        setPreflight(result);
      }
    });
    // playbookId is intentionally read but not a dependency here — see the
    // comment above: only the FIRST fetch for a given file uses it directly;
    // every later change is handled by the match-only effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file]);

  // Fix round 1 (issue #491): the SEPARATE, cheap half of what a dial
  // change used to buy via the full effect above — recomputes just the
  // match verdict (`POST /api/reviews/preflight/match`, no file, no
  // cheap-model call) against the NEW playbookId, keeping the already-
  // classified `agreementTypeGuess` from the last full response. Guarded by
  // `preflightMatchPlaybookId` so this never fires for the playbookId the
  // current `preflight.match` already reflects — including its own update
  // below, which changes `preflight` and would otherwise re-run this same
  // effect on its own output.
  useEffect(() => {
    if (!file || !preflight || preflight.classification !== 'ok') {
      return;
    }
    if (preflightMatchPlaybookId.current === playbookId) {
      return;
    }
    // Captured at request time: a LATER file swap or a SECOND dial flip
    // before this resolves must not let a now-stale response overwrite
    // whatever the current selection actually reflects.
    const requestedFileKey = preflightFor.current;
    const requestedPlaybookId = playbookId;
    preflightMatchPlaybookId.current = requestedPlaybookId;
    void refreshMatchVerdict(preflight.agreementTypeGuess, requestedPlaybookId).then((match) => {
      if (
        preflightFor.current === requestedFileKey &&
        preflightMatchPlaybookId.current === requestedPlaybookId
      ) {
        setPreflight((current) => (current ? { ...current, match } : current));
      }
    });
  }, [file, playbookId, preflight]);

  /**
   * Issue #730: the ONE way a reviewer's own choice reaches `playbookId`.
   *
   * Every manual affordance routes here — the dial, the console's select and
   * its browse list, the mismatch banner's "Switch to X" and the `R`
   * shortcut — so "a manual selection always wins" is a property of one
   * function rather than a rule each call site has to remember. `fetchCatalog`
   * deliberately does NOT: its fall back to the first active entry when the
   * remembered id is gone is the app choosing, not the reviewer.
   */
  const choosePlaybookManually = useCallback((nextPlaybookId: string) => {
    setPlaybookSelection('user');
    setPlaybookId(nextPlaybookId);
  }, []);

  /**
   * Issue #730 (owner decision H5): apply the preflight recommendation to the
   * dial, silently.
   *
   * The decision itself belongs to the vendored, PURE helper
   * (`orbit-diner/autoPlaybook.ts`) — it makes no model call, fetches nothing,
   * writes no preference and invents no confidence threshold. Everything here
   * is the adapter's half of that contract: supply the current selection, the
   * recommendation resolved by the SAME `findMatchingPlaybook` the `R`
   * shortcut and the verdict card use, the file keys, the override flag and
   * whether submission has started.
   *
   * It used to be gated on the build-time Orbit Diner switch, so that with the
   * console off a mismatch was still answered by the old tree's advisory
   * banner and its explicit "Switch to X" button. #727 deleted that tree, and
   * with it the only alternative answer: the console is the Review tab now,
   * and a guard whose "off" arm defers to deleted markup would leave a real
   * reviewer's mismatch unanswered. So the effect is unconditional, and #728
   * retired the switch itself.
   *
   * `working` is `submitting || reviewId !== null` — the two states that mean
   * a submission has started. Once it has, the playbook of an in-flight or
   * completed review is not this effect's to change (issue #730, Out of
   * scope), and the id already went to the server with the POST.
   *
   * No storage of its own: an automatic choice reaches `writeLastPlaybookId`
   * through the SAME `playbookId` effect every other change does (issue
   * #489, which deliberately persists the app's own fall back as well as a
   * reviewer's pick), so this adds no key and no second write path.
   */
  useEffect(() => {
    if (!preflight) {
      return;
    }
    const currentFileKey = preflightFor.current ?? '';
    // One automatic choice per document. Re-running would let a second
    // playbook that also matches the guess take over on the next render.
    if (autoPlaybookAppliedFor.current === currentFileKey) {
      return;
    }
    const recommended = findMatchingPlaybook(
      preflight.classification === 'ok' ? preflight.agreementTypeGuess : null,
      playbooks,
      playbookId,
    );
    const chosen = preflightPlaybookChoice({
      selectedId: playbookId,
      recommendedId: recommended?.playbook_id,
      classification: preflight.classification,
      currentFileKey,
      responseFileKey: preflightResponseFor.current ?? '',
      userOverride: playbookSelection === 'user',
      working: submitting || reviewId !== null,
      playbooks,
    });
    // No usable recommendation resolved: keep the current selection, and with
    // it the existing advisory warning and switch action (issue #730, Scope).
    if (chosen === playbookId) {
      return;
    }
    autoPlaybookAppliedFor.current = currentFileKey;
    setPlaybookSelection('automatic');
    setPlaybookId(chosen);
  }, [preflight, playbookId, playbooks, playbookSelection, submitting, reviewId]);

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) {
      clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  // Issue #489, item 2: reattach to a running review after a reload.
  // `reviewId` lives only in this component's in-memory state, so a reload
  // during a RUNNING review used to leave the reviewer looking at a blank
  // submit form while the pipeline kept going server-side -- the redline
  // eventually surfaced in History with nothing shown in between. On mount,
  // ask for the caller's own reviews (the same `?scope=mine` listing History
  // itself uses -- newest first, backend/src/reviews.py's list_reviews) and,
  // if the most recent one is still non-terminal, attach to it exactly like
  // a fresh submission would: setting `reviewId` is enough, since the poll
  // effect below and every render that follows already key off it alone.
  //
  // Deliberately does NOT resurrect a TERMINAL review that finished while
  // the reviewer was away -- that is History's job (issue #449's own scope),
  // and showing a finished review inside the submit panel would misrepresent
  // it as still in flight.
  //
  // Mount-only ([] deps). A resubmit sets its own `reviewId` through
  // `submitReview`, which this must never clobber -- guarded by scoping the
  // eventual update to "only if nothing is already tracked"
  // (`current ?? …`), on top of the fact that this effect fires once, before
  // any user gesture could reach `submitReview` in the first place.
  useEffect(() => {
    let cancelled = false;

    async function reattach(): Promise<void> {
      try {
        const response = await authorizedFetch('/api/reviews?scope=mine');
        if (!response.ok) {
          return;
        }
        const data = (await response.json()) as { reviews?: unknown };
        const rows = Array.isArray(data.reviews)
          ? (data.reviews as Array<{ review_id: string; status: string }>)
          : [];
        const running = rows.find((row) => NON_TERMINAL_STATUSES.has(row.status));
        if (!cancelled && running) {
          setReviewId((current) => current ?? running.review_id);
        }
      } catch {
        // Best-effort only -- a failed probe just leaves the fresh submit
        // form on screen, exactly as it always has.
      }
    }

    void reattach();
    return () => {
      cancelled = true;
    };
  }, []);

  // Cancellation. `cancelPending` covers only the round trip of the POST
  // itself; the "stopping…" state the reviewer actually sees comes from the
  // server (`detail.cancel_requested`), so it survives a reload and a tab
  // switch rather than living in a component that can unmount.
  const [cancelPending, setCancelPending] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  // Disposition capture (issue #486) — optional, never gating. The note is
  // local UI state (cleared on a fresh submission below); the recorded
  // value itself lives on `detail.attorney_disposition`, which the server
  // response after a successful POST merges straight back in, so a reload
  // or the next poll can never disagree with what this panel just showed.
  const [dispositionNote, setDispositionNote] = useState('');
  const [dispositionSaving, setDispositionSaving] = useState<AttorneyDisposition | null>(null);
  const [dispositionError, setDispositionError] = useState<string | null>(null);

  // `note` defaults to the note currently in state — the only value the
  // existing controls can mean. Issue #719's console supplies it explicitly
  // instead: it dispatches the disposition and its note together, and reading
  // state here would record whatever the last committed keystroke left behind
  // rather than what the reviewer had typed when they pressed the key.
  const handleRecordDisposition = useCallback(
    async (outcome: AttorneyDisposition, note: string = dispositionNote) => {
      if (!reviewId) {
        return;
      }
      setDispositionSaving(outcome);
      setDispositionError(null);
      try {
        const result = await recordDisposition(reviewId, outcome, note);
        setDetail((current) =>
          current
            ? {
                ...current,
                attorney_disposition: result.attorney_disposition,
              }
            : current,
        );
      } catch (err) {
        setDispositionError(
          err instanceof Error ? err.message : "We couldn't record that. Please try again.",
        );
      } finally {
        setDispositionSaving(null);
      }
    },
    [reviewId, dispositionNote],
  );

  // "Butter it" (issue #499) — the drafted counterparty cover email, copy-
  // only. `coverNoteLastRealCostCents` tracks the last NON-cached
  // generation's cost specifically (never the currently-displayed
  // `coverNoteCostCents`, which is 0 for a cache hit) so the Regenerate
  // button's cost hint has something honest to show even while the
  // currently-viewed draft is the free cached one.
  const [coverNoteDraft, setCoverNoteDraft] = useState<string | null>(null);
  const [coverNoteCostCents, setCoverNoteCostCents] = useState<number | null>(null);
  const [coverNoteLastRealCostCents, setCoverNoteLastRealCostCents] = useState<number | null>(
    null,
  );
  const [coverNoteCached, setCoverNoteCached] = useState(false);
  const [coverNoteLoading, setCoverNoteLoading] = useState(false);
  const [coverNoteFailed, setCoverNoteFailed] = useState(false);
  // Issue #499 fix round 3 (review finding): `coverNote.ts` deliberately
  // throws (rather than returning `{ ok: false }`) for a REAL, non-
  // retryable problem (404/403/409, or the fetch itself failing) — a 409
  // "nothing to butter" won't change on retry the way a 502 might. Losing
  // that distinction here meant every thrown error rendered the exact same
  // quiet "Couldn't butter this one, try again" copy as a transient 502,
  // so a non-retryable failure looked identical to one that might resolve
  // itself. Tracked separately from `coverNoteFailed` so the two failure
  // channels render distinctly, same as `submitError`'s own danger banner.
  const [coverNoteErrorMessage, setCoverNoteErrorMessage] = useState<string | null>(null);
  const [coverNoteCopied, setCoverNoteCopied] = useState(false);

  const handleButterIt = useCallback(
    async (regenerate: boolean) => {
      if (!reviewId) {
        return;
      }
      setCoverNoteLoading(true);
      setCoverNoteFailed(false);
      setCoverNoteErrorMessage(null);
      setCoverNoteCopied(false);
      try {
        const outcome = await butterIt(reviewId, { regenerate });
        if (!outcome.ok) {
          setCoverNoteFailed(true);
          return;
        }
        setCoverNoteDraft(outcome.draft);
        setCoverNoteCostCents(outcome.costUsdCents);
        setCoverNoteCached(outcome.cached);
        if (!outcome.cached) {
          setCoverNoteLastRealCostCents(outcome.costUsdCents);
        } else if (outcome.lastGenerationCostUsdCents !== null) {
          // Cached path (reload / History revisit): seed the Regenerate
          // hint from the stored generation cost rather than leaving it at
          // whatever (possibly null) value was already in state (issue
          // #499 fix round 1).
          setCoverNoteLastRealCostCents(outcome.lastGenerationCostUsdCents);
        }
      } catch (error) {
        setCoverNoteErrorMessage(error instanceof Error ? error.message : String(error));
      } finally {
        setCoverNoteLoading(false);
      }
    },
    [reviewId],
  );

  // `navigator.clipboard` guard mirrors copyReviewId above — same reason.
  const copyCoverNote = useCallback((text: string) => {
    const clipboard = navigator.clipboard;
    if (!clipboard?.writeText) {
      return;
    }
    void clipboard
      .writeText(text)
      .then(() => {
        setCoverNoteCopied(true);
        window.setTimeout(() => setCoverNoteCopied(false), 2000);
      })
      .catch(() => {
        // Clipboard permission denied — the draft text is still visible on
        // screen for a manual select-and-copy.
      });
  }, []);

  // The console's two receipt actions (issue #721). Each raises a
  // `scope: 'receipt'` / `tone: 'success'` confirmation, which the console
  // renders INSIDE the open receipt dialog rather than as a global alert —
  // it lands next to the button that was just pressed instead of over the
  // register. Both flags clear themselves on the same 2-second shape "Copy
  // review ID" and the cover note's own "Copy" already use.
  const [receiptCopied, setReceiptCopied] = useState(false);
  const [receiptSaved, setReceiptSaved] = useState(false);
  // The same two events as a monotonic sequence (issue #723), which is what
  // the console's one-shot tear tug and its paper-tear cue hang off. The
  // booleans above cannot serve: they clear after two seconds, they cannot
  // tell a second copy from the first, and `toReviewModel` runs on every
  // render, so a motion id derived from a flag would either replay forever or
  // fire once and never again. See `ReviewProjectionState.receiptAction`.
  const [receiptAction, setReceiptAction] = useState<
    { seq: number; kind: 'copy' | 'save' } | undefined
  >(undefined);
  const recordReceiptAction = useCallback((kind: 'copy' | 'save') => {
    setReceiptAction((previous) => ({ seq: (previous?.seq ?? 0) + 1, kind }));
  }, []);

  // `navigator.clipboard` guard mirrors copyReviewId above — same reason.
  const copyReceiptSlip = useCallback((rows: readonly string[]) => {
    const clipboard = navigator.clipboard;
    if (!clipboard?.writeText) {
      return;
    }
    void copyOrbitReceipt(rows)
      .then(() => {
        setReceiptCopied(true);
        recordReceiptAction('copy');
        window.setTimeout(() => setReceiptCopied(false), 2000);
      })
      .catch(() => {
        // Clipboard permission denied. The slip is still printed in the open
        // dialog for a manual select-and-copy, and nothing confirms a copy
        // that did not happen.
      });
  }, [recordReceiptAction]);

  // The filename is `receiptFilename()`'s, the same one "Save receipt" uses on
  // the existing panel — the console never names the file itself.
  const saveReceiptSlip = useCallback((rows: readonly string[], id: string | null) => {
    if (!saveOrbitReceipt(rows, receiptFilename(id, 'png'))) {
      // No 2D context — nothing was downloaded, so nothing is confirmed.
      return;
    }
    setReceiptSaved(true);
    recordReceiptAction('save');
    window.setTimeout(() => setReceiptSaved(false), 2000);
  }, [recordReceiptAction]);

  const handleCancel = useCallback(async (): Promise<void> => {
    if (!reviewId) {
      return;
    }
    setCancelPending(true);
    setCancelError(null);
    try {
      const response = await authorizedFetch(`/api/reviews/${reviewId}/cancel`, {
        method: 'POST',
      });
      // 409 means it reached a terminal status first. That is not an error
      // worth alarming anyone about — the next poll is about to render the
      // real outcome — so say what happened and let the poll do the rest.
      if (response.status === 409) {
        setCancelError('This review finished before it could be stopped.');
        return;
      }
      if (!response.ok) {
        throw new Error(`POST /api/reviews/${reviewId}/cancel returned HTTP ${response.status}`);
      }
      // Reflect the request immediately rather than waiting up to a full poll
      // interval to acknowledge a button press.
      setDetail((current) => (current ? { ...current, cancel_requested: true } : current));
    } catch (err) {
      setCancelError(
        friendlyErrorMessage(err, "We couldn't stop this review. It is still running."),
      );
    } finally {
      setCancelPending(false);
    }
  }, [reviewId]);

  // Poll GET /api/reviews/{review_id} on an interval while the review is
  // non-terminal (PENDING/RUNNING); stop once a terminal status arrives.
  useEffect(() => {
    if (!reviewId) {
      return undefined;
    }

    let cancelled = false;
    let attempt = 0;
    const startedAt = Date.now();

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
          }, pollIntervalFor(Date.now() - startedAt));
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
    // `pollNonce` is the "Check now" retry (#726): a change re-runs this
    // effect, whose cleanup cancels the pending attempt and clears the
    // backoff timer before `poll()` runs again straight away.
  }, [reviewId, stopPolling, pollNonce]);

  // Issue #494 split `handleSubmit` into the form handler and this, the
  // submission itself. The lever is a second way to reach the SAME code path —
  // not a second implementation of it. Anything that guards, spends, or
  // records lives below, so the two affordances cannot drift apart, and the
  // existing submission tests keep exercising the identical function.
  const submitReview = useCallback(
    async () => {
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
      // Issue #735 (review finding): the captured estimate is frozen at submit
      // time for ONE review, and the two lines above have just taken that
      // review off the screen synchronously. A POST that then throws leaves
      // `reviewId` null — a pre-submission screen — while `projectCost` still
      // prefers ANY captured value, including the `null` a submit made before
      // the price landed records, over the live figure. Without this the
      // register reads "Estimate unavailable" (or the PREVIOUS document's
      // price) on a screen with no review on it. The happy path is unchanged:
      // the successful POST re-captures from `reviewCostUsdCents` below.
      setSubmittedEstimateCents(undefined);
      // Clear the previous completion handoff. The ref is keyed on review id
      // rather than simply "has run", and re-dropping the same file inside the
      // same idempotency bucket RESUMES the same review id — so without this
      // reset a resubmit that resolves straight back to an already-DONE review
      // would announce nothing, focus nothing and save nothing.
      setReadyAnnouncement('');
      setAutoSaved(false);
      handedOffReviewRef.current = null;
      // A resubmit starts a new review (or resumes a pre-existing one via
      // idempotency) — either way, any disposition note typed for the
      // PREVIOUS review on screen must not silently ride along onto this one.
      setDispositionNote('');
      setDispositionError(null);
      // Same reasoning for the previous review's cover-note draft (#499) —
      // it belongs to that review's own row, not this one.
      setCoverNoteDraft(null);
      setCoverNoteCostCents(null);
      setCoverNoteLastRealCostCents(null);
      setCoverNoteCached(false);
      setCoverNoteFailed(false);
      // Issue #499 fix round 3 (review finding): coverNoteErrorMessage is a
      // SEPARATE state from coverNoteFailed (real thrown error vs. the quiet
      // 502 case) added alongside it -- omitting it here left a resubmit
      // showing the PREVIOUS review's danger banner (e.g. "past its
      // retention window") on the new review's cover-note section before
      // the reviewer had even clicked "Butter it" for it.
      setCoverNoteErrorMessage(null);
      setCoverNoteCopied(false);

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
        //
        // Issue #495 composes the browning sentence in FRONT of the typed
        // text (see composeGuidance). At Medium it contributes nothing, so an
        // untouched control still sends the byte-identical request.
        const guidance = composeGuidance(browning, toasterGuidance);
        if (guidance) {
          formData.append('toaster_guidance', guidance);
        }
        // Issue #523: the footnote audience for THIS review. Appended only
        // when it differs from the backend's own default (`external`,
        // backend/src/reviews.py::DEFAULT_NOTES_MODE), so a reviewer who
        // never touches the control — or whose stored preference IS the
        // default — sends a request byte-identical to the one this form sent
        // before the control existed. The value is the wire vocabulary
        // itself, never a translated label.
        if (notesMode !== DEFAULT_NOTES_MODE) {
          formData.append('notes_mode', notesMode);
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
        setSubmittedFilename(file.name);
        // Issue #735: frozen only once the submission actually landed — a POST
        // that failed priced nothing, so there is nothing to capture for it.
        setSubmittedEstimateCents(reviewCostUsdCents);
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
    // `browning` and `notesMode` (issue #523) belong here for the same reason
    // every other value read inside does: without them this callback keeps
    // the closure it was built with, and a reviewer who picks the file BEFORE
    // touching either control submits the value it held beforehand. Nothing
    // caught it while `file` happened to change last — choosing a file is a
    // dependency change, which rebuilt the callback and hid the staleness —
    // but the reverse order is just as ordinary a thing to do, and it sent
    // the wrong request. `browning` was already missing before #523 added
    // `notesMode` beside it; both are one and the same defect.
    // `reviewCostUsdCents` (issue #735) is here for exactly the reason spelled
    // out above: it is read inside, so without it this callback would freeze
    // whatever the estimate was when the callback was last rebuilt — which,
    // for a reviewer who picks a file before the price arrives, is `null`.
    [
      browning,
      file,
      notesMode,
      playbookId,
      playbooks,
      reviewCostUsdCents,
      stopPolling,
      toasterGuidance,
    ],
  );

  // Mint a short-lived presigned URL for this review's output. Shared by the
  // button the attorney clicks and by the automatic save on completion, so the
  // two can never drift on which endpoint they call or how they read a failure.
  const fetchOutputUrl = useCallback(async (): Promise<string> => {
    const response = await authorizedFetch(`/api/reviews/${reviewId}/output`);
    if (!response.ok) {
      // A 503 here carries a `detail` naming the unset storage env var
      // (#465's own failure mode) — server configuration, never something a
      // reviewer should read. Route through the shared friendlyDownloadError
      // instead of rendering readErrorDetail's string verbatim (issue #466);
      // the raw detail still reaches the console.
      const errorDetail = await readErrorDetail(response);
      throw new Error(
        friendlyDownloadError(
          errorDetail ?? `GET /api/reviews/${reviewId}/output returned HTTP ${response.status}`,
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
      setDownloadError(err instanceof Error ? err.message : friendlyDownloadError(err));
    } finally {
      setDownloading(false);
    }
  }, [reviewId, fetchOutputUrl]);

  /**
   * "Save original" — the review's INPUT document (issue #719). Same route
   * family and the same failure handling as `fetchOutputUrl` above and as
   * ReviewHistory's per-row control (issue #466): a 503 here carries server
   * configuration in `detail`, never something a reviewer should read, so it
   * goes through the shared `friendlyDownloadError` and renders in the same
   * `downloadError` banner the redline download uses. HTTP 410 is the
   * retention case — nothing is handed to the browser, so there is no dead
   * link.
   */
  const downloadInputDocument = useCallback(async () => {
    if (!reviewId) {
      return;
    }
    setDownloadError(null);
    try {
      const response = await authorizedFetch(`/api/reviews/${reviewId}/input`);
      if (response.status === 410) {
        setDownloadError(DOCUMENT_PURGED_COPY);
        return;
      }
      if (!response.ok) {
        const errorDetail = await readErrorDetail(response);
        throw new Error(
          friendlyDownloadError(
            errorDetail ?? `GET /api/reviews/${reviewId}/input returned HTTP ${response.status}`,
          ),
        );
      }
      const data = (await response.json()) as { url?: string };
      if (!data.url) {
        throw new Error(
          friendlyDownloadError(`GET /api/reviews/${reviewId}/input returned no url`),
        );
      }
      triggerBrowserDownload(data.url);
    } catch (err) {
      setDownloadError(err instanceof Error ? err.message : friendlyDownloadError(err));
    }
  }, [reviewId]);

  /**
   * "Toast another slice" — the reset that clears the finished/failed review
   * off the screen. Extracted from the button's own onClick (issue #719) so
   * the console's `retry` action reaches the SAME reset rather than a second,
   * subtly different one. Deliberately not a resubmit: several classified
   * failure causes need the reviewer to change something first.
   */
  const resetForRetry = useCallback(() => {
    stopPolling();
    setDetail(null);
    setReviewId(null);
    setFile(null);
    setSubmitError(null);
    setDownloadError(null);
    setReadyAnnouncement('');
    // Issue #735: the captured estimate belongs to the review this reset just
    // took off the screen, and `projectCost` treats ANY captured value —
    // including the `null` a submit made before the price landed records — as
    // authoritative over the live figure. Leaving it set would price a
    // pre-submission screen that has no review on it at the previous review's
    // number, or, after a `null` capture, read "Estimate unavailable" for the
    // rest of the session even once the live estimate has arrived.
    setSubmittedEstimateCents(undefined);
    handedOffReviewRef.current = null;
  }, [stopPolling]);

  // The automatic save (issue #448) — the same anchor click the button
  // performs, fired once on completion without a user gesture.
  //
  // Deliberately NOT routed through handleDownload: this is a background
  // courtesy the attorney did not ask for, so it must not flip the visible
  // button into its disabled "Preparing download…" state (which would yank
  // away the focus we just placed there). Unlike before #466, a failure here
  // is NOT silent: the review finished fine but nothing was actually saved,
  // so the same downloadError banner the manual button uses renders the same
  // honest, no-false-"try again" copy — the button below still works as a
  // retry path, it just isn't described as one. Success is likewise the only
  // thing allowed to upgrade the live-region announcement to "saved" (see the
  // block comment above READY_FOCUSED_COPY).
  const autoSaveOutput = useCallback(async (): Promise<void> => {
    if (!reviewId) {
      return;
    }
    // The fetch below is in flight while a resubmit can reset reviewId,
    // detail and handedOffReviewRef out from under it (handleSubmit above).
    // Capture which review this save belongs to and re-check it against
    // handedOffReviewRef — which handleSubmit clears to null and the
    // handoff effect re-stamps with the *new* review's id — before either
    // setter runs, so a save for review A can never paint review B's screen.
    const savingReviewId = reviewId;
    try {
      triggerBrowserDownload(await fetchOutputUrl());
      if (handedOffReviewRef.current === savingReviewId) {
        setReadyAnnouncement(READY_SAVED_COPY);
        // Issue #492: the ONE fact allowed to turn on the visible
        // REDLINE_SAVED_COPY line — set only here, only once the fetch has
        // actually resolved for the review still on screen. Never on a
        // promise that a save is under way (the #466 discipline).
        setAutoSaved(true);
      }
    } catch {
      // fetchOutputUrl already logged the real technical detail to the
      // console via friendlyDownloadError — nothing further to log here.
      if (handedOffReviewRef.current === savingReviewId) {
        setDownloadError(DOWNLOAD_ERROR_COPY);
      }
    }
  }, [reviewId, fetchOutputUrl]);

  // Sound mute state (persisted by the sounds module; no localStorage here).
  const { muted, toggle } = useSoundMuted();

  // Opt-in "toast's ready" Notification preference (issue #497) — its own
  // localStorage key (notify.ts), independent of the mute flag above.
  const { optedIn: notifyOptedIn, toggle: toggleNotify } = useNotifyPreference();

  // A single derived phase, now read only by the tab title and the favicon
  // (`useTabTheater` below — the hero it also used to drive went with #727):
  // idle before a review is in flight; working while the pipeline is
  // non-terminal (or the first poll hasn't landed); done on DONE; error on any
  // other terminal status.
  const phase: ToasterPhase = !reviewId
    ? 'idle'
    : !detail || NON_TERMINAL_STATUSES.has(detail.status)
      ? 'working'
      : detail.status === 'DONE'
        ? 'done'
        : // A review the reviewer stopped rests, it does not burn. `error`
          // would give it the burnt-toast treatment and read as a malfunction
          // — the #458 lesson, at its most obvious: nothing went wrong, the
          // user asked for this. The result panel below still renders the
          // terminal outcome ("Stopped"), so nothing is hidden.
          detail.status === 'CANCELLED'
          ? 'idle'
          : 'error';

  // Favicon browning + tab title (issue #497) — reads the same `phase` and
  // `progress_stage` the console renders from, one projection away
  // (stageTheater.ts) from the caption under the glass. Designer answer D6:
  // the tab chrome stays ours, fed from that one stage mapping.
  useTabTheater(phase, detail?.progress_stage ?? null);

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
      // Issue #497: the opt-in Notification layer on top of the ding above.
      // notifyToastDone re-checks opted-in/permission/hidden itself — this
      // call site's only job is to supply the real outcome, from the same
      // shared outcome map (issue #470) every other surface uses, so the
      // notification body can never drift from what the result panel says.
      notifyToastDone({
        failed: false,
        outcomeLabel: describeOutcome(detail?.status, detail?.decision).label,
      });
    }
    // Issue #501: a failure gets the low clunk, never the pop. The pop is the
    // sound of finished work; playing it when nothing was produced would be
    // the machine misreporting its own state in the one channel a user cannot
    // re-read.
    if (phase === 'error') {
      playClunk();
      notifyToastDone({ failed: true, outcomeLabel: null });
    }
    return () => stopTicking();
  }, [phase]);

  // Power-user Keyboard Shortcuts suite. The cheat sheet itself is the
  // console's dialog and the console's state (issues #720 and #727); nothing
  // about whether it is open is this component's to remember.
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      // Issue #720, guard one. An event another handler has already answered
      // is not ours to answer a second time. This listener is on `window` in
      // the BUBBLE phase, so anything nearer the user — the console's dialogs
      // above all — has already had its say by the time we run.
      if (event.defaultPrevented) return;

      const hasModifier = event.metaKey || event.ctrlKey;
      const target = event.target instanceof HTMLElement ? event.target : null;

      // Issue #720, guard two. Inside an open dialog the vocabulary belongs to
      // the dialog. Every branch below reaches a control on the surface BEHIND
      // the scrim: it would focus a dial the reader cannot see, or eject their
      // file out from under an open receipt. `isInputFocused` gates only the
      // single-key shortcuts, so the modified ones need this guard to be kept
      // out of a dialog at all.
      if (target?.closest('dialog[open]')) return;

      const isInputFocused =
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement ||
        Boolean(target?.isContentEditable);

      // The two controls these shortcuts reach. With the console mounted both
      // selectors resolve INSIDE it — the console carries the same testids the
      // legacy tree did — so this is one vocabulary addressing whichever
      // surface is rendered, not two.
      const dialSelector = '[data-testid="review-playbook-dial"]';
      const guidanceSelector =
        '[data-testid="review-guidance-field"] textarea, textarea[name="guidance"]';

      // Issue #720. A chord that swallows the browser's own binding and then
      // focuses nothing is worse than no chord at all: `Cmd+Shift+P` is the
      // command palette in more than one shell. So the lookup runs FIRST and
      // `preventDefault` follows only a control that actually took focus.
      const focusControl = (selector: string): boolean => {
        const el = document.querySelector<HTMLElement>(selector);
        if (!el) return false;
        el.focus();
        return document.activeElement === el;
      };

      // Issue #720, and #727's half of it. Both cheat-sheet bindings reach
      // the console's dialog by pressing the control that owns it — the same
      // "reach the real control" convention the file-input binding above uses
      // — so whether the sheet is up stays the console's own state and there
      // is exactly one list with exactly one owner.
      //
      // The other arm of this used to flip a local `showShortcutsModal`, for
      // the legacy tree's own modal. #720 deleted that modal and #727 deleted
      // the tree, so the state had no reader left and is gone with them.
      const openShortcuts = (): boolean => {
        const shortcutsKey = document.querySelector<HTMLElement>(
          '[data-testid="review-shortcuts-key"]',
        );
        if (!shortcutsKey) return false;
        shortcutsKey.click();
        return true;
      };

      // 1. Modifiers & Escape (Global even if inside input/textarea)
      if (hasModifier && event.key === 'Enter') {
        event.preventDefault();
        if (file && !submitting && phase !== 'working') {
          void submitReview();
        }
        return;
      }

      if (event.key === 'Escape') {
        if (!isInputFocused && file && phase === 'idle' && !submitting) {
          event.preventDefault();
          setFile(null);
          return;
        }
      }

      // 2. Modifiers
      if (hasModifier && (event.key.toLowerCase() === 'u' || event.key.toLowerCase() === 'o')) {
        event.preventDefault();
        const fileInput = document.querySelector<HTMLInputElement>(
          '[data-testid="review-file-input"] input[type="file"], [data-testid="review-file-input"]',
        );
        fileInput?.click();
        return;
      }

      if (hasModifier && (event.key.toLowerCase() === 'd' || event.key.toLowerCase() === 's')) {
        if (detail?.has_output && !downloading) {
          event.preventDefault();
          void handleDownload();
          return;
        }
      }

      if (hasModifier && event.key === '/') {
        if (!openShortcuts()) return;
        event.preventDefault();
        return;
      }

      if (hasModifier && event.shiftKey && event.key.toLowerCase() === 'p') {
        if (!focusControl(dialSelector)) return;
        event.preventDefault();
        return;
      }

      if (hasModifier && event.shiftKey && event.key.toLowerCase() === 'g') {
        if (!focusControl(guidanceSelector)) return;
        event.preventDefault();
        return;
      }

      if (hasModifier && event.shiftKey && event.key.toLowerCase() === 'm') {
        event.preventDefault();
        toggle();
        return;
      }

      if (hasModifier && event.shiftKey && event.key.toLowerCase() === 'r') {
        const matching = findMatchingPlaybook(
          preflight?.classification === 'ok' ? preflight.agreementTypeGuess : null,
          playbooks,
          playbookId,
        );
        if (matching) {
          event.preventDefault();
          playDetent();
          choosePlaybookManually(matching.playbook_id);
          return;
        }
      }

      // 3. Single-key shortcuts (Disabled when typing in text fields)
      if (isInputFocused) return;

      if (event.key === '?') {
        if (!openShortcuts()) return;
        event.preventDefault();
        return;
      }

      if (event.key.toLowerCase() === 'u') {
        event.preventDefault();
        const fileInput = document.querySelector<HTMLInputElement>(
          '[data-testid="review-file-input"] input[type="file"], [data-testid="review-file-input"]',
        );
        fileInput?.click();
        return;
      }

      if (event.key.toLowerCase() === 'g') {
        if (!focusControl(guidanceSelector)) return;
        event.preventDefault();
        return;
      }

      if (event.key.toLowerCase() === 'p') {
        if (!focusControl(dialSelector)) return;
        event.preventDefault();
        return;
      }

      if (event.key.toLowerCase() === 'm') {
        event.preventDefault();
        toggle();
        return;
      }

      if (event.key.toLowerCase() === 'r') {
        const matching = findMatchingPlaybook(
          preflight?.classification === 'ok' ? preflight.agreementTypeGuess : null,
          playbooks,
          playbookId,
        );
        if (matching) {
          event.preventDefault();
          playDetent();
          choosePlaybookManually(matching.playbook_id);
          return;
        }
      }

      if (event.key === '1' && notesModeInternalAvailable) {
        event.preventDefault();
        handleNotesModeChange('internal');
        return;
      }
      if (event.key === '2') {
        event.preventDefault();
        handleNotesModeChange('external');
        return;
      }
      if (event.key === '3' && notesModeInternalAvailable) {
        event.preventDefault();
        handleNotesModeChange('both');
        return;
      }
      if (event.key === '4') {
        event.preventDefault();
        handleNotesModeChange('none');
        return;
      }

      if (event.key === '[' || event.key === '-') {
        event.preventDefault();
        const order: BrowningLevel[] = ['light', 'medium', 'dark'];
        const currentIndex = order.indexOf(browning);
        const nextIndex = Math.max(0, (currentIndex === -1 ? 1 : currentIndex) - 1);
        const next = order[nextIndex];
        if (next !== browning) {
          handleBrowningChange(next);
        }
        return;
      }
      if (event.key === ']' || event.key === '=' || event.key === '+') {
        event.preventDefault();
        const order: BrowningLevel[] = ['light', 'medium', 'dark'];
        const currentIndex = order.indexOf(browning);
        const nextIndex = Math.min(order.length - 1, (currentIndex === -1 ? 1 : currentIndex) + 1);
        const next = order[nextIndex];
        if (next !== browning) {
          handleBrowningChange(next);
        }
        return;
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [
    file,
    submitting,
    phase,
    submitReview,
    detail,
    downloading,
    handleDownload,
    toggle,
    preflight,
    playbooks,
    playbookId,
    playDetent,
    choosePlaybookManually,
    notesModeInternalAvailable,
    handleNotesModeChange,
    handleBrowningChange,
    browning,
  ]);

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
      !canSave ? READY_NO_OUTPUT_COPY : gateSatisfied ? READY_FOCUSED_COPY : READY_GATED_COPY,
    );

    // ct-button renders a real <button> into its light DOM (ui/components/
    // ct-button.ts), so the focusable node is a descendant of this wrapper,
    // not the wrapper itself.
    //
    // Issue #733, completed by #727. The wrapper `saveControlRef` used to
    // name belonged to the deleted tree, so the focus half of #448's handoff
    // had silently stopped happening while the announcement went on saying
    // focus had moved. The console owns its own save key; reach it the same
    // way the shortcut dispatcher reaches the console's cheat sheet (#720),
    // by the control's id.
    const saveControl = document.querySelector<HTMLElement>(
      '[data-testid="review-download-button"]',
    );
    saveControl?.focus();

    if (canSave && gateSatisfied) {
      void autoSaveOutput();
    }
  }, [phase, detail, autoSaveOutput]);

  // ACCEPT never reads "approved" / "no action needed" (ARCHITECTURE.md's
  // Wrong-format rejection UX / accept framing) — always "no requested
  // changes identified by tool". This is NOT redundant with the outcome
  // headline below: the headline's "Accepted" is the shared outcome-map
  // label every surface uses (issue #470); this sentence is the specific
  // legal-safety phrasing ACCEPT alone requires, and stays even though the
  // attorney-approval watermark that used to sit beside it does not
  // (issue #492).
  //
  // Issue #492 removed the REQUEST_CHANGE fallback that used to live here
  // ('Changes requested.'): the outcome headline already says exactly that
  // (OUTCOME_CHIPS.REQUEST_CHANGE), and printing it twice was the kind of
  // clutter this rewrite exists to cut. A `message` the backend actually
  // sent is never dropped — only the hardcoded stand-in for "no message"
  // is.
  const decisionCopy: string | null =
    detail?.decision === 'ACCEPT'
      ? 'No requested changes identified by tool.'
      : (detail?.message ?? null);

  const failureExplanation = detail ? explainFailure(detail) : null;

  // The quiet meta line (issue #492, redesign item 3): filename · contract
  // type · finished-at time. Built as parts rather than a single joined
  // string so `review-submitted-playbook` (the existing testid
  // playbook-selector.test.tsx already asserts against) keeps naming the
  // contract-type clause specifically. Never inventing a part it doesn't
  // have — the same "absent, not guessed" convention toaster/receipt.ts's
  // receiptLines uses — a review with no recorded finish time simply omits
  // that clause rather than printing a wrong one.
  const metaParts: { key: string; testid: string; text: string }[] = [];
  if (submittedFilename) {
    metaParts.push({ key: 'filename', testid: 'review-meta-filename', text: submittedFilename });
  }
  if (submittedPlaybookLabel) {
    metaParts.push({
      key: 'playbook',
      testid: 'review-submitted-playbook',
      text: submittedPlaybookLabel,
    });
  }
  const finishedAt = toastedOn(detail?.updated_at);
  if (finishedAt) {
    metaParts.push({ key: 'finished-at', testid: 'review-meta-finished-at', text: finishedAt });
  }

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

  // -------------------------------------------------------------------------
  // The Orbit Diner console (issue #719, epic #729)
  // -------------------------------------------------------------------------
  //
  // THE Review tab, not one of two. This arrived as a single branch at the top
  // of the render returning the console instead of the tree below; #727
  // deleted that tree and #728 deleted the build-time switch that chose
  // between them, so what is left is the only render path this component has.
  //
  // What is inside it is a TRANSCRIPTION, not a decision: `toReviewModel`
  // reads values this component already holds at render time, and every
  // callback is a one-to-one hand-off to a guarded handler above. No fetch, no
  // state and no storage moves into the console (final plan, "Standing
  // invariants").
  //
  // A hook, so it is declared BEFORE the early return below and runs on every
  // render path: "prime audio inside the first pointer/keyboard gesture"
  // (issue #722, Scope). `toaster/sounds.ts` decodes nothing until
  // `primeAudio` runs and `play()` drops any clip whose buffer is still
  // absent, so without this the console's own recordings — slice-insert,
  // register-key, paper-slide, pen-scratch, refusal — are silent for the
  // whole setup phase of a review, because the panel's only other prime is
  // inside `submitReview`.
  //
  // `{ once: true }` on the window, in the capture phase: pointerdown and
  // keydown land BEFORE the click that produces the MotionEvent, so the
  // fetch+decode has a head start on the first sounding event, and the pair
  // unregisters itself the moment either one fires. Priming again later is a
  // no-op — `ensureCtx` and `loadAll` are both idempotent — which is why the
  // `onSound` adapter can prime a second time for gestures that reach the
  // console without one of these events (a file dragged in from the desktop
  // fires no pointerdown on the page).
  useEffect(() => {
    const prime = (): void => {
      window.removeEventListener('pointerdown', prime, true);
      window.removeEventListener('keydown', prime, true);
      primeAudio();
    };
    window.addEventListener('pointerdown', prime, { capture: true, once: true });
    window.addEventListener('keydown', prime, { capture: true, once: true });
    return () => {
      window.removeEventListener('pointerdown', prime, true);
      window.removeEventListener('keydown', prime, true);
    };
  }, []);

  // The preflight recommendation, resolved by the SAME helper the `R`
  // shortcut and the verdict card use, so the three cannot disagree about
  // what is being recommended.
  const recommendedPlaybook =
    preflight?.classification === 'ok'
      ? findMatchingPlaybook(preflight.agreementTypeGuess, playbooks, playbookId)
      : null;

  // The preflight's own sentences, composed HERE (issue #733) with the same
  // describers the existing card uses: the indefinite article, the "Other"
  // special case and the neutral paper-side phrasing are product wording
  // with their own reasons, and a second implementation of them inside the
  // console would drift the first time one of those reasons changed.
  const preflightReadsLike = (() => {
    if (!preflight || preflight.classification !== 'ok') return undefined;
    const clause = [
      preflight.agreementTypeGuess
        ? `reads like ${describeAgreementTypeGuess(preflight.agreementTypeGuess)}`
        : null,
      describePaperSide(preflight.paperSide) || null,
    ]
      .filter(Boolean)
      .join(' ');
    return clause ? `This ${clause}.` : undefined;
  })();
  const selectedLabel =
    playbooks.find((entry) => entry.playbook_id === playbookId)?.display_name ?? null;
  const preflightMismatchNote =
    preflight?.classification === 'ok' && preflight.match === 'unlikely'
      ? `${
          preflight.agreementTypeGuess
            ? `This reads like ${describeAgreementTypeGuess(preflight.agreementTypeGuess)}`
            : 'This document'
        }${selectedLabel ? `, not ${selectedLabel}` : ''}. You can toast it anyway.`
      : undefined;

  // The canonical slip, composed ONCE. "Copy as text" and "Save image" are
  // handed the SAME array from the SAME call, so the clipboard payload and
  // the PNG cannot disagree even transiently — the property `toaster/
  // receipt.ts` exists to guarantee, held here at the call site too.
  const orbitReceipt = detail ? orbitReceiptLines(detail, submittedPlaybookLabel) : null;

  const orbitState: ReviewProjectionState = {
    file,
    submitting,
    reviewId,
    detail,
    submittedFilename,
    submittedPlaybookLabel,
    submittedResumed,
    playbookId,
    // Issue #730: who chose the playbook on the dial, kept for the record.
    // It drives no visible copy anywhere (H5 — the choice is silent): the
    // browse overlay used to branch on it and no longer does.
    playbookSelection,
    browning,
    notesMode,
    toasterGuidance,
    appliedGuidance,
    readyAnnouncement,
    metaParts,
    preflightReadsLike,
    preflightMismatchNote,
    dispositionNote,
    playbooks,
    notesModeInternalAvailable,
    preflight,
    recommendedPlaybook,
    reviewCostUsdCents,
    capturedReviewCostUsdCents: submittedEstimateCents,
    coverNoteCostCents,
    coverNoteLastRealCostCents,
    coverNoteDraft,
    coverNoteCached,
    coverNoteLoading,
    coverNoteFailed,
    coverNoteErrorMessage,
    coverNoteCopied,
    dispositionSaving,
    dispositionError,
    downloading,
    downloadStarted: autoSaved,
    cancelPending,
    submitError,
    pollError,
    downloadError,
    catalogError,
    notesModeSaveError,
    cancelError,
    decisionCopy,
    failureExplanation,
    unclassifiedReason: UNCLASSIFIED_REASON,
    copiedReviewId,
    receiptCopied,
    receiptSaved,
    receiptAction,
    muted,
    notificationsSupported: notificationsSupported(),
    notifyOptedIn,
    // A live read of the browser's own state. Reading the property never
    // prompts — notify.ts keeps `requestPermission()` to the one opt-in
    // click, and nothing here goes near it.
    notificationPermission: notificationsSupported() ? Notification.permission : undefined,
    // `isAdmin`/`adminDaily` are deliberately absent: nothing on this panel
    // holds an authorized spend aggregate, and the console must never be the
    // reason one is fetched for a reviewer.
  };

  const orbitCallbacks: ReviewSubmissionCallbacks = {
    submitReview: () => void submitReview(),
    handleCancel: () => void handleCancel(),
    resetForRetry,
    handleDownload: () => void handleDownload(),
    downloadInput: () => void downloadInputDocument(),
    copyReviewId: () => {
      if (reviewId) copyReviewId(reviewId);
    },
    // The slip the console prints is the canonical `receiptLines(detail)`
    // one — the same lines `toReviewModel` puts on the model, rendered
    // through the same `receiptText` — never text rebuilt from whatever
    // labels happen to be on screen.
    copyReceipt: () => {
      if (orbitReceipt) copyReceiptSlip(orbitReceipt);
    },
    saveReceipt: () => {
      if (orbitReceipt) saveReceiptSlip(orbitReceipt, detail?.review_id ?? reviewId);
    },
    handleButterIt: (regenerate: boolean) => void handleButterIt(regenerate),
    copyCoverNote: () => {
      if (coverNoteDraft) copyCoverNote(coverNoteDraft);
    },
    retryPreference: () => void saveNotesModePreference(notesMode),
    // The two channel retries the one status window needs (#726). Both are
    // the app's existing loaders: "Check now" restarts the poll effect and
    // "Reload contract types" re-runs the same `fetchCatalog` the mount
    // effect and an admin mutation already use. The console starts no
    // request of its own.
    retryPoll: () => setPollNonce((nonce) => nonce + 1),
    retryCatalog: () => void fetchCatalog(),
    toggleSound: toggle,
    toggleNotifications: toggleNotify,
    // The hash route App.tsx already listens for — not a second navigation
    // mechanism invented here.
    openHistory: () => {
      window.location.hash = HISTORY_TAB_HASH;
    },
    // The real per-review "run again" control lives on the History tab, and
    // this projection supplies no `history` strip, so the console cannot
    // dispatch this action at all today. Until #726 lands that strip
    // alongside its own helper, the honest hand-off is to open the tab that
    // owns the control rather than to invent a second submit path here.
    runAgain: () => {
      window.location.hash = HISTORY_TAB_HASH;
    },
    switchToRecommendedPlaybook: () => {
      if (recommendedPlaybook) {
        playDetent();
        choosePlaybookManually(recommendedPlaybook.playbook_id);
      }
    },
    handleRecordDisposition: (outcome, note) => void handleRecordDisposition(outcome, note),
    // Issue #730: the console's dial and its browse list both arrive here,
    // and both are the reviewer choosing by hand — so this is the manual
    // path, never the raw setter, or a late recommendation could reverse a
    // choice the reviewer just made.
    setPlaybookId: choosePlaybookManually,
    // NOT `handleBrowningChange`: the console fires `key` for the very click
    // that routes here, and the adapter sounds that as `register-key`. The
    // panel's own detent would be a second voice for one interaction, so the
    // console gets the silent setter and the console's own event is the one
    // sound (issue #722). The `[`/`]` shortcut still calls the sounding
    // handler, because no console event accompanies a keystroke.
    handleBrowningChange: (level: BrowningLevel) => {
      applyBrowningChange(level);
    },
    handleNotesModeChange,
    setToasterGuidance,
    setDispositionNote,
    // The real file-chosen path: the preflight effect and every validation
    // hang off this one `file` state, exactly as the drop well's own handler
    // sets it. `clearFile` is the real reset, never a display-only blank
    // that would leave a stale File retained behind an empty toast slot.
    selectFile: (chosen: File) => setFile(chosen),
    clearFile: () => setFile(null),
  };

  // Composed once, so the `stage` the console announces and the `stage` the
  // sound owner is told about are the same read of the same model.
  const orbitModel = toReviewModel(orbitState);

  // No React `key`: a changing key would remount the console on every status
  // change and lose its cosmetic references and dialog focus. Changing data
  // travels through `model`, and only through `model`.
  //
  // `keyboardShortcuts={false}` is owner decision H6 — this component's own
  // `keydown` dispatcher stays the single owner of the shortcut vocabulary,
  // so the kit's local handler must stay inert. The kit's modal Escape is
  // local to its own dialog and is unaffected.
  //
  // `onSound` hands every console event to the ONE audio owner
  // (`toaster/sounds.ts`, issue #722) — not to the kit's `createSoundBus`,
  // which would be a second AudioContext with its own budget and its own
  // idea of the mute flag.
  //
  // "No event sounds twice" is held on both sides of the seam. The owner
  // stays silent for the four status transitions this component's own
  // handlers already sound (`PANEL_OWNED_EVENTS` in toaster/sounds.ts), and
  // for the one PREFERENCE the console both sounds and routes back here —
  // markup intensity, which fires `key` — the callback above is the silent
  // setter, so the console's `register-key` is that click's only voice. That
  // voice is only real because the console primes: see the effect above and
  // the `primeAudio()` in the adapter below.
  return (
    <OrbitDiner
      model={orbitModel}
      keyboardShortcuts={false}
      onSound={(event) => {
        // The second half of the priming rule. The gesture listener above
        // covers pointer and keyboard input, but a file dragged in from the
        // desktop reaches `file-loaded` with no pointerdown on the page at
        // all, and an un-primed owner has no decoded buffers, so `play()`
        // would return at its empty-buffer guard. Idempotent, so an already
        // primed session pays nothing for this.
        primeAudio();
        playMotionEvent(event, orbitModel.stage ?? null);
      }}
      {...connectReviewSubmission(orbitCallbacks)}
    />
  );
}
