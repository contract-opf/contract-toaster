/**
 * Browning control (issue #495) — Light / Medium / Dark markup intensity.
 *
 * The mechanism is deliberately boring: each setting contributes ONE
 * predefined plain-English sentence to the per-review `toaster_guidance` the
 * form already sends. No new prompt machinery, no backend change, no hidden
 * knob — `toaster_guidance` reaches both model passes today
 * (`scripts/primary_review_pass.py::assemble_system_blocks`, via
 * `critic_review_pass.run_critic_pass`) and is projected back verbatim by
 * `get_review_detail`, so History's "Show instructions" stays a faithful
 * record for free.
 *
 * THE TRANSPARENCY RULE, and why this module exists at all: the sentence shown
 * under the control and the sentence sent to the model are the same string
 * literal, read from here by both. What you see is literally what is injected.
 * If the UI copy and the injected text ever came from two places, a reviewer
 * could be shown one instruction while the model received another — which is
 * the failure this control's whole design is arranged to make impossible.
 * `browning-control.test.tsx` asserts that identity rather than trusting it.
 */

export type BrowningLevel = 'light' | 'medium' | 'dark';

export interface BrowningSetting {
  id: BrowningLevel;
  label: string;
  /**
   * The exact text appended to the review's instructions. Empty for Medium:
   * the default contributes NOTHING, so a reviewer who never touches this
   * control sends a request byte-identical to the one sent before the control
   * existed.
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
      'Keep the markup light: flag only material issues, accept reasonable ' +
      'counterparty positions, minimal ink.',
    note: 'This adds to your instructions:',
  },
  {
    id: 'medium',
    label: 'Medium',
    sentence: '',
    note: 'The playbook drives. Nothing is added to your instructions.',
  },
  {
    id: 'dark',
    label: 'Dark',
    sentence:
      'Push hard: mark up every open point the playbook gives us room on, ' +
      'and prefer our preferred positions throughout.',
    note: 'This adds to your instructions:',
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
 * The instructions text actually submitted: the browning sentence first, the
 * reviewer's own words after.
 *
 * Order is not cosmetic. The precedence copy already shown beside the
 * instructions box tells the reviewer their text governs, and later text in a
 * prompt reads as the more specific, later-arriving instruction — so the
 * reviewer's own words go last, and a reviewer who writes "don't touch the
 * indemnity" on a Dark toast gets what the UI promised them.
 *
 * Floor and hard requirements are untouched by construction: this returns a
 * guidance string, and guidance has never been able to override them.
 */
export function composeGuidance(level: BrowningLevel, userText: string): string {
  const sentence = browningSetting(level).sentence;
  const typed = userText.trim();
  // Whitespace-only stays empty, matching the form's existing rule (and
  // `render_toaster_guidance_block`'s): no guidance is not blank guidance.
  return [sentence, typed].filter(Boolean).join('\n\n');
}
