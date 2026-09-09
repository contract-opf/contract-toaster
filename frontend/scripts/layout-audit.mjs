#!/usr/bin/env node
/**
 * layout-audit.mjs — the ways this design system has actually broken
 * narrow-viewport reflow (issue #457, audit PART I; the first of the three
 * also bit the gallery harness at F2), plus the tab-strip wrap-containment
 * guard #477 added.
 *
 * Plain Node ESM, zero dependencies, deterministic and offline (node 20),
 * matching focus-audit.mjs's conventions — including its self-test of fixture
 * mutations, so this audit fails closed if a later edit narrows what it
 * detects.
 *
 * BE HONEST ABOUT WHAT THIS IS. None of these checks renders anything, so
 * none is evidence that the app reflows: they are source guards on spellings
 * and properties that have each produced (checks 1-2) or would produce
 * (check 3) a measured, reproduced overflow or wrap failure. jsdom implements
 * no layout, so a vitest assertion cannot do better; the real evidence for
 * #457 is the browser measurement recorded in
 * docs/planning/frontend-release-audit-2026-07-27.md (PART I). Check 3 has no
 * such recorded browser measurement behind it (see
 * admin-tab-grouping-599.test.tsx's docstring) — it is the CSS-property guard
 * that AC1/AC2 of #477 (unchanged by #599's flattening — see that test's own
 * docstring) rely on, not proof those criteria render correctly.
 * What these checks buy is that the exact regression cannot land again
 * unnoticed.
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
 * 3. Tab-strip selectors (TAB_STRIP_SELECTORS below) must keep
 *    `flex-wrap: wrap` and must not declare `overflow-x`/`overflow` (other
 *    than `visible`) or `white-space: nowrap`. Issue #477's fix depends on
 *    `.ct-tab-bar__track` being free to move an orphaned tab onto its own row
 *    instead of clipping it or scrolling it off-screen; a later edit that
 *    flips any of these three properties would silently reopen #477 without
 *    any test noticing, since jsdom cannot see the resulting overflow.
 *
 * 4. `ct-columns` (issue #601) must declare, outside any max-width media
 *    query, BOTH `display: grid` (or `inline-grid`) AND
 *    `grid-template-columns` as exactly two `minmax(0, …fr)` tracks on the
 *    same rule (the desktop two-column layout — a track list alone is inert
 *    without `display: grid` establishing the grid formatting context at
 *    all, which silently collapses the primitive to a plain block and
 *    stacks its children full-width), and exactly one track inside
 *    `@media (max-width: 640px)` (the collapse to one column, matching
 *    `ct-app-shell`'s own breakpoint). vitest's `css: false` and jsdom's
 *    absent layout engine mean none of this is otherwise guarded — see
 *    ui-columns.test.tsx's docstring; all of it can be deleted with the
 *    entire component-test suite staying green without this check.
 *
 * 5. `ct-field[narrow] > :is(input, select, textarea)` (issue #601) must set
 *    `align-self: flex-start` (or another non-`stretch` value), so a control
 *    marked `narrow` opts out of `ct-field`'s flex column defaulting every
 *    child to `align-items: stretch` and keeps its own intrinsic width
 *    instead of filling the container. ui-field.test.tsx only asserts that
 *    `narrow` reflects as a boolean attribute (property plumbing); this is
 *    the check that guards the width behaviour the attribute exists for.
 *
 * 6. Every `src/ui/components/ct-*.css` file must be `import`ed by its
 *    sibling `ct-*.ts` (issue #601 follow-up). Every check above reads CSS
 *    text straight off disk, so a stylesheet whose `import './ct-*.css';`
 *    line got deleted from its component module satisfies checks 1-5 (and
 *    every other file-presence check in this file) while its rules never
 *    load into the running app at all — vitest can't catch this either,
 *    since component tests run with `css: false` (frontend/vitest.config.ts).
 *    Verified empirically (2026-08-23): deleting the import line from
 *    ct-columns.ts left both `npm run audit:layout` and
 *    ui-columns.test.tsx fully green.
 *
 * 7. THE TYPE-SCALE FLOOR (issue #600). No font size anywhere under
 *    `frontend/src` may resolve below `MIN_FONT_SIZE_PX` — not in a
 *    stylesheet, not in a Lit `css` tagged template, not in a React inline
 *    `style={{ fontSize }}` prop. The owner measured 12px and 12.48px text on
 *    the live Review screen and could not read it. Two halves:
 *      (a) the `--ct-text-*` scale in `styles/tokens.css` may not DEFINE a
 *          size below the floor (so the retired 12px `--ct-text-xs` cannot
 *          come back), and
 *      (b) every `font-size` / `fontSize` declaration must resolve to a
 *          value at or above it. Only absolute forms are resolvable, so only
 *          absolute forms are allowed: a `--ct-text-*` token, a px/rem
 *          literal, or `inherit`. An `em`/`%` size is rejected outright
 *          rather than guessed at — `0.92em` (base.css's old mono size) is
 *          14.7px inside body copy but 12.9px inside a 14px table cell, and
 *          compounds again on every nesting level.
 *    Same honesty caveat as above: this reads source text, it does not
 *    render. It cannot see a size that only a browser computes (a UA
 *    default, a `font` shorthand, a size injected at runtime), and vitest
 *    cannot do better here either — `css: false` means no stylesheet is even
 *    loaded in the component suite.
 *
 * 8. THE WRAPPABLE-HEADINGS OPT-IN (issue #668). `ct-table.css` pins every
 *    `thead th` to `white-space: nowrap`, which makes a heading's min-content
 *    width the width of the WHOLE heading string — so a two-word heading, not
 *    the data under it, becomes the widest thing in the column and pushes the
 *    table past the viewport. That is measured, not hypothetical: at the
 *    browser's default window size on prod, History's `DOCUMENTS` column was
 *    cut off mid-word. `.ct-table--wrap-headings` is the per-table opt-out,
 *    and it has two halves that fail independently:
 *      (a) the rule must exist and must set a WRAPPING `white-space` (a rule
 *          that is deleted, or edited back to `nowrap`, silently restores the
 *          overflow), and
 *      (b) at least one component under src/ must actually apply the class —
 *          a stylesheet rule with no consumer is inert, and neither vitest
 *          (`css: false`) nor any other check here would notice.
 *    Same honesty caveat as every check above: this reads source text. The
 *    DOM half — that the History table is the component carrying the class —
 *    is asserted in src/__tests__/history-table-width-668.test.tsx.
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
  // Issue #492: ReviewSubmission.tsx's completion-handoff live region
  // (`review-ready-announcement`) is visually hidden via app.css's
  // `.ct-sr-only` rather than plain muted text — its containing block is
  // the component's root section, positioned via app.css's own
  // `[data-testid='review-submission']` rule.
  '.ct-sr-only': "[data-testid='review-submission']",
};

const failures = [];

// --------------------------------------------------------------- Helpers

function filesUnder(dir, matches) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    if (entry.name === 'node_modules') continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...filesUnder(full, matches));
    else if (matches(entry.name)) out.push(full);
  }
  return out;
}

const cssFilesUnder = (dir) => filesUnder(dir, (name) => name.endsWith('.css'));

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

/** `stripComments` plus whole-line `//` comments — for scanning TS/TSX
 * sources, where a class name mentioned in a docstring must not be mistaken
 * for the class being applied. Only leading-`//` lines are removed, so a
 * `https://` inside a string survives. */
const stripJsComments = (source) =>
  stripComments(source)
    .split('\n')
    .filter((line) => !/^\s*\/\//.test(line))
    .join('\n');

/** Split a comma-separated selector list on top-level commas only — a naive
 * `.split(',')` also splits inside a `:is(input, select, textarea)`-style
 * functional pseudo-class, which would break matching against selectors
 * like `ct-field[narrow] > :is(input, select, textarea)` below. */
function splitSelectorList(selectorText) {
  const parts = [];
  let depth = 0;
  let current = '';
  for (const ch of selectorText) {
    if (ch === '(') depth += 1;
    if (ch === ')') depth -= 1;
    if (ch === ',' && depth === 0) {
      parts.push(current.trim());
      current = '';
    } else {
      current += ch;
    }
  }
  if (current.trim()) parts.push(current.trim());
  return parts;
}

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

// --------------------------------- 3. Tab-strip wrap containment (#477)

// Every selector that renders the tab strip's flex track. A stylesheet is
// free to add more as the tab-strip grows; each one is required to satisfy
// all three properties below.
const TAB_STRIP_SELECTORS = ['.ct-tab-bar__track'];
const FLEX_WRAP_RE = /flex-wrap\s*:\s*wrap\b/i;
const NO_WRAP_RE = /flex-wrap\s*:\s*nowrap\b/i;
// Any `overflow`/`overflow-x` value other than `visible` opts into clipping
// or scrolling instead of wrapping.
const OVERFLOW_X_RE = /overflow(-x)?\s*:\s*(?!visible\b)[a-z-]+/i;
const WHITE_SPACE_NOWRAP_RE = /white-space\s*:\s*nowrap\b/i;

/** Tab-strip wrap failures for one stylesheet, RETURNED for the self-test. */
function tabStripWrapFailures(source, label, selectors = TAB_STRIP_SELECTORS) {
  const found = [];
  const text = stripComments(source);
  const rules = [...eachRule(text)];
  for (const target of selectors) {
    const matches = rules.filter((rule) =>
      rule.selector
        .split(',')
        .map((part) => part.trim())
        .includes(target),
    );
    if (matches.length === 0) continue; // not defined in this stylesheet
    const bodies = matches.map((rule) => rule.body).join('\n');
    if (!FLEX_WRAP_RE.test(bodies) || NO_WRAP_RE.test(bodies)) {
      found.push(
        `tab-strip wrap: ${label} — "${target}" must declare \`flex-wrap: wrap\` and never \`nowrap\`; ` +
          'without it an orphaned tab can only overflow or get clipped instead of moving to its own row ' +
          '(issue #477).',
      );
    }
    if (OVERFLOW_X_RE.test(bodies)) {
      found.push(
        `tab-strip wrap: ${label} — "${target}" declares an \`overflow\`/\`overflow-x\` other than ` +
          '`visible`, which turns wrapping into a horizontal scrollbar the #477 fix deliberately avoids.',
      );
    }
    if (WHITE_SPACE_NOWRAP_RE.test(bodies)) {
      found.push(
        `tab-strip wrap: ${label} — "${target}" declares \`white-space: nowrap\`, which keeps every tab ` +
          'on one un-wrappable line (issue #477).',
      );
    }
  }
  return found;
}

for (const cssPath of cssFilesUnder(FRONTEND_SRC)) {
  failures.push(...tabStripWrapFailures(readFileSync(cssPath, 'utf8'), path.relative(FRONTEND_SRC, cssPath)));
}

// Every declared tab-strip selector must actually exist somewhere, or the
// list rots into a target nothing checks (same reasoning as the
// HIDDEN_HELPER_OWNERS existence check above).
{
  const allCss = cssFilesUnder(FRONTEND_SRC)
    .map((p) => stripComments(readFileSync(p, 'utf8')))
    .join('\n');
  for (const selector of TAB_STRIP_SELECTORS) {
    if (!allCss.includes(selector)) {
      failures.push(
        `tab-strip wrap: TAB_STRIP_SELECTORS lists "${selector}", which no stylesheet under src/ defines — ` +
          'drop the entry or fix the selector (issue #477).',
      );
    }
  }
}

// --------------------------------- 4. ct-columns two/one-track breakpoint (#601)

const COLUMNS_SELECTOR = 'ct-columns';
// The exact prelude of the media query the breakpoint must live under —
// matching ct-app-shell's own collapse point (issue #390).
const MEDIA_MAX_640_RE = /^@media\s*\(\s*max-width:\s*640px\s*\)$/i;
const MINMAX_ZERO_FR_RE = /^minmax\(\s*0,\s*\d*\.?\d+fr\s*\)$/i;
const DISPLAY_GRID_RE = /display\s*:\s*(?:inline-)?grid\b/i;

/** Split a track-list value into its individual tracks on top-level
 * whitespace only — a naive `.split(/\s+/)` would also split inside
 * `minmax(0, 1fr)`'s internal comma-space, which this avoids by tracking
 * paren depth instead. */
function splitTrackList(value) {
  const tracks = [];
  let depth = 0;
  let current = '';
  for (const ch of value.trim()) {
    if (ch === '(') depth += 1;
    if (ch === ')') depth -= 1;
    if (/\s/.test(ch) && depth === 0) {
      if (current) tracks.push(current);
      current = '';
    } else {
      current += ch;
    }
  }
  if (current) tracks.push(current);
  return tracks;
}

/** ct-columns breakpoint failures given CSS source text, RETURNED (not
 * pushed) so the self-test below can run the real checker over fixtures.
 * This scans the WHOLE source passed in (the caller joins every stylesheet
 * before calling, in the real invocation below) rather than being run
 * per-file: "no rule targets ct-columns" must fire once for the codebase,
 * not once per stylesheet that happens not to define it. */
function columnsBreakpointFailures(source, selector = COLUMNS_SELECTOR) {
  const found = [];
  const text = stripComments(source);
  const rules = [...eachRule(text)];
  const targets = (sel) => splitSelectorList(sel).includes(selector);

  const desktopRules = rules.filter((rule) => targets(rule.selector) && !MEDIA_MAX_640_RE.test(rule.context));
  const mediaRules = rules.filter((rule) => targets(rule.selector) && MEDIA_MAX_640_RE.test(rule.context));

  if (desktopRules.length === 0 && mediaRules.length === 0) {
    found.push(
      `columns breakpoint: no rule targets "${selector}" anywhere under src/; neither the two-column ` +
        'desktop layout nor its single-column collapse below 640px is declared (issue #601).',
    );
    return found;
  }

  const trackListsFor = (ruleSet) =>
    ruleSet.flatMap((rule) => {
      const match = rule.body.match(/grid-template-columns\s*:\s*([^;}]+)/i);
      return match ? [splitTrackList(match[1])] : [];
    });

  const desktopTrackLists = trackListsFor(desktopRules);
  const desktopOk = desktopRules.some((rule) => {
    const match = rule.body.match(/grid-template-columns\s*:\s*([^;}]+)/i);
    if (!match) return false;
    const tracks = splitTrackList(match[1]);
    return (
      tracks.length === 2 &&
      tracks.every((track) => MINMAX_ZERO_FR_RE.test(track)) &&
      DISPLAY_GRID_RE.test(rule.body)
    );
  });
  if (!desktopOk) {
    found.push(
      `columns breakpoint: "${selector}" outside any max-width media query must declare ` +
        '`display: grid` (or `inline-grid`) together with `grid-template-columns` as exactly two ' +
        '`minmax(0, …fr)` tracks on the same rule, so it actually establishes a grid and renders two ' +
        `columns at desktop width; found track lists: ${desktopTrackLists.map((t) => (t.length ? t.join(' ') : '(empty)')).join(' | ') || '(no grid-template-columns declared)'}; ` +
        `\`display: grid\`/\`inline-grid\` present: ${desktopRules.some((rule) => DISPLAY_GRID_RE.test(rule.body))} (issue #601).`,
    );
  }

  const mediaTrackLists = trackListsFor(mediaRules);
  const mediaOk = mediaTrackLists.some((tracks) => tracks.length === 1);
  if (!mediaOk) {
    found.push(
      `columns breakpoint: "${selector}" inside \`@media (max-width: 640px)\` must declare ` +
        '`grid-template-columns` with exactly ONE track, so the layout collapses to a single column below ' +
        `the breakpoint; found: ${mediaTrackLists.map((t) => (t.length ? t.join(' ') : '(empty)')).join(' | ') || '(no matching @media rule)'} (issue #601).`,
    );
  }

  return found;
}

{
  const allCss = cssFilesUnder(FRONTEND_SRC)
    .map((p) => readFileSync(p, 'utf8'))
    .join('\n');
  failures.push(...columnsBreakpointFailures(allCss));
}

// --------------------------------- 5. ct-field[narrow] control opt-out (#601)

const NARROW_CONTROL_SELECTOR = 'ct-field[narrow] > :is(input, select, textarea)';
const ALIGN_SELF_START_RE = /align-self\s*:\s*(flex-start|start|self-start)\b/i;

/** Narrow-control failures given a flat list of `{ selector, body }` rules
 * gathered across every stylesheet (RETURNED, not pushed, so the self-test
 * below can run the real checker over fixture rule lists). This check is
 * global rather than per-file: the rule is declared once, and the mutation
 * it guards against — deleting it outright — leaves zero matches in EVERY
 * file, which a per-file "not defined in this stylesheet" skip (the tab-strip
 * check's pattern) would silently let through. */
function narrowFieldFailures(rules, selector = NARROW_CONTROL_SELECTOR) {
  const found = [];
  const matches = rules.filter((rule) => splitSelectorList(rule.selector).includes(selector));
  if (matches.length === 0) {
    found.push(
      `narrow field: no stylesheet declares "${selector}" — a control marked \`narrow\` never opts out of ` +
        'the flex column\'s default `align-items: stretch`, so it still fills the full container width ' +
        `(issue #601).`,
    );
    return found;
  }
  const bodies = matches.map((rule) => rule.body).join('\n');
  if (!ALIGN_SELF_START_RE.test(bodies)) {
    found.push(
      `narrow field: "${selector}" is declared but sets no non-stretch \`align-self\` (expected ` +
        '`flex-start` or equivalent), so a control marked `narrow` still fills the full container width ' +
        `instead of keeping its own intrinsic width (issue #601).`,
    );
  }
  return found;
}

{
  const allRules = [];
  for (const cssPath of cssFilesUnder(FRONTEND_SRC)) {
    const text = stripComments(readFileSync(cssPath, 'utf8'));
    for (const rule of eachRule(text)) allRules.push(rule);
  }
  failures.push(...narrowFieldFailures(allRules));
}

// --------------------------------- 6. Every ct-*.css is imported by its sibling ct-*.ts

/**
 * Checks a list of `{ cssName, tsSource }` pairs — one per `ct-*.css` file
 * under src/ui/components, `tsSource` being the sibling `ct-*.ts` file's
 * contents (or `null` if that file doesn't exist). Takes the pairs as data
 * rather than reading the filesystem itself, so the self-test below can run
 * the real checker over fixtures.
 */
function cssImportFailures(pairs) {
  const found = [];
  for (const { cssName, tsSource } of pairs) {
    if (tsSource === null) {
      found.push(
        `orphan stylesheet: ui/components/${cssName} has no sibling .ts file of the same name — a component ` +
          'stylesheet with nothing to import it can never load into the app (issue #601 follow-up).',
      );
      continue;
    }
    const importRe = new RegExp(`import\\s+['"]\\./${cssName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}['"]`);
    if (!importRe.test(tsSource)) {
      found.push(
        `unimported stylesheet: ui/components/${cssName}'s sibling .ts file does not \`import './${cssName}'\` — ` +
          'its rules never load into the app even though the stylesheet still satisfies every other check in ' +
          `this audit and the component's own tests (which run with \`css: false\`) (issue #601 follow-up).`,
      );
    }
  }
  return found;
}

{
  const componentsDir = path.join(FRONTEND_SRC, 'ui', 'components');
  const pairs = readdirSync(componentsDir)
    .filter((name) => /^ct-.*\.css$/.test(name))
    .sort()
    .map((cssName) => {
      const tsPath = path.join(componentsDir, cssName.replace(/\.css$/, '.ts'));
      let tsSource = null;
      try {
        tsSource = readFileSync(tsPath, 'utf8');
      } catch {
        tsSource = null;
      }
      return { cssName, tsSource };
    });
  failures.push(...cssImportFailures(pairs));
}

// --------------------------------- 7. Type-scale floor (#600)

/** The smallest font size any UI text may render at, in CSS px. This is a
 * CONSTANT on purpose: deriving it from the smallest token in tokens.css
 * would let anyone lower the floor simply by adding a smaller token, which
 * is exactly the regression this guards. Changing this number is a design-
 * system decision — update docs/frontend-design-system.md §4.5 with it. */
const MIN_FONT_SIZE_PX = 14;

const TOKENS_CSS = path.join(FRONTEND_SRC, 'styles', 'tokens.css');
const TYPE_TOKEN_DEF_RE = /(--ct-text-[a-z0-9-]+)\s*:\s*([\d.]+)px/gi;

/** The `--ct-text-*` scale, as `{ token: px }`, parsed out of tokens.css
 * text. Takes source rather than reading the file so the self-test can run
 * the real parser over fixtures. */
function parseTypeScale(source) {
  const scale = {};
  for (const m of stripComments(source).matchAll(TYPE_TOKEN_DEF_RE)) scale[m[1]] = Number(m[2]);
  return scale;
}

/** Half (a): the scale itself may not define a sub-floor size. */
function typeScaleFailures(scale) {
  const found = [];
  for (const [token, px] of Object.entries(scale)) {
    if (px < MIN_FONT_SIZE_PX) {
      found.push(
        `type scale: styles/tokens.css defines ${token}: ${px}px, below the ${MIN_FONT_SIZE_PX}px floor — the ` +
          '12px --ct-text-xs was retired for exactly this reason (issue #600). Do not re-add a sub-floor size to ' +
          'the scale; a token that exists will get used.',
      );
    }
  }
  return found;
}

/**
 * Resolve one font-size value to CSS px. Returns `{ px }` when resolvable
 * (`px: null` means "safe but context-dependent", i.e. `inherit`), or
 * `{ reason }` when the value cannot be proven to sit above the floor.
 */
function resolveFontSize(raw, scale) {
  const value = raw
    .replace(/!important/i, '')
    .replace(/;\s*$/, '')
    .trim();
  if (/^inherit$/i.test(value)) return { px: null };

  const varMatch = /^var\(\s*(--[a-z0-9-]+)\s*(?:,([\s\S]*))?\)$/i.exec(value);
  if (varMatch) {
    const [, token, fallback] = varMatch;
    if (!(token in scale)) {
      return {
        reason: `var(${token}) is not a size in the --ct-text-* scale (styles/tokens.css) — it may not even be ` +
          'defined, in which case the declaration is dropped and the element silently inherits',
      };
    }
    // A fallback is what renders whenever the token is missing (the toaster
    // hero is styled to survive standalone), so it must clear the floor too.
    if (fallback !== undefined && fallback.trim() !== '') {
      const inner = resolveFontSize(fallback, scale);
      if (inner.reason) return inner;
      if (inner.px !== null && inner.px < MIN_FONT_SIZE_PX) return { px: inner.px };
    }
    return { px: scale[token] };
  }

  const px = /^([\d.]+)px$/i.exec(value);
  if (px) return { px: Number(px[1]) };
  const rem = /^([\d.]+)rem$/i.exec(value);
  if (rem) return { px: Number(rem[1]) * 16 };

  return {
    reason: `"${value}" is not an absolute size — use a --ct-text-* token. Relative units (em/%/smaller) compound ` +
      'with their context, so no source guard can prove they clear the floor',
  };
}

const CSS_FONT_SIZE_RE = /font-size\s*:\s*([^;}\n]+)/gi;
const JSX_FONT_SIZE_RE = /fontSize\s*:\s*(['"])([^'"]+)\1/g;

/** Half (b): every font size declared in one source file. RETURNED, not
 * pushed, so the self-test below can run the real checker over fixtures. */
function fontSizeFailures(source, label, scale) {
  const found = [];
  const text = stripComments(source);
  const declarations = [];
  for (const m of text.matchAll(CSS_FONT_SIZE_RE)) declarations.push(m[1]);
  for (const m of text.matchAll(JSX_FONT_SIZE_RE)) declarations.push(m[2]);

  for (const raw of declarations) {
    const { px, reason } = resolveFontSize(raw, scale);
    if (reason) {
      found.push(`font-size floor: ${label} declares \`font-size: ${raw.trim()}\` — ${reason} (issue #600).`);
    } else if (px !== null && px < MIN_FONT_SIZE_PX) {
      found.push(
        `font-size floor: ${label} declares \`font-size: ${raw.trim()}\`, which resolves to ${px}px — below the ` +
          `${MIN_FONT_SIZE_PX}px floor the owner set after measuring unreadable 12px text on the live Review ` +
          'screen (issue #600).',
      );
    }
  }
  return found;
}

const TYPE_SCALE = parseTypeScale(readFileSync(TOKENS_CSS, 'utf8'));

{
  failures.push(...typeScaleFailures(TYPE_SCALE));
  // Stylesheets AND component modules: ct-chip.ts holds its rules in a Lit
  // `css` tagged template and orbit-diner/motion.ts in an inline <style>
  // string (it was Toaster.tsx until issue #727 deleted that file), so a
  // CSS-files-only sweep misses both — and the 12.48px chip was one of the
  // three sizes measured on the live screen.
  const sources = filesUnder(FRONTEND_SRC, (name) => /\.(css|ts|tsx)$/.test(name));
  for (const file of sources) {
    failures.push(...fontSizeFailures(readFileSync(file, 'utf8'), path.relative(FRONTEND_SRC, file), TYPE_SCALE));
  }
}

// ------------------- 8. The wrappable-headings opt-in is live (issue #668)

const WRAP_HEADINGS_CLASS = 'ct-table--wrap-headings';
const WRAP_HEADINGS_SELECTOR = `ct-table table.${WRAP_HEADINGS_CLASS} thead th`;
// Anything that lets a line break happen. `nowrap` and `pre` are the two
// values that do not, and they are exactly the regression this guards.
const WRAPPING_WHITE_SPACE_RE = /white-space\s*:\s*(normal|pre-wrap|pre-line|break-spaces)\b/i;

/**
 * Failures for the STYLESHEET half, given a flat list of `{ selector, body }`
 * rules gathered across every stylesheet. Global rather than per-file for the
 * same reason as the narrow-field check above: the mutation being guarded
 * against is deleting the rule outright, which leaves zero matches in every
 * file, and a per-file "not declared here" skip would wave that straight
 * through. Declarations may be split across several rules with the same
 * selector (the tab-strip check's convention), so the bodies are joined.
 */
function wrapHeadingsFailures(rules, selector = WRAP_HEADINGS_SELECTOR) {
  const found = [];
  const matches = rules.filter((rule) => splitSelectorList(rule.selector).includes(selector));
  if (matches.length === 0) {
    found.push(
      `wrappable headings: no stylesheet declares "${selector}" — every column heading is back to ` +
        "ct-table.css's `white-space: nowrap`, so the heading string's full width becomes the column's " +
        'minimum and a wide table scrolls horizontally again (issue #668).',
    );
    return found;
  }
  const bodies = matches.map((rule) => rule.body).join('\n');
  if (!WRAPPING_WHITE_SPACE_RE.test(bodies)) {
    found.push(
      `wrappable headings: "${selector}" is declared but sets no wrapping \`white-space\` (expected ` +
        '`normal` or equivalent), so the opt-in class is inert and the base `nowrap` still pins every ' +
        'heading to one line (issue #668).',
    );
  }
  return found;
}

/**
 * Failures for the CONSUMER half, given `{ label, source }` pairs for every
 * component module under src/. A rule nothing applies is dead CSS: the class
 * would satisfy the stylesheet check above forever while no table on the
 * screen ever wraps a heading.
 *
 * A bare substring search is NOT enough, and this was caught by running the
 * mutation rather than by reasoning about it: deleting `className=` from the
 * History table left this check green, because the JSX comment ABOVE that
 * table names the class in prose. So comments are stripped first, and the
 * class then has to appear as the value of a `className`/`class` attribute
 * — an actual application, not a mention.
 */
function wrapHeadingsConsumerFailures(sources, className = WRAP_HEADINGS_CLASS) {
  const applied = new RegExp(
    `class(?:Name)?\\s*=\\s*['"\`{]+[^'"\`]*\\b${className.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`,
  );
  const used = sources.some(({ source }) => applied.test(stripJsComments(source)));
  return used
    ? []
    : [
        `wrappable headings: no component under src/ applies the "${className}" class as a className, so the ` +
          'rule that lets headings wrap is never on any table and cannot affect the rendered app — a mention ' +
          'in a comment does not count (issue #668).',
      ];
}

{
  const allRules = [];
  for (const cssPath of cssFilesUnder(FRONTEND_SRC)) {
    const text = stripComments(readFileSync(cssPath, 'utf8'));
    for (const rule of eachRule(text)) allRules.push(rule);
  }
  failures.push(...wrapHeadingsFailures(allRules));

  // Component modules only. A mention inside a stylesheet is the rule
  // itself, not a consumer of it — and a mention inside `__tests__/` is an
  // ASSERTION about a consumer, which would let this check pass on the
  // strength of the very test it is here to back up.
  const modules = filesUnder(FRONTEND_SRC, (name) => /\.(ts|tsx)$/.test(name))
    .filter((file) => !file.split(path.sep).includes('__tests__'))
    .map((file) => ({
      label: path.relative(FRONTEND_SRC, file),
      source: readFileSync(file, 'utf8'),
    }));
  failures.push(...wrapHeadingsConsumerFailures(modules));
}

// ------------------------------------------- 9. Self-test (mutation cover)
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

const TAB_STRIP_CASES = [
  { name: 'no flex-wrap declared', flagged: true, css: '.ct-tab-bar__track { display: flex; }' },
  { name: 'flex-wrap: nowrap', flagged: true, css: '.ct-tab-bar__track { display: flex; flex-wrap: nowrap; }' },
  {
    name: 'overflow-x: auto',
    flagged: true,
    css: '.ct-tab-bar__track { display: flex; flex-wrap: wrap; overflow-x: auto; }',
  },
  {
    name: 'overflow: scroll shorthand',
    flagged: true,
    css: '.ct-tab-bar__track { display: flex; flex-wrap: wrap; overflow: scroll; }',
  },
  {
    name: 'white-space: nowrap',
    flagged: true,
    css: '.ct-tab-bar__track { display: flex; flex-wrap: wrap; white-space: nowrap; }',
  },
  // --- must NOT be flagged -------------------------------------------
  {
    name: 'flex-wrap: wrap, no overflow/white-space overrides',
    flagged: false,
    css: '.ct-tab-bar__track { display: flex; flex-wrap: wrap; }',
  },
  {
    name: 'overflow: visible is fine',
    flagged: false,
    css: '.ct-tab-bar__track { display: flex; flex-wrap: wrap; overflow: visible; }',
  },
  {
    name: 'declarations split across rules with the same selector',
    flagged: false,
    css: '.ct-tab-bar__track { display: flex; } .ct-tab-bar__track { flex-wrap: wrap; }',
  },
  { name: 'a different selector is out of scope', flagged: false, css: '.something-else { overflow-x: auto; }' },
];

for (const testCase of TAB_STRIP_CASES) {
  const hits = tabStripWrapFailures(testCase.css, 'self-test', ['.ct-tab-bar__track']);
  if (testCase.flagged && hits.length === 0) {
    failures.push(`self-test: mutation "${testCase.name}" was NOT detected by the tab-strip wrap check (issue #477)`);
  } else if (!testCase.flagged && hits.length > 0) {
    failures.push(`self-test: "${testCase.name}" must be exempt from the tab-strip wrap check but was flagged: ${hits[0]}`);
  }
}

const COLUMNS_CASES = [
  {
    name: 'two-track desktop + one-track media (correct)',
    flagged: false,
    css:
      'ct-columns { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } ' +
      '@media (max-width: 640px) { ct-columns { grid-template-columns: minmax(0, 1fr); } }',
  },
  {
    // The exact mutation the ticket's reviewer reproduced: replace the whole
    // stylesheet with a comment (no display:grid, no grid-template-columns,
    // no media query at all).
    name: 'no rule targets ct-columns at all',
    flagged: true,
    css: '/* nothing here */',
  },
  {
    name: 'desktop two-column rule present, but no @media collapse at all',
    flagged: true,
    css: 'ct-columns { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }',
  },
  {
    name: 'media collapse present, but no desktop two-column rule',
    flagged: true,
    css: '@media (max-width: 640px) { ct-columns { grid-template-columns: minmax(0, 1fr); } }',
  },
  {
    name: 'desktop declares only one track (never renders two columns)',
    flagged: true,
    css:
      'ct-columns { display: grid; grid-template-columns: minmax(0, 1fr); } ' +
      '@media (max-width: 640px) { ct-columns { grid-template-columns: minmax(0, 1fr); } }',
  },
  {
    name: 'media collapse still declares two tracks (never collapses)',
    flagged: true,
    css:
      'ct-columns { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } ' +
      '@media (max-width: 640px) { ct-columns { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } }',
  },
  {
    name: 'desktop uses bare fr tracks instead of minmax(0, …fr)',
    flagged: true,
    css:
      'ct-columns { display: grid; grid-template-columns: 1fr 1fr; } ' +
      '@media (max-width: 640px) { ct-columns { grid-template-columns: minmax(0, 1fr); } }',
  },
  {
    name: 'collapse declared at the wrong breakpoint (900px, not 640px)',
    flagged: true,
    css:
      'ct-columns { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } ' +
      '@media (max-width: 900px) { ct-columns { grid-template-columns: minmax(0, 1fr); } }',
  },
  {
    // Both track lists are exactly correct, but the desktop rule never
    // establishes a grid at all: `grid-template-columns`/`grid-auto-columns`
    // are inert without `display: grid` (or `inline-grid`), so ct-columns
    // renders as a plain block and its children stack to full width — the
    // single-full-width-column regression issue #601 exists to remove, and
    // exactly what desktopOk missed before this check gained the
    // DISPLAY_GRID_RE assertion.
    name: 'correct track lists present, but display: grid missing on the desktop rule',
    flagged: true,
    css:
      'ct-columns { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } ' +
      '@media (max-width: 640px) { ct-columns { grid-template-columns: minmax(0, 1fr); } }',
  },
];

for (const testCase of COLUMNS_CASES) {
  const hits = columnsBreakpointFailures(testCase.css);
  if (testCase.flagged && hits.length === 0) {
    failures.push(`self-test: mutation "${testCase.name}" was NOT detected by the ct-columns breakpoint check (issue #601)`);
  } else if (!testCase.flagged && hits.length > 0) {
    failures.push(`self-test: "${testCase.name}" must be exempt from the ct-columns breakpoint check but was flagged: ${hits[0]}`);
  }
}

const NARROW_FIELD_CASES = [
  {
    name: 'align-self: flex-start present (correct)',
    flagged: false,
    rules: [{ selector: NARROW_CONTROL_SELECTOR, body: 'align-self: flex-start;' }],
  },
  {
    // The exact mutation the ticket's reviewer reproduced: delete the rule
    // outright.
    name: 'rule deleted outright',
    flagged: true,
    rules: [{ selector: 'ct-field', body: 'display: flex;' }],
  },
  {
    name: 'rule present but declares no align-self at all',
    flagged: true,
    rules: [{ selector: NARROW_CONTROL_SELECTOR, body: 'width: auto;' }],
  },
  {
    name: 'align-self explicitly reset to stretch',
    flagged: true,
    rules: [{ selector: NARROW_CONTROL_SELECTOR, body: 'align-self: stretch;' }],
  },
  {
    name: 'selector present in a multi-selector rule',
    flagged: false,
    rules: [{ selector: `.something-else, ${NARROW_CONTROL_SELECTOR}`, body: 'align-self: flex-start;' }],
  },
  {
    name: 'align-self: start is an accepted equivalent',
    flagged: false,
    rules: [{ selector: NARROW_CONTROL_SELECTOR, body: 'align-self: start;' }],
  },
];

for (const testCase of NARROW_FIELD_CASES) {
  const hits = narrowFieldFailures(testCase.rules);
  if (testCase.flagged && hits.length === 0) {
    failures.push(`self-test: mutation "${testCase.name}" was NOT detected by the narrow-field check (issue #601)`);
  } else if (!testCase.flagged && hits.length > 0) {
    failures.push(`self-test: "${testCase.name}" must be exempt from the narrow-field check but was flagged: ${hits[0]}`);
  }
}

const CSS_IMPORT_CASES = [
  {
    name: 'css imported by sibling ts, single quotes (correct)',
    flagged: false,
    pairs: [{ cssName: 'ct-example.css', tsSource: "import './ct-example.css';\nexport class Example {}" }],
  },
  {
    name: 'css imported by sibling ts, double quotes (correct)',
    flagged: false,
    pairs: [{ cssName: 'ct-example.css', tsSource: 'import "./ct-example.css";\nexport class Example {}' }],
  },
  {
    // The exact mutation the ticket's reviewer reproduced: delete the import
    // line from ct-columns.ts and watch every other check stay green.
    name: 'import line deleted from sibling ts (issue #601 follow-up empirical repro)',
    flagged: true,
    pairs: [{ cssName: 'ct-example.css', tsSource: 'export class Example {}' }],
  },
  {
    name: 'no sibling ts file at all',
    flagged: true,
    pairs: [{ cssName: 'ct-example.css', tsSource: null }],
  },
  {
    name: 'sibling ts imports a different stylesheet only',
    flagged: true,
    pairs: [{ cssName: 'ct-example.css', tsSource: "import './ct-other.css';\nexport class Example {}" }],
  },
];

for (const testCase of CSS_IMPORT_CASES) {
  const hits = cssImportFailures(testCase.pairs);
  if (testCase.flagged && hits.length === 0) {
    failures.push(`self-test: mutation "${testCase.name}" was NOT detected by the css-import check (issue #601 follow-up)`);
  } else if (!testCase.flagged && hits.length > 0) {
    failures.push(`self-test: "${testCase.name}" must be exempt from the css-import check but was flagged: ${hits[0]}`);
  }
}

const SCALE_FIXTURE = { '--ct-text-sm': 14, '--ct-text-md': 16 };
const FONT_SIZE_CASES = [
  { name: '12px literal', flagged: true, source: '.a { font-size: 12px; }' },
  { name: 'sub-floor rem literal', flagged: true, source: ':host { font-size: 0.78rem; }' },
  { name: 'sub-floor with !important', flagged: true, source: '.b { font-size: 10px !important; }' },
  { name: 'em size (unresolvable)', flagged: true, source: 'code { font-size: 0.92em; }' },
  { name: 'percentage size (unresolvable)', flagged: true, source: '.c { font-size: 90%; }' },
  { name: 'retired --ct-text-xs token', flagged: true, source: '.d { font-size: var(--ct-text-xs); }' },
  { name: 'token with sub-floor fallback', flagged: true, source: '.e { font-size: var(--ct-text-sm, 11px); }' },
  { name: 'sub-floor inside @media', flagged: true, source: '@media (max-width: 640px) { .f { font-size: 11px; } }' },
  { name: 'React inline fontSize below floor', flagged: true, source: "<p style={{ fontSize: '0.85rem' }} />" },
  // --- must NOT be flagged -------------------------------------------
  { name: 'the floor token itself', flagged: false, source: '.g { font-size: var(--ct-text-sm); }' },
  { name: 'token with matching fallback', flagged: false, source: '.h { font-size: var(--ct-text-sm, 14px); }' },
  { name: 'a larger token', flagged: false, source: '.i { font-size: var(--ct-text-md); }' },
  { name: 'exactly the floor, in px', flagged: false, source: '.j { font-size: 14px; }' },
  { name: 'exactly the floor, in rem', flagged: false, source: '.k { font-size: 0.875rem; }' },
  { name: 'inherit', flagged: false, source: '.l { font-size: inherit; }' },
  { name: 'React inline fontSize on a token', flagged: false, source: "<p style={{ fontSize: 'var(--ct-text-md)' }} />" },
  { name: 'sub-floor size inside a comment', flagged: false, source: '/* was font-size: 12px */ .m { font-size: 16px; }' },
];
for (const testCase of FONT_SIZE_CASES) {
  const hits = fontSizeFailures(testCase.source, 'self-test', SCALE_FIXTURE);
  if (testCase.flagged && hits.length === 0) {
    failures.push(`self-test: mutation "${testCase.name}" was NOT detected by the font-size floor check (issue #600)`);
  } else if (!testCase.flagged && hits.length > 0) {
    failures.push(`self-test: "${testCase.name}" must be exempt from the font-size floor check but was flagged: ${hits[0]}`);
  }
}

const TYPE_SCALE_CASES = [
  { name: 're-added 12px --ct-text-xs', flagged: true, tokens: ':root { --ct-text-xs: 12px; --ct-text-sm: 14px; }' },
  { name: 'the floor token lowered', flagged: true, tokens: ':root { --ct-text-sm: 11px; --ct-text-md: 16px; }' },
  // --- must NOT be flagged -------------------------------------------
  { name: 'the shipped scale', flagged: false, tokens: ':root { --ct-text-sm: 14px; --ct-text-md: 16px; }' },
  { name: 'sub-floor token only inside a comment', flagged: false, tokens: ':root { /* --ct-text-xs: 12px; */ --ct-text-sm: 14px; }' },
];
for (const testCase of TYPE_SCALE_CASES) {
  const hits = typeScaleFailures(parseTypeScale(testCase.tokens));
  if (testCase.flagged && hits.length === 0) {
    failures.push(`self-test: mutation "${testCase.name}" was NOT detected by the type-scale check (issue #600)`);
  } else if (!testCase.flagged && hits.length > 0) {
    failures.push(`self-test: "${testCase.name}" must be exempt from the type-scale check but was flagged: ${hits[0]}`);
  }
}

const WRAP_HEADINGS_SELF_SELECTOR = 'ct-table table.ct-table--wrap-headings thead th';
const WRAP_HEADINGS_CASES = [
  {
    name: 'the opt-in rule deleted outright',
    flagged: true,
    rules: [{ selector: 'ct-table thead th', body: 'white-space: nowrap;' }],
  },
  {
    name: 'opt-in present but edited back to nowrap',
    flagged: true,
    rules: [{ selector: WRAP_HEADINGS_SELF_SELECTOR, body: 'white-space: nowrap;' }],
  },
  {
    name: 'opt-in present but declares no white-space at all',
    flagged: true,
    rules: [{ selector: WRAP_HEADINGS_SELF_SELECTOR, body: 'vertical-align: bottom;' }],
  },
  // --- must NOT be flagged -------------------------------------------
  {
    name: 'the shipped rule',
    flagged: false,
    rules: [{ selector: WRAP_HEADINGS_SELF_SELECTOR, body: 'white-space: normal; vertical-align: bottom;' }],
  },
  {
    name: 'declared inside a multi-selector rule',
    flagged: false,
    rules: [
      { selector: `.something-else, ${WRAP_HEADINGS_SELF_SELECTOR}`, body: 'white-space: normal;' },
    ],
  },
  {
    name: 'declarations split across two rules with the same selector',
    flagged: false,
    rules: [
      { selector: WRAP_HEADINGS_SELF_SELECTOR, body: 'vertical-align: bottom;' },
      { selector: WRAP_HEADINGS_SELF_SELECTOR, body: 'white-space: normal;' },
    ],
  },
  {
    name: 'pre-wrap is an accepted wrapping value',
    flagged: false,
    rules: [{ selector: WRAP_HEADINGS_SELF_SELECTOR, body: 'white-space: pre-wrap;' }],
  },
];
for (const testCase of WRAP_HEADINGS_CASES) {
  const hits = wrapHeadingsFailures(testCase.rules);
  if (testCase.flagged && hits.length === 0) {
    failures.push(`self-test: mutation "${testCase.name}" was NOT detected by the wrappable-headings check (issue #668)`);
  } else if (!testCase.flagged && hits.length > 0) {
    failures.push(`self-test: "${testCase.name}" must be exempt from the wrappable-headings check but was flagged: ${hits[0]}`);
  }
}

const WRAP_HEADINGS_CONSUMER_CASES = [
  {
    name: 'the class applied by no component at all',
    flagged: true,
    sources: [{ label: 'ReviewHistory.tsx', source: '<table data-testid="history-table">' }],
  },
  {
    name: 'a near-miss class name',
    flagged: true,
    sources: [{ label: 'ReviewHistory.tsx', source: '<table className="ct-table--wrap" />' }],
  },
  {
    // The mutation that exposed the bare-substring version of this check: the
    // className is gone, but the JSX comment explaining it is still there.
    name: 'named only in a JSX block comment above the table',
    flagged: true,
    sources: [
      {
        label: 'ReviewHistory.tsx',
        source: '{/* ct-table--wrap-headings lets headings wrap */}\n<table data-testid="history-table">',
      },
    ],
  },
  {
    name: 'named only in a line comment',
    flagged: true,
    sources: [{ label: 'ReviewHistory.tsx', source: '// ct-table--wrap-headings is the opt-in\n<table />' }],
  },
  // --- must NOT be flagged -------------------------------------------
  {
    name: 'a component applies the class',
    flagged: false,
    sources: [{ label: 'ReviewHistory.tsx', source: '<table className="ct-table--wrap-headings" />' }],
  },
  {
    name: 'applied alongside another class',
    flagged: false,
    sources: [{ label: 'X.tsx', source: '<table className="ct-table--wrap-headings ct-other" />' }],
  },
  {
    name: 'applied through a template literal',
    flagged: false,
    sources: [{ label: 'X.tsx', source: '<table className={`ct-table--wrap-headings ${extra}`} />' }],
  },
];
for (const testCase of WRAP_HEADINGS_CONSUMER_CASES) {
  const hits = wrapHeadingsConsumerFailures(testCase.sources);
  if (testCase.flagged && hits.length === 0) {
    failures.push(`self-test: mutation "${testCase.name}" was NOT detected by the wrappable-headings consumer check (issue #668)`);
  } else if (!testCase.flagged && hits.length > 0) {
    failures.push(`self-test: "${testCase.name}" must be exempt from the wrappable-headings consumer check but was flagged: ${hits[0]}`);
  }
}

// ------------------------------------------------------------------ Report

if (failures.length > 0) {
  console.error('FAIL: layout audit found problems:');
  for (const f of failures) console.error(`  - ${f}`);
  process.exitCode = 1;
} else {
  console.log(
    `layout-audit: grid tracks OK; ${Object.keys(HIDDEN_HELPER_OWNERS).length} visually-hidden helper(s) contained by a positioned owner; ${TAB_STRIP_SELECTORS.length} tab-strip selector(s) keep flex-wrap: wrap (issue #477); ct-columns keeps its two-track/one-track breakpoint and ct-field[narrow]'s control opts out of stretch (issue #601); every ct-*.css is imported by its sibling ct-*.ts (issue #601 follow-up); every font size under src/ resolves to >= ${MIN_FONT_SIZE_PX}px against a ${Object.keys(TYPE_SCALE).length}-step --ct-text-* scale (issue #600); the .ct-table--wrap-headings opt-in is declared and applied (issue #668)`,
  );
  console.log('LAYOUT AUDIT: ALL GREEN');
}
