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

// ------------------------------------------------------------------ Report

if (failures.length > 0) {
  console.error('FAIL: focus/reduced-motion audit found problems:');
  for (const f of failures) console.error(`  - ${f}`);
  process.exitCode = 1;
} else {
  console.log(
    `focus-audit: ${FOCUS_REQUIRED_COMPONENTS.length} component(s) + base.css focus rings OK; reduced-motion guards OK`,
  );
  console.log('FOCUS AUDIT: ALL GREEN');
}
