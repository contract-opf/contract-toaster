/**
 * projection.ts — the adapter between ReviewSubmission's existing state and
 * the Orbit Diner console's `ReviewModel` (issue #718, epic #729).
 *
 * ## What this file is, and what it deliberately is not
 *
 * `toReviewModel()` is a PURE function. It fetches nothing, persists nothing,
 * schedules nothing and logs nothing — the standing invariant for every ticket
 * in this epic is that the app stays the one state owner
 * (`docs/planning/2026-09-07-orbit-diner-04p1-final-plan.md` → "Standing
 * invariants"). Everything it returns is a shape conversion of a value
 * `ReviewSubmission` already holds at render time.
 *
 * Two consequences worth stating once:
 *
 *   1. **Already-derived display values arrive as inputs, not as re-derivations.**
 *      `decisionCopy`, `failureExplanation` and `recommendedPlaybook` are
 *      computed inside `ReviewSubmission` today. They are passed in rather
 *      than recomputed here, so the two surfaces cannot drift on copy, and so
 *      this module never has to import `ReviewSubmission.tsx` (which will
 *      import this one — a cycle nothing needs).
 *   2. **Canonical facts are computed here, from the canonical source.**
 *      `receiptLines` is the exception that proves the rule: the console needs
 *      a `string[]`, and the only honest way to produce it is our own
 *      `receiptLines(detail)` rendered through `receiptText()`. It is never
 *      rebuilt from anything visible on screen.
 *
 * ## The status mapping, and the one place it cannot be total
 *
 * `Status` is the kit's closed union. `QUARANTINED` and `SUPERSEDED` — the two
 * post-terminal administrative overlays (outcome.ts) — have no member in it.
 * They resolve to `ERROR` for the console's phase, and the real outcome is
 * carried alongside as a `support`-scoped message built from `describeOutcome`,
 * so an overlay is never silently reported as a burnt review. `MANUAL_REVIEW_REQUIRED`
 * and `ERROR_MANUAL_REVIEW_REQUIRED` are preserved distinctly and never travel
 * through that generic phase — the whole point of the kit's separate manual state.
 */
import { COVER_NOTE_FAILURE_COPY } from '../coverNote';
import { GUIDANCE_PRECEDENCE_COPY } from '../guidancePrecedenceCopy';
import type { AttorneyDisposition } from '../disposition';
import {
  INTERNAL_NOTES_DISCLOSURE,
  notesModeSetting,
  type NotesMode as AppNotesMode,
} from '../notesMode';
import { describeOutcome } from '../outcome';
import type { PreflightResult } from '../preflight';
import { browningSetting, type BrowningLevel } from '../toaster/browning';
import { receiptLines, receiptText, type ReceiptSource } from '../toaster/receipt';
import type { ActionHandlers, PreferenceHandlers } from './connect';
import { connectActions, connectPreferences } from './connect';
import type {
  Action,
  Cost,
  Disposition,
  Intensity,
  Message,
  NotesMode,
  Playbook,
  Preferences,
  Preflight,
  Result,
  ReviewModel,
  Status,
} from './types';

// ---------------------------------------------------------------------------
// Input shapes
// ---------------------------------------------------------------------------

/**
 * The subset of `get_review_detail`'s projection this adapter reads. Declared
 * structurally (rather than importing `ReviewSubmission`'s own private
 * `ReviewDetail`) so this module stays free of a cycle, and extended from
 * `ReceiptSource` so what is handed to `receiptLines()` is the same record by
 * construction rather than by a cast.
 */
export interface ReviewDetailLike extends ReceiptSource {
  status: string;
  has_output?: boolean;
  /** Recorded as a pointer only, the same way `has_output` is. */
  has_input?: boolean;
  progress_stage?: string | null;
  confidence_band?: string | null;
  critic_delta?: {
    contested_replacements?: { critic_objection?: string | null; critic_suggested_replacement?: string | null }[] | null;
    added_issues?: unknown[] | null;
    contested_issue_ids?: unknown;
  } | null;
  failing_stage?: string | null;
  reason?: string | null;
  cancel_requested?: boolean | null;
  attorney_disposition?: string | null;
  /**
   * Issue #518, unmerged: absent on every review today. When it lands it may
   * supply DISPLAY text for a restored, result-only view — it never arms
   * submit, which is `fileSelected`'s job and `fileSelected`'s alone.
   */
  original_filename?: string | null;
}

/** The failure cause/fix pair `ReviewSubmission` already resolves through its
 *  exported `explainFailure` — passed in, never re-derived. */
export interface FailureExplanationLike {
  cause: string;
  fix: string;
}

/**
 * Every value `toReviewModel` reads. Each field names the `ReviewSubmission`
 * state (or already-computed render value) it comes from, so the wiring in
 * #719 is a transcription rather than a decision.
 */
export interface ReviewProjectionState {
  // --- file + submission ---------------------------------------------------
  /** `file` — the REAL retained File. Nothing else may stand in for it. */
  file: File | null;
  /** `submitting` — an outstanding upload. */
  submitting: boolean;
  /** `reviewId` */
  reviewId: string | null;
  /** `detail` — null until the first poll lands. */
  detail: ReviewDetailLike | null;
  /** `submittedFilename` — frozen at submit; display text only. */
  submittedFilename?: string | null;
  /** `submittedPlaybookLabel` — frozen at submit; the receipt's playbook name. */
  submittedPlaybookLabel?: string | null;
  /** `submittedResumed` — the submit landed on a pre-existing review. */
  submittedResumed?: boolean;

  // --- preferences ---------------------------------------------------------
  /** `playbookId` */
  playbookId: string;
  /** `browning` */
  browning: BrowningLevel;
  /** `notesMode` */
  notesMode: AppNotesMode;
  /** `toasterGuidance` — what is CURRENTLY typed. Never `submittedGuidance`. */
  toasterGuidance: string;
  /**
   * What the finished review ACTUALLY ran under, already resolved by the panel
   * (issue #733): the server's record when it has one, the value frozen at
   * submit time when it does not, and NOTHING on a resumed submission — the
   * idempotency key does not cover guidance, so text typed into a re-drop
   * governed nothing and must not be shown as though it had. That rule lives
   * in `ReviewSubmission` because that is where the frozen value lives;
   * re-deriving it here from `detail.toaster_guidance` alone silently dropped
   * the fallback and the carve-out.
   */
  appliedGuidance: string | null;
  /** The composed meta-line parts (issue #492): filename, type, finished-at. */
  metaParts?: readonly { testid: string; text: string }[];
  /** "This reads like …" — the preflight's neutral type/side sentence (#491). */
  preflightReadsLike?: string;
  /** The preflight mismatch sentence, naming type and selected playbook. */
  preflightMismatchNote?: string;
  /** The composed completion-handoff announcement (issues #448/#492). */
  readyAnnouncement?: string;
  /** `dispositionNote` */
  dispositionNote: string;
  /** #730 supplies this; until then the selection is always the user's. */
  playbookSelection?: 'automatic' | 'user';

  // --- catalog -------------------------------------------------------------
  /** `playbooks` */
  playbooks: readonly Playbook[];
  /** `notesModeInternalAvailable` */
  notesModeInternalAvailable: boolean;

  // --- preflight -----------------------------------------------------------
  /** `preflight` */
  preflight?: PreflightResult | null;
  /** A full preflight request is in flight for the current file. */
  preflightPending?: boolean;
  /** `findMatchingPlaybook`'s result. Gated on `status === 'active'` below. */
  recommendedPlaybook?: Playbook | null;

  // --- money ---------------------------------------------------------------
  /** `reviewCostUsdCents` — the deployment's typical per-review estimate. */
  reviewCostUsdCents?: number | null;
  /**
   * The estimate CAPTURED at submit time for the review now on screen (#735,
   * owner decision H2). `undefined` means nothing has been submitted in this
   * session, so the live estimate above is the right thing to show; `null`
   * records that a submit was made with no estimate to capture, which is a
   * different fact and must not fall back to a figure read afterwards for a
   * different document.
   */
  capturedReviewCostUsdCents?: number | null;
  /** `coverNoteCostCents` — kept separate; never folded into the review cost. */
  coverNoteCostCents?: number | null;
  /** The last NON-cached generation's cost — what a Regenerate press costs. */
  coverNoteLastRealCostCents?: number | null;
  /** True only for a signed-in admin. Gates `adminDaily` entirely. */
  isAdmin?: boolean;
  /** An authorized aggregate ALREADY in memory. Never fetched for a reviewer. */
  adminDaily?: Cost['adminDaily'];

  // --- cover note ----------------------------------------------------------
  coverNoteDraft?: string | null;
  coverNoteCached?: boolean;
  coverNoteLoading?: boolean;
  /** The retryable 502 channel. */
  coverNoteFailed?: boolean;
  /** The non-retryable channel (404/403/409, or the fetch itself failing). */
  coverNoteErrorMessage?: string | null;
  coverNoteCopied?: boolean;

  // --- disposition ---------------------------------------------------------
  dispositionSaving?: AttorneyDisposition | null;
  dispositionError?: string | null;

  // --- download / cancel ---------------------------------------------------
  downloading?: boolean;
  /** `autoSaved` — the once-only save actually resolved for this review. */
  downloadStarted?: boolean;
  cancelPending?: boolean;

  // --- already-classified copy --------------------------------------------
  submitError?: string | null;
  pollError?: string | null;
  downloadError?: string | null;
  catalogError?: string | null;
  notesModeSaveError?: string | null;
  cancelError?: string | null;
  /** `decisionCopy` — the ACCEPT sentence, or the backend's own `message`. */
  decisionCopy?: string | null;
  /** `explainFailure(detail)` */
  failureExplanation?: FailureExplanationLike | null;
  /** The backend's "could not classify" token, suppressed from `result.reason`. */
  unclassifiedReason?: string;

  // --- local confirmations -------------------------------------------------
  copiedReviewId?: boolean;
  receiptCopied?: boolean;
  receiptSaved?: boolean;
  /**
   * The console's one-shot receipt motion and its paper-tear cue (#723).
   *
   * A monotonic COUNT of real copies/saves, not a flag, because
   * `toReviewModel` is pure and runs on every render: an id minted in here
   * would differ on every render and the console would replay the tear tug
   * forever, and a boolean carries no way to tell a second copy from the
   * first. `seq` moves only when a copy or a save actually succeeded, so the
   * projected `motionEvent.id` is stable between those moments and unique
   * across them — which is exactly what `OrbitDiner`'s `lastEvent` ref
   * compares against.
   *
   * The two-second confirmation MESSAGE stays on the two booleans above;
   * they answer a different question ("say so, briefly") and clear themselves.
   */
  receiptAction?: { seq: number; kind: 'copy' | 'save' };

  // --- sound / notification ------------------------------------------------
  muted: boolean;
  notificationsSupported: boolean;
  notifyOptedIn?: boolean;
  notificationPermission?: 'default' | 'granted' | 'denied';

  // --- retained history ----------------------------------------------------
  history?: ReviewModel['history'];
  runAgainAvailable?: boolean;
  /** #501: omitted until a real lifetime completed-review count exists. */
  odometer?: number;
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

/** Statuses the kit's own union carries, preserved verbatim. */
const PASSTHROUGH_STATUSES: ReadonlySet<string> = new Set<Status>([
  'PENDING',
  'RUNNING',
  'DONE',
  'ERROR',
  'MANUAL_REVIEW_REQUIRED',
  'ERROR_MANUAL_REVIEW_REQUIRED',
  'CANCELLED',
]);

const TERMINAL_STATUSES: ReadonlySet<Status> = new Set<Status>([
  'DONE',
  'ERROR',
  'MANUAL_REVIEW_REQUIRED',
  'ERROR_MANUAL_REVIEW_REQUIRED',
  'CANCELLED',
]);

const DEFAULT_UNCLASSIFIED_REASON = 'unhandled_exception';

const DISPOSITIONS: ReadonlySet<string> = new Set<Disposition>([
  'ACCEPTED',
  'EDITED',
  'REJECTED',
]);

function projectStatus(state: ReviewProjectionState): Status {
  // An outstanding upload outranks everything: there is a file, there may even
  // be a previous review still on screen, and neither is what is happening.
  if (state.submitting) {
    return 'SUBMITTING';
  }
  if (!state.reviewId) {
    return state.file ? 'LOADED' : 'EMPTY';
  }
  // Submitted, nothing polled back yet — the pipeline's own first status.
  if (!state.detail) {
    return 'PENDING';
  }
  if (PASSTHROUGH_STATUSES.has(state.detail.status)) {
    return state.detail.status as Status;
  }
  // QUARANTINED / SUPERSEDED (and any future terminal status this bundle has
  // not caught up with). See the module docstring: the phase degrades, the
  // outcome itself is carried in `messages` rather than lost.
  return 'ERROR';
}

// ---------------------------------------------------------------------------
// Money
// ---------------------------------------------------------------------------

/**
 * Today's endpoint answers with a deployment-wide TYPICAL estimate, so that is
 * exactly what is claimed: `basis:'typical'`. `'document-model'` is reserved
 * for a backend estimate actually computed for this document and model policy
 * (#735), a hold is only ever an authorized server projection (never the
 * estimate multiplied by anything), and a non-positive estimate is
 * `unavailable` rather than an inferred `$0.00` — the same positivity check
 * `ReviewSubmission` already applies before it renders the figure at all.
 */
function projectCost(state: ReviewProjectionState): Cost {
  // Owner decision H2 (#735): once something has been submitted, the figure on
  // screen belongs to THAT review. `GET /api/review-cost-estimate` answers for
  // the NEXT one, and letting a later read of it move a finished review's
  // number would misreport what that review was quoted at. `undefined` is the
  // only value that means "nothing captured yet"; a captured `null` is a
  // submit made with no estimate and stays `unavailable`.
  const cents =
    state.capturedReviewCostUsdCents === undefined
      ? state.reviewCostUsdCents
      : state.capturedReviewCostUsdCents;
  const cost: Cost =
    typeof cents === 'number' && cents > 0
      ? { kind: 'estimated', cents, basis: 'typical' }
      : { kind: 'unavailable' };
  if (typeof state.coverNoteCostCents === 'number') {
    cost.coverCents = state.coverNoteCostCents;
  }
  // `adminDaily` is an authorized aggregate or it is nothing. Nothing here
  // ever calls /api/admin/spend, and a reviewer never carries the field.
  if (state.isAdmin === true && state.adminDaily) {
    cost.adminDaily = state.adminDaily;
  }
  return cost;
}

// ---------------------------------------------------------------------------
// Preflight
// ---------------------------------------------------------------------------

function projectPreflight(state: ReviewProjectionState): Preflight | undefined {
  const result = state.preflight;
  if (!result) {
    return state.preflightPending ? { state: 'checking' } : undefined;
  }
  const projected: Preflight = {
    state: state.preflightPending
      ? 'checking'
      : result.classification === 'ok'
        ? 'ready'
        : 'unavailable',
    classification: result.classification,
    wordCount: result.wordCount,
    pageEstimate: result.pageEstimate,
    paragraphCount: result.paragraphCount,
  };
  if (result.oneLineSummary) {
    projected.summary = result.oneLineSummary;
  }
  if (result.title) {
    projected.title = result.title;
  }
  if (result.match) {
    projected.match = result.match;
  }
  if (state.preflightReadsLike) {
    projected.readsLike = state.preflightReadsLike;
  }
  if (state.preflightMismatchNote) {
    projected.mismatchNote = state.preflightMismatchNote;
  }
  // Only an ACTIVE catalog entry can be recommended: a coming-soon playbook
  // reaches the backend's "no active playbook" 503, and recommending one would
  // be an invitation to that error. `preflightPlaybookChoice` (autoPlaybook.ts)
  // applies the same rule again on the selection side under #730.
  const recommended = state.recommendedPlaybook;
  if (recommended && recommended.status === 'active') {
    projected.recommendedPlaybookId = recommended.playbook_id;
    projected.recommendedPlaybookName = recommended.display_name;
  }
  // Ids and counts only. The scan carries no locator and no document text.
  if (result.injectionScan) {
    projected.injectionCount = result.injectionScan.findingCount;
    projected.injectionRuleIds = result.injectionScan.ruleIds;
  }
  return projected;
}

// ---------------------------------------------------------------------------
// Result
// ---------------------------------------------------------------------------

function projectResult(
  state: ReviewProjectionState,
  status: Status,
): Result | undefined {
  const detail = state.detail;
  if (!detail || !TERMINAL_STATUSES.has(status)) {
    return undefined;
  }
  const outcome = describeOutcome(detail.status, detail.decision);
  const result: Result = { outcome: outcome.label };
  if (detail.decision) {
    result.decision = detail.decision;
  }
  if (state.decisionCopy) {
    result.copy = state.decisionCopy;
  }
  if (detail.confidence_band) {
    result.confidenceBand = detail.confidence_band;
  }
  // "Only supply counts with an actual field" — an absent `issues` means the
  // count is unknown, which is not the same as zero.
  if (Array.isArray(detail.issues)) {
    result.issueCount = detail.issues.length;
  }
  const delta = detail.critic_delta;
  if (delta) {
    const contested = delta.contested_replacements ?? [];
    const added = delta.added_issues ?? [];
    result.criticContested = contested.length;
    result.criticAdded = added.length;
    const details: string[] = [];
    for (const item of contested) {
      if (item.critic_objection) {
        details.push(`Critic flagged this replacement: ${item.critic_objection}`);
      }
      if (item.critic_suggested_replacement) {
        details.push(`Critic suggestion: ${item.critic_suggested_replacement}`);
      }
    }
    if (details.length > 0) {
      result.criticDetails = details;
    }
  }
  const metadata: { label: string; value: string }[] = [];
  if (detail.playbook_version) {
    metadata.push({ label: 'Playbook version', value: `v${detail.playbook_version}` });
  }
  if (detail.instructions_version) {
    metadata.push({
      label: 'Standing instructions',
      value: `v${detail.instructions_version}`,
    });
  }
  if (detail.primary_model_id) {
    metadata.push({ label: 'Primary', value: detail.primary_model_id });
  }
  if (detail.critic_model_id) {
    metadata.push({ label: 'Critic', value: detail.critic_model_id });
  }
  if (metadata.length > 0) {
    result.metadata = metadata;
  }
  if (state.metaParts?.length) {
    result.meta = state.metaParts.map((part) => ({ id: part.testid, text: part.text }));
  }
  // The guidance the review ACTUALLY ran under — resolved by the panel, never
  // the box's current contents.
  if (state.appliedGuidance) {
    result.appliedGuidance = state.appliedGuidance;
  }
  if (state.failureExplanation) {
    result.failureCause = state.failureExplanation.cause;
    result.failureFix = state.failureExplanation.fix;
  }
  if (detail.normalization_notes) {
    result.normalizationNotes = detail.normalization_notes;
  }
  const unclassified = state.unclassifiedReason ?? DEFAULT_UNCLASSIFIED_REASON;
  if (detail.reason && detail.reason !== unclassified) {
    result.reason = detail.reason;
  }
  if (detail.failing_stage) {
    result.failingStage = detail.failing_stage;
  }
  return result;
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------

/**
 * Each channel gets one stable id and one classified scope. Titles are the
 * short line the register glass shows; the app's own already-friendly string
 * is the detail beneath it. Nothing here reads a server exception, a stack, or
 * any document text — every input is a string the app was already rendering.
 */
function projectMessages(
  state: ReviewProjectionState,
  status: Status,
): Message[] | undefined {
  const messages: Message[] = [];
  const push = (message: Message) => messages.push(message);

  // Each of the four channels that used to own a banner of its own carries
  // its own retry key (#726). The ACTION is the only thing that differs — the
  // wording is the app's, and the handler on the other side of each is the
  // one `ReviewSubmission` already guards.
  if (state.submitError) {
    push({
      id: 'submit-error',
      scope: 'submit',
      tone: 'error',
      title: "That didn't go through",
      detail: state.submitError,
      // The existing lever handler, not a second submit path: it re-checks
      // `file` and refuses with the same copy when there is nothing to send.
      action: 'submit',
      actionLabel: 'Try again',
    });
  }
  if (state.pollError) {
    push({
      id: 'poll-error',
      scope: 'poll',
      // The channel is degraded; the REVIEW is not. `projectStatus` never
      // reads `pollError`, so the last real state stays on screen, and the
      // console renders this scope with status semantics rather than as an
      // alert (OrbitDiner's `messageRole`). The poller is already retrying on
      // its own backoff — this key only asks it to go now.
      tone: 'error',
      title: 'Still checking',
      detail: state.pollError,
      action: 'poll-retry',
      actionLabel: 'Check now',
    });
  }
  if (state.catalogError) {
    push({
      id: 'catalog-error',
      scope: 'catalog',
      tone: 'error',
      title: 'Contract types unavailable',
      detail: state.catalogError,
      action: 'catalog-retry',
      actionLabel: 'Reload contract types',
    });
  }
  if (state.notesModeSaveError) {
    push({
      id: 'preference-error',
      scope: 'preference',
      tone: 'error',
      title: "Preference didn't save",
      detail: state.notesModeSaveError,
      action: 'preference-retry',
      actionLabel: 'Retry',
    });
  }
  if (state.cancelError) {
    push({
      id: 'cancel-error',
      scope: 'cancel',
      tone: 'error',
      title: "Couldn't stop that one",
      detail: state.cancelError,
    });
  }
  if (state.downloadError) {
    push({
      id: 'download-error',
      scope: 'download',
      tone: 'error',
      title: "Couldn't prepare the download",
      detail: state.downloadError,
    });
  }
  if (state.dispositionError) {
    push({
      id: 'disposition-error',
      scope: 'disposition',
      tone: 'error',
      title: 'Not recorded',
      detail: state.dispositionError,
    });
  }
  // The two cover-note failure channels stay distinct: a 502 is worth another
  // press, a 404/403/409 is not, and offering Retry on the second would be a
  // lie the button tells.
  if (state.coverNoteFailed) {
    push({
      id: 'cover-error',
      scope: 'cover',
      tone: 'error',
      title: COVER_NOTE_FAILURE_COPY,
      action: 'cover-retry',
      actionLabel: 'Try again',
    });
  }
  if (state.coverNoteErrorMessage) {
    push({
      id: 'cover-failed',
      scope: 'cover',
      tone: 'error',
      title: "Couldn't butter this one",
      detail: state.coverNoteErrorMessage,
    });
  }
  // A terminal status the kit's union cannot name still has to say what it is.
  if (status === 'ERROR' && state.detail && !PASSTHROUGH_STATUSES.has(state.detail.status)) {
    push({
      id: 'outcome-overlay',
      scope: 'support',
      title: describeOutcome(state.detail.status, state.detail.decision).label,
    });
  }
  if (state.submittedResumed && state.reviewId) {
    push({
      id: 'resumed',
      scope: 'submit',
      tone: 'info',
      title: 'Picked up your earlier review',
    });
  }
  if (state.coverNoteCopied) {
    push({ id: 'cover-copied', scope: 'cover', tone: 'success', title: 'Cover note copied' });
  }
  if (state.receiptCopied) {
    push({ id: 'receipt-copied', scope: 'receipt', tone: 'success', title: 'Receipt copied' });
  }
  if (state.receiptSaved) {
    push({ id: 'receipt-saved', scope: 'receipt', tone: 'success', title: 'Receipt saved' });
  }
  if (state.copiedReviewId) {
    push({ id: 'review-id-copied', scope: 'support', tone: 'success', title: 'Review ID copied' });
  }
  return messages.length > 0 ? messages : undefined;
}

// ---------------------------------------------------------------------------
// The projection
// ---------------------------------------------------------------------------

/**
 * `ReviewSubmission`'s state → the console's `ReviewModel`. Pure: same input,
 * same output, no side effects of any kind.
 */
export function toReviewModel(state: ReviewProjectionState): ReviewModel {
  const status = projectStatus(state);
  const detail = state.detail;
  const terminal = TERMINAL_STATUSES.has(status);

  // The retained File, and nothing else, arms submit. A resumed record's
  // `original_filename` is display text; it can never stand in for the bytes.
  const fileSelected = state.file instanceof File;
  const filename = fileSelected
    ? state.file!.name
    : (state.submittedFilename ?? detail?.original_filename ?? undefined);

  const browning = browningSetting(state.browning);
  const notes = notesModeSetting(state.notesMode);

  const preferences: Preferences = {
    playbookId: state.playbookId,
    intensity: state.browning as Intensity,
    notesMode: state.notesMode as NotesMode,
    // What is CURRENTLY typed. Deriving this from the record on every poll is
    // how an edited box silently reverts mid-review.
    instructions: state.toasterGuidance,
    dispositionNote: state.dispositionNote,
  };

  const model: ReviewModel = {
    status,
    fileSelected,
    preferences,
    playbooks: [...state.playbooks],
    internalNotesAvailable: state.notesModeInternalAvailable,
    // The exact strings the existing controls print — the readback is the
    // submitted sentence itself, not a paraphrase of it.
    browningReadback: browning.sentence
      ? `${browning.note} “${browning.sentence}”`
      : browning.note,
    browningNote: browning.note,
    // One sentence, one owner: the same constant AdminInstructions.tsx and
    // the existing per-review field print (issue #484).
    guidancePrecedence: `Your instructions ${GUIDANCE_PRECEDENCE_COPY}`,
    readyAnnouncement: state.readyAnnouncement,
    cost: projectCost(state),
    muted: state.muted,
    notification: !state.notificationsSupported
      ? 'unsupported'
      : state.notificationPermission === 'denied'
        ? 'denied'
        : state.notifyOptedIn && state.notificationPermission === 'granted'
          ? 'granted'
          : 'off',
  };

  if (browning.sentence) {
    model.browningSentence = browning.sentence;
  }
  if (notes.carriesInternalNotes) {
    model.notesDisclosure = INTERNAL_NOTES_DISCLOSURE;
  }
  if (filename) {
    model.filename = filename;
  }
  if (fileSelected) {
    model.fileBytes = state.file!.size;
  }
  if (state.playbookSelection) {
    model.playbookSelection = state.playbookSelection;
  }
  if (state.reviewId) {
    model.reviewId = state.reviewId;
  }
  if (detail) {
    // Unknown stage strings pass through deliberately: `stageInfo` refuses
    // what it does not know, which beats this adapter inventing a step.
    model.stage = detail.progress_stage ?? null;
    model.hasInput = detail.has_input === true;
    model.hasOutput = detail.has_output === true;
  }

  const preflight = projectPreflight(state);
  if (preflight) {
    model.preflight = preflight;
  }
  const result = projectResult(state, status);
  if (result) {
    model.result = result;
  }

  // The canonical slip, only on DONE, only from `receiptLines(detail)`.
  if (status === 'DONE' && detail) {
    model.receiptLines = receiptText(
      receiptLines(detail, state.submittedPlaybookLabel),
    ).split('\n');
  }

  if (detail || state.coverNoteLoading || state.coverNoteDraft) {
    const coverState = state.coverNoteLoading
      ? 'loading'
      : state.coverNoteFailed || state.coverNoteErrorMessage
        ? 'error'
        : state.coverNoteDraft
          ? 'ready'
          : 'idle';
    model.cover = { state: coverState };
    if (state.coverNoteDraft) {
      model.cover.draft = state.coverNoteDraft;
    }
    if (state.coverNoteCached) {
      model.cover.cached = true;
    }
    const lastRealCost = state.coverNoteLastRealCostCents ?? state.coverNoteCostCents;
    if (typeof lastRealCost === 'number') {
      model.cover.lastCostCents = lastRealCost;
    }
    if (coverState === 'error') {
      model.cover.error = state.coverNoteErrorMessage ?? COVER_NOTE_FAILURE_COPY;
      // Only the backend's own "couldn't generate this one" (502) is worth
      // another press.
      model.cover.retryable = state.coverNoteFailed === true && !state.coverNoteErrorMessage;
    }
  }

  if (detail) {
    const recorded = detail.attorney_disposition;
    model.disposition = { saving: state.dispositionSaving != null };
    if (recorded && DISPOSITIONS.has(recorded)) {
      model.disposition.recorded = recorded as Disposition;
    }
  }

  if (state.downloading) {
    model.downloading = true;
  }
  if (state.downloadStarted) {
    model.downloadStarted = true;
  }
  // A stop that has not taken effect yet is real and is shown. Once the review
  // is terminal the request is spent, whatever flag is still set locally.
  if (!terminal && (state.cancelPending === true || detail?.cancel_requested === true)) {
    model.cancelRequested = true;
  }
  if (state.submittedResumed) {
    model.resumed = true;
  }
  // The one semantic motion event the console cannot infer from a model diff
  // (#723). Everything else it plays — the slice, the lever, the stage glow,
  // the pop, the till — is a visible CHANGE between two models, so `inferMotion`
  // derives it. A receipt copy or save changes nothing on the model that the
  // console could read, and it is the only event `MOTION-AND-SOUND.md` lists
  // whose trigger is "the action succeeded" rather than "the state moved".
  if (state.receiptAction) {
    model.motionEvent = {
      id: `receipt-${state.receiptAction.kind}-${state.receiptAction.seq}`,
      type: state.receiptAction.kind === 'save' ? 'receipt-save' : 'receipt-copy',
    };
  }

  const messages = projectMessages(state, status);
  if (messages) {
    model.messages = messages;
  }
  if (state.history) {
    model.history = state.history;
  }
  if (state.runAgainAvailable !== undefined) {
    model.runAgainAvailable = state.runAgainAvailable;
  }
  if (state.odometer !== undefined) {
    model.odometer = state.odometer;
  }
  return model;
}

// ---------------------------------------------------------------------------
// Callback adapters
// ---------------------------------------------------------------------------

/**
 * The existing guarded handlers, named as `ReviewSubmission` names them. The
 * adapters below own no state and make no decisions — every entry is a
 * one-to-one hand-off, so a guard added to a handler cannot be bypassed by
 * going through the console.
 */
export interface ReviewSubmissionCallbacks {
  /** `submitReview` */
  submitReview: () => void;
  /** `handleCancel` */
  handleCancel: () => void;
  /**
   * The "Toast another slice" reset. NOT a resubmit: several classified
   * failure causes need the reviewer to change something first, and a
   * one-click retry would invite them to repeat the same failure.
   */
  resetForRetry: () => void;
  /** `handleDownload` */
  handleDownload: () => void;
  /** History's per-row original-document download. */
  downloadInput: () => void;
  /** `copyReviewId` */
  copyReviewId: () => void;
  /** The receipt's existing "Copy as text" helper. */
  copyReceipt: () => void;
  /** The receipt's existing image export. */
  saveReceipt: () => void;
  /** `handleButterIt` — `true` pays for a fresh draft. */
  handleButterIt: (regenerate: boolean) => void;
  /** `copyCoverNote` */
  copyCoverNote: () => void;
  /** `saveNotesModePreference` retry. */
  retryPreference: () => void;
  /** Poll `GET /api/reviews/{id}` now, instead of waiting out the backoff. */
  retryPoll: () => void;
  /** `fetchCatalog` — re-read `GET /api/playbooks`. */
  retryCatalog: () => void;
  /** The existing sound-mute toggle. */
  toggleSound: () => void;
  /** `useNotifyPreference().toggle` */
  toggleNotifications: () => void;
  /** Navigate to the History tab. */
  openHistory: () => void;
  /** History's existing run-again helper. */
  runAgain: (reviewId: string) => void;
  /** Apply the preflight recommendation — `setPlaybookId` with the matched id. */
  switchToRecommendedPlaybook: () => void;
  /** `handleRecordDisposition` */
  handleRecordDisposition: (outcome: AttorneyDisposition, note: string) => void;
  /** `setPlaybookId` */
  setPlaybookId: (playbookId: string) => void;
  /** `handleBrowningChange` */
  handleBrowningChange: (level: BrowningLevel) => void;
  /** `handleNotesModeChange` */
  handleNotesModeChange: (mode: AppNotesMode) => void;
  /** `setToasterGuidance` */
  setToasterGuidance: (text: string) => void;
  /** `setDispositionNote` */
  setDispositionNote: (text: string) => void;
  /** The existing file-chosen path (preflight, validation and all). */
  selectFile: (file: File) => void;
  /** The REAL clear/reset path. Never a no-op that leaves a stale File. */
  clearFile: () => void;
}

/**
 * Every member of `ActionHandlers`, connected. The return type is what makes
 * that a compile-time fact rather than a promise: omitting one is a type
 * error, not a dead button.
 */
export function reviewActionHandlers(cb: ReviewSubmissionCallbacks): ActionHandlers {
  return {
    submit: () => cb.submitReview(),
    cancel: () => cb.handleCancel(),
    retry: () => cb.resetForRetry(),
    download: () => cb.handleDownload(),
    downloadInput: () => cb.downloadInput(),
    copyId: () => cb.copyReviewId(),
    receiptCopy: () => cb.copyReceipt(),
    receiptSave: () => cb.saveReceipt(),
    butter: () => cb.handleButterIt(false),
    coverCopy: () => cb.copyCoverNote(),
    coverRegenerate: () => cb.handleButterIt(true),
    coverRetry: () => cb.handleButterIt(false),
    preferenceRetry: () => cb.retryPreference(),
    pollRetry: () => cb.retryPoll(),
    catalogRetry: () => cb.retryCatalog(),
    soundToggle: () => cb.toggleSound(),
    notificationToggle: () => cb.toggleNotifications(),
    history: () => cb.openHistory(),
    runAgain: (reviewId: string) => cb.runAgain(reviewId),
    switchRecommended: () => cb.switchToRecommendedPlaybook(),
    disposition: (outcome: Disposition, note: string) =>
      cb.handleRecordDisposition(outcome as AttorneyDisposition, note),
  };
}

/** Every member of `PreferenceHandlers`, connected to the existing setters. */
export function reviewPreferenceHandlers(
  cb: ReviewSubmissionCallbacks,
): PreferenceHandlers {
  return {
    playbook: (id: string) => cb.setPlaybookId(id),
    intensity: (value: Intensity) => cb.handleBrowningChange(value as BrowningLevel),
    notesMode: (value: NotesMode) => cb.handleNotesModeChange(value as AppNotesMode),
    instructions: (text: string) => cb.setToasterGuidance(text),
    dispositionNote: (text: string) => cb.setDispositionNote(text),
  };
}

/**
 * The three console callbacks, ready to spread onto `<OrbitDiner>`. Exported
 * as wiring only — #719 owns actually rendering it.
 *
 * `onFile(null)` routes to the real clear/reset path. A component that merely
 * blanked its own display would leave the previous `File` retained behind an
 * empty toast slot, which is the one file-handling bug this contract names.
 */
export function connectReviewSubmission(cb: ReviewSubmissionCallbacks): {
  onFile: (file: File | null) => void;
  onPreferences: (patch: Partial<Preferences>) => void;
  onAction: (action: Action) => void;
} {
  return {
    onFile: (file) => (file ? cb.selectFile(file) : cb.clearFile()),
    onPreferences: connectPreferences(reviewPreferenceHandlers(cb)),
    onAction: connectActions(reviewActionHandlers(cb)),
  };
}
