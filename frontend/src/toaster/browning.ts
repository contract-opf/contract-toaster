/**
 * Browning control (issue #495) — Light / Medium / Dark markup intensity.
 *
 * Issue #54 (2026-09-05 audit, finding F5, action A5 — HARD CUTOVER, owner
 * decision Q3) made the dial a wire field. Each setting used to contribute ONE
 * predefined plain-English sentence to the per-review `toaster_guidance`; it
 * now travels as the closed-vocabulary multipart field `markup_intensity`
 * (`light | medium | heavy`, `backend/src/reviews.py::MARKUP_INTENSITIES`),
 * is recorded on the review row and in the execution payload, and is rendered
 * by `scripts/primary_review_pass.py::render_markup_intensity_block` as its
 * OWN system block with fixed wording per level, immediately before the
 * toaster-guidance block on both review paths (OPF and registry-v1). Nothing about the dial rides in the free-text
 * instructions any more, so two reviews at different intensities are told
 * apart in audit, and the model reads the setting as a block, not as prose it
 * may weigh differently per run.
 *
 * THE TRANSPARENCY RULE, and why this module still exists: the sentence shown
 * under the control and the sentence the model is told are THE SAME STRING.
 * `sentence` below is the exact text of the backend's fixed block for that
 * level (`primary_review_pass.MARKUP_INTENSITY_SENTENCES`); the two are pinned
 * character-for-character across the language boundary by
 * `tests/test_review_routes_markup_intensity_54.py`, which reads this file. If
 * the UI copy and the injected text ever came from two places, a reviewer
 * could be shown one instruction while the model received another — which is
 * the failure this control's whole design is arranged to make impossible.
 */

export type BrowningLevel = 'light' | 'medium' | 'dark';

/** The wire vocabulary of `POST /api/reviews`' `markup_intensity` field. */
export type MarkupIntensity = 'light' | 'medium' | 'heavy';

export interface BrowningSetting {
  id: BrowningLevel;
  label: string;
  /**
   * The exact text of the system block the model is told for this level —
   * `scripts/primary_review_pass.py::MARKUP_INTENSITY_SENTENCES[level]`, verbatim.
   * Empty for Medium: the default contributes NOTHING — no field on the wire
   * and no block in the prompt — so a reviewer who never touches this control
   * sends a request byte-identical to the one sent before the control existed.
   */
  sentence: string;
  /** Shown under the control when this level is selected. */
  note: string;
}

export const BROWNING_SETTINGS: ReadonlyArray<BrowningSetting> = [
  {
    id: 'light',
    label: 'Light',
    sentence:
      'Keep the markup light: flag and footnote issues rather than editing them, ' +
      'and make edits only where the document breaches the Floor.',
    note: 'Sent as this review’s markup intensity. The model is told:',
  },
  {
    id: 'medium',
    label: 'Medium',
    sentence: '',
    note: 'The playbook drives. No markup-intensity instruction is sent.',
  },
  {
    id: 'dark',
    label: 'Dark',
    sentence:
      'Push hard: mark up every open point the playbook gives us room on, ' +
      'and prefer our preferred positions throughout.',
    note: 'Sent as this review’s markup intensity. The model is told:',
  },
];

export const DEFAULT_BROWNING: BrowningLevel = 'medium';

export function browningSetting(level: BrowningLevel): BrowningSetting {
  return (
    BROWNING_SETTINGS.find((setting) => setting.id === level) ??
    BROWNING_SETTINGS.find((setting) => setting.id === DEFAULT_BROWNING)!
  );
}

/**
 * The wire value for a control level (issue #54). The control's own
 * vocabulary — and the `lastBrowning` localStorage value, which is NOT
 * translated — says `dark`; the API's closed enum says `heavy`. This is the
 * one place the two meet, so the translation happens at the FormData line
 * and nowhere else.
 */
export function toMarkupIntensity(level: BrowningLevel): MarkupIntensity {
  return level === 'dark' ? 'heavy' : level;
}

/**
 * The instructions text actually submitted: the reviewer's own words, and
 * nothing else.
 *
 * Before issue #54 this prepended the level's sentence; it no longer does —
 * the dial is its own wire field now (`toMarkupIntensity` above) and the
 * sentence reaches the model as its own system block, not as guidance.
 * Kept as the single seam the submit path composes guidance through, so the
 * whitespace rule stays in one place.
 *
 * Floor and hard requirements are untouched by construction: this returns a
 * guidance string, and guidance has never been able to override them.
 */
export function composeGuidance(_level: BrowningLevel, userText: string): string {
  // Whitespace-only stays empty, matching the form's existing rule (and
  // `render_toaster_guidance_block`'s): no guidance is not blank guidance.
  return userText.trim();
}
