/**
 * toasting-theater.test.tsx — the stage table itself (issue #496, as amended
 * by issues #667 and #727).
 *
 * The wait is minutes long and was a progress bar. The architecture underneath
 * is genuinely dramatic — a primary reviewer marks the document up, then an
 * ADVERSARIAL critic argues with the markup before anything is decided — and
 * the truthful signal already exists in `progress_stage`.
 *
 * The load-bearing property is not "the vignettes are pretty". It is that the
 * theater NEVER claims a stage the backend did not report:
 *
 *   - an unknown token resolves to nothing, so the caller can only render the
 *     indeterminate treatment rather than a guess
 *   - a null/absent stage does the same
 *   - every caption comes from ONE table, so the glass and (via #497) the tab
 *     title cannot disagree
 *
 * A vignette that advanced on a timer would look identical to a truthful one
 * right up until the moment it lied, which is why the table is keyed only to
 * tokens `run_review` actually emits and there is no interpolation between
 * them.
 *
 * WHAT #667 CHANGED. Each stage used to draw a small paper-and-pencil scene
 * beside the darkening toast slice — two illustrations for one state — and the
 * owner kept the toast. The CAPTION is the carrier, and that is what these
 * assertions protect.
 *
 * WHAT #727 CHANGED. This file used to render `ToasterProgress` and
 * `ToasterStyles`, the hero's own progress markup and inline stylesheet. #727
 * deleted that surface, so what is left here is the table on its own — which
 * is what the module docstring above was always describing. The RENDERED half
 * moved rather than disappearing:
 *
 *   - a reported stage's caption, its step text, its polite announcement and
 *     its doneness hook — review-progress-stages.test.tsx, against the real
 *     `ReviewSubmission`;
 *   - the indeterminate treatment for an unreported or unrecognised stage —
 *     the same file;
 *   - the reduced-motion guard — orbit-diner-motion-723.test.tsx and
 *     `scripts/focus-audit.mjs`, both against `orbit-diner/motion.ts`.
 *
 * The one assertion that did NOT move is the old "#510 rail": the hero's
 * caption had to stay out of the live-region role because its step text
 * already announced every transition. The console decided that the other way
 * round — `review-stage-caption` IS its polite region — so the rail belonged
 * to the deleted surface, not to the table, and it is gone with it rather than
 * being restated as something the console does not do.
 *
 * `STAGE_VIGNETTES` is a projection of `orbit-diner/state.ts`'s `stages` since
 * #727 (one caption table, per that ticket's acceptance criteria), so these
 * are assertions about what that projection produces.
 */
import { describe, expect, it } from 'vitest';
import { stages } from '../orbit-diner/state';
import { STAGE_VIGNETTES, stageNumber, vignetteForStage } from '../toaster/stageTheater';

describe('issue #496 — every reported stage gets its caption', () => {
  it.each(STAGE_VIGNETTES.map((v) => [v.token, v] as const))(
    '%s resolves to its own caption',
    (token, vignette) => {
      expect(vignetteForStage(token)).toBe(vignette);
      expect(vignette.caption.length).toBeGreaterThan(0);
    },
  );

  it('the two model passes are distinguishable, not two shades of one thing', () => {
    // The critic stage's whole message is "a DIFFERENT model wrote this one".
    // If both passes said the same thing the stage would be truthful and still
    // say nothing. Held on the captions since #667 left one illustration.
    const primary = STAGE_VIGNETTES.find((v) => v.token === 'primary_pass');
    const critic = STAGE_VIGNETTES.find((v) => v.token === 'critic_pass');
    expect(primary?.caption).not.toBe(critic?.caption);
    expect(primary?.label).not.toBe(critic?.label);
  });

  it('the critic caption says plainly what the critic is', () => {
    // The most reassuring true sentence this product can show a lawyer.
    // Pinned so a later copy edit cannot quietly bury it behind "Step 2 of 4".
    expect(vignetteForStage('critic_pass')?.caption).toMatch(/second model/i);
  });
});

describe('issue #496 — it never claims a stage the backend did not report', () => {
  it.each([['polishing_the_prose'], [null], [undefined], ['']])(
    'vignetteForStage(%s) returns null on its own, not just via the caller',
    (stage) => {
      // Asserted directly because the rendered fallback is decided one level
      // up, by `stageNumber` — a mutation that made this function guess would
      // not have failed any test that goes through the component. It is
      // defence in depth, and defence in depth that nothing checks is just an
      // unverified claim.
      expect(vignetteForStage(stage)).toBeNull();
    },
  );

  it('the step number and the caption always agree about which stage it is', () => {
    // Two derivations of "which stage" that could drift. They come from the
    // same array, and this is what pins that they still do.
    for (const vignette of STAGE_VIGNETTES) {
      expect(stageNumber(vignette.token)).toBe(STAGE_VIGNETTES.indexOf(vignette) + 1);
    }
    expect(stageNumber('polishing_the_prose')).toBe(0);
  });
});

describe('issue #496 — one map, so the glass and the tab cannot disagree', () => {
  it('the browning ramp is monotonic across the real stage order', () => {
    // #497 reads this same number for the favicon. A ramp that went backwards
    // would show a review getting LESS done as it progressed.
    const ramp = STAGE_VIGNETTES.map((v) => v.browning);
    expect(ramp).toEqual([...ramp].sort((a, b) => a - b));
    expect(ramp[ramp.length - 1]).toBe(1);
  });

  it('every stage has a caption and no two are the same', () => {
    const captions = STAGE_VIGNETTES.map((v) => v.caption);
    expect(captions.every((c) => c.length > 0)).toBe(true);
    expect(new Set(captions).size).toBe(captions.length);
  });

  it('is the ONLY caption table — it projects the console’s, never restates it', () => {
    // Issue #727's acceptance criterion, as a fact rather than a promise: the
    // labels and captions here must BE the console's, so a copy edit in
    // `orbit-diner/state.ts` cannot leave the tab title saying the old words.
    // Read out of the shipped module, not out of a fixture.
    const table = stages;
    expect(STAGE_VIGNETTES).toHaveLength(Object.keys(table).length);
    for (const vignette of STAGE_VIGNETTES) {
      expect(vignette.label).toBe(table[vignette.token].label);
      expect(vignette.caption).toBe(table[vignette.token].caption);
      expect(vignette.token).toBe(
        (Object.keys(table) as (keyof typeof table)[]).find(
          (key) => table[key].step === STAGE_VIGNETTES.indexOf(vignette) + 1,
        ),
      );
    }
  });
});
