/**
 * orbitCss.ts — the SHIPPED stylesheet's cascade, resolved against the DOM the
 * console actually rendered.
 *
 * ## Why this exists
 *
 * vitest runs with `css: false` (frontend/vitest.config.ts) and jsdom
 * implements no cascade, so `getComputedStyle(el).display` reports the jsdom
 * default and asserting it asserts nothing. This reads `orbit-diner/orbit.css`
 * off disk and resolves the winning declaration for a real element —
 * importance, then specificity, then document order — over the rules that
 * apply in the emulated mode.
 *
 * The join to the live DOM is `Element.matches`, which is the whole point: a
 * rule is part of an element's cascade only if it actually selects that
 * element, so a class the component stops emitting, or a structural selector
 * (`.od-console > *:not(…)`, `:empty`) the rendered tree no longer satisfies,
 * fails here. Reading the stylesheet as text and asserting on the text proves
 * only that somebody typed the rule.
 *
 * ## Honesty rules for callers
 *
 * A stylesheet assertion that resolves NOTHING passes just as quietly as one
 * that resolves the right thing, so:
 *
 *   - pair every negative with a positive that fails if the resolver stops
 *     discriminating (see resilience-a11y.test.tsx's illustrated-view
 *     controls, and toast-receipt.test.tsx's screen-vs-print pair), and
 *   - mutation-check as you write: delete the rule you are asserting and
 *     watch the test go red.
 *
 * ## What a mode means
 *
 * Only the unconditional rules, plus the at-rule blocks whose condition the
 * emulated mode actually meets. Everything else states a condition this render
 * does not meet — viewport widths, colour scheme, container sizes — and is
 * dropped rather than quietly merged in, because merging a rule that would not
 * apply is how a stylesheet test starts proving the opposite of the truth.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The rendering condition to resolve under.
 *
 *   - `screen`: the unconditional rules alone — an ordinary sighted render.
 *   - `forced-colors`: those plus `@media (forced-colors: active)`.
 *   - `print`: those plus `@media print`, which is what a browser applies to
 *     the printed sheet (issue #740).
 */
export type OrbitMode = 'screen' | 'forced-colors' | 'print';

interface StyleRule {
  selector: string;
  body: string;
  order: number;
}

const ORBIT_CSS = path.join(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..'),
  'orbit-diner',
  'orbit.css'
);

const ruleCache = new Map<OrbitMode, StyleRule[]>();

/** Whether an at-rule prelude states a condition this mode meets. Deliberately
 *  narrow: a compound query (`@media print and (min-width: 40em)`) states more
 *  than the mode emulates, so it is dropped rather than assumed. */
function atRuleApplies(prelude: string, mode: OrbitMode): boolean {
  if (!/^@media\b/.test(prelude)) return false;
  if (mode === 'forced-colors') return /forced-colors\s*:\s*active/.test(prelude);
  if (mode === 'print') return /^@media\s+print\s*$/.test(prelude);
  return false;
}

/** Every rule the shipped stylesheet applies in the given mode. */
export function orbitRules(mode: OrbitMode): StyleRule[] {
  const cached = ruleCache.get(mode);
  if (cached) return cached;
  const css = readFileSync(ORBIT_CSS, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules: StyleRule[] = [];
  const collect = (source: string): void => {
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
        if (atRuleApplies(prelude, mode)) collect(body);
        continue;
      }
      if (prelude) rules.push({ selector: prelude, body, order: rules.length });
    }
  };
  collect(css);
  ruleCache.set(mode, rules);
  return rules;
}

/** nwsapi throws on selector syntax it does not implement; a rule this
 *  element cannot be tested against is simply not part of its cascade. */
export function matchesSafely(el: Element, selector: string): boolean {
  try {
    return el.matches(selector);
  } catch {
    return false;
  }
}

function specificity(selector: string): number {
  const ids = (selector.match(/#[\w-]+/g) ?? []).length;
  const classes = (selector.match(/\.[\w-]+|\[[^\]]*\]|:(?!:)[a-z-]+(\([^)]*\))?/gi) ?? [])
    .length;
  const elements = (selector.match(/(^|[\s>+~])[a-z][\w-]*/gi) ?? []).length;
  return ids * 10000 + classes * 100 + elements;
}

/**
 * The value the stylesheet resolves for `properties` on this element, or
 * `undefined` when nothing applicable declares any of them. Several property
 * names are accepted at once so a shorthand and its longhand compete in one
 * cascade (`border` against `border-width`).
 */
export function resolveStyle(
  el: Element,
  properties: string[],
  mode: OrbitMode
): string | undefined {
  let bestRank: [number, number, number] | null = null;
  let bestValue: string | undefined;
  for (const rule of orbitRules(mode)) {
    const matched = rule.selector
      .split(',')
      .map((one) => one.trim())
      .filter((one) => one.length > 0 && matchesSafely(el, one));
    if (matched.length === 0) continue;
    const spec = Math.max(...matched.map(specificity));
    for (const declaration of rule.body.split(';')) {
      const colon = declaration.indexOf(':');
      if (colon === -1) continue;
      const property = declaration.slice(0, colon).trim().toLowerCase();
      if (!properties.includes(property)) continue;
      let value = declaration.slice(colon + 1).trim();
      const important = /!\s*important$/i.test(value);
      if (important) value = value.replace(/!\s*important$/i, '').trim();
      const rank: [number, number, number] = [important ? 1 : 0, spec, rule.order];
      const wins =
        !bestRank ||
        rank[0] > bestRank[0] ||
        (rank[0] === bestRank[0] &&
          (rank[1] > bestRank[1] || (rank[1] === bestRank[1] && rank[2] > bestRank[2])));
      if (wins) {
        bestRank = rank;
        bestValue = value;
      }
    }
  }
  return bestValue;
}
