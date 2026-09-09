/**
 * filename.ts — the inscription burnt into the bread (issue #739, epic #729).
 *
 * The 04p2 artwork places the name in the slice's broad central face
 * (`.od-console:not(.od-plain) .od-toast-name`: left/right 20%, top 40%,
 * `max-height: 24%`) at 20 / 18 / 14 px by console step. The designer supplied
 * the box and the sizes and explicitly did NOT supply the text algorithm. This
 * module is that algorithm, and it holds three promises:
 *
 *   1. TWO LINES AT MOST, at the size the stylesheet asks for. Nothing here
 *      shrinks type — the floor is 14 px (#600, enforced by `audit:layout`) —
 *      so a name that cannot fit is SHORTENED, never set smaller.
 *   2. MEASURED, never counted. `Mutual NDA draft-v3.docx` and
 *      `WWWWWWWWWWWWWWWWWWWWWWWW` are both 24 characters and are nowhere near
 *      the same width in Space Grotesk. Every decision below comes from
 *      `measure()`, which in the browser is a canvas `measureText` primed with
 *      the element's own computed font.
 *   3. THE START AND THE EXTENSION SURVIVE. A truncated name still has to say
 *      which document this is, so the head is kept, `.docx` is kept, and the
 *      stem's final segment (a `v7`-style version token) is kept too when it
 *      fits — `Mutual-NDA-counterparty-pa…v7.docx`.
 *
 * WHY IT EMITS HARD LINE BREAKS. The lines this module computes are joined
 * with `\n` and rendered into one `white-space: pre-wrap` element, so the
 * browser is never asked to reproduce our wrapping decisions. That matters:
 * the CSS backstop is `-webkit-line-clamp: 2`, which cuts the LAST line, which
 * is exactly where `.docx` lives — a browser that wrapped one line further
 * than we predicted would silently eat the extension. With the breaks already
 * in the string the browser only has to fit each line, which `measure()` has
 * already proven it can. The clamp stays as a backstop for the impossible
 * case; it is not the mechanism.
 *
 * WHICH IS WHY THE NAME IS NORMALIZED FIRST. `pre-wrap` preserves every
 * whitespace character, so a `\n`, `\r`, `\t`, U+2028 or U+2029 ALREADY in
 * the filename would paint as a forced break of its own — one this module
 * never counted, because `measureText` is blind to them, and one that pushes
 * `.docx` onto a third line for the clamp to eat. A POSIX filename may hold
 * any of them (`projection.ts` takes `file.name` verbatim; the backend only
 * strips the ends), so `normalizeFilename` collapses whitespace runs to a
 * single space and trims the ends before anything is measured or painted.
 * The break decisions stay this module's alone. The whole, untouched name
 * still goes to the accessible name, the tooltip, the receipt and History.
 *
 * The accessible name, the tooltip, the receipt and History are untouched:
 * they carry the whole filename, always. This is a painted inscription.
 */
import { useLayoutEffect, useState } from 'react';

/** Lines the inscription may occupy. Paired with `-webkit-line-clamp` in
 *  `orbit.css`; they are one decision, not two opinions. */
export const FILENAME_MAX_LINES = 2;

/** The ellipsis stands for the removed middle of the name. */
export const FILENAME_ELLIPSIS = '…';

/**
 * `clientWidth` is an integer, rounded from a fractional layout width, so it
 * can report up to half a pixel more room than the box really has. One pixel
 * of margin costs nothing and keeps a line that measures exactly full from
 * wrapping on the device that rounded the other way.
 */
const WIDTH_MARGIN_PX = 1;

/**
 * A tail is only worth keeping while it stays a tail. Past this share of the
 * whole inscription it stops being "…v7.docx" and starts eating the head,
 * which is the part that says which document this is. Measured against the
 * real budget, never counted — `…-EXECUTION-COPY.docx` and `…-v7.docx` are
 * different widths at the same character count.
 */
const TAIL_SHARE_OF_BUDGET = 0.35;

/**
 * The name as it may be painted into a `white-space: pre-wrap` box: one space
 * for every run of whitespace, and no whitespace at either end.
 *
 * `\s` is the whole set on purpose — `\n`, `\r`, `\t`, the vertical tab, the
 * form feed, U+2028, U+2029, NBSP and the rest — because the box preserves all
 * of them and CSS treats several as forced breaks. Leading whitespace matters
 * too: the inscription is `text-align: center`, so three preserved spaces
 * shove the name off the centre of the bread.
 */
export const normalizeFilename = (name: string): string => name.replace(/\s+/g, ' ').trim();

/** Measures a string's advance width in CSS px at the inscription's font. */
export type MeasureText = (text: string) => number;

export interface FitOptions {
  /** Advance width of a string at the rendered font. */
  measure: MeasureText;
  /** Usable width of the inscription box, in CSS px. */
  width: number;
  /** Lines available. Defaults to `FILENAME_MAX_LINES`. */
  maxLines?: number;
}

/**
 * The trailing `.docx` — or whatever this document's extension is.
 *
 * Bounded to a short alphanumeric run so a dotted stem (`v1.2 terms`) does not
 * hand back `.2 terms` as an "extension" and pin the whole thing to the end of
 * the name. That bound is on the SUFFIX PATTERN, not on the name: nothing here
 * limits how long a filename may be.
 */
export function splitExtension(name: string): { stem: string; extension: string } {
  const match = /\.[A-Za-z0-9]{1,10}$/.exec(name);
  if (!match || match.index <= 0) return { stem: name, extension: '' };
  return { stem: name.slice(0, match.index), extension: match[0] };
}

/**
 * Break the text into the chunks a line may end after: a word plus the spaces
 * that follow it, and the run up to and including a `-`, `_`, `.` or `/`.
 *
 * These are preferences, not correctness. Because the chosen breaks are
 * emitted as real newlines, a chunk boundary the browser would not have taken
 * on its own costs nothing — every line is measured before it is kept.
 *
 * ONE BREAK IS NOT A PREFERENCE. The `.` that begins the extension is a
 * separator like any other to the loop below, so `Amendment 2.docx` would
 * offer a break after `Amendment 2.` and let the extension be painted alone on
 * the next line. `glued()` takes that one opportunity away; see its note.
 */
function chunks(text: string): string[] {
  const parts: string[] = [];
  let current = '';
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    const next = text[i + 1];
    current += char;
    if (next === undefined) break;
    const space = /\s/.test(char);
    if (space ? !/\s/.test(next) : '-_./'.includes(char)) {
      parts.push(current);
      current = '';
    }
  }
  if (current) parts.push(current);
  return parts;
}

/**
 * The break opportunities for a FILENAME: `chunks()`, minus the one at the
 * extension's own dot.
 *
 * The stem is chunked on its own and the extension is welded onto the last
 * chunk, so no line can end between them and the last painted line always
 * carries `.docx`. That is the promise this module's header makes and the
 * one issue #739 asks for, and it has to hold on the whole-name path too:
 * `fitFilename` returns `wrapMeasured(text)` unchanged whenever it already
 * fits `maxLines`, so a short ordinary name never reaches the ellipsis path
 * where `protectedTail` would otherwise have defended the suffix.
 *
 * A name with no extension is chunked exactly as before.
 *
 * The welded chunk can end up wider than the box. That is not a regression:
 * `wrapMeasured` breaks inside an over-wide chunk, which is what it already
 * did for one long unbroken word, and `fitFilename` shortens the head until
 * the ellipsis and the suffix fit.
 */
function glued(text: string): string[] {
  const { stem, extension } = splitExtension(text);
  if (extension === '' || stem === '') return chunks(text);
  const parts = chunks(stem);
  if (parts.length === 0) return [extension];
  parts[parts.length - 1] += extension;
  return parts;
}

/** Trailing spaces hang at a line break; they never count against the width. */
const trimEnd = (text: string): string => text.replace(/\s+$/, '');

/**
 * `text` cut to `length`, moved back off the second half of a surrogate pair.
 *
 * `slice` counts UTF-16 code units and the search below counts in the same
 * units, so a cut can land between the two halves of one character. Half an
 * emoji is a .notdef box, and the head is the part that says which document
 * this is — one code unit of head is the cheap thing to give up.
 */
function cutAt(text: string, length: number): string {
  const splitsPair =
    length > 0 &&
    length < text.length &&
    (text.charCodeAt(length) & 0xfc00) === 0xdc00 &&
    (text.charCodeAt(length - 1) & 0xfc00) === 0xd800;
  return text.slice(0, splitsPair ? length - 1 : length);
}

/**
 * Greedy wrap of `text` into lines no wider than `width`, breaking at the
 * chunk boundaries above and — when a single chunk is wider than the whole
 * line — inside the chunk, which is what `overflow-wrap: anywhere` does.
 *
 * Returns at least one line, and may return more than `maxLines`: the count is
 * the answer `fitFilename` needs, so this reports it rather than hiding it.
 */
export function wrapMeasured(text: string, { measure, width }: FitOptions): string[] {
  const fits = (candidate: string): boolean => measure(trimEnd(candidate)) <= width;
  // `glued()` keeps a LINE from ending at the extension's dot. This keeps a
  // CHARACTER break from landing inside the extension, which is the same
  // orphan by another route: `EIAA….docx` in a box that holds six characters
  // would otherwise be cut as `EIAA….` + `docx`. Treating the suffix as one
  // atom moves the cut to the dot instead, so the last line reads `.docx`.
  const { extension } = splitExtension(text);
  const lines: string[] = [];
  let line = '';
  for (const chunk of glued(text)) {
    let rest = chunk;
    while (rest !== '') {
      if (fits(line + rest)) {
        line += rest;
        break;
      }
      if (line !== '') {
        // A line of nothing but the spaces that preceded an over-wide chunk is
        // not a line; dropping it spends the room on the name instead.
        const flushed = trimEnd(line);
        if (flushed !== '') lines.push(flushed);
        line = '';
        continue;
      }
      // The chunk does not fit even on an empty line, so it is broken inside.
      // The break walks CHARACTERS, not UTF-16 code units: `overflow-wrap:
      // anywhere`, which this branch reproduces, never splits a character, and
      // a cut between the halves of a surrogate pair paints two .notdef boxes
      // where one emoji belongs. At least one character is always taken, so
      // this cannot spin.
      const points = Array.from(rest);
      let take = points[0].length;
      for (let i = 1; i < points.length; i += 1) {
        if (!fits(rest.slice(0, take + points[i].length))) break;
        take += points[i].length;
      }
      // Never cut inside the extension. The boundary is a code-point boundary
      // (the suffix begins at its `.`), and it is only imposed when there is
      // something in front of the suffix to keep — a box too narrow for the
      // suffix alone has no better answer and breaks as it always did.
      const suffixStart = rest.length - extension.length;
      if (extension !== '' && suffixStart > 0 && rest.endsWith(extension) && take > suffixStart) {
        take = suffixStart;
      }
      lines.push(trimEnd(rest.slice(0, take)));
      rest = rest.slice(take);
    }
  }
  if (line !== '') lines.push(trimEnd(line));
  return lines.length > 0 ? lines : [''];
}

/**
 * The tail worth protecting from the truncation: the extension, preceded by
 * the stem's last segment when that segment still leaves the head most of the
 * inscription. `Mutual-NDA-counterparty-package-v7.docx` keeps `v7.docx`,
 * because two long names from the same matter differ only there; a stem with
 * no separator at all has no such segment and keeps the extension alone.
 */
function protectedTail(
  stem: string,
  extension: string,
  { measure, width }: FitOptions,
  maxLines: number,
): { head: string; tail: string } {
  const cut = Math.max(
    stem.lastIndexOf(' '),
    stem.lastIndexOf('-'),
    stem.lastIndexOf('_'),
    stem.lastIndexOf('.'),
  );
  const segment = cut >= 0 ? stem.slice(cut + 1) : '';
  const candidate = FILENAME_ELLIPSIS + segment + extension;
  if (segment !== '' && measure(candidate) <= width * maxLines * TAIL_SHARE_OF_BUDGET) {
    return { head: stem.slice(0, cut + 1), tail: candidate };
  }
  return { head: stem, tail: FILENAME_ELLIPSIS + extension };
}

/**
 * The lines to paint for `name`, at most `maxLines` of them.
 *
 * Whole when it fits. Otherwise the longest head that still leaves the tail on
 * screen, found by measuring — never by counting characters, and never by
 * shrinking the type.
 *
 * The name is normalized before anything else happens, so the only line breaks
 * in the result are the ones measured here. Every return path below goes
 * through the normalized text, the zero-width and empty ones included: those
 * are painted into the same `pre-wrap` box as the rest.
 */
export function fitFilename(name: string, options: FitOptions): string[] {
  const maxLines = options.maxLines ?? FILENAME_MAX_LINES;
  const text = normalizeFilename(name);
  if (text === '' || options.width <= 0) return [text];

  const whole = wrapMeasured(text, options);
  if (whole.length <= maxLines) return whole;

  const { stem, extension } = splitExtension(text);
  const { head, tail } = protectedTail(stem, extension, options, maxLines);
  const attempt = (length: number): string[] =>
    wrapMeasured(trimEnd(cutAt(head, length)) + tail, options);

  // Binary search the longest head that still fits. Greedy wrapping is very
  // nearly monotonic in the head's length but not provably so, so the answer
  // is the longest length actually VERIFIED to fit rather than whatever the
  // search's last probe happened to be.
  let best: string[] | null = null;
  let low = 0;
  let high = head.length;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const lines = attempt(middle);
    if (lines.length <= maxLines) {
      best = lines;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  // Nothing fits, not even the tail alone: the box is narrower than one
  // ellipsis and an extension. Show the start of the name and let the CSS
  // clamp be what it is — no width can honour the promise here.
  return best ?? whole.slice(0, maxLines);
}

/** One canvas for the whole console; a fresh element per measurement would
 *  allocate a backing store per keystroke of a resize. */
let scratch: HTMLCanvasElement | null = null;

/**
 * A measurer at `node`'s own rendered font, or `null` where the host cannot
 * measure text — jsdom has no 2D context, and a console that cannot be
 * measured must paint the whole name rather than guess at a shortening.
 *
 * The font is read from the element, not restated here, so the 20 / 18 / 14 px
 * steps and the family stay the stylesheet's business alone.
 */
export function measureIn(node: HTMLElement): MeasureText | null {
  let context: CanvasRenderingContext2D | null = null;
  try {
    scratch ??= document.createElement('canvas');
    context = scratch.getContext('2d');
  } catch {
    return null;
  }
  if (!context) return null;
  const ctx = context;
  const style = getComputedStyle(node);
  const family = style.fontFamily || 'sans-serif';
  const size = style.fontSize || '16px';
  ctx.font =
    style.font && style.font.trim() !== ''
      ? style.font
      : `${style.fontStyle || 'normal'} ${style.fontWeight || '400'} ${size} ${family}`;
  const measured = new Map<string, number>();
  return (text: string): number => {
    const cached = measured.get(text);
    if (cached !== undefined) return cached;
    const width = ctx.measureText(text).width;
    measured.set(text, width);
    return width;
  };
}

/**
 * The string to paint inside `node` for `filename`: the fitted lines joined
 * with `\n`, for a `white-space: pre-wrap` box.
 *
 * `node` arrives from a callback ref, so the fit runs when the slice mounts
 * rather than only when the console does — the inscription does not exist
 * until a document is chosen.
 *
 * NO SECOND LAYOUT OBSERVER (#725). The inscription's box is a chain of
 * percentages off the console's own width — scene padding, appliance track,
 * slice, then the 20/20 inset — so there is no resize that moves this box
 * without moving the console, and `revision` (the console width the one
 * observer already publishes) is the whole trigger. A private
 * `ResizeObserver` here would be a second reading of the same fact, and the
 * order in which the two were constructed would decide which one a test — or
 * a polyfill — saw first.
 *
 * `revision` is therefore the caller's whole statement of "the box may have
 * moved", and it has to carry more than the observed width: it has to carry
 * anything that changes whether the box is LAID OUT AT ALL. The plain toggle
 * is that second thing. `.od-plain .od-slice` is `display: none`, so a
 * document chosen under the plain controls mounts an inscription whose
 * `clientWidth` is 0 and gets the whole name by the fallback below; switching
 * back to illustrated swaps one class on the console root, leaving the node,
 * the filename and the console's width all untouched. Combine the inputs into
 * a primitive — a fresh array each render would re-fit on every render.
 *
 * Fonts are the one input that changes without a resize: the first paint may
 * measure Arial while Space Grotesk is still loading, and every advance width
 * moves when it swaps in. So the fit runs again once the document's faces are
 * ready, on hosts that report it.
 *
 * Before the first measurement, and on any host that cannot measure, this is
 * the complete filename — normalized, because the unmeasured fallback is
 * painted into the same `pre-wrap` box and a `\n` in the name would break it
 * onto a third line there too, where no measurement exists to notice. That is
 * the honest fallback: too long, never wrong.
 */
export function useFittedFilename(
  node: HTMLElement | null,
  filename: string,
  revision?: unknown,
): string {
  const [fitted, setFitted] = useState(() => normalizeFilename(filename));
  useLayoutEffect(() => {
    const text = normalizeFilename(filename);
    if (!node || text === '') {
      setFitted(text);
      return undefined;
    }
    let live = true;
    const fit = (): void => {
      if (!live) return;
      const width = node.clientWidth - WIDTH_MARGIN_PX;
      const measure = width > 0 ? measureIn(node) : null;
      const next = measure
        ? fitFilename(text, { measure, width }).join('\n')
        : text;
      setFitted((current) => (current === next ? current : next));
    };
    fit();
    void document.fonts?.ready.then(fit).catch(() => undefined);
    return () => {
      live = false;
    };
  }, [node, filename, revision]);
  return fitted;
}
