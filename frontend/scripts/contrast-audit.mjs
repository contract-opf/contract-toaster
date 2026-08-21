#!/usr/bin/env node
/**
 * contrast-audit.mjs — CTDS theme & a11y hardening (issue #396).
 *
 * Plain Node ESM, zero dependencies, deterministic and offline (runs on
 * node 20). Parses frontend/src/styles/tokens.css for BOTH themes (day —
 * the top-level `:root { }` block — and night — the explicit
 * `:root[data-theme='dark'] { }` block, which the file's own comment
 * documents as authoritative and identical to the `@media
 * (prefers-color-scheme: dark)` block) and asserts WCAG AA contrast for
 * every token pairing that carries text or is a UI/focus affordance
 * (docs/frontend-design-system.md §4.3).
 *
 * Exits non-zero and lists every failing pair with its computed ratio.
 * Token values that can't be resolved to a plain hex color (color-mix(),
 * rgba() with alpha, unresolvable var() chains) are SKIPPED with a printed
 * warning rather than failing the audit — see docs/frontend-design-system.md
 * §4.3 and the ticket notes on issue #396. Prefer plain hex in tokens.css so
 * this skip list stays empty; as of #396 it is empty.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TOKENS_PATH = path.resolve(__dirname, '../src/styles/tokens.css');

// --------------------------------------------------------------- Parsing

/** Extract the body of the first top-level rule matching `selectorRe`,
 * using brace-depth counting so nested parens/at-rules inside values never
 * confuse the boundary (values here never contain literal `{`/`}`). */
function extractBlock(css, selectorRe, label) {
  const m = selectorRe.exec(css);
  if (!m) {
    throw new Error(`contrast-audit: could not find ${label} block in tokens.css`);
  }
  const braceStart = css.indexOf('{', m.index);
  let depth = 0;
  let i = braceStart;
  for (; i < css.length; i++) {
    if (css[i] === '{') depth++;
    else if (css[i] === '}') {
      depth--;
      if (depth === 0) break;
    }
  }
  if (depth !== 0) {
    throw new Error(`contrast-audit: unbalanced braces reading ${label} block`);
  }
  return css.slice(braceStart + 1, i);
}

/** Parse `--name: value;` custom-property declarations out of a block body
 * into a plain object. Trailing `/* ... *\/` comments after the `;` (our
 * convention in tokens.css) are outside the captured value, so they don't
 * need special handling here. */
function parseDeclarations(blockText) {
  const decls = {};
  const re = /--([a-zA-Z0-9-]+)\s*:\s*([^;]+);/g;
  let m;
  while ((m = re.exec(blockText))) {
    decls[`--${m[1]}`] = m[2].trim();
  }
  return decls;
}

const SKIP = Symbol('unresolvable-color');

/** Resolve a token's raw value to a plain hex color string, following
 * var(--x[, fallback]) chains against `map`. Returns SKIP (with a reason
 * pushed onto `warnings`) for anything that isn't a 3/6-digit hex literal
 * after resolution — color-mix(), rgba()/8-digit alpha hex, or a var()
 * that never bottoms out. */
function resolveColor(name, map, warnings, depth = 0) {
  if (depth > 10) {
    warnings.push(`${name}: var() chain too deep (possible cycle) — skipped`);
    return SKIP;
  }
  const raw = map[name];
  if (raw === undefined) {
    throw new Error(`contrast-audit: token ${name} referenced by an audited pair is not defined`);
  }
  return resolveValue(raw, map, warnings, depth, name);
}

function resolveValue(raw, map, warnings, depth, originName) {
  const varMatch = /^var\(\s*--([a-zA-Z0-9-]+)\s*(?:,\s*([\s\S]+))?\)$/.exec(raw.trim());
  if (varMatch) {
    const refName = `--${varMatch[1]}`;
    if (map[refName] !== undefined) {
      return resolveColor(refName, map, warnings, depth + 1);
    }
    if (varMatch[2] !== undefined) {
      return resolveValue(varMatch[2], map, warnings, depth + 1, originName);
    }
    warnings.push(`${originName}: var(${refName}) has no fallback and ${refName} is undefined — skipped`);
    return SKIP;
  }

  const hex = raw.trim();
  if (/^#[0-9a-fA-F]{3}$/.test(hex) || /^#[0-9a-fA-F]{6}$/.test(hex)) {
    return normalizeHex(hex);
  }

  // 8-digit alpha hex, rgba()/color-mix()/etc — the pixel color depends on
  // whatever surface renders behind it, which this static token audit
  // can't know. Documented, intentional skip (see file docstring).
  warnings.push(`${originName}: value "${hex}" is not a resolvable plain hex color — skipped`);
  return SKIP;
}

function normalizeHex(hex) {
  let h = hex.slice(1);
  if (h.length === 3) {
    h = h
      .split('')
      .map((c) => c + c)
      .join('');
  }
  return `#${h.toLowerCase()}`;
}

// ------------------------------------------------------------ WCAG math

function hexToRgb(hex) {
  const h = hex.slice(1);
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ];
}

function linearize(c) {
  const cs = c / 255;
  return cs <= 0.03928 ? cs / 12.92 : Math.pow((cs + 0.055) / 1.055, 2.4);
}

function relativeLuminance(hex) {
  const [r, g, b] = hexToRgb(hex);
  return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b);
}

function contrastRatio(hexA, hexB) {
  const lA = relativeLuminance(hexA);
  const lB = relativeLuminance(hexB);
  const lighter = Math.max(lA, lB);
  const darker = Math.min(lA, lB);
  return (lighter + 0.05) / (darker + 0.05);
}

// -------------------------------------------------------- Audited pairs

// docs/frontend-design-system.md §4.3: all text/background pairings must
// meet WCAG AA (4.5:1); --ct-accent vs --ct-bg meets the UI-component /
// focus-indicator threshold (3:1) instead, since it's not text.
const PAIRS = [
  { label: 'body text (--ct-text) vs --ct-bg', fg: '--ct-text', bg: '--ct-bg', min: 4.5 },
  { label: 'body text (--ct-text) vs --ct-surface', fg: '--ct-text', bg: '--ct-surface', min: 4.5 },
  { label: '--ct-text-muted vs --ct-surface', fg: '--ct-text-muted', bg: '--ct-surface', min: 4.5 },
  { label: '--ct-ok vs --ct-ok-bg', fg: '--ct-ok', bg: '--ct-ok-bg', min: 4.5 },
  { label: '--ct-warn vs --ct-warn-bg', fg: '--ct-warn', bg: '--ct-warn-bg', min: 4.5 },
  { label: '--ct-danger vs --ct-danger-bg', fg: '--ct-danger', bg: '--ct-danger-bg', min: 4.5 },
  { label: '--ct-info vs --ct-info-bg', fg: '--ct-info', bg: '--ct-info-bg', min: 4.5 },
  { label: '--ct-neutral vs --ct-neutral-bg', fg: '--ct-neutral', bg: '--ct-neutral-bg', min: 4.5 },
  { label: '--ct-accent-contrast vs --ct-accent', fg: '--ct-accent-contrast', bg: '--ct-accent', min: 4.5 },
  {
    label: '--ct-accent vs --ct-bg (UI-component / focus-indicator threshold)',
    fg: '--ct-accent',
    bg: '--ct-bg',
    min: 3.0,
  },
  // Issue #492: the outcome headline (`.ct-outcome-headline`,
  // ReviewSubmission.tsx) paints its text in one of these five status
  // tokens via OUTCOME_HEADLINE_COLOR_VAR, on whichever of the app's two
  // surfaces it ends up rendered against — pinned at the text threshold
  // (4.5:1), not the 3:1 UI-affordance one, since this is the panel's
  // largest run of text, not a control.
  { label: '--ct-ok vs --ct-bg (outcome headline)', fg: '--ct-ok', bg: '--ct-bg', min: 4.5 },
  { label: '--ct-ok vs --ct-surface (outcome headline)', fg: '--ct-ok', bg: '--ct-surface', min: 4.5 },
  { label: '--ct-warn vs --ct-bg (outcome headline)', fg: '--ct-warn', bg: '--ct-bg', min: 4.5 },
  {
    label: '--ct-warn vs --ct-surface (outcome headline)',
    fg: '--ct-warn',
    bg: '--ct-surface',
    min: 4.5,
  },
  { label: '--ct-danger vs --ct-bg (outcome headline)', fg: '--ct-danger', bg: '--ct-bg', min: 4.5 },
  {
    label: '--ct-danger vs --ct-surface (outcome headline)',
    fg: '--ct-danger',
    bg: '--ct-surface',
    min: 4.5,
  },
  { label: '--ct-accent vs --ct-bg (outcome headline)', fg: '--ct-accent', bg: '--ct-bg', min: 4.5 },
  {
    label: '--ct-accent vs --ct-surface (outcome headline)',
    fg: '--ct-accent',
    bg: '--ct-surface',
    min: 4.5,
  },
  {
    label: '--ct-text-muted vs --ct-bg (outcome headline)',
    fg: '--ct-text-muted',
    bg: '--ct-bg',
    min: 4.5,
  },
  {
    label: '--ct-text-muted vs --ct-surface (outcome headline)',
    fg: '--ct-text-muted',
    bg: '--ct-surface',
    min: 4.5,
  },
];

// ------------------------------------------------------------------ Main

function main() {
  const css = readFileSync(TOKENS_PATH, 'utf8');

  const dayBody = extractBlock(css, /:root\s*\{/, 'day (:root)');
  const nightBody = extractBlock(
    css,
    /:root\[data-theme=['"]dark['"]\]\s*\{/,
    "night (:root[data-theme='dark'])",
  );
  // Sanity check the two dark blocks the file's own comment promises stay
  // identical (the OS-preference media-query block and the explicit
  // [data-theme='dark'] override) — drift here is exactly the kind of bug
  // a static audit should catch instead of assuming.
  const nightMediaBody = extractBlock(
    css,
    /:root:not\(\[data-theme=['"]light['"]\]\)\s*\{/,
    'night (@media prefers-color-scheme: dark)',
  );

  const dayMap = parseDeclarations(dayBody);
  const nightOverrides = parseDeclarations(nightBody);
  const nightMediaOverrides = parseDeclarations(nightMediaBody);
  // Night theme = day tokens with the dark block's overrides layered on
  // top (typography/spacing/motion tokens aren't re-declared per theme).
  const nightMap = { ...dayMap, ...nightOverrides };

  const driftKeys = [];
  for (const key of new Set([...Object.keys(nightOverrides), ...Object.keys(nightMediaOverrides)])) {
    if (nightOverrides[key] !== nightMediaOverrides[key]) {
      driftKeys.push(`${key}: [data-theme='dark'] has "${nightOverrides[key]}", @media block has "${nightMediaOverrides[key]}"`);
    }
  }

  const warnings = [];
  const failures = [];
  const results = [];

  for (const themeName of ['day', 'night']) {
    const map = themeName === 'day' ? dayMap : nightMap;
    for (const pair of PAIRS) {
      const fgColor = resolveColor(pair.fg, map, warnings);
      const bgColor = resolveColor(pair.bg, map, warnings);
      if (fgColor === SKIP || bgColor === SKIP) {
        continue;
      }
      const ratio = contrastRatio(fgColor, bgColor);
      const pass = ratio >= pair.min;
      results.push({ theme: themeName, ...pair, ratio, pass });
      if (!pass) {
        failures.push(
          `[${themeName}] ${pair.label}: ${ratio.toFixed(2)}:1 (need ${pair.min}:1)`,
        );
      }
    }
  }

  console.log(`contrast-audit: checked ${results.length} pairing(s) across day + night themes`);
  if (warnings.length > 0) {
    console.log('\nSkipped (unresolvable color syntax):');
    for (const w of [...new Set(warnings)]) console.log(`  - ${w}`);
  }

  if (driftKeys.length > 0) {
    console.error("\nFAIL: :root[data-theme='dark'] and the @media(prefers-color-scheme: dark) block have drifted apart:");
    for (const d of driftKeys) console.error(`  - ${d}`);
    console.error('\ntokens.css documents these as required to stay identical — fix one to match the other.');
    process.exitCode = 1;
    return;
  }

  if (failures.length > 0) {
    console.error('\nFAIL: token pairs below WCAG AA:');
    for (const f of failures) console.error(`  - ${f}`);
    process.exitCode = 1;
    return;
  }

  console.log('\nCONTRAST AUDIT: ALL GREEN');
}

main();
