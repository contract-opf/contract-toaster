/**
 * toasting-theater.test.tsx — issue #496.
 *
 * The wait is minutes long and was a progress bar. The architecture underneath
 * is genuinely dramatic — a primary reviewer marks the document up, then an
 * ADVERSARIAL critic argues with the markup before anything is decided — and
 * the truthful signal already exists in `progress_stage`.
 *
 * The load-bearing property is not "the vignettes are pretty". It is that the
 * theater NEVER claims a stage the backend did not report:
 *
 *   - an unknown token renders the indeterminate treatment, not a guess
 *   - a null/absent stage renders today's behaviour, unchanged
 *   - every caption comes from ONE exported map, so the glass and (via #497)
 *     the tab title cannot disagree
 *
 * A vignette that advances on a timer would look identical to a truthful one
 * right up until the moment it lied, which is why the map is keyed only to
 * tokens `run_review` actually emits and there is no interpolation between
 * them.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ToasterHero, ToasterStyles } from '../toaster/Toaster';
import { STAGE_VIGNETTES, stageNumber, vignetteForStage } from '../toaster/stageTheater';

const ENTRIES = [
  { playbook_id: 'eiaa', display_name: 'Affiliation', status: 'active' as const },
];

function renderWorking(progressStage: string | null) {
  return render(
    <>
      <ToasterStyles />
      <ToasterHero
        entries={ENTRIES}
        value="eiaa"
        onChange={() => {}}
        phase="working"
        progressStage={progressStage}
      />
    </>,
  );
}

describe('issue #496 — every reported stage gets its own vignette and caption', () => {
  it.each(STAGE_VIGNETTES.map((v) => [v.token, v] as const))(
    '%s renders its scene and its caption',
    (token, vignette) => {
      renderWorking(token);
      expect(screen.getByTestId('review-stage-caption').textContent).toBe(vignette.caption);
      expect(screen.getByTestId('review-stage-vignette')).toHaveAttribute(
        'data-vignette',
        vignette.art,
      );
    },
  );

  it('the two model passes are visually distinguishable, not two shades of one thing', () => {
    // The critic vignette's whole message is "a DIFFERENT model wrote this
    // one". If both passes drew the same scene the stage would be truthful
    // and still say nothing.
    const primary = STAGE_VIGNETTES.find((v) => v.token === 'primary_pass');
    const critic = STAGE_VIGNETTES.find((v) => v.token === 'critic_pass');
    expect(primary?.art).not.toBe(critic?.art);
    expect(primary?.caption).not.toBe(critic?.caption);
  });

  it('the critic caption says plainly what the critic is', () => {
    // The most reassuring true sentence this product can show a lawyer.
    // Pinned so a later copy edit cannot quietly bury it behind "Step 2 of 4".
    expect(vignetteForStage('critic_pass')?.caption).toMatch(/second model/i);
  });
});

describe('issue #496 — it never claims a stage the backend did not report', () => {
  it('an UNKNOWN token falls back to the indeterminate treatment', () => {
    // A stage renamed on the backend, or one a newer runner emits that this
    // build has never heard of. Guessing would be worse than saying nothing.
    renderWorking('polishing_the_prose');
    expect(screen.getByTestId('review-progress-indeterminate')).toBeTruthy();
    expect(screen.queryByTestId('review-stage-vignette')).toBeNull();
    expect(screen.queryByTestId('review-stage-caption')).toBeNull();
  });

  it('a NULL stage renders the pre-existing behaviour, unchanged', () => {
    renderWorking(null);
    expect(screen.getByTestId('review-progress-indeterminate')).toBeTruthy();
    expect(screen.queryByTestId('review-stage-vignette')).toBeNull();
  });

  it('an EMPTY stage token is treated as absent, not as a stage', () => {
    renderWorking('');
    expect(screen.getByTestId('review-progress-indeterminate')).toBeTruthy();
  });

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

  it('the step number and the vignette always agree about which stage it is', () => {
    // Two derivations of "which stage" that could drift. They come from the
    // same array, and this is what pins that they still do.
    for (const vignette of STAGE_VIGNETTES) {
      renderWorking(vignette.token).unmount();
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
});

describe('issue #496 — the rails', () => {
  it('the vignette is decoration; the caption carries the meaning', () => {
    renderWorking('critic_pass');
    expect(screen.getByTestId('review-stage-vignette')).toHaveAttribute('aria-hidden', 'true');
  });

  it('the caption is NOT a second live region', () => {
    // #510: the step text above it already announces every transition, and a
    // second polite region mutating in the same commit is exactly that defect.
    renderWorking('critic_pass');
    const caption = screen.getByTestId('review-stage-caption');
    expect(caption.getAttribute('aria-live')).toBeNull();
    expect(caption.getAttribute('role')).toBeNull();
  });

  it('reduced motion neutralises every vignette animation AND keeps the ink visible', () => {
    renderWorking('primary_pass');
    const css = Array.from(document.querySelectorAll('style'))
      .map((node) => node.textContent ?? '')
      .join('\n');
    const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
    for (const part of [
      'toaster-vignette__pen',
      'toaster-vignette__mark',
      'toaster-vignette__strike',
      'toaster-vignette__merge-a',
      'toaster-vignette__merge-b',
      'toaster-vignette__roll',
    ]) {
      expect(reduced).toContain(part);
    }
    // The marks animate IN from scaleX(0). Killing the animation without
    // neutralising the transform would leave the scene permanently blank —
    // a "reduced motion" that hides the content instead of stilling it.
    expect(reduced).toMatch(/transform:\s*none\s*!important/);
  });
});
