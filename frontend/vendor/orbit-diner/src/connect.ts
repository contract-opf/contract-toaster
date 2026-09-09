import type {
  Action,
  Disposition,
  Intensity,
  NotesMode,
  Preferences,
} from "./types";
export interface ActionHandlers {
  submit: () => void;
  cancel: () => void;
  retry: () => void;
  download: () => void;
  downloadInput: () => void;
  copyId: () => void;
  receiptCopy: () => void;
  receiptSave: () => void;
  butter: () => void;
  coverCopy: () => void;
  coverRegenerate: () => void;
  coverRetry: () => void;
  preferenceRetry: () => void;
  soundToggle: () => void;
  notificationToggle: () => void;
  history: () => void;
  runAgain: (reviewId: string) => void;
  switchRecommended: () => void;
  disposition: (outcome: Disposition, note: string) => void;
}
/** Connect these names to the existing guarded ReviewSubmission handlers. No network or state ownership here. */
export function connectActions(h: ActionHandlers): (action: Action) => void {
  return (a) => {
    switch (a.type) {
      case "submit":
        return h.submit();
      case "cancel":
        return h.cancel();
      case "retry":
        return h.retry();
      case "download":
        return h.download();
      case "download-input":
        return h.downloadInput();
      case "copy-id":
        return h.copyId();
      case "receipt-copy":
        return h.receiptCopy();
      case "receipt-save":
        return h.receiptSave();
      case "butter":
        return h.butter();
      case "cover-copy":
        return h.coverCopy();
      case "cover-regenerate":
        return h.coverRegenerate();
      case "cover-retry":
        return h.coverRetry();
      case "preference-retry":
        return h.preferenceRetry();
      case "sound-toggle":
        return h.soundToggle();
      case "notification-toggle":
        return h.notificationToggle();
      case "history":
        return h.history();
      case "run-again":
        if (a.reviewId) return h.runAgain(a.reviewId);
        return;
      case "switch-recommended":
        return h.switchRecommended();
      case "disposition":
        return h.disposition(a.outcome, a.note);
    }
  };
}
export interface PreferenceHandlers {
  playbook: (id: string) => void;
  intensity: (value: Intensity) => void;
  notesMode: (value: NotesMode) => void;
  instructions: (text: string) => void;
  dispositionNote: (text: string) => void;
}
export function connectPreferences(h: PreferenceHandlers) {
  return (p: Partial<Preferences>) => {
    if (p.playbookId !== undefined) h.playbook(p.playbookId);
    if (p.intensity !== undefined) h.intensity(p.intensity);
    if (p.notesMode !== undefined) h.notesMode(p.notesMode);
    if (p.instructions !== undefined) h.instructions(p.instructions);
    if (p.dispositionNote !== undefined) h.dispositionNote(p.dispositionNote);
  };
}
