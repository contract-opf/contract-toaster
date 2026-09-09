/**
 * Toasting theater — what each real pipeline stage is called and what it is
 * called on screen (issue #496), now read off ONE table (issue #727).
 *
 * ## Where the captions live
 *
 * They live in `orbit-diner/state.ts`'s `stages`, the console's own table, and
 * this module is a projection of it. Until #727 there were two: the console
 * carried `stages` and this file carried a hand-written `STAGE_VIGNETTES` with
 * the same four labels and the same four captions. Two tables mean the glass
 * and the tab can disagree about what the backend just said, and the moment
 * they do, both become untrustworthy. #727's acceptance criteria say it in as
 * many words — exactly one stage caption table exists — so the wording is
 * `stages`', once, and everything here is derived.
 *
 * ## Why this module still exists
 *
 * The tab title and the favicon stay ours (final plan, designer answer D6),
 * and they are not console code: `tabChrome.ts` and `faviconFrames.ts` are
 * plain modules that run whether or not the console is mounted. They need the
 * stage as an ORDERED LIST with a browning ramp, which is not the shape
 * `stages` has — `stages` is a keyed record whose fourth field is a CSS
 * filter for the toast slice. This module is that reshaping, and it is the
 * only place it happens.
 *
 * Issue #667 removed the `art` field. Each stage used to name a small scene
 * drawn beside the darkening toast slice, which meant two illustrations for
 * one state; the owner kept the toast. The CAPTION is what carried the
 * meaning and is what remains.
 *
 * ## Truthful, or silent
 *
 * Every entry is keyed to a token the backend actually reports
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
import { stages } from '../orbit-diner/state';
import type { Stage } from '../orbit-diner/types';

/** The four sub-stages `run_review` reports, in the order they happen. The
 *  console's `Stage` union and this one are the same wire contract, so it is
 *  aliased rather than restated — a stage added there cannot be missed here. */
export type ReviewStageToken = Stage;

export interface StageVignette {
  readonly token: ReviewStageToken;
  /** The short label used where space is tight (the "Step 2 of 4 · …" line). */
  readonly label: string;
  /** The plain-language sentence shown under the glass and in the tab title. */
  readonly caption: string;
  /**
   * How far along the browning ramp this stage sits, 0..1. Drives the
   * favicon (#497), so the tab and the caption read the same progress off one
   * number instead of each deriving their own.
   */
  readonly browning: number;
}

/**
 * The four vignettes, in pipeline order, projected from the console's table.
 *
 * The order is `stages`' own `step`, sorted rather than assumed: object key
 * order happens to match today, and relying on it would make a reordered
 * literal silently renumber every "Step n of 4" the tab title shows.
 *
 * `browning` is `step / count` — the same 0.25 / 0.5 / 0.75 / 1 ramp this
 * module used to state by hand, now a consequence of where the stage sits
 * rather than a fifth thing to keep in sync.
 */
export const STAGE_VIGNETTES: readonly StageVignette[] = (
  Object.keys(stages) as ReviewStageToken[]
)
  .sort((a, b) => stages[a].step - stages[b].step)
  .map((token, _index, all) => ({
    token,
    label: stages[token].label,
    caption: stages[token].caption,
    browning: stages[token].step / all.length,
  }));

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
