/**
 * orbitInscription.ts — the inscription's box and the metric fixture that
 * measures it, both derived from the SHIPPED stylesheet (issues #739, #78).
 *
 * ## Why this exists
 *
 * `orbit-diner/filename.ts` is a pure function of a `measure()` and a width, so
 * it can be tested exactly — but only if the width is the one the console
 * really hands it. vitest runs jsdom with `css: false` (frontend/vitest.config.ts)
 * and jsdom implements no layout, so the box has to be worked out of
 * `orbit-diner/orbit.css` here. Every number below is read from that sheet; a
 * stylesheet edit that moves the inscription's rectangle re-derives the box
 * instead of leaving a fixture asserting an arrangement that no longer ships.
 *
 * It lives in `support/` rather than inside one test file because two files now
 * need the same derivation — `orbit-diner-filename.test.tsx`'s width sweep and
 * `orbit-diner-filename-band-78.test.tsx` — and a duplicated derivation is a
 * derivation that drifts.
 *
 * ## THE CONSOLE HAS FIVE STEPS, NOT TWO (issue #78 part 2)
 *
 * The first version of this derivation knew two: the phone step
 * (`@container od-console (max-width: 560px)`) and the wide step
 * (`min-width: 1221px`). Every non-phone width was modelled with the WIDE
 * step's numbers — 24px scene padding, the two-column `minmax(0, 1fr)
 * minmax(0, 1.02fr)` appliance track list, 20px type — and that is simply not
 * what ships between 561px and 1220px, where `orbit.css` collapses
 * `.od-appliances` to ONE column capped at 720px and sets the inscription at
 * 18px. Modelling a 561px console as half of a two-column grid produced a
 * 58px box at 20px type, narrow enough that `….docx` did not fit on a line of
 * its own; #739's sweep skipped that band with a `continue` and #78 was filed
 * against it. The band was an artefact of this helper, not of the console: the
 * shipped rules give a 561px console a 124px box at 18px type.
 *
 * So the resolution below reads the at-rule conditions and keeps only the rules
 * that actually apply at the console width asked for. Deliberately narrow, the
 * way `support/orbitCss.ts` is: a rule sitting under anything OTHER than an
 * `od-console` container query — `@media print`, `forced-colors`,
 * `prefers-reduced-motion` — states a condition this derivation does not
 * emulate and is dropped rather than quietly merged in.
 */
import { expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { MeasureText } from '../../orbit-diner/filename';

// ---------------------------------------------------------------------------
// The metric fixture
// ---------------------------------------------------------------------------

/**
 * Approximate advance widths for Space Grotesk 600, in em, rounded.
 *
 * They are not exact and do not need to be: nothing that uses them claims a
 * real render. What the fixture must have is VARIATION — `lillil.docx` and
 * `MWMWMW.docx` are the same length and nowhere near the same width — so that
 * an implementation which imposed a character-count limit, which #739 forbids,
 * cannot satisfy the assertions built on it.
 */
export const ADVANCE: Record<string, number> = {
  ' ': 0.26,
  '-': 0.35,
  _: 0.5,
  '.': 0.28,
  '/': 0.4,
  '…': 0.9,
  a: 0.55, b: 0.57, c: 0.52, d: 0.58, e: 0.55, f: 0.33, g: 0.57, h: 0.56,
  i: 0.26, j: 0.26, k: 0.53, l: 0.26, m: 0.85, n: 0.56, o: 0.57, p: 0.57,
  q: 0.57, r: 0.38, s: 0.5, t: 0.38, u: 0.56, v: 0.5, w: 0.76, x: 0.5,
  y: 0.5, z: 0.48,
  A: 0.68, B: 0.66, C: 0.68, D: 0.7, E: 0.6, F: 0.58, G: 0.72, H: 0.72,
  I: 0.3, J: 0.5, K: 0.66, L: 0.56, M: 0.85, N: 0.72, O: 0.74, P: 0.64,
  Q: 0.74, R: 0.64, S: 0.62, T: 0.6, U: 0.72, V: 0.68, W: 0.96, X: 0.66,
  Y: 0.64, Z: 0.6,
};
/** Digits are tabular in this family: one width for all ten. */
const DIGIT_ADVANCE = 0.55;
/** Anything the table does not name — punctuation, accents, CJK. */
const FALLBACK_ADVANCE = 0.55;

const advanceEm = (char: string): number =>
  /[0-9]/.test(char) ? DIGIT_ADVANCE : (ADVANCE[char] ?? FALLBACK_ADVANCE);

/** A `measure()` at `fontPx`, summing the fixture's per-glyph advances. */
export function metricsAt(fontPx: number): MeasureText {
  return (text: string) =>
    Array.from(text).reduce((sum, char) => sum + advanceEm(char) * fontPx, 0);
}

// ---------------------------------------------------------------------------
// The shipped stylesheet
// ---------------------------------------------------------------------------

const ORBIT_CSS = path.join(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..'),
  'orbit-diner',
  'orbit.css',
);

export interface StyleRule {
  selector: string;
  body: string;
  /** At-rule preludes this rule sits under, outermost first. */
  conditions: string[];
}

/** Every style rule in the sheet, flattened, each carrying the at-rule
 *  conditions it sits under. Comments are stripped first — this stylesheet
 *  quotes CSS inside its comments, and a commented-out declaration must never
 *  satisfy an assertion. */
function readOrbitRules(): StyleRule[] {
  const css = readFileSync(ORBIT_CSS, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules: StyleRule[] = [];
  const collect = (source: string, conditions: string[]): void => {
    let i = 0;
    while (i < source.length) {
      const open = source.indexOf('{', i);
      if (open === -1) break;
      const prelude = source.slice(i, open).trim();
      let depth = 1;
      let j = open + 1;
      for (; j < source.length && depth > 0; j += 1) {
        if (source[j] === '{') depth += 1;
        else if (source[j] === '}') depth -= 1;
      }
      const body = source.slice(open + 1, j - 1);
      i = j;
      if (prelude.startsWith('@')) {
        collect(body, [...conditions, prelude]);
        continue;
      }
      if (prelude) rules.push({ selector: prelude, body, conditions });
    }
  };
  collect(css, []);
  return rules;
}

/** The whole sheet, in document order. */
export const RULES = readOrbitRules();

/** The declared value of `property` in one rule, or undefined. */
export function declared(rule: StyleRule, property: string): string | undefined {
  let found: string | undefined;
  for (const declaration of rule.body.split(';')) {
    const colon = declaration.indexOf(':');
    if (colon === -1) continue;
    if (declaration.slice(0, colon).trim().toLowerCase() !== property) continue;
    found = declaration
      .slice(colon + 1)
      .trim()
      .replace(/!\s*important$/i, '')
      .trim();
  }
  return found;
}

/** Whether a rule lists exactly this selector in its (possibly grouped) list. */
function lists(rule: StyleRule, selector: string): boolean {
  return rule.selector
    .split(',')
    .map((one) => one.trim())
    .includes(selector);
}

/** Rules listing `selector`, under the at-rule `condition` (top level when
 *  omitted). */
export function rulesFor(selector: string, condition?: string): StyleRule[] {
  return RULES.filter(
    (rule) =>
      lists(rule, selector) &&
      (condition === undefined
        ? rule.conditions.length === 0
        : rule.conditions.includes(condition)),
  );
}

/**
 * What the cascade resolves `property` to for `selector` at that scope: the
 * LAST declaration across every rule listing it. `orbit.css` restates several
 * selectors, so reading only the first block asserts a value the browser does
 * not use.
 */
export function valueOf(selector: string, property: string, condition?: string): string {
  const bodies = rulesFor(selector, condition);
  expect(
    bodies.length,
    `rule not found: ${selector}${condition ? ` inside ${condition}` : ''}`,
  ).toBeGreaterThan(0);
  let value: string | undefined;
  for (const rule of bodies) value = declared(rule, property) ?? value;
  expect(
    value,
    `${selector}${condition ? ` inside ${condition}` : ''} declares no ${property}`,
  ).toBeDefined();
  return value as string;
}

export const px = (value: string): number => {
  expect(value).toMatch(/^-?[\d.]+px$/);
  return Number.parseFloat(value);
};
export const percent = (value: string): number => {
  expect(value).toMatch(/^[\d.]+%$/);
  return Number.parseFloat(value) / 100;
};

// ---------------------------------------------------------------------------
// Resolving the cascade at one console width
// ---------------------------------------------------------------------------

const CONTAINER_QUERY = /^@container\s+od-console\s*\(\s*(max|min)-width\s*:\s*([\d.]+)px\s*\)$/;

/**
 * Whether one at-rule prelude is an `od-console` container query this console
 * width satisfies. Anything else — a media query, `@supports`, a container
 * query on another container — states a condition this derivation does not
 * emulate, so the rules under it are dropped.
 */
function conditionHolds(prelude: string, consoleWidth: number): boolean {
  const match = CONTAINER_QUERY.exec(prelude.trim());
  if (!match) return false;
  const bound = Number.parseFloat(match[2]);
  return match[1] === 'max' ? consoleWidth <= bound : consoleWidth >= bound;
}

/** Whether every at-rule this rule sits under holds at `consoleWidth`. */
function ruleApplies(rule: StyleRule, consoleWidth: number): boolean {
  return rule.conditions.every((prelude) => conditionHolds(prelude, consoleWidth));
}

/**
 * The winning declaration for `properties` on the first of `selectors` that
 * declares any of them at `consoleWidth`, or `undefined`.
 *
 * `selectors` is ordered MOST SPECIFIC FIRST and that ordering is the caller's
 * statement of the specificity relationship — `.od-console:not(.od-plain)
 * .od-toast-name` (0,3,0) beats `.od-toast-name` (0,1,0) whatever the document
 * order, so the 18px step and the plain-view-exempt rectangle cannot be
 * resolved by source position alone. Within one selector, and across the
 * `properties` listed (so a shorthand and its longhand compete in one
 * cascade), the LAST applicable declaration in document order wins.
 */
function resolveAt(
  selectors: string[],
  properties: string[],
  consoleWidth: number,
): string | undefined {
  for (const selector of selectors) {
    let winner: string | undefined;
    for (const rule of RULES) {
      if (!lists(rule, selector) || !ruleApplies(rule, consoleWidth)) continue;
      for (const property of properties) {
        const value = declared(rule, property);
        if (value !== undefined) winner = value;
      }
    }
    if (winner !== undefined) return winner;
  }
  return undefined;
}

/** `resolveAt`, but a miss is a failed assertion rather than a silent hole:
 *  a box derived from a rule that no longer exists is a fixture asserting
 *  nothing. */
function requireAt(selectors: string[], properties: string[], consoleWidth: number): string {
  const value = resolveAt(selectors, properties, consoleWidth);
  expect(
    value,
    `no ${properties.join('/')} for ${selectors.join(' | ')} at console ${consoleWidth}px`,
  ).toBeDefined();
  return value as string;
}

/** The left inset a `padding` shorthand or a `padding-left` longhand states. */
function leftInset(value: string): number {
  const parts = value.trim().split(/\s+/);
  // 1 value: all sides. 2 or 3: the second is the inline pair. 4: the fourth.
  const left = parts.length >= 4 ? parts[3] : parts.length >= 2 ? parts[1] : parts[0];
  return px(left);
}

// ---------------------------------------------------------------------------
// The inscription box
// ---------------------------------------------------------------------------

/** The rectangle 04p2 gives the name, exempt in the plain view. */
export const RECTANGLE = '.od-console:not(.od-plain) .od-toast-name';

export interface Box {
  /** Usable width of the inscription, in CSS px. */
  width: number;
  /** `max-height` of the inscription, in CSS px. */
  height: number;
  /** The font size the stylesheet sets at this step. */
  font: number;
}

/** The share of the slice the 04p2 rectangle leaves for the name. */
export function inscriptionShare(): number {
  const left = percent(valueOf(RECTANGLE, 'left'));
  const right = percent(valueOf(RECTANGLE, 'right'));
  return 1 - left - right;
}

/** The `fr` coefficients in a `grid-template-columns` track list. */
function frCoefficients(tracks: string): number[] {
  const found = Array.from(tracks.matchAll(/([\d.]+)fr/g)).map((m) => Number(m[1]));
  expect(found.length, `no fr track in ${JSON.stringify(tracks)}`).toBeGreaterThan(0);
  return found;
}

/**
 * The inscription's box at a console content width, worked out of the shipped
 * rules that apply AT THAT WIDTH: scene padding, the appliance track list (and
 * the cap it carries where it collapses to one column), the slice's share of
 * the figure, and the name's share of the slice.
 *
 * The toaster is the FIRST appliance column, so the figure is that column's
 * width: the free space after the gaps, split by the `fr` coefficients.
 * `.od-toaster-figure` is `width: 100%` of it, and `.od-slice` is a percentage
 * of the figure with `aspect-ratio: 1`.
 */
export function inscriptionBox(consoleWidth: number): Box {
  const scene = consoleWidth - 2 * leftInset(requireAt(['.od-scene'], ['padding', 'padding-left'], consoleWidth));
  const tracks = frCoefficients(
    requireAt(['.od-appliances'], ['grid-template-columns'], consoleWidth),
  );
  const cap = resolveAt(['.od-appliances'], ['max-width'], consoleWidth);
  const inner = cap === undefined ? scene : Math.min(scene, px(cap));
  const gap = px(requireAt(['.od-appliances'], ['gap'], consoleWidth));
  const frTotal = tracks.reduce((sum, one) => sum + one, 0);
  const figure = ((inner - gap * (tracks.length - 1)) / frTotal) * tracks[0];
  const sliceWidth = figure * percent(requireAt(['.od-slice'], ['width'], consoleWidth));
  // The slice is `aspect-ratio: 1`, so its height is its width.
  expect(valueOf('.od-slice', 'aspect-ratio')).toBe('1');
  return {
    width: Math.floor(sliceWidth * inscriptionShare()),
    height: sliceWidth * percent(requireAt([RECTANGLE], ['max-height'], consoleWidth)),
    font: px(requireAt([RECTANGLE, '.od-toast-name'], ['font-size'], consoleWidth)),
  };
}

/** The reachable console range: 300px is the narrowest the shell allows and
 *  1272px the widest (a 1320px `--ct-maxw` shell less 24px of padding a side,
 *  issue #725). */
export const CONSOLE_MIN_PX = 300;
export const CONSOLE_MAX_PX = 1272;

/** 1360px viewport: the console at its 1272px ceiling, 20px type. */
export const DESKTOP = inscriptionBox(CONSOLE_MAX_PX);
/** 390px viewport: a 342px console on the phone step, 14px type. */
export const PHONE = inscriptionBox(342);

// ---------------------------------------------------------------------------
// Fixtures two files share
// ---------------------------------------------------------------------------

/** The designer's own short fixture (round-3, O1). */
export const SHORT = 'Mutual NDA draft-v3.docx';

/**
 * Ordinary, short names whose stem-plus-dot fits a box the whole name does
 * not — the shape that painted `["Amendment 2.", "docx"]` before #739 welded
 * the extension onto the last chunk. They are the width sweep's subjects in
 * both files, so they live here rather than being retyped into the second one.
 */
export const ORPHAN_PRONE = [
  'Amendment 2.docx',
  'Schedule 3.docx',
  'EIAA Northwestern.docx',
  SHORT,
];
