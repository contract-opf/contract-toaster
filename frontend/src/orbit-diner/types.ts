export type Status =
  | "EMPTY"
  | "LOADED"
  | "SUBMITTING"
  | "PENDING"
  | "RUNNING"
  | "DONE"
  | "ERROR"
  | "MANUAL_REVIEW_REQUIRED"
  | "ERROR_MANUAL_REVIEW_REQUIRED"
  | "CANCELLED";
export type Stage =
  | "primary_pass"
  | "critic_pass"
  | "reconciliation"
  | "redline";
export type Intensity = "light" | "medium" | "dark";
export type NotesMode = "none" | "external" | "internal" | "both";
export type Disposition = "ACCEPTED" | "EDITED" | "REJECTED";
export interface Playbook {
  playbook_id: string;
  display_name: string;
  status: string;
}
export interface Preferences {
  playbookId: string;
  intensity: Intensity;
  notesMode: NotesMode;
  instructions: string;
  dispositionNote: string;
}
export interface Message {
  id: string;
  /** Explicit success confirmations may stay local; unknown/error messages are never silently discarded. */
  tone?: "info" | "success" | "error";
  scope:
    | "submit"
    | "catalog"
    | "preference"
    | "download"
    | "disposition"
    | "poll"
    | "cancel"
    | "cover"
    | "receipt"
    | "support";
  title: string;
  detail?: string;
  action?: Action["type"];
  actionLabel?: string;
}
export interface Preflight {
  state: "checking" | "ready" | "unavailable";
  wordCount?: number;
  pageEstimate?: number;
  paragraphCount?: number;
  summary?: string;
  /** The document's own title, as extracted — deterministic, not a guess. */
  title?: string;
  match?: "likely" | "unclear" | "unlikely";
  classification?: "ok" | "unavailable";
  recommendedPlaybookId?: string;
  recommendedPlaybookName?: string;
  injectionCount?: number;
  injectionRuleIds?: string[];
  /**
   * "This reads like a Master Services Agreement on your own paper." —
   * composed by the panel (issue #733), because the article, the
   * unclassified-type special case and the neutral paper-side phrasing are
   * product wording with their own reasons, not something to re-derive here.
   */
  readsLike?: string;
  /** The mismatch sentence, naming the type AND the selected playbook. */
  mismatchNote?: string;
}
export interface Cost {
  kind: "unavailable" | "estimated" | "held" | "settled";
  cents?: number;
  holdCents?: number;
  basis?: "typical" | "document-model";
  coverCents?: number;
  settlementComplete?: boolean;
  adminDaily?: {
    settledCents: number;
    reservedCents: number;
    capCents: number;
    utcDate: string;
  };
}
export interface Result {
  outcome: string;
  decision?: string;
  copy?: string;
  confidenceBand?: string;
  issueCount?: number;
  criticContested?: number;
  criticAdded?: number;
  criticDetails?: string[];
  /**
   * The quiet meta line (issue #492 redesign item 3, wired through in #733):
   * filename · contract type · finished-at, whichever this review has. Each
   * part keeps the id the existing assertions read it by, and the panel
   * composes them — "never invent precision" is its rule, not the console's.
   */
  meta?: { id: string; text: string }[];
  metadata?: { label: string; value: string }[];
  appliedGuidance?: string;
  failureCause?: string;
  failureFix?: string;
  reason?: string;
  /**
   * `normalization_notes` (issue #530): the per-paragraph disclosure saying
   * WHICH tracked change could not be read and why. Wired through in #733 —
   * without it a refusal fell back to the generic reason copy, which is the
   * exact regression #530 fixed.
   */
  normalizationNotes?: string;
  failingStage?: string;
}
export interface ReviewModel {
  status: Status;
  fileSelected: boolean;
  stage?: Stage | string | null;
  reviewId?: string;
  filename?: string;
  fileBytes?: number;
  hasInput?: boolean;
  hasOutput?: boolean;
  preferences: Preferences;
  /** Provenance supplied by the adapter; automatic choices never override a user selection. */
  playbookSelection?: "automatic" | "user";
  playbooks: Playbook[];
  internalNotesAvailable: boolean;
  browningReadback: string;
  /**
   * The readback, in its two parts (issue #733): the note the app writes about
   * the current intensity, and the EXACT sentence that will be injected. They
   * are separate because the promise is "what you see is what is sent" — the
   * sentence is quoted verbatim and compared character-for-character against
   * the submitted `toaster_guidance`, so it has to be readable on its own
   * rather than embedded in a composed line. `browningReadback` stays as the
   * one-line form for the title and the screen-reader readback.
   */
  browningNote: string;
  browningSentence?: string;
  notesDisclosure?: string;
  /**
   * How free-text instructions relate to the playbook (issue #733). Supplied
   * by the adapter for the same reason `browningReadback` is: the wording is
   * the app's promise about what the model is INSTRUCTED to do, not a caption
   * the console gets to phrase — ARCHITECTURE.md's guidance-precedence model
   * enforces it by instruction, never mechanically, so a paraphrase that
   * dropped "which nothing can override" would overstate the guarantee.
   */
  guidancePrecedence: string;
  /**
   * The completion-handoff live-region text (issues #448/#492, wired through
   * in #733). The panel composes it and the console prints it verbatim: it is
   * the ONLY channel that tells someone who cannot see the screen whether the
   * redline was saved, why it was not, or that the critic flagged the review,
   * and a console-authored "Your review is ready." replaced all of that with
   * the one fact they could already infer.
   */
  readyAnnouncement?: string;
  preflight?: Preflight;
  cost: Cost;
  result?: Result;
  receiptLines?: readonly string[];
  cover?: {
    state: "idle" | "loading" | "ready" | "error";
    draft?: string;
    cached?: boolean;
    error?: string;
    retryable?: boolean;
    /**
     * The last NON-cached generation's cost (issue #499 fix round 1, wired
     * through in #733). Not `cost.coverCents`, which is zero while a cached
     * draft is on screen: a "Regenerate" key beside a figure of $0.00 invites
     * a billed press under a false price.
     */
    lastCostCents?: number;
  };
  disposition?: { saving: boolean; recorded?: Disposition };
  downloading?: boolean;
  downloadStarted?: boolean;
  cancelRequested?: boolean;
  muted: boolean;
  notification: "unsupported" | "off" | "granted" | "denied";
  messages?: Message[];
  history?: {
    reviewId: string;
    filename: string;
    outcome: string;
    hasInput: boolean;
  }[];
  runAgainAvailable?: boolean;
  odometer?: number;
  resumed?: boolean;
  motionEvent?: { id: string; type: MotionEvent };
}
export type Action =
  | {
      type:
        | "submit"
        | "cancel"
        | "retry"
        | "download"
        | "download-input"
        | "copy-id"
        | "receipt-copy"
        | "receipt-save"
        | "butter"
        | "cover-copy"
        | "cover-regenerate"
        | "cover-retry"
        | "preference-retry"
        /**
         * The two channel retries added by #726, so every entry in the one
         * status window carries its own key: re-poll the review now, and
         * re-fetch the contract-type catalog. Both hand off to the app's
         * existing loaders — the console starts no request of its own.
         */
        | "poll-retry"
        | "catalog-retry"
        | "sound-toggle"
        | "notification-toggle"
        | "history"
        | "run-again"
        | "switch-recommended";
      reviewId?: string;
    }
  | { type: "disposition"; outcome: Disposition; note: string };
export type MotionEvent =
  | "file-loaded"
  | "file-removed"
  | "submit-accepted"
  | "stage"
  | "done"
  | "error"
  | "manual"
  | "cancelled"
  | "cover-ready"
  | "receipt-open"
  | "receipt-copy"
  | "receipt-save"
  | "disposition-saved"
  | "reservation-confirmed"
  | "settled"
  | "odometer"
  | "key"
  | "pad-focus"
  | "guidance-readback"
  | "refusal";
export interface OrbitDinerProps {
  model: ReviewModel;
  /**
   * Serve the material plates from this folder (`<assetBase>/toaster.webp`).
   * Omit it — as this app does — and the bundled, fingerprinted plates are
   * used instead; see plates.ts.
   */
  assetBase?: string;
  onFile: (file: File | null) => void;
  onPreferences: (patch: Partial<Preferences>) => void;
  onAction: (action: Action) => void;
  onSound?: (event: MotionEvent) => void;
  plain?: boolean;
  onPlainChange?: (plain: boolean) => void;
  /**
   * Set false when the host app owns keyboard shortcuts. Modal Escape remains
   * local. Defaults to FALSE in this copy (issue #720, owner decision H6):
   * `ReviewSubmission` owns the vocabulary, so a forgotten prop must be inert
   * rather than a second dispatcher. The supplier's copy under
   * `vendor/orbit-diner/` still defaults to true.
   */
  keyboardShortcuts?: boolean;
}
