/**
 * notesMode.ts — the four footnote audiences (issue #523, epic #519 item F).
 *
 * Epic #519's axis 1: who a review's footnotes are written FOR.
 *
 *   none     bare tracked changes, no footnotes at all
 *   external counterparty-facing rationale (today's behaviour, the default)
 *   internal the toaster's own reasoning, for us
 *   both     both, visually distinguishable in the document
 *
 * The ids are the wire values `POST /api/reviews`'s `notes_mode` form field
 * takes (`backend/src/reviews.py::NOTES_MODES`) and the values the per-user
 * preference stores (`backend/src/user_preferences.py`) — one vocabulary, no
 * client-side translation layer to drift.
 *
 * THE DISCLOSURE RULE. `internal` and `both` put the toaster's own reasoning
 * into the deliverable, which makes that document unshippable. Epic #519
 * decision 4 retired the always-on nag premise — the user is an attorney, not
 * a haste-prone clerk — so this is ONE honest sentence at the point of
 * choice, shown only for the two modes that warrant it. No dialog, no
 * confirmation, no repetition. It follows #495's transparency rule in spirit:
 * `INTERNAL_MARKER_TEXT` below is the marker the document will ACTUALLY
 * carry, quoted verbatim from `scripts/redline_generate.py::MARKER_TEXT`
 * rather than paraphrased, and `tests/test_user_preferences_523.py` pins the
 * two strings identical so the promise on screen cannot drift from the ink on
 * the page.
 *
 * What the sentence deliberately does NOT promise: a signposted download
 * FILENAME. Epic #519's table wants `<name>-redline (with internal notes).docx`,
 * but that half of item E has not landed (`grep -r "with internal notes"`
 * finds nothing in backend/ or scripts/), and a control that describes a
 * safeguard the pipeline does not yet apply is worse than one that stays
 * quiet about it. The in-document marker below DOES exist today (#513).
 */

export type NotesMode = 'none' | 'external' | 'internal' | 'both';

/**
 * The exact marker every page of an internal-notes document carries.
 * MUST stay character-identical to `scripts/redline_generate.py`'s
 * `MARKER_TEXT` — see the module docstring.
 */
export const INTERNAL_MARKER_TEXT = 'contains internal notes — not for external transmission';

/** The one sentence shown when the chosen mode puts internal notes in the
 *  document. Rendered by the control itself; never buried in a tooltip. */
export const INTERNAL_NOTES_DISCLOSURE =
  `This document will carry our own notes and every page will be marked ` +
  `“${INTERNAL_MARKER_TEXT}” — your copy, not the counterparty’s.`;

export interface NotesModeSetting {
  id: NotesMode;
  /** Dial-stop label. Short: these render as stops, not sentences. */
  label: string;
  /** What this mode does, shown under the control when it is selected. */
  note: string;
  /** True for the modes that put the toaster's own reasoning in the file. */
  carriesInternalNotes: boolean;
}

export const NOTES_MODE_SETTINGS: ReadonlyArray<NotesModeSetting> = [
  {
    id: 'none',
    label: 'None',
    note: 'Tracked changes only — no footnotes.',
    carriesInternalNotes: false,
  },
  {
    id: 'external',
    label: 'External',
    note: 'Footnotes explain each change in counterparty-facing terms. Safe to send.',
    carriesInternalNotes: false,
  },
  {
    id: 'internal',
    label: 'Internal',
    note: 'Footnotes carry our own reasoning instead of the counterparty-facing rationale.',
    carriesInternalNotes: true,
  },
  {
    id: 'both',
    label: 'Both',
    note: 'Both kinds of footnote, told apart in the document.',
    carriesInternalNotes: true,
  },
];

/** Today's behaviour, and `backend/src/reviews.py::DEFAULT_NOTES_MODE`. A
 *  reviewer who never touches the control sends what they sent before it
 *  existed. */
export const DEFAULT_NOTES_MODE: NotesMode = 'external';

export function notesModeSetting(mode: NotesMode): NotesModeSetting {
  return (
    NOTES_MODE_SETTINGS.find((setting) => setting.id === mode) ??
    NOTES_MODE_SETTINGS.find((setting) => setting.id === DEFAULT_NOTES_MODE)!
  );
}

export function isNotesMode(value: unknown): value is NotesMode {
  return NOTES_MODE_SETTINGS.some((setting) => setting.id === value);
}

/**
 * Whether `mode` can be chosen in THIS deployment.
 *
 * `internal` and `both` are refused server-side while the #572
 * `NOTES_MODE_ENABLED` kill switch is off (`backend/src/reviews.py::
 * resolve_notes_mode` raises → HTTP 400), and the browser cannot see
 * deployment config, so `GET /api/me/preferences` reports it as
 * `notes_mode_available`. Offering a stop that is guaranteed to fail would be
 * a dead affordance dressed as a live one — the same reason the console's
 * playbook control (`orbit-diner/OrbitDiner.tsx`) renders an unloaded playbook
 * as a visible-but-`disabled` "coming soon" option rather than hiding it or
 * letting it 503.
 */
export function isNotesModeAvailable(mode: NotesMode, internalAvailable: boolean): boolean {
  return internalAvailable || !notesModeSetting(mode).carriesInternalNotes;
}
