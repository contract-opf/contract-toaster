/**
 * Toasting theater — the one map of what each real pipeline stage is called
 * and what it looks like (issue #496).
 *
 * The wait is minutes long, and the architecture underneath is genuinely
 * dramatic: a primary reviewer marks the document up, then an ADVERSARIAL
 * critic argues with the markup before anything is decided. That second pass
 * existing is the product's best trust story, and a progress bar hides it.
 *
 * ## Why this is a separate module
 *
 * The captions have to come from ONE place. #497 puts the same stage into the
 * tab title and the favicon; if the glass and the tab disagree about what is
 * happening, both become untrustworthy. Everything stage-shaped that any
 * surface renders is defined here and imported.
 *
 * ## Truthful, or silent
 *
 * Every entry below is keyed to a token the backend actually reports
 * (`scripts/review_spine.py`'s four `PROGRESS_*` tokens, written as each
 * sub-stage STARTS). There is deliberately no entry for "probably nearly
 * done", no interpolation between stages, and no timer: `vignetteForStage`
 * returns null for an absent, null, or unrecognised token, and the caller
 * falls back to the indeterminate treatment that claims nothing.
 *
 * That fallback is not an edge case. A runner that predates the seam, a
 * deployment that reports nothing, and a stage renamed on the backend all
 * land there, and all three are better served by an honest "still working"
 * than by a stage we guessed.
 */

/** The four sub-stages `run_review` reports, in the order they happen. */
export type ReviewStageToken = 'primary_pass' | 'critic_pass' | 'reconciliation' | 'redline';

export interface StageVignette {
  readonly token: ReviewStageToken;
  /** The short label used where space is tight (the "Step 2 of 4 · …" line). */
  readonly label: string;
  /** The plain-language sentence shown under the glass and in the tab title. */
  readonly caption: string;
  /**
   * Which vignette to draw. Named for what it DEPICTS, not for its stage, so
   * two stages could share one if the art ever consolidates.
   */
  readonly art: 'marking-up' | 'arguing' | 'merging' | 'rolling';
  /**
   * How far along the browning ramp this stage sits, 0..1. Drives the slice's
   * darkening and (via #497) the favicon, so both read the same progress off
   * one number instead of each deriving their own.
   */
  readonly browning: number;
}

export const STAGE_VIGNETTES: readonly StageVignette[] = [
  {
    token: 'primary_pass',
    label: 'First read-through',
    caption: 'Reading your contract against the playbook…',
    art: 'marking-up',
    browning: 0.25,
  },
  {
    token: 'critic_pass',
    label: 'Adversarial critic',
    // Said plainly on purpose. "A second model is arguing with the markup" is
    // the most reassuring true sentence this product can show a lawyer, and
    // burying it behind "Step 2 of 4" wastes it.
    caption: 'A second model is arguing with the markup…',
    art: 'arguing',
    browning: 0.5,
  },
  {
    token: 'reconciliation',
    label: 'Reconciling both passes',
    caption: 'Settling what survives…',
    art: 'merging',
    browning: 0.75,
  },
  {
    token: 'redline',
    label: 'Writing your redline',
    caption: 'Writing your redline…',
    art: 'rolling',
    browning: 1,
  },
];

/**
 * The vignette for a reported token, or null when there is no honest one to
 * show. Null covers absent, null, empty, and unrecognised alike — a stage this
 * build does not know about is not a stage it may render.
 */
export function vignetteForStage(stage: string | null | undefined): StageVignette | null {
  if (!stage) return null;
  return STAGE_VIGNETTES.find((entry) => entry.token === stage) ?? null;
}

/** 1-based position of a reported token, or 0 for "no honest step to show". */
export function stageNumber(stage: string | null | undefined): number {
  if (!stage) return 0;
  return STAGE_VIGNETTES.findIndex((entry) => entry.token === stage) + 1;
}
