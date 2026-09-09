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
  match?: "likely" | "unclear" | "unlikely";
  classification?: "ok" | "unavailable";
  recommendedPlaybookId?: string;
  recommendedPlaybookName?: string;
  injectionCount?: number;
  injectionRuleIds?: string[];
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
  metadata?: { label: string; value: string }[];
  appliedGuidance?: string;
  failureCause?: string;
  failureFix?: string;
  reason?: string;
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
  notesDisclosure?: string;
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
  assetBase?: string;
  onFile: (file: File | null) => void;
  onPreferences: (patch: Partial<Preferences>) => void;
  onAction: (action: Action) => void;
  onSound?: (event: MotionEvent) => void;
  plain?: boolean;
  onPlainChange?: (plain: boolean) => void;
  /** Set false when the host app owns keyboard shortcuts. Modal Escape remains local. */
  keyboardShortcuts?: boolean;
}
