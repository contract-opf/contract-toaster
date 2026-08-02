#!/usr/bin/env node
/**
 * focus-audit.mjs — CTDS theme & a11y hardening (issue #396).
 *
 * Plain Node ESM, zero dependencies, deterministic and offline (node 20).
 * Two things locked in here (docs/frontend-design-system.md §4.4, §11):
 *
 * 1. Focus visibility — every component that renders an interactive part
 *    (button, icon-button, tab-bar, field, file-drop) must style
 *    `:focus-visible` with `--ct-focus-ring`; `base.css` must do the same
 *    for bare links and native form controls.
 * 2. Reduced motion — the global `prefers-reduced-motion: reduce` kill
 *    switch lives in base.css; no `ui/components/*.css` file hardcodes an
 *    animation/transition duration (must route through `--ct-dur*`); the
 *    toaster hero's inline stylesheet keeps its own guard (§3.2 — it can't
 *    use base.css's `*` selector because it's not in the document tree
 *    base.css's cascade reaches the same way bundled CSS is).
 * 3. Forced colors — `forced-colors: active` (Windows High Contrast)
 *    discards `box-shadow`, so a ring built only from `--ct-focus-ring`
 *    vanishes. No `:focus` / `:focus-visible` / `:focus-within` rule
 *    anywhere under `frontend/src` may suppress the outline — not with the
 *    `outline: none` / `outline: 0` shorthand, nor with the
 *    `outline-style: none` / `outline-width: 0` longhands. It must leave
 *    the UA outline alone, declare `--ct-focus-outline`
 *    (`2px solid transparent`, which forced-colors repaints with the system
 *    highlight colour), or sit inside an explicit
 *    `@media (forced-colors: active)` block (issue #438). A self-test of
 *    fixture mutations runs alongside, so the check fails closed if a later
 *    edit narrows what it detects.
 *
 * Exits non-zero listing every failing check.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { readdirSync } from 'node:fs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = path.resolve(__dirname, '../src');
const COMPONENTS_DIR = path.join(FRONTEND_SRC, 'ui/components');
const BASE_CSS_PATH = path.join(FRONTEND_SRC, 'styles/base.css');
const TOKENS_CSS_PATH = path.join(FRONTEND_SRC, 'styles/tokens.css');
const TOASTER_TSX_PATH = path.join(FRONTEND_SRC, 'toaster/Toaster.tsx');

// Components that render an interactive part of their own (issue #396
// scope). ct-card/ct-chip are shadow leaves with no interactive control;
// ct-app-shell/ct-toolbar/ct-table/ct-progress/ct-banner lay out or
// present, they don't own a focusable control themselves.
const FOCUS_REQUIRED_COMPONENTS = ['ct-button', 'ct-icon-button', 'ct-tab-bar', 'ct-field', 'ct-file-drop'];

const failures = [];

// --------------------------------------------------------------- Helpers

/** Escape a literal so it can be embedded in a RegExp source. */
function escapeRe(literal) {
  return literal.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** A selector matcher anchored to a selector boundary, so a check for
 * `a:focus-visible` cannot be satisfied by the tail of
 * `textarea:focus-visible` (nor `input:focus-visible` by
 * `.text-input:focus-visible`). */
function selectorPattern(selector) {
  return new RegExp(`(?:^|[\\s,{}])${escapeRe(selector)}\\b`, 'g');
}

/** True if `pattern` matches in `text` and the rule body that match opens
 * (i.e. up to the next `}`) contains `--ct-focus-ring`. Handles
 * multi-selector rules (`a, b:focus-visible { ... }`) and multi-line
 * declarations — it only needs the ring token to appear somewhere in the
 * same block as the matched :focus-visible occurrence. */
function focusRingFollows(text, pattern) {
  const re = new RegExp(pattern.source, pattern.flags.includes('g') ? pattern.flags : `${pattern.flags}g`);
  let m;
  while ((m = re.exec(text)) !== null) {
    const idx = m.index;
    const blockEnd = text.indexOf('}', idx);
    const block = blockEnd === -1 ? text.slice(idx) : text.slice(idx, blockEnd);
    if (block.includes('--ct-focus-ring')) return true;
    if (m[0].length === 0) re.lastIndex += 1;
  }
  return false;
}

// --------------------------------------------------------- 1. Focus rings

for (const name of FOCUS_REQUIRED_COMPONENTS) {
  const cssPath = path.join(COMPONENTS_DIR, `${name}.css`);
  let text;
  try {
    text = readFileSync(cssPath, 'utf8');
  } catch {
    failures.push(`focus: ${name}.css not found at ${cssPath}`);
    continue;
  }
  if (!text.includes(':focus-visible')) {
    failures.push(`focus: ${name}.css has no :focus-visible rule`);
    continue;
  }
  if (!focusRingFollows(text, /:focus-visible/g)) {
    failures.push(`focus: ${name}.css has :focus-visible but no --ct-focus-ring in that rule's body`);
  }
}

// base.css: links and native form controls (issue #396 scope explicitly
// calls these out, on top of the component list above).
{
  const base = readFileSync(BASE_CSS_PATH, 'utf8');
  if (!focusRingFollows(base, selectorPattern('a:focus-visible'))) {
    failures.push('focus: base.css has no a:focus-visible rule using --ct-focus-ring');
  }
  // Native form controls: input/select/textarea are declared together as a
  // multi-selector rule — checking any one of the three selector tokens
  // finds that shared rule body. Anchored, so a class ending in the same
  // text (`.text-input:focus-visible`) can't satisfy it.
  if (!focusRingFollows(base, selectorPattern('input:focus-visible'))) {
    failures.push('focus: base.css has no input:focus-visible rule using --ct-focus-ring');
  }
}

// ------------------------------------------------------- 2. Reduced motion

{
  const base = readFileSync(BASE_CSS_PATH, 'utf8');
  if (!/@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)/.test(base)) {
    failures.push('motion: base.css has no global @media (prefers-reduced-motion: reduce) guard');
  }
}

{
  const toaster = readFileSync(TOASTER_TSX_PATH, 'utf8');
  if (!/prefers-reduced-motion:\s*reduce/.test(toaster)) {
    failures.push('motion: Toaster.tsx inline stylesheet has no prefers-reduced-motion guard (§3.2)');
  }
}

// No hardcoded animation/transition durations in ui/components/*.css — must
// route through --ct-dur-fast/--ct-dur/--ct-dur-slow. A literal duration is
// digits immediately followed by "ms" or "s" (word-bounded, so it doesn't
// match the unitless numbers inside cubic-bezier(...) token values).
const HARDCODED_DURATION_RE = /\b\d+(?:\.\d+)?(?:ms|s)\b/;

for (const entry of readdirSync(COMPONENTS_DIR)) {
  if (!entry.endsWith('.css')) continue;
  const cssPath = path.join(COMPONENTS_DIR, entry);
  const text = readFileSync(cssPath, 'utf8');
  const declRe = /\b(animation|transition)(?:-duration|-delay)?\s*:\s*([^;]+);/g;
  let m;
  while ((m = declRe.exec(text))) {
    const [, prop, value] = m;
    if (HARDCODED_DURATION_RE.test(value)) {
      failures.push(
        `motion: ${entry} has a hardcoded duration in its "${prop}" declaration — use var(--ct-dur*) instead: ${value.trim()}`,
      );
    }
  }
}

// -------------------------------------------------------- 3. Forced colors

// `forced-colors: active` throws away box-shadow but honours outline, so a
// :focus-visible rule that pairs `--ct-focus-ring` with `outline: none`
// leaves keyboard users in Windows High Contrast Mode with NO focus
// indicator at all (WCAG 2.4.7). This check makes that a standing
// invariant rather than a one-time fix: it walks every stylesheet under
// frontend/src (plus the toaster's inline stylesheet) and rejects any
// :focus-visible rule that suppresses the outline.

/** Every *.css under `dir`, recursively, in a stable order. */
function cssFilesUnder(dir) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    if (entry.name === 'node_modules') continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...cssFilesUnder(full));
    else if (entry.name.endsWith('.css')) out.push(full);
  }
  return out;
}

/** Walk every `selector { declarations }` rule in `text`, carrying the
 * preludes of the at-rules it is nested inside. A plain rule regex cannot do
 * this: for a rule inside `@media (…) { … }` the match necessarily begins
 * *after* the at-rule's `{`, so the prelude is already gone by the time the
 * selector is captured — which is why the earlier draft's forced-colors
 * opt-out never fired. Brace depth is tracked instead. (This codebase uses
 * no native CSS nesting; a declaration block therefore ends at the next
 * `}`.) */
function* eachRule(text) {
  const atRules = [];
  let preludeStart = 0;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === '{') {
      const head = text.slice(preludeStart, i).trim();
      if (head.startsWith('@')) {
        atRules.push(head);
        preludeStart = i + 1;
        continue;
      }
      let end = text.indexOf('}', i + 1);
      if (end === -1) end = text.length;
      yield { selector: head, body: text.slice(i + 1, end), context: atRules.join(' ') };
      i = end;
      preludeStart = i + 1;
    } else if (ch === '}') {
      atRules.pop();
      preludeStart = i + 1;
    }
  }
}

// Any :focus-family selector, not just :focus-visible — an equal-specificity
// `a:focus { outline: none }` ordered after `a:focus-visible` wins on source
// order and silently restores the invisible-focus bug.
const FOCUS_SELECTOR_RE = /:focus(?:-visible|-within)?(?![\w-])/i;
// `outline`, plus the longhands that suppress it just as effectively —
// `outline-style: none` / `outline-width: 0` leave nothing for forced-colors
// to repaint. `outline-offset` is deliberately not in the alternation.
const OUTLINE_DECL_RE = /(?:^|[;{\s])(outline(?:-style|-width)?)\s*:\s*([^;}]+)/gi;
const ZERO_WIDTH_RE = /^0(?:px|em|rem|pt|pc|in|cm|mm|q|ex|ch|vw|vh|vmin|vmax)?$/;
const FORCED_COLORS_RE = /forced-colors\s*:\s*active/i;

/** The normalised value if `prop: value` leaves no paintable outline, else
 * null. */
function outlineSuppression(prop, rawValue) {
  const value = rawValue.replace(/!\s*important/i, '').trim().toLowerCase();
  if (!value) return null;
  if (prop === 'outline-style') return value === 'none' ? value : null;
  if (prop === 'outline-width') return ZERO_WIDTH_RE.test(value) ? value : null;
  // Shorthand: `none` anywhere is a style of none, `0` anywhere is a zero
  // width. `2px solid transparent` (the token) survives both tests.
  const parts = value.split(/\s+/);
  return parts.some((part) => part === 'none' || ZERO_WIDTH_RE.test(part)) ? value : null;
}

/** Every :focus-family rule in `text` that kills the outline, as failure
 * strings (returned rather than pushed, so the self-test below can run the
 * real checker over fixtures). */
function forcedColorsOutlineFailures(source, label) {
  const found = [];
  // Drop /* … */ comments first: they would otherwise land in the selector
  // capture (this file's rules are heavily commented) and a commented-out
  // `outline: none` would read as live.
  const text = source.replace(/\/\*[\s\S]*?\*\//g, '');
  for (const { selector, body, context } of eachRule(text)) {
    if (!FOCUS_SELECTOR_RE.test(selector)) continue;
    // A rule nested inside `@media (forced-colors: active)` is already the
    // forced-colors branch — whatever it does there is deliberate.
    if (FORCED_COLORS_RE.test(context)) continue;
    OUTLINE_DECL_RE.lastIndex = 0;
    let decl;
    while ((decl = OUTLINE_DECL_RE.exec(body)) !== null) {
      const prop = decl[1].toLowerCase();
      const value = outlineSuppression(prop, decl[2]);
      if (value === null) continue; // a real outline survives
      found.push(
        `forced-colors: ${label} — "${selector.replace(/\s+/g, ' ')}" sets "${prop}: ${value}", ` +
          'so under forced-colors: active (which discards box-shadow) there is no focus indicator; ' +
          'use `outline: var(--ct-focus-outline)` instead (issue #438)',
      );
    }
  }
  return found;
}

for (const cssPath of cssFilesUnder(FRONTEND_SRC)) {
  failures.push(...forcedColorsOutlineFailures(readFileSync(cssPath, 'utf8'), path.relative(FRONTEND_SRC, cssPath)));
}
failures.push(...forcedColorsOutlineFailures(readFileSync(TOASTER_TSX_PATH, 'utf8'), 'toaster/Toaster.tsx'));

// ------------------------------------------- 3b. Self-test (mutation cover)
//
// A static audit is only worth its runtime if it actually catches the thing
// it claims to catch, and every bypass below has been a real review finding
// against an earlier draft of this file. The fixtures run the REAL checker
// on every invocation, so the audit fails closed if a future edit narrows
// its detection.

const SELF_TEST_CASES = [
  // --- must be flagged -----------------------------------------------
  { name: 'outline shorthand none', flagged: true, css: '.a:focus-visible { outline: none; box-shadow: var(--ct-focus-ring); }' },
  { name: 'outline shorthand 0', flagged: true, css: '.a:focus-visible { outline: 0; }' },
  { name: 'outline-style longhand', flagged: true, css: '.b:focus-visible { outline-style: none; box-shadow: var(--ct-focus-ring); }' },
  { name: 'outline-width longhand', flagged: true, css: '.b:focus-visible { outline-width: 0; box-shadow: var(--ct-focus-ring); }' },
  { name: 'bare :focus overriding the fix', flagged: true, css: '.c:focus { outline: none; }' },
  { name: 'universal :focus', flagged: true, css: '*:focus { outline: none; }' },
  { name: ':focus-within', flagged: true, css: '.d:focus-within { outline: none; }' },
  // --- must NOT be flagged -------------------------------------------
  {
    name: 'explicit forced-colors block (the ticket’s documented fallback)',
    flagged: false,
    css: '@media (forced-colors: active) { .a:focus-visible { outline: none; } }',
  },
  {
    name: 'the token form this repo uses',
    flagged: false,
    css: '.e:focus-visible { outline: var(--ct-focus-outline); outline-offset: var(--ct-focus-outline-offset); box-shadow: var(--ct-focus-ring); }',
  },
  { name: 'non-focus rule', flagged: false, css: '.f:hover { outline: none; }' },
  { name: 'commented-out suppression', flagged: false, css: '/* .g:focus-visible { outline: none; } */' },
  { name: 'outline-offset is not outline-width', flagged: false, css: '.h:focus-visible { outline-offset: 0; outline: var(--ct-focus-outline); }' },
  { name: 'plain @media wrapper still scanned but compliant', flagged: false, css: '@media (max-width: 640px) { .i:focus-visible { outline: var(--ct-focus-outline); } }' },
];

for (const testCase of SELF_TEST_CASES) {
  const hits = forcedColorsOutlineFailures(testCase.css, 'self-test');
  if (testCase.flagged && hits.length === 0) {
    failures.push(`self-test: mutation "${testCase.name}" was NOT detected by the forced-colors check (issue #438)`);
  } else if (!testCase.flagged && hits.length > 0) {
    failures.push(`self-test: "${testCase.name}" must be exempt from the forced-colors check but was flagged: ${hits[0]}`);
  }
}

// The escape hatch is only an escape hatch if the token stays transparent
// rather than `none` — forced-colors repaints a transparent outline, it
// cannot repaint an absent one.
{
  const tokens = readFileSync(TOKENS_CSS_PATH, 'utf8');
  const tokenDecl = tokens.match(/--ct-focus-outline\s*:\s*([^;]+);/);
  if (!tokenDecl) {
    failures.push('forced-colors: tokens.css does not define --ct-focus-outline (issue #438)');
  } else if (!/\btransparent\b/i.test(tokenDecl[1])) {
    failures.push(
      `forced-colors: --ct-focus-outline must resolve to a transparent outline for forced-colors to repaint it, got "${tokenDecl[1].trim()}"`,
    );
  }
}

// ------------------------------------------------------------------ Report

if (failures.length > 0) {
  console.error('FAIL: focus/reduced-motion audit found problems:');
  for (const f of failures) console.error(`  - ${f}`);
  process.exitCode = 1;
} else {
  console.log(
    `focus-audit: ${FOCUS_REQUIRED_COMPONENTS.length} component(s) + base.css focus rings OK; reduced-motion guards OK; forced-colors outlines OK`,
  );
  console.log('FOCUS AUDIT: ALL GREEN');
}
