#!/usr/bin/env node
/**
 * css-refs-audit.mjs — every stylesheet reference the TS/TSX/CSS under
 * frontend/src makes must resolve to something a stylesheet defines.
 *
 * Plain Node ESM, zero dependencies, deterministic and offline (node 20),
 * matching focus-audit.mjs / layout-audit.mjs — including a self-test of
 * fixture mutations, so the audit fails closed if a later edit narrows what
 * it detects.
 *
 * WHY THIS EXISTS. vitest runs jsdom with `css: false` (CLAUDE.md, "A green
 * gate is necessary, not sufficient"), so no component test can tell a class
 * name with no rule, or a `var(--ct-…)` with no declaration, from a working
 * one. The 2026-10-31 QA pass found exactly that class of defect shipped
 * green: `--ct-bg-subtle` / `--ct-border-subtle` / `--ct-bg-card` and a
 * family of `--ct-color-*` names that tokens.css never declared (so every
 * panel fell back to Vite's stock `#646cff` or a dark-only rgba), and
 * `ct-row--between` / `ct-row--wrap` / `ct-input` class names with no rule
 * behind them. Checks:
 *
 * 1. Custom properties. Every `var(--ct-*)` and `var(--od-*)` used in a
 *    stylesheet, a Lit `css\`` block or a TS/TSX string must name a property
 *    DECLARED somewhere under src (a `--x:` declaration in CSS, or a
 *    `'--x':` style-object key in TS/TSX for the ones the console sets at
 *    runtime). A fallback (`var(--x, #646cff)`) does not excuse an undeclared
 *    name — the fallback is precisely what painted the defect.
 * 2. Theme parity. Every token tokens.css overrides in its
 *    `prefers-color-scheme: dark` block must also be overridden in its
 *    explicit `[data-theme='dark']` block and vice versa — otherwise the two
 *    routes to dark mode paint differently.
 * 3. Class names. Every `ct-*` / `od-*` / `toaster-*` class a TS/TSX file
 *    applies (`className=`, Lit `class=`, `.className =`, `classList.*()`)
 *    must have a selector in some stylesheet under src or in a Lit `css\``
 *    block / the console's inline `motionStyles`. A template literal such as
 *    `ct-chip--${tone}` is checked as a PREFIX. Intentional exceptions — JS
 *    hook classes, deliberate no-style semantic markers — live in
 *    CLASS_ALLOWLIST below, each with its reason.
 * 4. The literal `#646cff` (Vite's starter-template accent) appears nowhere
 *    under src — it is never a deliberate colour in this design system.
 *
 * BE HONEST ABOUT WHAT THIS IS: a source guard, not a renderer. It proves a
 * name resolves, not that the rule behind it lays out well. Class names
 * computed outside those call sites (a variable assembled elsewhere and
 * passed in) are not seen.
 *
 * Exits non-zero listing every failing check.
 */

import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = path.resolve(__dirname, '../src');
const TOKENS_CSS_PATH = path.join(FRONTEND_SRC, 'styles/tokens.css');

/**
 * Class names applied from TS/TSX that deliberately have no stylesheet rule.
 * Every entry needs a reason; an entry that stops being used, or that gains a
 * rule, is reported as stale so this list cannot silently grow.
 */
const CLASS_ALLOWLIST = new Map([
  [
    'ct-button__label',
    'ct-button.ts keeps a reference to this inline <span> to swap its text in place (confirm/busy labels); it inherits everything from .ct-button__el and needs no rule of its own',
  ],
]);

const SKIP_DIRS = new Set(['node_modules', '__tests__']);

// --------------------------------------------------------------- Helpers

function filesUnder(dir, exts) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...filesUnder(full, exts));
    else if (exts.some((e) => entry.name.endsWith(e)) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

// Comments are blanked rather than removed, keeping newlines, so line
// numbers in the report still point at the real source line.
const blank = (m) => m.replace(/[^\n]/g, ' ');
const stripCssComments = (text) => text.replace(/\/\*[\s\S]*?\*\//g, blank);
/** Block comments plus whole-line `//` comments — prose that MENTIONS a
 *  retired token (faviconFrames.ts documents `--ct-doneness-deep`) is not a
 *  use. Line-trailing `//` is left alone so a URL in a string survives. */
const stripTsComments = (text) => stripCssComments(text).replace(/^\s*\/\/.*$/gm, blank);

/** The bodies of every Lit `css\`…\`` tagged template in a TS file. */
function litCssBlocks(text) {
  const out = [];
  const re = /\bcss`([\s\S]*?)`/g;
  let m;
  while ((m = re.exec(text)) !== null) out.push(m[1]);
  return out;
}

/** The console's inline stylesheet — a plain string constant named
 *  `motionStyles` (orbit-diner/motion.ts). Read by NAME, as focus-audit does. */
function namedStyleStrings(text) {
  const out = [];
  const re = /\b\w*[Ss]tyles\s*=\s*(`[\s\S]*?`|'[^'\n]*'|"[^"\n]*")/g;
  let m;
  while ((m = re.exec(text)) !== null) out.push(m[1].slice(1, -1));
  return out;
}

const PROP_DECL_RE = /(?:^|[;{\s(,])(--(?:ct|od)-[A-Za-z0-9_-]+)\s*:/g;
const TS_PROP_KEY_RE = /['"](--(?:ct|od)-[A-Za-z0-9_-]+)['"]\s*:/g;
const VAR_USE_RE = /var\(\s*(--(?:ct|od)-[A-Za-z0-9_-]+)(\$\{)?/g;
const CLASS_SELECTOR_RE = /\.((?:ct|od|toaster)-[A-Za-z0-9_-]+)/g;
const CLASS_TOKEN_RE = /(?<![\w$.-])((?:ct|od|toaster)-[A-Za-z0-9_-]*)(\$\{)?/g;

function collect(re, text, group = 1) {
  const out = [];
  re.lastIndex = 0;
  let m;
  while ((m = re.exec(text)) !== null) out.push({ name: m[group], prefix: Boolean(m[2]), index: m.index });
  return out;
}

const lineOf = (text, index) => text.slice(0, index).split('\n').length;

/** From `start` (an opening `{`, `(`, quote or backtick), return the text up
 *  to its balanced close. Strings and template literals are respected. */
function balanced(text, start) {
  const open = text[start];
  if (open === '"' || open === "'") {
    const end = text.indexOf(open, start + 1);
    return text.slice(start, end === -1 ? text.length : end + 1);
  }
  const close = open === '{' ? '}' : open === '(' ? ')' : '`';
  let depth = 0;
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i];
    if (open === '`') {
      if (i > start && ch === '`') return text.slice(start, i + 1);
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      const sub = balanced(text, i);
      i += sub.length - 1;
      continue;
    }
    if (ch === open) depth += 1;
    else if (ch === close) {
      depth -= 1;
      if (depth === 0) return text.slice(start, i + 1);
    }
  }
  return text.slice(start);
}

/** Every class-name expression a TS/TSX file applies, with its offset. */
function classExpressions(text) {
  const out = [];
  const re = /(?:\bclassName\s*=\s*|\bclass\s*=\s*|\.className\s*=\s*|\bclassList\.(?:add|remove|toggle|replace|contains)\s*)(?=[{"'`(]|\$\{)/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    let start = m.index + m[0].length;
    if (text[start] === '$') start += 1; // Lit `class=${…}`
    out.push({ expr: balanced(text, start), index: m.index });
  }
  return out;
}

// ------------------------------------------------------------- The checks

/**
 * Run every check over an in-memory tree ({ path: text }) and return the
 * failure strings. Pure, so the self-test can run it on fixtures.
 */
function auditTree(files, tokensText, { checkAllowlist = true } = {}) {
  const failures = [];
  const declaredProps = new Set();
  const definedClasses = new Set();
  const varUses = [];
  const classUses = [];

  for (const [file, raw] of Object.entries(files)) {
    if (file.endsWith('.css')) {
      const text = stripCssComments(raw);
      collect(PROP_DECL_RE, text).forEach((d) => declaredProps.add(d.name));
      collect(CLASS_SELECTOR_RE, text).forEach((d) => definedClasses.add(d.name));
      collect(VAR_USE_RE, text).forEach((u) => varUses.push({ ...u, file, line: lineOf(text, u.index) }));
    } else {
      for (const block of [...litCssBlocks(raw), ...namedStyleStrings(raw)]) {
        const text = stripCssComments(block);
        collect(PROP_DECL_RE, text).forEach((d) => declaredProps.add(d.name));
        collect(CLASS_SELECTOR_RE, text).forEach((d) => definedClasses.add(d.name));
      }
      collect(TS_PROP_KEY_RE, raw).forEach((d) => declaredProps.add(d.name));
      const code = stripTsComments(raw);
      collect(VAR_USE_RE, code).forEach((u) => varUses.push({ ...u, file, line: lineOf(code, u.index) }));
      for (const { expr, index } of classExpressions(raw)) {
        CLASS_TOKEN_RE.lastIndex = 0;
        let t;
        while ((t = CLASS_TOKEN_RE.exec(expr)) !== null) {
          classUses.push({ name: t[1], prefix: Boolean(t[2]), file, line: lineOf(raw, index) });
        }
      }
    }
  }

  // 1. Custom properties.
  for (const u of varUses) {
    const ok = u.prefix ? [...declaredProps].some((d) => d.startsWith(u.name)) : declaredProps.has(u.name);
    if (!ok) {
      failures.push(`undefined custom property: var(${u.name}${u.prefix ? '${…}' : ''}) at ${u.file}:${u.line}`);
    }
  }

  // 2. Theme parity inside tokens.css.
  if (tokensText !== null) {
    const tokens = stripCssComments(tokensText);
    const mediaStart = tokens.search(/@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)/);
    const attrStart = tokens.search(/:root\[data-theme=['"]dark['"]\]\s*\{/);
    if (mediaStart === -1 || attrStart === -1) {
      failures.push('theme parity: tokens.css must keep both the prefers-color-scheme dark block and the [data-theme=\'dark\'] block');
    } else {
      const media = balanced(tokens, tokens.indexOf('{', mediaStart));
      const attr = balanced(tokens, tokens.indexOf('{', attrStart));
      const mediaSet = new Set(collect(PROP_DECL_RE, media).map((d) => d.name));
      const attrSet = new Set(collect(PROP_DECL_RE, attr).map((d) => d.name));
      for (const n of mediaSet) if (!attrSet.has(n)) failures.push(`theme parity: ${n} is dark-overridden by prefers-color-scheme but not by [data-theme='dark']`);
      for (const n of attrSet) if (!mediaSet.has(n)) failures.push(`theme parity: ${n} is dark-overridden by [data-theme='dark'] but not by prefers-color-scheme`);
    }
  }

  // 3. Class names.
  const usedAllowlisted = new Set();
  for (const u of classUses) {
    const resolved = u.prefix
      ? [...definedClasses].some((c) => c.startsWith(u.name))
      : definedClasses.has(u.name);
    if (resolved) continue;
    if (CLASS_ALLOWLIST.has(u.name)) {
      usedAllowlisted.add(u.name);
      continue;
    }
    failures.push(`class with no rule: ${u.prefix ? `${u.name}\${…}` : u.name} at ${u.file}:${u.line}`);
  }
  if (checkAllowlist) {
    for (const name of CLASS_ALLOWLIST.keys()) {
      if (definedClasses.has(name)) failures.push(`stale allowlist entry: ${name} now has a rule — remove it from CLASS_ALLOWLIST`);
      else if (!usedAllowlisted.has(name)) failures.push(`stale allowlist entry: ${name} is no longer applied anywhere — remove it from CLASS_ALLOWLIST`);
    }
  }

  // 4. Vite's stock accent.
  for (const [file, raw] of Object.entries(files)) {
    const re = /#646cff\b/gi;
    let m;
    while ((m = re.exec(raw)) !== null) failures.push(`Vite starter colour #646cff at ${file}:${lineOf(raw, m.index)}`);
  }

  return failures;
}

// ------------------------------------------------------------------ Run it

const failures = [];
const tree = {};
for (const file of filesUnder(FRONTEND_SRC, ['.css', '.ts', '.tsx'])) {
  tree[path.relative(FRONTEND_SRC, file)] = readFileSync(file, 'utf8');
}
const realFailures = auditTree(tree, readFileSync(TOKENS_CSS_PATH, 'utf8'));
failures.push(...realFailures);

// ------------------------------------------------- Self-test (mutation cover)
//
// The REAL checker runs on every fixture on every invocation, so the audit
// fails closed if a later edit narrows its detection.

const BASE_TOKENS = ":root{--ct-a:#000;}@media (prefers-color-scheme: dark){:root:not([data-theme='light']){--ct-a:#fff;}}:root[data-theme='dark']{--ct-a:#fff;}";
const SELF_TEST_CASES = [
  // --- must be flagged ---------------------------------------------------
  { name: 'undeclared token with a fallback', flagged: true, files: { 'a.tsx': "const s = { color: 'var(--ct-bg-subtle, #fff)' };" } },
  { name: 'undeclared token in a stylesheet', flagged: true, files: { 'a.css': '.ct-x { color: var(--ct-color-success); }', 'b.tsx': '<div className="ct-x" />' } },
  { name: 'undeclared --od- token', flagged: true, files: { 'a.css': '.od-x { color: var(--od-nope); }' } },
  { name: 'className with no rule', flagged: true, files: { 'a.tsx': '<div className="ct-row ct-row--between" />', 'a.css': '.ct-row{}' } },
  { name: 'class in a ternary with no rule', flagged: true, files: { 'a.tsx': "<p className={on ? 'ct-a' : 'ct-b'} />", 'a.css': '.ct-a{}' } },
  { name: 'Lit class= with no rule', flagged: true, files: { 'a.ts': 'html`<span class="ct-chip__nope"></span>`' } },
  { name: '.className assignment with no rule', flagged: true, files: { 'a.ts': "el.className = 'ct-ghost';" } },
  { name: 'classList.add with no rule', flagged: true, files: { 'a.ts': "el.classList.add('od-ghost');" } },
  { name: 'template prefix with no rule', flagged: true, files: { 'a.tsx': '<i className={`ct-chip--${tone}`} />', 'a.css': '.ct-card{}' } },
  { name: 'rule only inside a comment', flagged: true, files: { 'a.tsx': '<i className="ct-dead" />', 'a.css': '/* .ct-dead {} */' } },
  { name: 'Vite stock accent', flagged: true, files: { 'a.css': '.ct-x { color: #646CFF; }', 'a.tsx': '<i className="ct-x" />' } },
  { name: 'undeclared token named by a template prefix', flagged: true, files: { 'a.ts': 'el.style.width = `var(--ct-gap-${n})`;' } },
  { name: 'theme parity drift', flagged: true, files: {}, tokens: ":root{--ct-a:#000;--ct-b:#000;}@media (prefers-color-scheme: dark){:root:not([data-theme='light']){--ct-a:#fff;--ct-b:#fff;}}:root[data-theme='dark']{--ct-a:#fff;}" },
  // --- must NOT be flagged -----------------------------------------------
  { name: 'declared token', flagged: false, files: { 'a.css': ':root{--ct-a:1px}.ct-x{margin:var(--ct-a)}', 'a.tsx': '<i className="ct-x" />' } },
  { name: 'declared token named by a template prefix', flagged: false, files: { 'a.css': ':root{--ct-space-1:4px}', 'a.ts': 'el.style.width = `var(--ct-space-${n})`;' } },
  { name: 'retired token mentioned in a comment', flagged: false, files: { 'a.ts': '/* was var(--ct-doneness-deep) */\n// var(--ct-gone)\nconst x = 1;' } },
  { name: 'token set from a TS style object', flagged: false, files: { 'a.tsx': "const s = { '--od-doneness': 2 }; const t = 'var(--od-doneness)';" } },
  { name: 'Lit css block defines the class', flagged: false, files: { 'a.ts': 'static styles = css`.ct-chip__dot{}`; html`<i class="ct-chip__dot"></i>`;' } },
  { name: 'template prefix with a rule', flagged: false, files: { 'a.tsx': '<i className={`ct-chip--${tone}`} />', 'a.css': '.ct-chip--ok{}' } },
  { name: 'non-class ct- strings are ignored', flagged: false, files: { 'a.tsx': '<ct-button data-testid="ct-thing" />' } },
  { name: 'inline motionStyles string defines the class', flagged: false, files: { 'm.ts': 'export const motionStyles = `.od-console *{animation:none}`;', 'a.tsx': '<div className="od-console" />' } },
];
for (const tc of SELF_TEST_CASES) {
  const hits = auditTree(tc.files, tc.tokens ?? BASE_TOKENS, { checkAllowlist: false });
  if (tc.flagged && hits.length === 0) failures.push(`self-test: mutation "${tc.name}" was NOT detected`);
  else if (!tc.flagged && hits.length > 0) failures.push(`self-test: "${tc.name}" must pass but was flagged: ${hits[0]}`);
}

// ------------------------------------------------------------------ Report

if (failures.length > 0) {
  console.error('FAIL: css-refs audit found problems:');
  for (const f of failures) console.error(`  - ${f}`);
  process.exitCode = 1;
} else {
  console.log(
    `css-refs-audit: ${Object.keys(tree).length} file(s) scanned; every var(--ct-*/--od-*) declared, tokens.css dark blocks in parity, every ct-/od-/toaster- class has a rule (${CLASS_ALLOWLIST.size} allowlisted)`,
  );
  console.log('CSS-REFS AUDIT: ALL GREEN');
}
