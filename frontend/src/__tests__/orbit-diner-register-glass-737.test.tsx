/**
 * orbit-diner-register-glass-737.test.tsx — the register's second glass reads
 * as deliberately switched off when it carries no operational message, and the
 * announcer that speaks those messages survives the off state (issue #737,
 * owner decision H3, epic #729).
 *
 * ## The two halves, and why they are one ticket
 *
 * VISUAL. `registerLine` falls through to `''` on a terminal review with no
 * `submit` / `catalog` / `preference` / `cancel` / `poll` message and no
 * pending settlement. The finished unlit glass is already photographed into
 * `register.webp` (the 04p2 artwork answer to N2), so the off state is the
 * empty text layer getting out of the way — never a gradient, fill or panel
 * painted into the aperture, and never a raster asset of its own.
 *
 * ACCESSIBILITY. That is precisely why the glass cannot also be the announcer.
 * A polite live region has to be in the accessibility tree BEFORE its content
 * changes; a region the stylesheet hides while empty is a region that only
 * appears at the moment the message arrives, and a newly inserted region is
 * not announced. So the glass is decorative and `aria-hidden`, and a separate
 * `.od-sr` region stays mounted and empty for the whole session. Nothing ever
 * says "display off" — the off state's announcement is the empty string.
 *
 * ## Be honest about the CSS half
 *
 * vitest runs with `css: false` and jsdom implements no cascade, so
 * `getComputedStyle` would assert nothing here. The stylesheet claims below
 * are read off the SHIPPED `orbit.css`, and the two rules that carry the whole
 * ticket are joined to the live DOM with `Element.matches` — the
 * `matchesSafely` pattern `resilience-a11y.test.tsx` uses for the
 * forced-colours cascade — because `textContent === ''` cannot prove that the
 * element React actually renders still satisfies `:empty`. Wrapping the line
 * in a child element would keep every text assertion green while silently
 * lighting the glass back up. Each claim was mutation-checked as it was
 * written:
 * deleting the H3 `:empty` block, dropping the narrow-layout `display: none`,
 * and giving `.od-sr` a `display: none` each turn a different assertion red.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { OrbitDiner } from '../orbit-diner/OrbitDiner';
import type { Message, ReviewModel } from '../orbit-diner/types';

const PLAYBOOKS = [{ playbook_id: 'nda', display_name: 'NDA', status: 'active' }];

/** jsdom ships no `matchMedia`; the console reads it for forced colours. */
function stubMatchMedia(matching: string[] = []): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: matching.some((feature) => query.includes(feature)),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}

function consoleModel(patch: Partial<ReviewModel> = {}): ReviewModel {
  return {
    status: 'DONE',
    reviewId: 'rev-1',
    fileSelected: true,
    preferences: {
      playbookId: 'nda',
      intensity: 'medium',
      notesMode: 'none',
      instructions: '',
      dispositionNote: '',
    },
    playbooks: PLAYBOOKS,
    internalNotesAvailable: false,
    browningReadback: 'Medium markup.',
    browningNote: 'Medium markup.',
    guidancePrecedence: 'Your instructions govern the playbook’s positions.',
    // A settled, COMPLETE settlement: the one other thing that would put a
    // line in the glass on a terminal review is "Settlement pending", so the
    // off state has to be reached with settlement finished rather than absent.
    cost: { kind: 'settled', cents: 412, settlementComplete: true },
    muted: true,
    notification: 'unsupported',
    ...patch,
  };
}

function renderConsole(model: ReviewModel): void {
  render(
    <OrbitDiner
      model={model}
      keyboardShortcuts={false}
      onFile={vi.fn()}
      onPreferences={vi.fn()}
      onAction={vi.fn()}
    />,
  );
}

const glass = (): HTMLElement =>
  document.querySelector<HTMLElement>('.od-register-status') as HTMLElement;
const announcer = (): HTMLElement => screen.getByTestId('review-register-announcement');
const consoleRoot = (): HTMLElement =>
  document.querySelector<HTMLElement>('.od-console') as HTMLElement;

/** The two shipped selectors that switch the empty window off. Asserted as
 *  strings against `orbit.css` below, and joined to the rendered element here.
 *  nwsapi throws on selector syntax it does not implement; a selector this
 *  element cannot be tested against is not part of its cascade. */
const ILLUSTRATED_OFF = '.od-console:not(.od-plain) .od-register-status:empty';
const PLAIN_OFF = '.od-plain .od-register-status:empty';

function matchesSafely(el: Element, selector: string): boolean {
  try {
    return el.matches(selector);
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// The shipped stylesheet
// ---------------------------------------------------------------------------

const ORBIT_CSS = path.join(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'),
  'orbit-diner',
  'orbit.css',
);

interface StyleRule {
  /** The selector list, verbatim. */
  selector: string;
  /** The declaration block, verbatim. */
  body: string;
  /** The at-rule preludes this rule is nested inside, outermost first. */
  conditions: string[];
}

/** Every style rule in the sheet, flattened, each carrying the at-rule
 *  conditions it sits under. Comments are stripped first so a commented-out
 *  declaration can never satisfy an assertion. */
function orbitRules(): StyleRule[] {
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

const RULES = orbitRules();

/** The declared value for `property` in this rule, or undefined. */
function declared(rule: StyleRule, property: string): string | undefined {
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

/** Rules with at least one selector in their list satisfying `matches`. */
function rulesWhere(matches: (selector: string) => boolean): StyleRule[] {
  return RULES.filter((rule) =>
    rule.selector
      .split(',')
      .map((one) => one.trim())
      .some((one) => one.length > 0 && matches(one)),
  );
}

/** Selectors that target the message window only while it is empty. */
const emptyGlassRules = (): StyleRule[] =>
  rulesWhere((selector) => /\.od-register-status:empty\s*$/.test(selector));

/** Selectors that target the screen-reader-only helper class. `.od-sr-…`
 *  would be a different class, so the boundary is part of the test. */
const srRules = (): StyleRule[] => rulesWhere((selector) => /\.od-sr(?![\w-])/.test(selector));

// ---------------------------------------------------------------------------
// 1. The off state
// ---------------------------------------------------------------------------

describe('issue #737 — the second glass reads as switched off when empty', () => {
  beforeEach(() => stubMatchMedia());
  afterEach(() => vi.unstubAllGlobals());

  it('puts no text in the glass on a terminal review with no operational message', () => {
    renderConsole(
      consoleModel({
        messages: [
          // Scoped to an overlay, so designer answer B5 keeps it out of the
          // glass: the glass has no message and must go dark.
          { id: 'receipt-copied', scope: 'receipt', tone: 'success', title: 'Receipt copied' },
        ],
      }),
    );
    expect(glass()).toBeTruthy();
    expect(glass().textContent).toBe('');
    // Empty text is not enough: the shipped rule is structural, so the element
    // React renders has to actually satisfy `:empty` for it to apply.
    expect(matchesSafely(glass(), ILLUSTRATED_OFF)).toBe(true);
    // The photographed register — the unlit glass included — is still what is
    // on screen. The off state is the text layer getting out of the way, not
    // the artwork being swapped or removed.
    expect(document.querySelector('.od-register-full')).toBeTruthy();
  });

  it('makes the glass decorative and keeps the announcer mounted and empty', () => {
    renderConsole(consoleModel());

    // The glass is a picture, not a voice. It is the element the stylesheet
    // hides when empty, so it must not be the one carrying the live region.
    expect(glass()).toHaveAttribute('aria-hidden', 'true');
    expect(glass()).not.toHaveAttribute('aria-live');

    // The announcer is a different node, mounted before any message exists,
    // and silent — an empty string, never wording for the off state.
    const region = announcer();
    expect(region).not.toBe(glass());
    expect(region).not.toHaveClass('od-register-status');
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region).not.toHaveAttribute('aria-hidden');
    expect(region).not.toHaveAttribute('hidden');
    expect(region.textContent).toBe('');
    expect(document.body.textContent).not.toMatch(/display off/i);
  });

  it('renders an operational message exactly as before, and announces it once', () => {
    const poll: Message = {
      id: 'poll-error',
      scope: 'poll',
      tone: 'error',
      title: 'Still checking',
      detail: "Still checking on your review's status — reconnecting…",
      action: 'poll-retry',
      actionLabel: 'Check now',
    };
    renderConsole(consoleModel({ status: 'RUNNING', messages: [poll] }));

    expect(glass().textContent).toBe('Still checking');
    expect(announcer().textContent).toBe('Still checking');
    // One voice for the glass, not two: the decorative copy stays silent.
    expect(
      Array.from(
        document.querySelectorAll('[aria-live]:not([aria-live="off"])'),
      ).filter((el) => el.textContent === 'Still checking'),
    ).toEqual([announcer()]);
  });
});

// ---------------------------------------------------------------------------
// 2. What the stylesheet does with the empty window
// ---------------------------------------------------------------------------

describe('issue #737 — the empty window is removed, never repainted', () => {
  it('takes the empty glass out of the picture in every layout and mode', () => {
    const rules = emptyGlassRules();
    expect(rules.length).toBeGreaterThan(0);

    const illustrated = rules.filter((rule) => /:not\(\.od-plain\)/.test(rule.selector));
    expect(illustrated.length).toBe(1);
    // The selector the DOM tests join against is the shipped one, verbatim.
    expect(illustrated[0].selector).toBe(ILLUSTRATED_OFF);
    // Illustrated: `register.webp` is underneath, so the text layer is made
    // invisible rather than removed — the aperture keeps its geometry.
    expect(declared(illustrated[0], 'visibility')).toBe('hidden');

    // Plain (and therefore forced colours, which sets `od-plain`): the window
    // is in normal flow with a 44px minimum, so an invisible one would leave a
    // hole. It is omitted entirely.
    const plain = rules.filter((rule) => /\.od-plain\s/.test(rule.selector));
    expect(plain.length).toBe(1);
    expect(plain[0].selector).toBe(PLAIN_OFF);
    expect(declared(plain[0], 'display')).toBe('none');

    // Narrow: the compact register draws no aperture, so the window is a
    // panel of its own in flow. Same reasoning, same answer.
    const narrow = rules.filter((rule) =>
      rule.conditions.some((condition) => /max-width:\s*560px/.test(condition)),
    );
    expect(narrow.length).toBeGreaterThan(0);
    expect(narrow.every((rule) => declared(rule, 'display') === 'none')).toBe(true);
  });

  it('paints no gradient, fill or panel in the aperture', () => {
    for (const rule of emptyGlassRules()) {
      const background = declared(rule, 'background');
      if (background !== undefined) expect(background).toBe('none');
      expect(rule.body).not.toMatch(/gradient|url\(|background-image|background-color/i);
      const shadow = declared(rule, 'box-shadow');
      if (shadow !== undefined) expect(shadow).toBe('none');
    }
    // And nothing is smuggled in as an inline style either.
    stubMatchMedia();
    renderConsole(consoleModel());
    expect(glass()).not.toHaveAttribute('style');
    vi.unstubAllGlobals();
  });

  it('never hides the announcer, so it is in the tree before a message arrives', () => {
    const rules = srRules();
    // The helper exists and hides by CLIPPING — an element that is still
    // rendered, still measured, still in the accessibility tree.
    expect(rules.length).toBeGreaterThan(0);
    expect(rules.some((rule) => declared(rule, 'clip-path') !== undefined)).toBe(true);
    for (const rule of rules) {
      expect(declared(rule, 'display')).not.toBe('none');
      expect(declared(rule, 'visibility')).not.toBe('hidden');
      expect(declared(rule, 'content-visibility')).not.toBe('hidden');
    }
  });
});

// ---------------------------------------------------------------------------
// 3. Plain and forced colours
// ---------------------------------------------------------------------------

describe('issue #737 — plain and forced colours carry no empty decorative window', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('keeps the announcer mounted with forced colours active', () => {
    stubMatchMedia(['forced-colors']);
    renderConsole(consoleModel());

    // Forced colours takes the plain path, which is where `display: none`
    // removes the empty window rather than merely hiding it.
    expect(consoleRoot()).toHaveClass('od-plain');
    expect(glass().textContent).toBe('');
    expect(matchesSafely(glass(), PLAIN_OFF)).toBe(true);
    expect(glass()).toHaveAttribute('aria-hidden', 'true');
    // The window is gone; the voice is not.
    const region = announcer();
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region.textContent).toBe('');
  });

  it('keeps the announcer mounted in the plain view', () => {
    stubMatchMedia();
    render(
      <OrbitDiner
        model={consoleModel()}
        plain
        keyboardShortcuts={false}
        onFile={vi.fn()}
        onPreferences={vi.fn()}
        onAction={vi.fn()}
      />,
    );
    expect(consoleRoot()).toHaveClass('od-plain');
    expect(glass().textContent).toBe('');
    expect(matchesSafely(glass(), PLAIN_OFF)).toBe(true);
    expect(announcer()).toHaveAttribute('aria-live', 'polite');
    expect(announcer().textContent).toBe('');
  });
});
