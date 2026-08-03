#!/usr/bin/env node
/**
 * layout-audit.mjs — the two ways this design system has actually broken
 * narrow-viewport reflow (issue #457, audit PART I; the first of the two also
 * bit the gallery harness at F2).
 *
 * Plain Node ESM, zero dependencies, deterministic and offline (node 20),
 * matching focus-audit.mjs's conventions — including its self-test of fixture
 * mutations, so this audit fails closed if a later edit narrows what it
 * detects.
 *
 * BE HONEST ABOUT WHAT THIS IS. Neither check renders anything, so neither is
 * evidence that the app reflows: they are source guards on two spellings that
 * have each produced a measured, reproduced overflow. jsdom implements no
 * layout, so a vitest assertion cannot do better; the real evidence for #457
 * is the browser measurement recorded in
 * docs/planning/frontend-release-audit-2026-07-27.md (PART I). What these
 * checks buy is that the exact regression cannot land again unnoticed.
 *
 * 1. Flexible grid tracks must be `minmax(0, …fr)`, never a bare `…fr`.
 *    A bare `1fr` is `minmax(auto, 1fr)`, and that automatic minimum is the
 *    MIN-CONTENT width of everything in the track — one non-shrinkable child
 *    (an admin table, a long email) pins the track open and drags the whole
 *    page with it. Measured twice: `.gallery-shell` (F2) and `ct-app-shell`
 *    (#457, where it pushed three tabs off-screen at a 375 viewport).
 *
 * 2. An absolutely-positioned visually-hidden helper must have a positioned
 *    OWNER. Such a box is clipped by its containing block's overflow, not by
 *    whatever element it sits inside — so a helper whose nearest positioned
 *    ancestor is outside a scroll container escapes that container and
 *    enlarges the DOCUMENT's scrollable overflow instead. That is what
 *    `.ct-button__live` did from inside a horizontally scrolled `ct-table`
 *    row: a 60px horizontal page scroll with nothing painted in it. Which
 *    element is the owner is a DOM fact CSS text cannot show, so it is
 *    declared per helper in HIDDEN_HELPER_OWNERS below and a helper that
 *    isn't declared there is itself a failure — new components fail closed
 *    rather than being skipped.
 *
 * Exits non-zero listing every failing check.
 */

import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = path.resolve(__dirname, '../src');

/**
 * Every visually-hidden clip-pattern helper in the design system, mapped to
 * the selector that must establish its containing block. Keep both halves
 * accurate when adding a helper: the value is the element the helper is a
 * child of in the component's rendered DOM, and that element must carry
 * `position: relative` (or another positioning value) in the same file.
 */
const HIDDEN_HELPER_OWNERS = {
  '.ct-button__live': 'ct-button',
  '.ct-file-drop__input': '.ct-file-drop__well',
};

const failures = [];

// --------------------------------------------------------------- Helpers

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

/** Walk every `selector { declarations }` rule, carrying the preludes of the
 * at-rules it is nested inside (same brace-depth walker focus-audit uses —
 * a plain rule regex loses the `@media` prelude). */
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

const stripComments = (source) => source.replace(/\/\*[\s\S]*?\*\//g, '');

// ------------------------------------------- 1. Flexible grid track minima

// The inline-axis track-list properties. `grid-template-rows` is deliberately
// absent: a bare `1fr` row track cannot cause a horizontal overflow.
const TRACK_DECL_RE = /(?:^|[;{\s])(grid-template-columns|grid-auto-columns)\s*:\s*([^;}]+)/gi;
// A `<flex>` value not already wrapped in minmax(): `1fr`, `.5fr`, `2.5fr`.
const BARE_FR_RE = /(?:^|[\s,(])(\d*\.?\d+fr)/i;

/** Track-list failures for one stylesheet, RETURNED (not pushed) so the
 * self-test below can run the real checker over fixtures. */
function bareFlexTrackFailures(source, label) {
  const found = [];
  const text = stripComments(source);
  for (const { selector, body } of eachRule(text)) {
    TRACK_DECL_RE.lastIndex = 0;
    let decl;
    while ((decl = TRACK_DECL_RE.exec(body)) !== null) {
      const prop = decl[1].toLowerCase();
      const value = decl[2].trim();
      // Remove every minmax(...) — what remains is the unwrapped part of the
      // track list, which is where a bare <flex> is a bug.
      const unwrapped = value.replace(/minmax\s*\([^)]*\)/gi, ' ');
      const bare = unwrapped.match(BARE_FR_RE);
      if (!bare) continue;
      found.push(
        `grid track: ${label} — "${selector.replace(/\s+/g, ' ')}" sets "${prop}: ${value}"; ` +
          `a bare \`${bare[1]}\` is \`minmax(auto, ${bare[1]})\`, whose automatic minimum is the ` +
          'min-content width of the track\'s contents — one non-shrinkable child pins the track ' +
          `open and the page scrolls horizontally. Write \`minmax(0, ${bare[1]})\` (issue #457, audit F2).`,
      );
    }
  }
  return found;
}

for (const cssPath of cssFilesUnder(FRONTEND_SRC)) {
  failures.push(...bareFlexTrackFailures(readFileSync(cssPath, 'utf8'), path.relative(FRONTEND_SRC, cssPath)));
}

// --------------------------------- 2. Visually-hidden helpers are contained

// The clip pattern: absolutely positioned, 1px box, `clip: rect(0, 0, 0, 0)`.
const CLIP_RECT_RE = /clip\s*:\s*rect\s*\(\s*0[\s,]/i;
const ABSOLUTE_RE = /position\s*:\s*absolute/i;
const POSITIONED_RE = /position\s*:\s*(relative|absolute|fixed|sticky)/i;

/** Containment failures for one stylesheet, RETURNED for the self-test. */
function hiddenHelperFailures(source, label, owners = HIDDEN_HELPER_OWNERS) {
  const found = [];
  const text = stripComments(source);
  const rules = [...eachRule(text)];
  for (const { selector, body } of rules) {
    if (!ABSOLUTE_RE.test(body) || !CLIP_RECT_RE.test(body)) continue;
    const helper = selector.trim();
    const owner = owners[helper];
    if (!owner) {
      found.push(
        `hidden helper: ${label} — "${helper}" is an absolutely-positioned visually-hidden helper that ` +
          'is not declared in HIDDEN_HELPER_OWNERS. Add it with the selector that establishes its ' +
          'containing block, so this audit can check that owner stays positioned (issue #457).',
      );
      continue;
    }
    const ownerPositioned = rules.some(
      (rule) =>
        rule.selector
          .split(',')
          .map((part) => part.trim())
          .includes(owner) && POSITIONED_RE.test(rule.body),
    );
    if (!ownerPositioned) {
      found.push(
        `hidden helper: ${label} — "${helper}" is absolutely positioned, but its owner "${owner}" ` +
          'declares no `position`, so the helper is laid out against a containing block further up and ' +
          'escapes any scroll container in between — enlarging the page\'s scrollable overflow with an ' +
          'invisible horizontal scroll (issue #457).',
      );
    }
  }
  return found;
}

for (const cssPath of cssFilesUnder(FRONTEND_SRC)) {
  failures.push(...hiddenHelperFailures(readFileSync(cssPath, 'utf8'), path.relative(FRONTEND_SRC, cssPath)));
}

// Every declared owner must actually exist somewhere, or the map rots into a
// list of selectors nothing checks.
{
  const allCss = cssFilesUnder(FRONTEND_SRC)
    .map((p) => stripComments(readFileSync(p, 'utf8')))
    .join('\n');
  for (const helper of Object.keys(HIDDEN_HELPER_OWNERS)) {
    if (!allCss.includes(helper)) {
      failures.push(
        `hidden helper: HIDDEN_HELPER_OWNERS lists "${helper}", which no stylesheet under src/ defines — ` +
          'drop the entry or fix the selector (issue #457).',
      );
    }
  }
}

// ------------------------------------------- 3. Self-test (mutation cover)
//
// The fixtures run the REAL checkers on every invocation: if a later edit
// narrows either check, the audit fails rather than quietly passing.

const TRACK_CASES = [
  { name: 'bare 1fr', flagged: true, css: 'ct-app-shell { grid-template-columns: 1fr auto; }' },
  { name: 'bare 1fr alone', flagged: true, css: '.a { grid-template-columns: 1fr; }' },
  { name: 'fractional bare flex', flagged: true, css: '.b { grid-template-columns: 220px 2.5fr; }' },
  { name: 'bare flex inside @media', flagged: true, css: '@media (max-width: 640px) { .c { grid-template-columns: 1fr; } }' },
  { name: 'bare flex on grid-auto-columns', flagged: true, css: '.d { grid-auto-columns: 1fr; }' },
  { name: 'one wrapped, one bare', flagged: true, css: '.e { grid-template-columns: minmax(0, 1fr) 1fr; }' },
  // --- must NOT be flagged -------------------------------------------
  { name: 'minmax(0, 1fr)', flagged: false, css: '.f { grid-template-columns: minmax(0, 1fr) auto; }' },
  { name: 'two wrapped tracks', flagged: false, css: '.g { grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); }' },
  { name: 'fixed tracks only', flagged: false, css: '.h { grid-template-columns: 220px auto; }' },
  { name: 'row tracks are out of scope', flagged: false, css: '.i { grid-template-rows: 1fr auto; }' },
  { name: 'commented-out bare flex is not live', flagged: false, css: '.j { /* grid-template-columns: 1fr; */ grid-template-columns: minmax(0, 1fr); }' },
];

for (const testCase of TRACK_CASES) {
  const hits = bareFlexTrackFailures(testCase.css, 'self-test');
  if (testCase.flagged && hits.length === 0) {
    failures.push(`self-test: mutation "${testCase.name}" was NOT detected by the grid-track check (issue #457)`);
  } else if (!testCase.flagged && hits.length > 0) {
    failures.push(`self-test: "${testCase.name}" must be exempt from the grid-track check but was flagged: ${hits[0]}`);
  }
}

const HELPER_OWNERS_FIXTURE = { '.x__live': 'x-host' };
const HIDDEN = 'position: absolute; width: 1px; height: 1px; clip: rect(0, 0, 0, 0);';
const HELPER_CASES = [
  {
    name: 'owner declares no position',
    flagged: true,
    css: `x-host { display: inline-block; } .x__live { ${HIDDEN} }`,
  },
  {
    name: 'position: relative on a descendant is not the owner',
    flagged: true,
    css: `x-host { display: inline-block; } x-host[data-armed] .x__el { position: relative; } .x__live { ${HIDDEN} }`,
  },
  {
    name: 'undeclared helper fails closed',
    flagged: true,
    css: `.y__live { ${HIDDEN} }`,
  },
  // --- must NOT be flagged -------------------------------------------
  {
    name: 'owner is positioned',
    flagged: false,
    css: `x-host { display: inline-block; position: relative; } .x__live { ${HIDDEN} }`,
  },
  {
    name: 'owner positioned in a multi-selector rule',
    flagged: false,
    css: `x-host, .other { position: relative; } .x__live { ${HIDDEN} }`,
  },
  {
    name: 'an absolute box that is not the clip pattern is out of scope',
    flagged: false,
    css: '.z::after { position: absolute; inset: -2px; }',
  },
];

for (const testCase of HELPER_CASES) {
  const hits = hiddenHelperFailures(testCase.css, 'self-test', HELPER_OWNERS_FIXTURE);
  if (testCase.flagged && hits.length === 0) {
    failures.push(`self-test: mutation "${testCase.name}" was NOT detected by the hidden-helper check (issue #457)`);
  } else if (!testCase.flagged && hits.length > 0) {
    failures.push(`self-test: "${testCase.name}" must be exempt from the hidden-helper check but was flagged: ${hits[0]}`);
  }
}

// ------------------------------------------------------------------ Report

if (failures.length > 0) {
  console.error('FAIL: layout audit found problems:');
  for (const f of failures) console.error(`  - ${f}`);
  process.exitCode = 1;
} else {
  console.log(
    `layout-audit: grid tracks OK; ${Object.keys(HIDDEN_HELPER_OWNERS).length} visually-hidden helper(s) contained by a positioned owner`,
  );
  console.log('LAYOUT AUDIT: ALL GREEN');
}
