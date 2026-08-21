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
 * scripts/redline_docx_writer.py — issue #513's separate scope).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  authorizedFetch,
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
import { GUIDANCE_PRECEDENCE_COPY as SHARED_GOVERNS_CLAUSE } from './guidancePrecedenceCopy';
// Last-selected contract type, persisted across a reload (issue #489, item
// 4). See lastPlaybook.ts's module docstring for the storage shape and why
// this is safe to keep in localStorage.
import { readLastPlaybookId, writeLastPlaybookId } from './lastPlaybook';
// The cheap, advisory upload-time preflight check (issue #491) — fired the
// moment a file is chosen, never gating "Upload for review". See
// preflight.ts's module docstring for the full injection-defense posture;
// this component's own job is just rendering `PreflightResult` as inert
// text, never as markup or a link (see the render site below).
import { refreshMatchVerdict, runPreflight, type PreflightResult } from './preflight';
// The shared disposition capture (issue #486) — vocabulary, display labels,
// copy, and the POST call, all in one place so this panel and History's
// per-row control can never drift on wording. See disposition.ts's module
// docstring for why the copy here does NOT reference attorney approval.
import {
  DISPOSITION_CHOICES,
  DISPOSITION_PROMPT_COPY,
  DISPOSITION_RECORD_COPY,
  DISPOSITIONABLE_STATUSES,
  describeDisposition,
  recordDisposition,
  type AttorneyDisposition,
} from './disposition';
// "Butter it" (issue #499) — the shared cover-note client + copy, so this
// panel and History's expanded row can never drift on wording or on how a
// failure is turned into copy. See coverNote.ts's module docstring.
import { butterIt, formatCostUsdCents, COVER_NOTE_FAILURE_COPY } from './coverNote';
import { butterSlide } from './toaster/motion';
import { ToasterHero, ToasterStyles, type ToasterPhase } from './toaster/Toaster';
import { ToastReceipt } from './toaster/ToastReceipt';
// toastedOn: the same epoch-seconds -> "YYYY-MM-DD  HH:MM UTC" formatter the
// receipt uses for its own date line (issue #492's meta line reuses it for
// `updated_at` rather than duplicating the formatting).
import { toastedOn } from './toaster/receipt';
import {
  composeGuidance,
  DEFAULT_BROWNING,
  type BrowningLevel,
} from './toaster/browning';
import {
  primeAudio,
  playLever,
  startTicking,
  stopTicking,
  playPop,
  playDetent,
  playClunk,
  useSoundMuted,
} from './toaster/sounds';
// Favicon browning + tab title (issue #497) — one hook, driven by the same
// `phase`/`progress_stage` pair the hero itself renders from, so the tab
// chrome can never disagree with what is on screen.
import { useTabTheater } from './toaster/tabChrome';
// The opt-in "toast's ready" Notification (issue #497) — a second, optional
// layer on top of the ding above; see notify.ts's docstring for the
// permission rule.
import { useNotifyPreference, notifyToastDone, notificationsSupported } from './toaster/notify';
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
//
// The shared middle clause now lives in guidancePrecedenceCopy.ts (issue
// #484) — AdminInstructions.tsx's standing-instructions field states the
// identical precedence, so the two surfaces compose it from one constant
// rather than risking the wording drifting apart. The rendered text here is
// unchanged from before that extraction.
const GUIDANCE_PRECEDENCE_COPY =
  `These instructions ${SHARED_GOVERNS_CLAUSE} A sentence or two is plenty.`;

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
// Issue #492: the one truthful, VISIBLE save/download line — shown only once
// autoSaveOutput's fetch has actually resolved successfully (never on a
// promise that it is "saving", the same #466 discipline the announcement
// above follows). Deliberately says nothing about focus: that is the live
// region's job, not this line's.
const REDLINE_SAVED_COPY = 'Redline saved to your downloads.';
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
  // Issue #472: the pre-call sibling of model_key_rejected above — no key
  // was configured at all, so the review never reached the model. This is
  // the single most likely first-run mistake (upload before setting a key).
  model_key_missing: {
    cause: 'No API key is configured for the model provider, so the review was never sent.',
    fix: 'An admin can add one under “Model & API key”. Until then every review will fail here.',
  },
  model_timeout: {
    cause: 'The model provider did not respond in time, so the review was not completed.',
    fix: 'This is usually temporary — it is worth submitting again. If it keeps happening, an admin should check the account and model under “Model & API key”.',
  },
  // Issue #527: the model returned no usable content at all -- distinct
  // from model_output_truncated below, which has a specific, actionable
  // cause (the token budget ran out) this one does not.
  model_empty_content: {
    cause: 'The model returned an empty response, so the review could not be completed.',
    fix: 'This is usually temporary — it is worth submitting again. If it keeps happening, an admin should try a different model under “Model & API key”.',
  },
  // Issue #527: the model was cut off before it finished (its response hit
  // the token budget) -- a reasoning-class model spends part of that budget
  // on internal reasoning before it can produce any output.
  model_output_truncated: {
    cause: 'The model ran out of room to finish its answer, so the review could not be completed.',
    fix: 'This has been recorded. An admin can select a different model under “Model & API key”. If it keeps happening with the same model, whoever operates this deployment needs to raise that model’s reasoning allowance.',
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
  // --- The operator's problem: the activated playbook itself (issue #479) -
  // scripts/review_spine.py's REASON_OPF_KNOWLEDGE_REFUSED /
  // REASON_OPF_DIGEST_MISSING / REASON_FLOOR_INVARIANT_UNJUDGED -- all three
  // are fail-closed outcomes of trying to compose the activated OPF
  // playbook into a review, never something wrong with the submitted
  // document.
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
    fix: 'This has been recorded. It is worth submitting again; if it keeps happening, an admin should check the account and model under “Model & API key”.',
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

// Issue #491: the preflight "What we're looking at" card's plain stats
// line, present regardless of `classification` — deterministic, no-model
// stats render even when the cheap classifier degraded to "unavailable"
// (issue: "classification: unavailable -> show the deterministic stats
// alone, no apology banner"). Never renders the type/side/match language;
// that is PreflightVerdict's job, gated on classification === 'ok'.
function describePreflightStats(preflight: PreflightResult): string {
  const parts: string[] = [
    `~${preflight.wordCount.toLocaleString()} word${preflight.wordCount === 1 ? '' : 's'}`,
    `${preflight.pageEstimate} page${preflight.pageEstimate === 1 ? '' : 's'}`,
  ];
  if (preflight.title) {
    parts.push(`“${preflight.title}”`);
  }
  return parts.join(' · ');
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

// Issue #491: the cheap-model verdict line — type + paper side + the
// server-computed match affirmation/mismatch note. A separate component
// (rather than inlined in the render below) so its early returns
// (`match === null` -> nothing) don't have to fight the surrounding JSX.
// `preflight.agreementTypeGuess`/`.oneLineSummary` are untrusted, sanitized
// text (see preflight.ts's docstring) rendered as plain children only —
// this component never injects raw HTML via React's escape-hatch prop and
// never builds a URL or href from any preflight field.
function PreflightVerdict({
  preflight,
  selectedPlaybookLabel,
}: {
  preflight: PreflightResult;
  selectedPlaybookLabel: string | null;
}): React.ReactElement | null {
  const sideText = describePaperSide(preflight.paperSide);
  const typeText = preflight.agreementTypeGuess;
  const readsLike = [
    typeText ? `reads like a ${typeText}` : null,
    sideText || null,
  ]
    .filter(Boolean)
    .join(' ');

  if (preflight.match === 'likely') {
    return (
      <CtBanner variant="ok" data-testid="review-preflight-match-likely">
        {readsLike ? `This ${readsLike}. ` : ''}Looks like a match for the selected contract
        type.
      </CtBanner>
    );
  }

  if (preflight.match === 'unlikely') {
    // Issue #491's Context offers, parenthetically, "if another installed
    // playbook matches the guess, offer a one-click dial switch." That is
    // NOT built here — a deliberate scope cut, not an oversight: it needs
    // the catalog to carry each playbook's agreement_type (`GET
    // /api/playbooks` today only returns playbook_id/display_name/status),
    // and it is a parenthetical enhancement, not one of the issue's
    // Acceptance criteria. The banner instead just names what it read and
    // points at the dial in words, exactly like the issue's own copy
    // example ("You can toast it anyway -- or turn the dial.").
    return (
      <CtBanner variant="warn" data-testid="review-preflight-match-unlikely">
        {typeText ? `This reads like a ${typeText}` : 'This document'}
        {selectedPlaybookLabel ? `, not ${selectedPlaybookLabel}` : ''}. You can toast it
        anyway — or turn the dial.
      </CtBanner>
    );
  }

  // `match === 'unclear'` (or absent): the server-side verdict has nothing
  // honest to say either way (scripts/preflight_pass.py::compute_match_
  // verdict's own docstring) — no affirmation, no mismatch note, just the
  // neutral type+side line if there is one at all.
  if (!readsLike) {
    return null;
  }
  return (
    <p className="ct-muted" data-testid="review-preflight-type-side">
      {`This ${readsLike}.`}
    </p>
  );
}

// The outcome headline's color (issue #492, redesign item 1) — the same
// --ct-* status tokens ct-chip.ts paints its variants with (ok/warn/danger/
// info/muted), so promoting the outcome from a small chip to the biggest
// text on the panel doesn't also flatten it to one color regardless of what
// happened.
const OUTCOME_HEADLINE_COLOR_VAR: Record<CtChipVariant, string> = {
  ok: '--ct-ok',
  warn: '--ct-warn',
  danger: '--ct-danger',
  info: '--ct-accent',
  muted: '--ct-text-muted',
};

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
  // Markup intensity (issue #495). Per-review like the guidance box, and
  // composed with it at submit time rather than being a second thing the
  // backend has to know about -- browning IS guidance, one sentence of it.
  const [browning, setBrowning] = useState<BrowningLevel>(DEFAULT_BROWNING);
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

  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Completion handoff (issue #448). `readyAnnouncement` populates a live
  // region that is mounted from first render; `saveControlRef` wraps the
  // download action row so focus can be moved onto the real <button> ct-button
  // renders into its light DOM; `handedOffReviewRef` remembers which review has
  // already been handed off so the announcement, the focus move and the
  // automatic save happen exactly once per review — never again on a re-render
  // or a late poll.
  const [readyAnnouncement, setReadyAnnouncement] = useState('');
  // Issue #492: the ONE truthful, visible save line (REDLINE_SAVED_COPY) is
  // gated on this, set true only once autoSaveOutput's fetch has actually
  // resolved for the review currently on screen — never optimistically, and
  // never for a stale save that resolves after a resubmit (same
  // savingReviewId-vs-handedOffReviewRef guard autoSaveOutput already uses
  // for the announcement and downloadError below).
  const [autoSaved, setAutoSaved] = useState(false);
  const saveControlRef = useRef<HTMLDivElement | null>(null);
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
      preflightMatchPlaybookId.current = null;
      return;
    }
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    preflightFor.current = key;
    preflightMatchPlaybookId.current = playbookId;
    setPreflight(null);
    void runPreflight(file, playbookId).then((result) => {
      if (preflightFor.current === key) {
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

  const handleRecordDisposition = useCallback(
    async (outcome: AttorneyDisposition) => {
      if (!reviewId) {
        return;
      }
      setDispositionSaving(outcome);
      setDispositionError(null);
      try {
        const result = await recordDisposition(reviewId, outcome, dispositionNote);
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
  const butterPatRef = useRef<HTMLDivElement | null>(null);

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
        if (butterPatRef.current) {
          butterSlide(butterPatRef.current);
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

  const handleSubmit = useCallback(
    async (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      await submitReview();
    },
    [submitReview],
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
        : // A review the reviewer stopped rests, it does not burn. `error`
          // would give it the burnt-toast treatment and read as a malfunction
          // — the #458 lesson, at its most obvious: nothing went wrong, the
          // user asked for this. The result panel below still renders the
          // terminal outcome ("Stopped"), so nothing is hidden.
          detail.status === 'CANCELLED'
          ? 'idle'
          : 'error';

  // Favicon browning + tab title (issue #497) — reads the same `phase` and
  // `progress_stage` the hero renders from, one map away (stageTheater.ts)
  // from the caption under the glass.
  useTabTheater(phase, detail?.progress_stage ?? null);

  // Issue #494. The lever is armed only when a submission would actually be
  // legitimate — a file chosen, nothing already in flight, no review already
  // running for this panel. That is the SAME condition the submit button's
  // `disabled` expresses, derived once here so the two affordances cannot
  // disagree about whether the appliance is ready. A lever that clicks down
  // and does nothing is worse than one that will not move.
  const leverArmed = Boolean(file) && !submitting && phase !== 'working';

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
    saveControlRef.current?.querySelector('button')?.focus();

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

  // The outcome headline (issue #492, redesign item 1): the SAME
  // outcome→(label, variant) map every other surface renders from (issue
  // #470's describeOutcome), promoted from a small chip to the biggest text
  // in the finished panel. Computed only once `detail` has actually landed
  // — the non-terminal (PENDING/RUNNING) window shows the progress display
  // instead (the hero + CtProgress above), never this.
  const outcome = detail ? describeOutcome(detail.status, detail.decision) : null;

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
            /* Issue #494: pushing the lever IS the submission. It reaches
               `submitReview` — the same function the form's submit button
               reaches — so every guard, every spend and every record lives in
               one place and the two affordances cannot drift. */
            onLeverPull={() => {
              primeAudio();
              playLever();
              void submitReview();
            }}
            leverArmed={leverArmed}
            browning={browning}
            onBrowningChange={(level) => {
              // A detent click per move, through the same muted-aware seam as
              // every other toaster sound. Only on an actual change, so
              // re-clicking the current stop is silent like a real detent.
              if (level !== browning) {
                playDetent();
              }
              setBrowning(level);
            }}
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

          {/* Stop control. Present for the whole working phase — the reported
              failure was a review wedged on step 1 with nothing to press, so
              this must be reachable exactly when nothing else is happening.
              Once a stop is requested the button is replaced by an honest
              status line, not a disabled button: cancellation is cooperative
              and the wait is real (up to one in-flight model call), so
              claiming "stopped" here would be a lie the reviewer could act
              on by closing the tab. */}
          {phase === 'working' && reviewId && (
            <div className="ct-row" data-testid="review-cancel-row">
              {detail?.cancel_requested ? (
                <span data-testid="review-cancel-pending" role="status">
                  Stopping — this finishes the step it is on, then stops.
                </span>
              ) : (
                <CtButton
                  type="button"
                  variant="ghost"
                  disabled={cancelPending}
                  data-testid="review-cancel-button"
                  onClick={() => void handleCancel()}
                >
                  {cancelPending ? 'Stopping…' : 'Stop this review'}
                </CtButton>
              )}
            </div>
          )}
          {cancelError && (
            <CtBanner variant="warn" data-testid="review-cancel-error">
              {cancelError}
            </CtBanner>
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

            {/*
              Issue #491: the "What we're looking at" preflight card.
              Renders as soon as `preflight` arrives (or not at all — never a
              loading placeholder that would make the panel jump); nothing
              here disables or delays the Upload button above/below.

              Injection-defense rider: `preflight.title`, `.agreementTypeGuess`,
              and `.oneLineSummary` are all UNTRUSTED MODEL/DOCUMENT-derived
              text (preflight.ts already re-validates the enum fields, but
              title/summary are free text by construction). Every one of
              them is passed as a plain React child below — never through
              React's raw-HTML escape hatch, never interpolated into an
              `href` or a URL, never passed to a link/markup parser — so the WORST a
              crafted document can do is put inert, on-screen text inside
              this card, exactly like any other reviewer-visible string in
              this panel.
            */}
            {preflight && (
              <CtCard data-testid="review-preflight-card">
                <p className="ct-muted" data-testid="review-preflight-stats">
                  {describePreflightStats(preflight)}
                </p>
                {/*
                  Issue #491 rider item 4: the #506 document-injection scan
                  is folded into this SAME card -- one flag, not two -- and
                  runs (and can render) whether or not the cheap-model
                  classification below is available. `ruleIds` is a fixed
                  set of internal rule identifiers, never document or model
                  text, so joining it straight into this string carries no
                  payload.
                */}
                {preflight.injectionScan && (
                  <CtBanner variant="warn" data-testid="review-preflight-injection-flag">
                    Flagged {preflight.injectionScan.findingCount} item
                    {preflight.injectionScan.findingCount === 1 ? '' : 's'} for review before you
                    upload: {preflight.injectionScan.ruleIds.join(', ')}.
                  </CtBanner>
                )}
                {preflight.classification === 'ok' && (
                  <>
                    <PreflightVerdict
                      preflight={preflight}
                      selectedPlaybookLabel={
                        playbooks.find((entry) => entry.playbook_id === playbookId)
                          ?.display_name ?? null
                      }
                    />
                    {preflight.oneLineSummary && (
                      <p className="ct-muted" data-testid="review-preflight-summary">
                        {preflight.oneLineSummary}
                      </p>
                    )}
                  </>
                )}
              </CtCard>
            )}

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
              {/* Issue #494. The button is the keyboard-first, always-present
                  path and stays exactly as it was; this only tells someone
                  who can see the appliance that the lever above does the same
                  thing. Rendered only when the lever is actually armed —
                  pointing at a control that will not move is worse than
                  saying nothing. */}
              {leverArmed && (
                <span className="ct-muted" data-testid="lever-hint">
                  <small>…or push the toaster’s lever</small>
                </span>
              )}
              <CtIconButton
                type="button"
                label={muted ? 'Sound off' : 'Sound on'}
                aria-pressed={muted}
                onClick={toggle}
                data-testid="sound-toggle"
              >
                {muted ? '🔇 Sound off' : '🔊 Sound on'}
              </CtIconButton>
              {/* Issue #497. Rendered only where the browser Notification API
                  actually exists — an affordance that can never fire anything
                  is worse than no affordance. The click is the ONLY place
                  permission is ever requested (notify.ts); nothing here shows
                  a browser prompt on its own. */}
              {notificationsSupported() && (
                <CtIconButton
                  type="button"
                  label={notifyOptedIn ? 'Notifications on' : 'Notify me when toasts finish'}
                  aria-pressed={notifyOptedIn}
                  onClick={toggleNotify}
                  data-testid="notify-toggle"
                >
                  {notifyOptedIn ? '🔔 Notifications on' : '🔕 Notify me when toasts finish'}
                </CtIconButton>
              )}
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

        Issue #492: `.ct-sr-only` (app.css) — announced, never painted. This
        copy exists to tell someone who cannot see the screen where their
        keyboard focus went; printing it as visible prose too (the original
        bug) told a sighted reader something no one asked the screen. What a
        sighted reader sees instead is the outcome headline and the
        REDLINE_SAVED_COPY line below, inside review-result.
      */}
      <p
        role="status"
        aria-live="polite"
        className="ct-sr-only"
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

      {/*
        The status block below is NOT a live region (issue #510). It wraps the
        copy-id control, the outcome headline, the decision copy, the
        critic-delta indicator and the download row — several things that all
        change in the SAME commit as the terminal poll. Announcing them
        narrated the whole block as one run-on utterance, back to back with
        the purpose-written handoff copy in the sibling region above, on the
        single most important moment in the flow. The handoff region owns
        every terminal announcement now, including failure; this is the
        visual surface only.
      */}
      {reviewId && (
        <div data-testid="review-status" className="ct-stack">
          {/*
            Issue #492: no raw UUID and no bare RUNNING/PENDING chip here —
            the progress display above (the hero + CtProgress) already says
            what is happening while non-terminal, and repeating "In progress"
            here added nothing. What stays, in EVERY state (RUNNING and
            finished alike, per the ticket's AC2), is a way to get the id
            onto the clipboard for a support/diagnostics report — without
            printing the id itself, which no reviewer needs to read off the
            screen.
          */}
          <div className="ct-row" data-testid="review-id-row">
            <CtButton
              type="button"
              variant="ghost"
              size="sm"
              data-testid="review-copy-id-button"
              onClick={() => copyReviewId(reviewId)}
            >
              {copiedReviewId ? 'Copied' : 'Copy review ID'}
            </CtButton>
          </div>

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
              {/*
                Issue #501. "That one burnt." is a headline, never a
                REPLACEMENT for the explanation: the classified cause and next
                step below are untouched, and a test asserts the full text is
                still in the DOM. Burnt is allowed to be charming; it is not
                allowed to be the only thing said.
              */}
              <p data-testid="review-failure-headline">
                <strong>That one burnt.</strong>
              </p>
              <p>
                <strong>{failureExplanation.cause}</strong>
              </p>
              <p>{failureExplanation.fix}</p>
              {/*
                Issue #530: `normalization_notes` is the SAME free-text
                disclosure channel the accepted-changes receipt line reads
                on a successful review (issue #563) — reused here, not a
                second channel, so a refusal carries the per-paragraph
                detail scripts/normalize_input.py already computed (which
                paragraph, and why) instead of the generic reason copy
                above being the only thing shown. Present on ANY fail-
                closed reason, not just unnormalizable_input: a review that
                accepted pending changes before failing at a LATER stage
                (scripts/review_spine.py's post-stage-1 `_terminal()`
                returns) must not have that disclosure silently dropped
                just because the review terminated early.
              */}
              {detail.normalization_notes && (
                <p data-testid="review-failure-normalization-notes">{detail.normalization_notes}</p>
              )}
              {/*
                The retry affordance (issue #501). It clears the burnt review
                so the form is ready again -- it does NOT resubmit: several
                classified causes ("split it into smaller documents", "pick a
                different contract type") need the reviewer to change
                something first, and a one-click resubmit would invite them to
                repeat the same failure. The file selection is cleared for the
                same reason.
              */}
              <CtButton
                type="button"
                variant="secondary"
                data-testid="review-retry-button"
                onClick={() => {
                  stopPolling();
                  setDetail(null);
                  setReviewId(null);
                  setFile(null);
                  setSubmitError(null);
                  setDownloadError(null);
                  setReadyAnnouncement('');
                  handedOffReviewRef.current = null;
                }}
              >
                Toast another slice
              </CtButton>
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

          {/*
            The receipt (issue #498): the review's provenance wearing a
            charming costume. Only on a review that actually FINISHED -- a
            burnt slice gets the failure banner and no slip, because a
            provenance record for a review that produced nothing would be a
            record of nothing.
          */}
          {detail && detail.status === 'DONE' && (
            <ToastReceipt review={detail} playbookName={submittedPlaybookLabel} />
          )}

          {detail && !NON_TERMINAL_STATUSES.has(detail.status) && (
            <div data-testid="review-result" className="ct-stack">
              {/*
                Outcome headline (issue #492, redesign item 1) — the biggest
                text in the panel, driven by the SAME outcome→(label,
                variant) map every other surface renders from (issue #470's
                describeOutcome). No DONE/RUNNING token ever reaches this: by
                the time this block renders, `detail.status` is already
                terminal, and the map turns it (or the more specific
                `decision`) into the label a reviewer actually reads.
              */}
              {outcome && (
                <h3
                  className="ct-outcome-headline"
                  style={{ color: `var(${OUTCOME_HEADLINE_COLOR_VAR[outcome.variant]})` }}
                  data-testid="review-outcome"
                >
                  {outcome.label}
                </h3>
              )}

              {/*
                Issue #492: ACCEPT's legal-safety sentence stays (see the
                comment above `decisionCopy`'s definition) — everything ELSE
                that used to live here, including the attorney-approval
                disclaimer, is gone. Owner policy: attorney/legal review is a
                policy the deploying organization owns entirely outside this
                product, so the panel no longer asserts or nags about it.
              */}
              {decisionCopy && <p>{decisionCopy}</p>}

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
                separate from the binary ACCEPT | REQUEST_CHANGE decision —
                never a legal category.
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
                <div className="ct-stack" data-testid="review-save-block">
                  {/*
                    Issue #492, redesign item 2 — the ONE truthful, VISIBLE
                    save line. Gated on `autoSaved`, which is only ever set
                    once autoSaveOutput's fetch has actually resolved for
                    THIS review (never optimistically, and never for a stale
                    save racing a resubmit — see `autoSaved`'s own comment).
                    Says nothing about focus: that fact is for the aria-live
                    region above, not this line.
                  */}
                  {autoSaved && <p data-testid="review-saved-line">{REDLINE_SAVED_COPY}</p>}
                  {/* ref: the completion handoff moves keyboard focus onto
                      the real <button> inside this row (issue #448). */}
                  <div className="ct-actions" ref={saveControlRef}>
                    <CtButton
                      type="button"
                      variant="primary"
                      onClick={() => void handleDownload()}
                      disabled={downloading}
                      loading={downloading}
                      data-testid="review-download-button"
                    >
                      {downloading ? 'Preparing download…' : 'Download redline'}
                    </CtButton>
                  </div>
                </div>
              )}

              {downloadError && (
                <CtBanner variant="danger" data-testid="review-download-error">
                  {downloadError}
                </CtBanner>
              )}

              {/*
                "Butter it" (issue #499) — drafts the counterparty cover
                email from this review's own analysis artifact. Gated on
                REQUEST_CHANGE + has_output: an ACCEPT review made no
                requested changes, so there is nothing to describe, and the
                backend itself 409s that case — this hides the control
                rather than offering something that only bounces. Copy-only:
                the card below is a read-only record of what was generated;
                Copy puts plain text on the clipboard, nothing is ever sent
                from here.
              */}
              {detail.decision === 'REQUEST_CHANGE' && detail.has_output && (
                <div className="ct-stack" data-testid="review-cover-note">
                  <div ref={butterPatRef} className="ct-butter-pat" aria-hidden="true" />
                  {!coverNoteDraft && (
                    <div className="ct-actions">
                      <CtButton
                        type="button"
                        variant="secondary"
                        size="sm"
                        disabled={coverNoteLoading}
                        loading={coverNoteLoading}
                        data-testid="review-cover-note-butter"
                        onClick={() => void handleButterIt(false)}
                      >
                        {detail.has_cover_note_draft
                          ? 'View cover note draft 🧈'
                          : 'Butter it 🧈'}
                      </CtButton>
                    </div>
                  )}
                  {coverNoteFailed && (
                    <p className="ct-muted" data-testid="review-cover-note-error">
                      <small>
                        {COVER_NOTE_FAILURE_COPY}{' '}
                        <CtButton
                          type="button"
                          variant="ghost"
                          size="sm"
                          data-testid="review-cover-note-retry"
                          onClick={() => void handleButterIt(false)}
                        >
                          Try again
                        </CtButton>
                      </small>
                    </p>
                  )}
                  {coverNoteErrorMessage && (
                    <CtBanner variant="danger" data-testid="review-cover-note-real-error">
                      {coverNoteErrorMessage}
                    </CtBanner>
                  )}
                  {coverNoteDraft && (
                    <CtCard data-testid="review-cover-note-card">
                      <p className="ct-muted" style={{ margin: 0 }}>
                        <small>
                          Draft cover note — copy it into your own email client. Nothing is
                          sent from here.
                        </small>
                      </p>
                      <p
                        data-testid="review-cover-note-text"
                        style={{ whiteSpace: 'pre-wrap' }}
                      >
                        {coverNoteDraft}
                      </p>
                      <div className="ct-actions">
                        <CtButton
                          type="button"
                          variant="primary"
                          size="sm"
                          data-testid="review-cover-note-copy"
                          onClick={() => copyCoverNote(coverNoteDraft)}
                        >
                          {coverNoteCopied ? 'Copied!' : 'Copy'}
                        </CtButton>
                        <CtButton
                          type="button"
                          variant="ghost"
                          size="sm"
                          disabled={coverNoteLoading}
                          loading={coverNoteLoading}
                          data-testid="review-cover-note-regenerate"
                          onClick={() => void handleButterIt(true)}
                        >
                          {coverNoteLastRealCostCents !== null
                            ? `Regenerate (~${formatCostUsdCents(coverNoteLastRealCostCents)})`
                            : 'Regenerate'}
                        </CtButton>
                      </div>
                      <p className="ct-muted" data-testid="review-cover-note-cost">
                        <small>
                          {coverNoteCached
                            ? 'Cached — no charge to view.'
                            : `Cost: ${formatCostUsdCents(coverNoteCostCents ?? 0)}`}
                        </small>
                      </p>
                    </CtCard>
                  )}
                </div>
              )}

              {/*
                Disposition capture (issue #486) — optional, one click, no
                modal. Gated on the SAME dispositionable-status set the
                backend enforces (`disposition.py::DISPOSITIONABLE_REVIEW_
                STATUSES`) rather than a bare `=== 'DONE'` check: `has_output`
                does not gate this — MANUAL_REVIEW_REQUIRED / ERROR_MANUAL_
                REVIEW_REQUIRED are dispositionable too (there is a result,
                even if it isn't a redline), and CANCELLED/QUARANTINED/
                SUPERSEDED are not (nothing was produced to accept/edit/
                reject). See disposition.ts's module docstring for why this
                copy never mentions attorney approval.
              */}
              {DISPOSITIONABLE_STATUSES.has(detail.status) && (
                <div className="ct-stack" data-testid="review-disposition">
                  <p className="ct-muted" style={{ margin: 0 }}>
                    <small>{DISPOSITION_PROMPT_COPY}</small>
                  </p>
                  <div className="ct-actions" role="group" aria-label={DISPOSITION_PROMPT_COPY}>
                    {DISPOSITION_CHOICES.map((choice) => (
                      <CtButton
                        key={choice.value}
                        type="button"
                        variant={detail.attorney_disposition === choice.value ? 'primary' : 'secondary'}
                        size="sm"
                        disabled={dispositionSaving !== null}
                        loading={dispositionSaving === choice.value}
                        data-testid={`review-disposition-${choice.value.toLowerCase()}`}
                        onClick={() => void handleRecordDisposition(choice.value)}
                      >
                        {choice.label}
                      </CtButton>
                    ))}
                  </div>
                  <CtField label="Note (optional)">
                    <input
                      type="text"
                      data-testid="review-disposition-note"
                      value={dispositionNote}
                      onChange={(event) => setDispositionNote(event.target.value)}
                    />
                  </CtField>
                  {detail.attorney_disposition && (
                    <p className="ct-muted" data-testid="review-disposition-recorded">
                      <small>Recorded: {describeDisposition(detail.attorney_disposition)}. {DISPOSITION_RECORD_COPY}</small>
                    </p>
                  )}
                  {dispositionError && (
                    <CtBanner variant="danger" data-testid="review-disposition-error">
                      {dispositionError}
                    </CtBanner>
                  )}
                </div>
              )}

              {/*
                Quiet meta line (issue #492, redesign item 3): filename ·
                contract type · finished-at time — whichever of those three
                this review actually has. `review-submitted-playbook` is the
                pre-existing testid playbook-selector.test.tsx already reads
                ("shows the type in the result view"); kept stable rather
                than renamed so that assertion keeps meaning what it says
                now that the contract-type clause lives in this line instead
                of its own always-visible paragraph.
              */}
              {metaParts.length > 0 && (
                <p className="ct-muted" data-testid="review-meta-line">
                  {metaParts.map((part, i) => (
                    <span key={part.key}>
                      {i > 0 && ' · '}
                      <span data-testid={part.testid}>{part.text}</span>
                    </span>
                  ))}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
