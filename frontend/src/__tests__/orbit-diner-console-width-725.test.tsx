/**
 * orbit-diner-console-width-725.test.tsx — the console owns its own grid,
 * inside the existing warm shell, at the existing 1320px ceiling (issue #725,
 * epic #729).
 *
 * BE HONEST ABOUT WHAT THIS IS. vitest runs jsdom with `css: false`
 * (frontend/vitest.config.ts) and jsdom implements no layout at all, so no
 * assertion here measures a box, and none of them is evidence that the console
 * reflows. They come in two halves, and each half is only as strong as it
 * says:
 *
 *   - SOURCE guards, in `layout-audit.mjs`'s idiom: the stylesheet declares
 *     the mechanism the ticket names. What they buy is that the exact
 *     regression cannot land again unnoticed — a `@media` breakpoint creeping
 *     back into the console, the container losing its name, the 1320px
 *     ceiling drifting.
 *   - DOM facts, which jsdom CAN see: what the console is mounted inside, what
 *     classes it carries, what the layout observer publishes, and what text a
 *     long playbook name puts in front of a screen reader.
 *
 * The four claims worth naming, because each is a way this went wrong or would
 * go wrong silently:
 *
 *   1. THE CONSOLE'S OWN WIDTH IS THE BREAKPOINT. Inside `ct-app-shell` the
 *      viewport is always wider than the console — 1600px of window is 1272px
 *      of console — so every viewport query in the kit switched the layout at
 *      the wrong moment, and the kit's roomiest step (`min-width: 1500px`)
 *      could never fire at all. Every width query in `orbit.css` is now a
 *      container query, and it NAMES the container, because the notepad skin
 *      establishes a second, nested one that an unnamed query would silently
 *      resolve against instead.
 *   2. NO HOST RULE REACHES THE CONSOLE. The hero's 360px cap and
 *      `.ct-review-console`'s grid-area assignments belonged to the surface
 *      the console replaces, and #727 deleted both the rules and the markup
 *      that carried them. The assertion below outlives that deletion on
 *      purpose: it is now the standing guard that neither is reintroduced
 *      around the console, and it is a DOM fact rather than a comment.
 *   3. ONE LAYOUT OBSERVER, and it publishes what the stylesheet asks. #738
 *      picks the toaster plate from the same reading, so a second opinion
 *      (`window.innerWidth`, a private ResizeObserver) is how the plate and
 *      the layout come to disagree.
 *   4. N6 — a long playbook name stays one ellipsised line inside the glass,
 *      and the accessible selected value stays COMPLETE. The visual span is
 *      `aria-hidden`; the `<select>` is what is announced, and truncating it
 *      would hide the very thing the reviewer is choosing.
 *
 * `--ct-maxw` is unchanged (owner decision A2) and `review-submission` keeps
 * its `position: relative` (layout-audit check 2's declared owner for
 * `.ct-sr-only`); both are pinned below so a well-meaning edit to app.css
 * cannot quietly take them.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import {
  CONSOLE_PHONE_MAX_PX,
  CONSOLE_STACK_MAX_PX,
  consoleSize,
} from '../orbit-diner/consoleWidth';

const here = path.dirname(fileURLToPath(import.meta.url));
const srcDir = path.resolve(here, '..');
/** Comments carry example CSS in this repo's stylesheets — including, right
 *  below, a comment that quotes the very `@media` prelude these guards forbid.
 *  Strip them before parsing or the file fails on its own explanation. */
const stripComments = (text: string): string => text.replace(/\/\*[\s\S]*?\*\//g, '');

const orbitCss = stripComments(
  fs.readFileSync(path.join(srcDir, 'orbit-diner', 'orbit.css'), 'utf8'),
);
const appCss = stripComments(fs.readFileSync(path.join(srcDir, 'styles', 'app.css'), 'utf8'));
const tokensCss = stripComments(fs.readFileSync(path.join(srcDir, 'styles', 'tokens.css'), 'utf8'));

// --------------------------------------------------------------- CSS reading

/** Every `@media`/`@container` prelude in `text`, at any nesting depth. */
function atRulePreludes(text: string): { kind: string; prelude: string }[] {
  return Array.from(text.matchAll(/@(media|container)([^{;]*)\{/g)).map((m) => ({
    kind: m[1],
    prelude: m[2].trim(),
  }));
}

/** Every `prelude { declarations }` rule, carrying the at-rule preludes it is
 *  nested inside — the same brace-depth walk layout-audit.mjs uses, because a
 *  flat rule regex loses the enclosing `@container`. */
function* eachRule(text: string): Generator<{ selectors: string[]; body: string; at: string[] }> {
  const at: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === '{') {
      const prelude = text.slice(start, i).trim();
      if (prelude.startsWith('@')) {
        at.push(prelude);
        depth += 1;
        start = i + 1;
      } else {
        const close = text.indexOf('}', i);
        yield {
          selectors: prelude.split(',').map((s) => s.trim()),
          body: text.slice(i + 1, close),
          at: [...at],
        };
        i = close;
        start = i + 1;
      }
    } else if (text[i] === '}') {
      if (depth > 0) {
        at.pop();
        depth -= 1;
      }
      start = i + 1;
    }
  }
}

/**
 * Every declaration block listing `selector`, inside the at-rule whose prelude
 * is `within` (top level when `within` is null), in source order.
 */
function ruleBodies(text: string, selector: string, within: string | null = null): string[] {
  const bodies = Array.from(eachRule(text))
    .filter((rule) => (within === null ? rule.at.length === 0 : rule.at.includes(within)))
    .filter((rule) => rule.selectors.includes(selector))
    .map((rule) => rule.body);
  expect(
    bodies.length,
    `rule not found: ${selector}${within ? ` inside ${within}` : ''}`,
  ).toBeGreaterThan(0);
  return bodies;
}

/**
 * What `selector` finally says about `property` — the LAST declaration of it
 * across every rule that lists the selector at that scope, which is what the
 * cascade resolves to at equal specificity. `orbit.css` restates several
 * selectors (`.od-console` four times), so reading only the first block, or
 * only the last block, would each assert a value the browser does not use.
 */
function declaredValue(
  text: string,
  selector: string,
  property: string,
  within: string | null = null,
): string | null {
  let value: string | null = null;
  for (const body of ruleBodies(text, selector, within)) {
    for (const found of body.matchAll(new RegExp(`(?:^|;)\\s*${property}\\s*:([^;]*)`, 'g'))) {
      value = found[1].trim();
    }
  }
  return value;
}

// ------------------------------------------------------------------ Fixtures

const NAME_60 = 'Mutual Non-Disclosure Agreement — Supplier Onboarding v4';
const NAME_UNBROKEN = 'Nondisclosureagreementsupplieronboardingrevisionfourteenb';

const PLAYBOOKS = [
  { playbook_id: 'nda', display_name: NAME_60, status: 'active' },
  { playbook_id: 'unbroken', display_name: NAME_UNBROKEN, status: 'active' },
];

function mockFetch() {
  return vi.fn(async (input: RequestInfo | URL) => {
    const pathname = new URL(String(input), 'http://localhost').pathname;
    if (pathname === '/api/playbooks') {
      return { ok: true, status: 200, json: async () => ({ playbooks: PLAYBOOKS }) } as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as Response;
  });
}

/** The stubbed observer's callbacks, so a test can report a width itself. */
let observerCallbacks: ResizeObserverCallback[] = [];

/** Report `width` as the content width of every observed console. */
function reportWidth(width: number): void {
  act(() => {
    for (const cb of observerCallbacks) {
      cb(
        [
          {
            contentBoxSize: [{ inlineSize: width, blockSize: 400 }],
            contentRect: { width } as DOMRectReadOnly,
          } as unknown as ResizeObserverEntry,
        ],
        {} as ResizeObserver,
      );
    }
  });
}

function consoleRoot(): HTMLElement {
  const node = document.querySelector<HTMLElement>('.od-console');
  if (!node) throw new Error('the Orbit Diner console did not render');
  return node;
}

beforeEach(() => {
  observerCallbacks = [];
  // jsdom ships neither of these. `matchMedia` answers "no preference" to
  // every query — forced colours and reduced motion are #724's and #723's, not
  // this file's. `ResizeObserver` is the one this file actually drives.
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(cb: ResizeObserverCallback) {
        observerCallbacks.push(cb);
      }
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {
        observerCallbacks = observerCallbacks.filter((cb) => cb !== undefined);
      }
    },
  );
  vi.stubGlobal('fetch', mockFetch());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------- The claims

describe('issue #725 — the console, not the viewport, is the breakpoint', () => {
  it('no width-based @media query is left in the console stylesheet', () => {
    const viewportWidthQueries = atRulePreludes(orbitCss).filter(
      (rule) => rule.kind === 'media' && /\b(min|max)-width\b/.test(rule.prelude),
    );
    // A failure here names the offender rather than reporting a bare count:
    // the fix is always "convert this one to @container od-console (…)".
    expect(viewportWidthQueries.map((rule) => `@media ${rule.prelude}`)).toEqual([]);
  });

  it('every container query names the console container', () => {
    const unnamed = atRulePreludes(orbitCss)
      .filter((rule) => rule.kind === 'container')
      .filter((rule) => !rule.prelude.startsWith('od-console '));
    // An unnamed query resolves against the NEAREST container ancestor, and
    // `.od-pad-art` is one (it sizes the notepad skin in `cqw`), so an unnamed
    // rule inside the pad silently starts asking the pad's width.
    expect(unnamed.map((rule) => `@container ${rule.prelude}`)).toEqual([]);
  });

  it('the console establishes that named inline-size container', () => {
    expect(declaredValue(orbitCss, '.od-console', 'container-type')).toBe('inline-size');
    expect(declaredValue(orbitCss, '.od-console', 'container-name')).toBe('od-console');
  });

  it('the appliances stack at a container width of 1220px or less (owner A2)', () => {
    // One track, and a flexible one wrapped per #457 — a bare `1fr` here is
    // `minmax(auto, 1fr)` and pins the track open at its min-content width.
    expect(
      declaredValue(
        orbitCss,
        '.od-appliances',
        'grid-template-columns',
        `@container od-console (max-width: ${CONSOLE_STACK_MAX_PX}px)`,
      ),
    ).toBe('minmax(0, 1fr)');
  });

  it('the keys keep their real size on the stacked step', () => {
    expect(
      declaredValue(
        orbitCss,
        '.od-radio-key > span',
        'min-height',
        `@container od-console (max-width: ${CONSOLE_STACK_MAX_PX}px)`,
      ),
    ).toBe('44px');
  });
});

describe('issue #725 — the 1320px shell is kept, not replaced', () => {
  it('--ct-maxw is unchanged', () => {
    expect(declaredValue(tokensCss, ':root', '--ct-maxw')).toBe('1320px');
  });

  it('the console caps itself at the same 1320px', () => {
    expect(declaredValue(orbitCss, '.od-console', 'max-width')).toBe('1320px');
  });

  it('review-submission stays the positioned owner of .ct-sr-only', () => {
    // layout-audit.mjs check 2 declares this element as the containing block
    // for the completion-handoff live region. Losing it lets an absolutely
    // positioned, visually hidden box enlarge an ANCESTOR's scrollable area —
    // the measured 60px phantom page scroll of #457.
    expect(declaredValue(appCss, "[data-testid='review-submission']", 'position')).toBe('relative');
  });
});

describe('issue #725 — no host rule reaches the console', () => {
  it('the console is mounted outside .ct-review-console and is not a hero', async () => {
    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    const node = consoleRoot();
    expect(node.closest('.ct-review-console')).toBeNull();
    expect(document.querySelector('.ct-review-console')).toBeNull();
    // The 360px cap was `.toaster-hero__svg`'s, so the console escapes it by
    // carrying no `toaster-hero*` class anywhere in its tree. #727 deleted
    // both that rule and the markup; this stays as the guard against either
    // coming back around the console.
    expect(document.querySelector('[class*="toaster-hero"]')).toBeNull();
    // And the rules themselves are gone from the stylesheet, not merely
    // unmatched by this render — a class nothing declares cannot be inherited
    // by a future wrapper either.
    expect(appCss).not.toContain('.ct-review-console');
    expect(appCss).not.toContain('toaster-hero');
  });
});

describe('issue #725 — one layout observer, published to the stylesheet', () => {
  it('an unmeasured console is wide, never phone', () => {
    // jsdom reports 0 for every box. `phone` would request the phone plate
    // (#738) for a console that may be 1272px on screen.
    expect(consoleSize(0)).toBe('wide');
    expect(consoleSize(-1)).toBe('wide');
  });

  it('the thresholds are the stylesheet"s own numbers', () => {
    expect(consoleSize(CONSOLE_PHONE_MAX_PX)).toBe('phone');
    expect(consoleSize(CONSOLE_PHONE_MAX_PX + 1)).toBe('narrow');
    expect(consoleSize(CONSOLE_STACK_MAX_PX)).toBe('narrow');
    expect(consoleSize(CONSOLE_STACK_MAX_PX + 1)).toBe('wide');
  });

  it('the console republishes its own content width, not the window width', async () => {
    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    expect(consoleRoot()).toHaveAttribute('data-console-size', 'wide');
    expect(observerCallbacks.length).toBeGreaterThan(0);

    // A 390px phone: window.innerWidth in jsdom is 1024 throughout, so a
    // reading that tracked the window could not produce any of these.
    reportWidth(342);
    expect(consoleRoot()).toHaveAttribute('data-console-size', 'phone');
    reportWidth(1002);
    expect(consoleRoot()).toHaveAttribute('data-console-size', 'narrow');
    // 1272px is the widest the console can be: ct-app-shell caps at 1320px and
    // spends 24px of padding per side.
    reportWidth(1272);
    expect(consoleRoot()).toHaveAttribute('data-console-size', 'wide');
  });

  it('the phone corner radius rides that attribute, since a container cannot query itself', () => {
    expect(
      declaredValue(orbitCss, '.od-console[data-console-size="phone"]', 'border-radius'),
    ).toBe('12px');
  });
});

describe('issue #725 — N6, a long playbook name on the phone step', () => {
  const phoneStep = `@container od-console (max-width: ${CONSOLE_PHONE_MAX_PX}px)`;

  it('is one line at 15px with an end ellipsis', () => {
    const value = (property: string): string | null =>
      declaredValue(orbitCss, '.od-playbook-value > span', property, phoneStep);
    expect(value('font-size')).toBe('15px');
    expect(value('white-space')).toBe('nowrap');
    expect(value('text-overflow')).toBe('ellipsis');
    expect(value('overflow')).toBe('hidden');
    // `-webkit-box` is the desktop two-line clamp; `text-overflow` is inert on
    // it, so the phone step must return the box to `block`.
    expect(value('display')).toBe('block');
  });

  it('cannot widen the glass it sits in', () => {
    // The label is a flex item of `.od-playbook-display`, so `min-width: auto`
    // gives it an automatic minimum equal to its min-content width — with
    // `nowrap` above, the width of the WHOLE name. Without this the box grows
    // past the 50%-wide glass and the ellipsis never fires.
    expect(declaredValue(orbitCss, '.od-playbook-value', 'min-width')).toBe('0');
    expect(declaredValue(orbitCss, '.od-playbook-value', 'max-width')).toBe('100%');
  });

  it('renders each long name whole, in one element, with nothing else beside it', async () => {
    render(<ReviewSubmission />);
    const dial = (await screen.findByTestId('review-playbook-dial')) as HTMLSelectElement;
    reportWidth(342);

    for (const { playbook_id: id, display_name: name } of PLAYBOOKS) {
      fireEvent.change(dial, { target: { value: id } });
      await waitFor(() => expect(dial.value).toBe(id));
      const painted = Array.from(
        document.querySelectorAll<HTMLElement>('.od-playbook-value > span'),
      );
      // ONE element holding the WHOLE string. The ellipsis is CSS's to apply,
      // so nothing here may pre-truncate the name, split it across a second
      // element, or add an extra appliance label beside it (N6 forbids all
      // three) — each of those is a change jsdom can see and CSS cannot fix.
      expect(painted.map((el) => el.textContent)).toEqual([name]);
      expect(painted[0].getAttribute('aria-hidden')).toBe('true');
    }
  });

  it('the announced value, and the picker, keep every name complete', async () => {
    render(<ReviewSubmission />);
    const dial = (await screen.findByTestId('review-playbook-dial')) as HTMLSelectElement;
    reportWidth(342);
    await waitFor(() => expect(dial.value).not.toBe(''));

    // The painted span is aria-hidden, so the `<select>` is what is announced.
    // Truncating it would hide the very thing the reviewer is choosing.
    expect(dial.getAttribute('aria-label')).toBe('Playbook type');
    expect(Array.from(dial.options).map((o) => o.textContent)).toEqual(
      PLAYBOOKS.map((p) => p.display_name),
    );
    // Which one is preselected is the host's business (last-used, then the
    // catalog's own order); that it is announced WHOLE is this ticket's.
    expect(PLAYBOOKS.map((p) => p.display_name)).toContain(
      dial.options[dial.selectedIndex].textContent,
    );
  });
});
