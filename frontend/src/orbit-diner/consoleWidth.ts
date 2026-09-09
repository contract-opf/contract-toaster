/**
 * consoleWidth.ts — the ONE layout observer for the Orbit Diner console
 * (issue #725).
 *
 * The console's own content width is the breakpoint for everything it does:
 * `orbit.css` asks it through `@container od-console (…)`, and the toaster
 * plate variant (#738) asks it through this module. Nothing may ask
 * `window.innerWidth` instead. The console sits inside `ct-app-shell`, which
 * caps at `--ct-maxw` (1320px) and spends 24px of padding per side, so the
 * viewport is ALWAYS wider than the console — at a 1600px window the console
 * is 1272px. A viewport reading therefore switches the layout, and would pick
 * the plate, at the wrong moment, and two places reading it separately can
 * disagree with each other and with the stylesheet.
 *
 * The measurement is the console's CONTENT box, which is exactly what a
 * container query on `.od-console` resolves against (that element carries no
 * padding and no border), so the attribute this feeds and the `@container`
 * rules always agree about which step is in force.
 *
 * The thresholds below are the same numbers `orbit.css` uses. Keep them in
 * step: they are a pair, not two independent opinions.
 */
import { useEffect, useState, type RefObject } from 'react';

/** At or below this content width the console is on its phone step. */
export const CONSOLE_PHONE_MAX_PX = 560;
/** At or below this content width the two appliances stack (owner A2). */
export const CONSOLE_STACK_MAX_PX = 1220;

export type ConsoleSize = 'phone' | 'narrow' | 'wide';

/**
 * The step a given console content width is in.
 *
 * Width `0` means "not measured yet" — first paint, a hidden tabpanel, or a
 * host without `ResizeObserver` — and resolves to `wide`, never `phone`. An
 * unmeasured console must not request the phone plate (#738) or announce a
 * phone arrangement it may never be in; `wide` is the layout the console
 * already renders before any observation arrives.
 */
export function consoleSize(width: number): ConsoleSize {
  if (width <= 0) return 'wide';
  if (width <= CONSOLE_PHONE_MAX_PX) return 'phone';
  if (width <= CONSOLE_STACK_MAX_PX) return 'narrow';
  return 'wide';
}

/**
 * Observe `ref`'s content width. Returns 0 until the first measurement lands.
 *
 * Rounded to whole pixels so a sub-pixel reflow cannot loop the component
 * through a render per frame, and set through a functional update that keeps
 * the previous value when nothing changed, so an observer firing on a
 * height-only change is inert.
 */
export function useConsoleWidth(ref: RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const measure = (next: number): void => {
      const rounded = Math.round(next);
      setWidth((current) => (current === rounded ? current : rounded));
    };
    // `clientWidth` is the content box, matching what the observer reports.
    // Taken first so a host without ResizeObserver (jsdom, an old browser)
    // still gets one honest reading instead of staying at 0 forever.
    measure(el.clientWidth);
    if (typeof ResizeObserver !== 'function') return undefined;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[entries.length - 1];
      if (!entry) return;
      const box = entry.contentBoxSize?.[0];
      measure(box ? box.inlineSize : entry.contentRect.width);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}
