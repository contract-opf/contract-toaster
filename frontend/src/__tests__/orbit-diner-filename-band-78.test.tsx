/**
 * orbit-diner-filename-band-78.test.tsx — the two follow-ups #739 deferred
 * (issue #78, epic #729).
 *
 * ## Be honest about what this proves
 *
 * The same caveat as `orbit-diner-filename.test.tsx`: vitest runs jsdom with
 * `css: false`, so nothing here measures a real box or a real glyph. What it
 * measures is `fitFilename`, which is a pure function of a `measure()` and a
 * width, against the box `support/orbitInscription.ts` derives from the shipped
 * `orbit.css`. Those are exact claims about the algorithm and the stylesheet,
 * and they are not claims about Space Grotesk on a screen.
 *
 * ## Part 1 — a separator-heavy name must not leave line 1 short
 *
 * `wrapMeasured` only ever broke INSIDE a chunk that could not fit an empty
 * line, so a name whose separators fall early spent line 1 on the run before
 * the first separator and threw the rest of that line away. At DESKTOP
 * (143px / 20px) the fixture below painted `["EIAA_", "Northw….docx"]`: 55 of
 * 143px used on line 1, and an 11-character head where 19 characters fit.
 *
 * The assertion is not "the head is longer" — that is satisfiable by one extra
 * character. It is that LINE 1 IS FULL: adding the name's next character to it
 * overflows the box. That is the defect stated as a measurement, and it fails
 * on the chunk-boundary-only wrap.
 *
 * A NOTE ON THE TICKET'S ESTIMATE. #78 asked for "at least 11 more characters"
 * of head on line 1. That is not reachable and never was: line 1 holds 143px,
 * `EIAA_` measures 55.2px, and the next eleven characters of this name measure
 * 114.6px — 169.8px in total. Seven more fit, for a twelve-character line 1,
 * and the head as a whole goes from 11 characters to 19, which is the arithmetic
 * ceiling (line 1 caps at `EIAA_Northwe`, and line 2 must still carry `….docx`).
 * The bounds below are the reachable ones, stated exactly.
 *
 * ## Part 2 — the 561–660px console band
 *
 * #739's width sweep carried a `continue` for widths whose inscription could
 * not hold `….docx` on a line of its own, and #78 recorded it as a cramped
 * band just above the console's 560px phone step.
 *
 * THE DECISION #78 ASKED FOR — phone step higher, or desktop grid collapsing
 * sooner — IS NEITHER. Read against `orbit.css`, the desktop grid already
 * collapses far below the band: `@container od-console (max-width: 1220px)`
 * sets `.od-appliances { grid-template-columns: minmax(0, 1fr); max-width:
 * 720px }` and drops the inscription to 18px. A 561px console gets a 124px box
 * at 18px type — `….docx` measures 60.3px there — not the 58px at 20px the
 * ticket describes. That 58px came from the test helper, which modelled every
 * non-phone width with the `min-width: 1221px` step's two-column track list.
 * So no breakpoint moved and no CSS changed: `CONSOLE_PHONE_MAX_PX` is still
 * 560, asserted below, and the derivation was corrected instead.
 */
import { describe, it, expect } from 'vitest';
import {
  FILENAME_ELLIPSIS,
  FILENAME_MAX_LINES,
  fitFilename,
  splitExtension,
} from '../orbit-diner/filename';
import { CONSOLE_PHONE_MAX_PX } from '../orbit-diner/consoleWidth';
import {
  CONSOLE_MAX_PX,
  CONSOLE_MIN_PX,
  DESKTOP,
  ORPHAN_PRONE,
  PHONE,
  SHORT,
  inscriptionBox,
  metricsAt,
  valueOf,
  type Box,
} from './support/orbitInscription';

// ---------------------------------------------------------------------------
// Part 1 — the head a separator-heavy name keeps
// ---------------------------------------------------------------------------

/**
 * #78's own fixture. Reachable: `projection.ts` hands `file.name` to the
 * console verbatim (the backend only strips the ends), so any name a file
 * chooser can produce arrives here, separators and all.
 */
const SEPARATOR_HEAVY =
  'EIAA_Northwestern_University_2026_redline_round3_final_FINAL.docx';

/** What the chunk-boundary-only wrap painted, quoted from #78. */
const BEFORE = ['EIAA_', 'Northw….docx'];
/** …and the head it preserved: eleven characters. */
const BEFORE_HEAD = 'EIAA_Northw';

/** The head the box can actually carry, worked out in the header above. */
const REACHABLE_HEAD = 'EIAA_Northwestern_U';

/** Everything painted before the ellipsis: the part of the real name kept. */
function headOf(lines: string[]): string {
  const painted = lines.join('');
  const cut = painted.indexOf(FILENAME_ELLIPSIS);
  return cut === -1 ? painted : painted.slice(0, cut);
}

/** Every line is inside the box, and there are no more than `maxLines`. */
function expectInside(lines: string[], box: Box): void {
  const measure = metricsAt(box.font);
  expect(lines.length).toBeLessThanOrEqual(FILENAME_MAX_LINES);
  for (const line of lines) {
    expect(
      Math.round(measure(line) * 100) / 100,
      `line wider than the ${box.width}px inscription: ${JSON.stringify(line)}`,
    ).toBeLessThanOrEqual(box.width);
  }
}

describe('issue #78 — a separator-heavy name fills line 1 before it truncates', () => {
  const lines = fitFilename(SEPARATOR_HEAVY, {
    measure: metricsAt(DESKTOP.font),
    width: DESKTOP.width,
  });
  const measure = metricsAt(DESKTOP.font);

  it('is measured against the box #78 quotes: 143px at 20px', () => {
    // If this ever moves, every number in this file's header is stale.
    expect([DESKTOP.width, DESKTOP.font]).toEqual([143, 20]);
  });

  it('leaves no room on line 1 that the name could have used', () => {
    // The defect, stated as a measurement rather than as a character count:
    // one more character of the real name does not fit. On the old wrap line 1
    // was `EIAA_` (55.2px of 143px) and the next character fitted easily.
    const first = lines[0];
    expect(SEPARATOR_HEAVY.startsWith(first)).toBe(true);
    expect(first).not.toBe(BEFORE[0]);
    expect(
      measure(first + SEPARATOR_HEAVY[first.length]),
      `line 1 still has room for ${JSON.stringify(SEPARATOR_HEAVY[first.length])}`,
    ).toBeGreaterThan(DESKTOP.width);
  });

  it('keeps 19 characters of head where it used to keep 11', () => {
    const head = headOf(lines);
    expect(SEPARATOR_HEAVY.startsWith(head)).toBe(true);
    expect(head.length).toBeGreaterThan(BEFORE_HEAD.length);
    expect(head).toBe(REACHABLE_HEAD);
  });

  it('still paints two lines, inside the box, ending in .docx', () => {
    // The #739 promises are not traded away for the longer head.
    expectInside(lines, DESKTOP);
    const last = lines[lines.length - 1];
    expect(last).not.toBe('docx');
    expect(last).toMatch(/\.docx$/);
    expect(lines.join('')).toBe(
      `${REACHABLE_HEAD}${FILENAME_ELLIPSIS}${splitExtension(SEPARATOR_HEAVY).extension}`,
    );
  });

  it('does not reach for an in-chunk break when the name fits without one', () => {
    // The guard on part 1's fix: packing is a truncation-time recovery, so a
    // name that fits at its separators is still painted at its separators.
    // Without this, `Mutual NDA draft-v3.docx` would be cut mid-word.
    expect(
      fitFilename(SHORT, { measure: metricsAt(PHONE.font), width: PHONE.width }),
    ).toEqual(['Mutual NDA', 'draft-v3.docx']);
  });
});

// ---------------------------------------------------------------------------
// Part 2 — the band just above the phone step
// ---------------------------------------------------------------------------

/** The band #78 names, inclusive. */
const BAND_MIN = 561;
const BAND_MAX = 660;

describe('issue #78 — the 561–660px console band is covered, not skipped', () => {
  it('sits inside the range the #739 sweep walks', () => {
    // The two claims have to be about the same console, or this file proves
    // nothing about the hole the sweep used to carry.
    expect(BAND_MIN).toBeGreaterThan(CONSOLE_MIN_PX);
    expect(BAND_MAX).toBeLessThan(CONSOLE_MAX_PX);
    expect(BAND_MIN).toBe(CONSOLE_PHONE_MAX_PX + 1);
  });

  it('is served by the collapsed one-column grid the stylesheet already ships', () => {
    // The decision #78 asked for, recorded as an assertion: the phone step did
    // NOT move (it is still 560) and the desktop grid did NOT have to collapse
    // sooner, because it already collapses at 1220 — far below this band.
    expect(CONSOLE_PHONE_MAX_PX).toBe(560);
    const collapsed = '@container od-console (max-width: 1220px)';
    expect(valueOf('.od-appliances', 'grid-template-columns', collapsed)).toBe(
      'minmax(0, 1fr)',
    );
    expect(
      valueOf('.od-console:not(.od-plain) .od-toast-name', 'font-size', collapsed),
    ).toBe('18px');
    for (const consoleWidth of [BAND_MIN, BAND_MAX]) {
      expect(inscriptionBox(consoleWidth).font).toBe(18);
    }
  });

  it('holds `….docx` on a line of its own at every width in the band', () => {
    for (let consoleWidth = BAND_MIN; consoleWidth <= BAND_MAX; consoleWidth += 1) {
      const box = inscriptionBox(consoleWidth);
      const measure = metricsAt(box.font);
      expect(
        measure(`${FILENAME_ELLIPSIS}.docx`),
        `box too narrow for the ellipsis and the extension at console ` +
          `${consoleWidth}px (box ${box.width}px / ${box.font}px)`,
      ).toBeLessThanOrEqual(box.width);
    }
  });

  it('keeps the extension on the last line for every orphan-prone name in the band', () => {
    for (let consoleWidth = BAND_MIN; consoleWidth <= BAND_MAX; consoleWidth += 1) {
      const box = inscriptionBox(consoleWidth);
      const measure = metricsAt(box.font);
      for (const name of ORPHAN_PRONE) {
        const lines = fitFilename(name, { measure, width: box.width });
        const where = `${JSON.stringify(name)} at console ${consoleWidth}px ` +
          `(box ${box.width}px / ${box.font}px)`;
        expect(lines[lines.length - 1], `orphaned extension: ${where}`).not.toBe('docx');
        expect(
          lines[lines.length - 1],
          `last line does not carry the extension: ${where}`,
        ).toMatch(/\.docx$/);
        expectInside(lines, box);
      }
    }
  });
});
