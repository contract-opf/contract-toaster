/**
 * orbit-diner-filename.test.tsx — the filename inscription fits the bread's
 * central face on two lines at most, measured rather than counted, with the
 * start and the extension preserved (issue #739, owner decision O1, epic
 * #729).
 *
 * ## Be honest about what this proves
 *
 * vitest runs jsdom with `css: false` (frontend/vitest.config.ts). jsdom
 * implements no layout, no cascade and no font metrics, so nothing here
 * measures a real box or a real glyph, and no assertion below is evidence that
 * Space Grotesk fits inside the crust on a real screen. That evidence is the
 * designer's browser measurement recorded in
 * `docs/planning/2026-09-07-orbit-diner-designer-round3.md` (O1). The claims
 * come in four halves, and each is only as strong as it says:
 *
 *   1. THE ALGORITHM, driven by a stated metric fixture. `fitFilename` is a
 *      pure function of a `measure()`, so it can be tested exactly — and the
 *      fixture's whole point is that its glyph widths VARY, so an
 *      implementation that counted characters cannot pass. The fixture is
 *      approximate Space Grotesk; it does not have to be exact, because
 *      nothing here asserts a real render.
 *   2. THE BOX, derived from the shipped stylesheet's own percentages rather
 *      than picked. Every number in that derivation is pinned to `orbit.css`
 *      (in `support/orbitInscription.ts`, which two files now share), so a
 *      stylesheet edit that changes the inscription's rectangle fails this
 *      file rather than quietly invalidating its fixtures.
 *   3. THE RECTANGLE IS STATE-INDEPENDENT. The ticket asks for the safe
 *      rectangle to be validated in every slice state, and jsdom cannot see
 *      one. What it can prove is the thing that makes ONE browser measurement
 *      enough: no `data-status` rule moves, resizes or re-sizes the type of
 *      `.od-toast-name`, and the element React renders in each of the five
 *      states really does match the 04p2 rectangle's selector — joined to the
 *      DOM with `Element.matches`, the `matchesSafely` pattern from
 *      `resilience-a11y.test.tsx`, because reading the rule out of the
 *      stylesheet as text cannot prove it still selects the rendered element.
 *   4. THE DOM FACTS. What the console paints, that it paints it in ONE
 *      element, that the accessible name and the tooltip keep the WHOLE
 *      filename, and that a host which cannot measure text paints the whole
 *      name rather than guessing at a shortening.
 *
 * Each claim was mutation-checked as it was written: removing the truncation
 * branch, replacing the measurer with `text.length`, dropping the extension
 * from the protected tail, and painting `m.filename` instead of the fitted
 * string each turn a different assertion red.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { OrbitDiner } from '../orbit-diner/OrbitDiner';
import {
  FILENAME_ELLIPSIS,
  FILENAME_MAX_LINES,
  fitFilename,
  splitExtension,
} from '../orbit-diner/filename';
// The metric fixture and the box derivation moved to `support/` when issue #78
// added a second file that needs them (orbit-diner-filename-band-78.test.tsx).
// They are the same numbers this file always used — `DESKTOP` is still
// 143px/20px and `PHONE` still 95px/14px — with one correction #78 forced: the
// derivation resolves the at-rule conditions that hold at the console width it
// is asked about, instead of modelling every non-phone width with the
// `min-width: 1221px` step's two-column grid and 20px type. See the module's
// header; the hole that correction closed is the sweep below.
import {
  CONSOLE_MAX_PX,
  CONSOLE_MIN_PX,
  DESKTOP,
  PHONE,
  RULES,
  declared,
  inscriptionBox,
  inscriptionShare,
  metricsAt,
  ORPHAN_PRONE,
  px,
  SHORT,
  valueOf,
  type Box,
} from './support/orbitInscription';
import type { ReviewModel, Status } from '../orbit-diner/types';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** 120 characters, separator-rich, ending in a version token. */
const LONG =
  'Mutual-Non-Disclosure-Agreement-counterparty-packages-final-execution-' +
  'copies-for-review-and-signature-2026-09-07-v7.docx';

/** 120 characters with no space and no separator anywhere in the stem. */
const UNBROKEN =
  'Nondisclosureagreementwiththecounterpartysupplieronboardingrevision' +
  'fourteenexecutioncopyforreviewandsignaturesixteo.docx';

/** Same length, wildly different width — the anti-character-count pair. */
const NARROW_26 = 'lillilliltilliltillil.docx';
const WIDE_26 = 'MWMWMWMWMWMWMWMWMWMWM.docx';

/**
 * Names carrying astral characters — one UTF-16 code unit each half, one
 * character each pair. The emoji sit inside a long separator-free run so the
 * in-chunk break has to land among them, and again next to the tail so the
 * head search does too.
 */
const ASTRAL_NAMES = [
  'Rocket🚀🚀launchagreementcounterpartyexecutioncopy-final-v7.docx',
  '🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀.docx',
  'Mutual-NDA-counterparty-package-𝐁𝐎𝐋𝐃-execution-copy-v7🚀.docx',
];

/** A high surrogate with no low after it, or a low with no high before it. */
const LONE_SURROGATE =
  /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

/** Fit `name` in `box`, at the font that box is set in. */
function fitIn(name: string, box: Box, maxLines = FILENAME_MAX_LINES): string[] {
  return fitFilename(name, { measure: metricsAt(box.font), width: box.width, maxLines });
}

/** Every line is inside the box, and there are no more than `maxLines`. */
function expectInside(lines: string[], box: Box, maxLines = FILENAME_MAX_LINES): void {
  const measure = metricsAt(box.font);
  expect(lines.length).toBeLessThanOrEqual(maxLines);
  for (const line of lines) {
    expect(
      Math.round(measure(line) * 100) / 100,
      `line wider than the ${box.width}px inscription: ${JSON.stringify(line)}`,
    ).toBeLessThanOrEqual(box.width);
  }
}

/**
 * Nothing was removed: the painted lines put the name back together.
 *
 * A break either consumed a space (`Mutual NDA` / `draft-v3.docx`) or did not
 * (`…lillil.` / `docx`), so the reconstruction ignores whitespace and each
 * line is separately required to be a real run of the real name.
 */
function expectWhole(lines: string[], name: string): void {
  expect(lines.join('\n')).not.toContain(FILENAME_ELLIPSIS);
  expect(lines.join('').replace(/\s/g, '')).toBe(name.replace(/\s/g, ''));
  for (const line of lines) expect(name).toContain(line);
}

// ---------------------------------------------------------------------------
// 1. The box the stylesheet actually gives the inscription
// ---------------------------------------------------------------------------

describe('issue #739 — the safe rectangle, derived from the shipped stylesheet', () => {
  it('is the bread’s central face, inset to the crust rather than to the slice', () => {
    // 04p2's answer to the designer's O1: the name was inset 12% to the
    // slice's 1254 x 1254 bounding box, and the visible crust is inset far
    // more than that.
    expect(valueOf('.od-console:not(.od-plain) .od-toast-name', 'left')).toBe('20%');
    expect(valueOf('.od-console:not(.od-plain) .od-toast-name', 'right')).toBe('20%');
    expect(valueOf('.od-console:not(.od-plain) .od-toast-name', 'top')).toBe('40%');
    expect(inscriptionShare()).toBeCloseTo(0.6, 10);
  });

  it('holds two lines of the declared type at both ends of the range', () => {
    const lineHeight = Number.parseFloat(valueOf('.od-toast-name', 'line-height'));
    expect(lineHeight).toBeGreaterThan(0);
    for (const box of [DESKTOP, PHONE]) {
      expect(FILENAME_MAX_LINES * box.font * lineHeight).toBeLessThanOrEqual(box.height);
    }
  });

  it('never sets the inscription below the 14px floor (#600)', () => {
    const sizes = RULES.filter((rule) => /\.od-toast-name\s*$/.test(rule.selector))
      .map((rule) => declared(rule, 'font-size'))
      .filter((size): size is string => size !== undefined);
    expect(sizes.length).toBeGreaterThan(0);
    for (const size of sizes) expect(px(size)).toBeGreaterThanOrEqual(14);
    // The two ends of the size table (#717 owns the table itself).
    expect(DESKTOP.font).toBe(20);
    expect(PHONE.font).toBe(14);
  });

  it('clamps to the same number of lines the algorithm emits', () => {
    // One decision, not two opinions: a clamp of 3 with an algorithm of 2
    // wastes a line, and a clamp of 2 with an algorithm of 3 eats `.docx`.
    expect(Number(valueOf('.od-toast-name', '-webkit-line-clamp'))).toBe(FILENAME_MAX_LINES);
  });

  it('paints the breaks the algorithm chose rather than re-deciding them', () => {
    // `pre-wrap`, not `pre`: an unmeasurable host paints the whole name, and
    // that name still has to wrap rather than run off the crust.
    expect(valueOf('.od-toast-name', 'white-space')).toBe('pre-wrap');
    expect(valueOf('.od-toast-name', 'overflow-wrap')).toBe('anywhere');
  });
});

// ---------------------------------------------------------------------------
// 2. The algorithm
// ---------------------------------------------------------------------------

describe('issue #739 — the short fixture is painted whole', () => {
  it('fits completely at 390px and at 1360px', () => {
    for (const box of [PHONE, DESKTOP]) {
      const lines = fitIn(SHORT, box);
      expectWhole(lines, SHORT);
      expectInside(lines, box);
    }
  });

  it('wraps at the separators rather than mid-word when it can', () => {
    expect(fitIn(SHORT, PHONE)).toEqual(['Mutual NDA', 'draft-v3.docx']);
  });
});

describe('issue #739 — a long name is shortened by measurement, not by counting', () => {
  it('puts a 120-character name on two lines, keeping the start and .docx', () => {
    expect(LONG).toHaveLength(120);
    for (const box of [PHONE, DESKTOP]) {
      const lines = fitIn(LONG, box);
      expectInside(lines, box);
      const painted = lines.join('\n');
      expect(painted.startsWith('Mutual-Non')).toBe(true);
      expect(painted).toContain(FILENAME_ELLIPSIS);
      expect(painted.endsWith('.docx')).toBe(true);
      // The stem's final segment survives beside the extension: two names from
      // one matter differ only in the version token.
      expect(painted.endsWith(`${FILENAME_ELLIPSIS}v7.docx`)).toBe(true);
      expect(painted.length).toBeLessThan(LONG.length);
    }
  });

  it('truncates an unbroken 120-character name rather than overflowing', () => {
    expect(UNBROKEN).toHaveLength(120);
    expect(UNBROKEN.slice(0, -'.docx'.length)).not.toMatch(/[\s\-_./]/);
    for (const box of [PHONE, DESKTOP]) {
      const lines = fitIn(UNBROKEN, box);
      expectInside(lines, box);
      const painted = lines.join('\n');
      expect(painted.startsWith('Nondisclosure')).toBe(true);
      expect(painted).toContain(FILENAME_ELLIPSIS);
      expect(painted.endsWith('.docx')).toBe(true);
    }
  });

  it('treats two names of the SAME length differently by their width', () => {
    // The claim the ticket makes in one line — "do not impose a
    // character-count limit; glyph widths vary, measure". A character-count
    // implementation cannot tell these two apart.
    expect(NARROW_26).toHaveLength(WIDE_26.length);
    expectWhole(fitIn(NARROW_26, PHONE), NARROW_26);
    const wide = fitIn(WIDE_26, PHONE);
    expect(wide.join('\n')).toContain(FILENAME_ELLIPSIS);
    expectInside(wide, PHONE);
  });

  it('keeps as much of the head as the box allows', () => {
    // Twice the room, same type: strictly more of the name survives. There is
    // no fixed budget hiding behind the measurement.
    const head = (lines: string[]): string => lines.join('\n').split(FILENAME_ELLIPSIS)[0];
    const narrow = head(fitIn(LONG, PHONE));
    const roomier = head(
      fitFilename(LONG, { measure: metricsAt(PHONE.font), width: PHONE.width * 2 }),
    );
    expect(roomier.length).toBeGreaterThan(narrow.length);
    // Whatever survives is a real prefix of the real name, newline included.
    expect(LONG.startsWith(narrow.replace(/\n/g, ''))).toBe(true);
    expect(LONG.startsWith(roomier.replace(/\n/g, ''))).toBe(true);
  });

  it('never emits a third line, however narrow the box', () => {
    for (const width of [8, 20, 40, 60]) {
      const lines = fitFilename(LONG, { measure: metricsAt(14), width });
      expect(lines.length).toBeLessThanOrEqual(FILENAME_MAX_LINES);
    }
  });

  it('never cuts an astral character in half, at any width', () => {
    // Both cuts count UTF-16 code units — the break inside an over-wide chunk
    // and the binary search for the longest head — and a filename is read in
    // characters. `overflow-wrap: anywhere`, which the in-chunk break
    // reproduces, never splits one; half a surrogate pair is a .notdef box on
    // screen, so it must never reach the painted string.
    const measure = metricsAt(16);
    for (const name of ASTRAL_NAMES) {
      for (let width = 8; width <= 400; width += 1) {
        const lines = fitFilename(name, { measure, width });
        expect(
          LONE_SURROGATE.test(lines.join('\n')),
          `lone surrogate at width ${width} in ${JSON.stringify(lines)}`,
        ).toBe(false);
      }
    }
  });
});

describe('issue #739 — the extension is what is protected, not a guessed suffix', () => {
  it('splits a real extension off the stem', () => {
    expect(splitExtension('Mutual NDA draft-v3.docx')).toEqual({
      stem: 'Mutual NDA draft-v3',
      extension: '.docx',
    });
  });

  it('does not mistake a dotted stem for an extension', () => {
    expect(splitExtension('Schedule 4.2 pricing')).toEqual({
      stem: 'Schedule 4.2 pricing',
      extension: '',
    });
    expect(splitExtension('no-extension-at-all')).toEqual({
      stem: 'no-extension-at-all',
      extension: '',
    });
  });

  it('still ends in an ellipsis when there is no extension to keep', () => {
    const name = 'Amendment number four to the master services agreement of 2026';
    const painted = fitIn(name, PHONE).join('\n');
    expect(painted).toContain(FILENAME_ELLIPSIS);
    expect(painted).not.toContain('.docx');
  });
});

describe('issue #739 \u2014 whitespace already in the name is not a line break', () => {
  /**
   * `.od-toast-name` is `white-space: pre-wrap`, so this module can emit its
   * own breaks \u2014 and so every whitespace character ALREADY in the filename
   * would paint as a forced break too. `measure()` is a canvas `measureText`,
   * which is blind to `\n`, so those breaks are ones the fit never counted:
   * `Contract\\nfinal-v3.docx` paints three visual lines and the clamp eats the
   * last one, which is where `.docx` lives. These names are reachable \u2014 a
   * POSIX filename may hold any of this, the projection takes `file.name`
   * verbatim and the backend only `.strip()`s it \u2014 so the fit normalizes the
   * whitespace away before it measures anything.
   */
  const WHITESPACE_NAMES = [
    'Contract\nfinal-v3.docx',
    'Mutual\tNDA\rdraft-v3.docx',
    'Deed\u2028of\u2029variation\u000bexecution\fcopy.docx',
    'Master-services-agreement\n\n\nexecution-copy-2026-09-07-v7.docx',
    `${LONG.slice(0, 60)}\n${LONG.slice(60)}`,
  ];

  it('paints at most two lines for a name carrying \\n, \\r or \\t', () => {
    for (const name of WHITESPACE_NAMES) {
      for (const box of [PHONE, DESKTOP]) {
        const lines = fitIn(name, box);
        // The painted string, not the array: a newline hiding INSIDE a line is
        // the whole failure, and only splitting the join can see it.
        expect(
          lines.join('\n').split('\n').length,
          `more than ${FILENAME_MAX_LINES} painted lines for ${JSON.stringify(name)}`,
        ).toBeLessThanOrEqual(FILENAME_MAX_LINES);
        expectInside(lines, box);
      }
    }
  });

  it('keeps the extension whole on the last line', () => {
    for (const name of WHITESPACE_NAMES) {
      expect(fitIn(name, PHONE).join('\n').endsWith('.docx')).toBe(true);
    }
  });

  it('does not offset the centred inscription with leading whitespace', () => {
    // The inscription is `text-align: center`, and `pre-wrap` keeps the three
    // spaces that `white-space: normal` used to collapse: they shove the name
    // off the middle of the bread. The leading run must change nothing at all.
    const padded = '   Mutual NDA.docx';
    for (const box of [PHONE, DESKTOP]) {
      expect(fitIn(padded, box)).toEqual(fitIn(padded.trim(), box));
      for (const line of fitIn(padded, box)) {
        expect(
          /^\s/.test(line),
          `line starts with whitespace: ${JSON.stringify(line)}`,
        ).toBe(false);
      }
    }
  });
});

// ---------------------------------------------------------------------------
// 3. The console
// ---------------------------------------------------------------------------

const PLAYBOOKS = [{ playbook_id: 'nda', display_name: 'NDA', status: 'active' }];

function consoleModel(patch: Partial<ReviewModel> = {}): ReviewModel {
  return {
    status: 'LOADED',
    fileSelected: true,
    filename: SHORT,
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
    cost: { kind: 'unavailable' },
    muted: true,
    notification: 'unsupported',
    ...patch,
  };
}

function renderConsole(model: ReviewModel, plain = false): { unmount: () => void } {
  return render(
    <OrbitDiner
      model={model}
      plain={plain}
      keyboardShortcuts={false}
      onFile={vi.fn()}
      onPreferences={vi.fn()}
      onAction={vi.fn()}
    />,
  );
}

/** jsdom ships no `matchMedia`; the console reads it for forced colours. */
function stubMatchMedia(): void {
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
}

/** The width the inscription's box reports. Everything else keeps a console
 *  width, so the layout observer stays on its widest step. */
const DOM_BOX = 120;
/** Mutable, so a test can shrink the box under a resize. */
let domBox = DOM_BOX;
function stubBoxWidth(): void {
  domBox = DOM_BOX;
  vi.spyOn(Element.prototype, 'clientWidth', 'get').mockImplementation(function (
    this: Element,
  ) {
    if (!this.classList.contains('od-toast-name')) return 1272;
    // `.od-plain .od-slice { display: none }` in the shipped stylesheet, which
    // gives every descendant of the slice a client width of zero. vitest runs
    // jsdom with `css: false` and lays nothing out, so the box has to say so
    // itself; without this the plain view would report a width it does not
    // have on any real screen.
    return this.closest('.od-console.od-plain') ? 0 : domBox;
  });
}

/**
 * A 2D context that measures with the fixture above, at whatever size the
 * font shorthand the module assigns carries. jsdom implements no canvas, so
 * without this the console has nothing to measure with — which is itself one
 * of the cases asserted below.
 */
function stubMeasuring(): void {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => {
    let font = '';
    return {
      get font() {
        return font;
      },
      set font(next: string) {
        font = next;
      },
      measureText: (text: string) => ({
        width: metricsAt(Number.parseFloat(/([\d.]+)px/.exec(font)?.[1] ?? '16'))(text),
      }),
    } as unknown as CanvasRenderingContext2D;
  });
}

const inscription = (): HTMLElement =>
  document.querySelector<HTMLElement>('.od-toast-name') as HTMLElement;
const slice = (): HTMLElement =>
  document.querySelector<HTMLElement>('.od-slice') as HTMLElement;

/** The 04p2 rectangle's own selector. nwsapi throws on selector syntax it
 *  does not implement, and a selector the element cannot be tested against is
 *  not part of its cascade. */
const RECTANGLE = '.od-console:not(.od-plain) .od-toast-name';
function matchesSafely(el: Element, selector: string): boolean {
  try {
    return el.matches(selector);
  } catch {
    return false;
  }
}

describe('issue #739 — what the console paints', () => {
  beforeEach(() => {
    stubMatchMedia();
    stubBoxWidth();
    stubMeasuring();
  });
  afterEach(() => vi.unstubAllGlobals());

  it('paints the fitted lines in ONE element, with the breaks already in it', () => {
    renderConsole(consoleModel({ filename: LONG }));
    const el = inscription();
    expect(el).toBeTruthy();
    // One element holding the whole inscription: no second span, no separate
    // extension plate, nothing else beside it.
    expect(document.querySelectorAll('.od-toast-name')).toHaveLength(1);
    expect(el.children).toHaveLength(0);
    const lines = el.textContent?.split('\n') ?? [];
    expect(lines.length).toBeLessThanOrEqual(FILENAME_MAX_LINES);
    expect(el.textContent).toContain(FILENAME_ELLIPSIS);
    expect(el.textContent?.endsWith('.docx')).toBe(true);
    expectInside(lines, { ...DESKTOP, width: DOM_BOX - 1, font: 16 });
  });

  it('paints at most two lines when the filename itself carries a newline', () => {
    // The DOM half of the whitespace claim, and the reason it is here as well
    // as against `fitFilename`: `pre-wrap` is a property of the RENDERED
    // element, so only the rendered `textContent` proves the clamp is never
    // asked to cut a third line. Painted verbatim this name is
    // `Contract` / `final-` / `v3.docx`, and the clamp eats `v3.docx`.
    const raw = 'Contract\nfinal-v3.docx';
    renderConsole(consoleModel({ filename: raw }));
    const el = inscription();
    const lines = el.textContent?.split('\n') ?? [];
    expect(lines.length).toBeLessThanOrEqual(FILENAME_MAX_LINES);
    expect(el.textContent?.endsWith('.docx')).toBe(true);
    expectInside(lines, { ...DESKTOP, width: DOM_BOX - 1, font: 16 });
    // Shortened on the bread, whole everywhere it is read out.
    expect(slice()).toHaveAttribute('aria-label', `Selected document: ${raw}`);
    expect(slice()).toHaveAttribute('title', raw);
  });

  it('keeps the WHOLE filename as the accessible name and the tooltip', () => {
    renderConsole(consoleModel({ filename: LONG }));
    expect(inscription().textContent).not.toBe(LONG);
    expect(slice()).toHaveAttribute('aria-label', `Selected document: ${LONG}`);
    expect(slice()).toHaveAttribute('title', LONG);
  });

  it('keeps the whole filename in the download affordance once there is output', () => {
    renderConsole(consoleModel({ status: 'DONE', filename: LONG, hasOutput: true }));
    expect(slice()).toHaveAttribute('aria-label', `Save redline for ${LONG}`);
    expect(slice()).toHaveAttribute('title', `Save redline: ${LONG}`);
  });

  it('measures the console’s own glyphs, so same-length names differ on screen', () => {
    // The DOM half of the anti-character-count claim, and the reason it is
    // here as well as against `fitFilename`: a console wired to a counting
    // measurer instead of the element's font passes every other assertion in
    // this file. These two names cannot be told apart by their length.
    const narrow = renderConsole(consoleModel({ filename: NARROW_26 }));
    expect(inscription().textContent).not.toContain(FILENAME_ELLIPSIS);
    expect(inscription().textContent?.replace(/\s/g, '')).toBe(NARROW_26);
    narrow.unmount();

    renderConsole(consoleModel({ filename: WIDE_26 }));
    expect(inscription().textContent).toContain(FILENAME_ELLIPSIS);
    expect(inscription().textContent?.endsWith('.docx')).toBe(true);
  });

  it('paints a short name unshortened', () => {
    renderConsole(consoleModel());
    expect(inscription().textContent).not.toContain(FILENAME_ELLIPSIS);
    expect(inscription().textContent?.replace(/\s+/g, ' ')).toBe(SHORT);
  });

  it('re-fits on a resize, through the console’s one layout observer', () => {
    // #725's invariant: the inscription's box is a chain of percentages off
    // the console's width, so the width that observer already publishes is
    // the whole trigger. A private observer here would be a second reading of
    // one fact.
    const observers: ResizeObserverCallback[] = [];
    const observed: Element[] = [];
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: ResizeObserverCallback) {
          observers.push(callback);
        }
        observe(target: Element): void {
          observed.push(target);
        }
        unobserve(): void {}
        disconnect(): void {}
      },
    );
    domBox = 700;
    renderConsole(consoleModel({ filename: LONG }));
    // Room enough for the whole name — over two lines, and nothing removed.
    // (`LONG` carries no space, so undoing the break restores it exactly.)
    expect(inscription().textContent).not.toContain(FILENAME_ELLIPSIS);
    expect(inscription().textContent?.replace(/\n/g, '')).toBe(LONG);
    // Nothing observes the inscription itself: the console's existing
    // observers (the console box, the instructions pad) are the only ones.
    expect(observed.some((el) => el.classList.contains('od-toast-name'))).toBe(false);
    expect(observed.length).toBeGreaterThan(0);

    domBox = 120;
    act(() => {
      for (const notify of observers) {
        notify(
          [
            {
              contentBoxSize: [{ inlineSize: 342, blockSize: 400 }],
              contentRect: { width: 342 } as DOMRectReadOnly,
            } as unknown as ResizeObserverEntry,
          ],
          {} as ResizeObserver,
        );
      }
    });
    expect(inscription().textContent).toContain(FILENAME_ELLIPSIS);
    expect(inscription().textContent?.endsWith('.docx')).toBe(true);
  });

  it('re-fits when the plain toggle gives the inscription a box to measure', () => {
    // The plain controls take the slice out of layout altogether, so a
    // document chosen there mounts an inscription of zero width and gets the
    // whole name by the honest fallback. Switching back to illustrated moves
    // one class on the console root: the node, the filename and the console
    // width are all unchanged, so a fit keyed on those three alone would
    // paint the untruncated name on the now-visible bread and hand the last
    // line — the one holding `.docx` — to `-webkit-line-clamp`.
    expect(valueOf('.od-plain .od-slice', 'display')).toBe('none');
    renderConsole(consoleModel({ filename: LONG }), true);
    // The display:none rule really does select the slice React renders, so
    // the zero width above is this element's width, not a stylesheet quote.
    expect(matchesSafely(slice(), '.od-plain .od-slice')).toBe(true);
    expect(inscription().textContent).toBe(LONG);

    fireEvent.click(screen.getByRole('button', { name: 'Illustrated controls' }));

    expect(matchesSafely(inscription(), RECTANGLE)).toBe(true);
    const painted = inscription().textContent ?? '';
    expect(painted).not.toBe(LONG);
    expect(painted).toContain(FILENAME_ELLIPSIS);
    expect(painted.endsWith('.docx')).toBe(true);
    expectInside(painted.split('\n'), { ...DESKTOP, width: DOM_BOX - 1, font: 16 });
  });

  it('paints the whole name on a host that cannot measure text', () => {
    // No 2D context — jsdom's own condition, and any browser that refuses one.
    // Too long is honest; a shortening guessed from a character count is not.
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    renderConsole(consoleModel({ filename: LONG }));
    expect(inscription().textContent).toBe(LONG);
  });

  it('paints the whole name while the box has not been measured yet', () => {
    vi.spyOn(Element.prototype, 'clientWidth', 'get').mockReturnValue(0);
    renderConsole(consoleModel({ filename: LONG }));
    expect(inscription().textContent).toBe(LONG);
  });
});

// ---------------------------------------------------------------------------
// 4. The rectangle is the same rectangle in every slice state
// ---------------------------------------------------------------------------

/** Properties that would move, resize, or re-type the inscription. */
const GEOMETRY = [
  'left',
  'right',
  'top',
  'bottom',
  'width',
  'height',
  'max-width',
  'max-height',
  'font-size',
  'line-height',
  'padding',
  'margin',
  'inset',
];

describe('issue #739 — the safe rectangle does not move with the slice state', () => {
  beforeEach(() => {
    stubMatchMedia();
    stubBoxWidth();
    stubMeasuring();
  });
  afterEach(() => vi.unstubAllGlobals());

  it('has no status-scoped rule that changes the inscription’s geometry', () => {
    // This is what makes ONE browser measurement of the crust enough for all
    // five states: `data-status` moves the SLICE, and the name is positioned
    // inside the slice, so the inscription travels with the artwork rather
    // than against it. A status rule that set `top` or `font-size` would break
    // that and would need its own measurement.
    const offenders: string[] = [];
    for (const rule of RULES) {
      const targets = rule.selector
        .split(',')
        .map((one) => one.trim())
        .filter((one) => /\.od-toast-name\s*$/.test(one) && one.includes('[data-status'));
      if (targets.length === 0) continue;
      for (const property of GEOMETRY) {
        if (declared(rule, property) !== undefined) {
          offenders.push(`${targets.join(', ')} { ${property} }`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it('renders the same inscription, matching the same rectangle, in every state', () => {
    // EMPTY is the state with no slice at all — there is no document, so
    // there is nothing to inscribe. The other four carry the artwork.
    const states: [Status, string][] = [
      ['LOADED', 'loaded'],
      ['RUNNING', 'running'],
      ['DONE', 'done'],
      ['ERROR', 'error'],
    ];
    const painted = new Set<string>();
    for (const [status, attribute] of states) {
      const { unmount } = render(
        <OrbitDiner
          model={consoleModel({ status, filename: LONG, hasOutput: status === 'DONE' })}
          keyboardShortcuts={false}
          onFile={vi.fn()}
          onPreferences={vi.fn()}
          onAction={vi.fn()}
        />,
      );
      expect(document.querySelector('.od-console')).toHaveAttribute(
        'data-status',
        attribute,
      );
      const el = inscription();
      expect(el, `no inscription in ${status}`).toBeTruthy();
      // Reading the rule out of the stylesheet cannot prove it still selects
      // what React renders; `matches` can.
      expect(matchesSafely(el, RECTANGLE), `${status} escapes ${RECTANGLE}`).toBe(true);
      painted.add(el.textContent ?? '');
      unmount();
    }
    // One name, one fit, whatever the toast is doing.
    expect(painted.size).toBe(1);
  });

  it('inscribes nothing when there is no document', () => {
    renderConsole(consoleModel({ status: 'EMPTY', fileSelected: false, filename: undefined }));
    expect(document.querySelector('.od-console')).toHaveAttribute('data-status', 'empty');
    expect(document.querySelector('.od-toast-name')).toBeNull();
    expect(document.querySelector('.od-slice')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 9. The extension is never orphaned onto a line of its own (issue #739).
//
// Round-3 review finding. `chunks()` offered a break after EVERY separator,
// the extension's own `.` included, so a name whose stem-plus-dot fitted the
// box while the whole name did not was painted as ["Amendment 2.", "docx"] —
// the last line carrying no `.docx` at all.
//
// The band this happens in is narrow, which is why the fixtures above missed
// it: SHORT, LONG, UNBROKEN, NARROW_26, WIDE_26 and WHITESPACE_NAMES never
// land in it, so nine `endsWith('.docx')` assertions passed over a hole. The
// names below are ordinary and short — the common case — and the sweep covers
// the band rather than only the two derived endpoints.
// ---------------------------------------------------------------------------

describe('issue #739 — the extension is never painted alone', () => {
  it('keeps the last line carrying `.docx` at both shipped widths', () => {
    for (const box of [DESKTOP, PHONE]) {
      for (const name of ORPHAN_PRONE) {
        const lines = fitIn(name, box);
        const last = lines[lines.length - 1];
        const where = `${JSON.stringify(name)} at ${box.width}px / ${box.font}px`;
        expect(last, `orphaned extension: ${where}`).not.toBe('docx');
        expect(last, `last line does not carry the extension: ${where}`).toMatch(
          /\.docx$/,
        );
        expectInside(lines, box);
      }
    }
  });

  it('holds at every console width the shell can produce, with no band skipped', () => {
    // The reachable range, derived the same way the two fixtures above are:
    // 300px is the narrowest console the shell allows and 1272px the widest.
    // Every width in between gets the box and the font that the shipped
    // stylesheet's own at-rules give it at that width.
    //
    // THE HOLE THAT USED TO BE HERE (issue #78 part 2). This loop carried a
    // `continue` for widths whose box could not hold `….docx` on a line of its
    // own, and it fired for 561–629px — never asserting anything there. Those
    // widths were not a documented floor, they were a bad model: the
    // derivation gave every non-phone console the `min-width: 1221px` step's
    // two-column grid and 20px type, while `orbit.css` actually collapses
    // `.od-appliances` to one 720px-capped column at `max-width: 1220px` and
    // sets the inscription at 18px. A 561px console really gets a 124px box at
    // 18px type, not 58px at 20px. With the derivation reading the rules that
    // apply, no width in the range is too narrow — so the escape hatch is gone
    // and its condition is asserted instead, which is what makes the band
    // covered rather than skipped.
    for (
      let consoleWidth = CONSOLE_MIN_PX;
      consoleWidth <= CONSOLE_MAX_PX;
      consoleWidth += 1
    ) {
      const box = inscriptionBox(consoleWidth);
      const measure = metricsAt(box.font);
      const where = `console ${consoleWidth}px (box ${box.width}px / ${box.font}px)`;
      expect(
        measure(`${FILENAME_ELLIPSIS}.docx`),
        `box too narrow for the ellipsis and the extension at ${where}`,
      ).toBeLessThanOrEqual(box.width);
      for (const name of ORPHAN_PRONE) {
        const lines = fitFilename(name, { measure, width: box.width });
        const last = lines[lines.length - 1];
        expect(last, `orphaned extension: ${JSON.stringify(name)} at ${where}`).not.toBe(
          'docx',
        );
        expect(
          last,
          `last line does not carry the extension: ${JSON.stringify(name)} at ${where}`,
        ).toMatch(/\.docx$/);
      }
    }
  });

  it('still breaks a name that has no extension exactly as before', () => {
    const lines = fitIn('Mutual NDA draft-v3', DESKTOP);
    expectInside(lines, DESKTOP);
    expectWhole(lines, 'Mutual NDA draft-v3');
  });
});
