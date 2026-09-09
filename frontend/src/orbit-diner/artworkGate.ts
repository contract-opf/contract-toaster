/**
 * artworkGate.ts — which toaster plate this console asks for, whether the
 * plates its controls sit on have arrived, and what it may show until they
 * have (issue #738, N4 and N5).
 *
 * WHY A GATE AT ALL. Every control in the illustrated scene is positioned
 * against a photographed appliance: the lever, the dial and the slot label sit
 * on `toaster*.webp`, the price, the intensity and footnote keys and the
 * receipt printer sit on `register.webp`, and the instructions textarea sits
 * in `pad.webp`. Painting those controls before their surface exists puts them
 * at scene coordinates with nothing behind them — the "controls floating at
 * unfinished scene coordinates" N4 forbids. So the console renders the PLAIN
 * layout, which is fully operable and owes nothing to the artwork, and swaps
 * to the scene only once the plates it actually needs have decoded.
 *
 * WHICH PLATES ARE ESSENTIAL. The four appliance plates plus the slice, and
 * exactly ONE toaster variant. `counter.webp` is deliberately not in the set:
 * it is the space background behind the whole scene, no control is placed
 * against it, and N5 says a counter that never arrives may simply leave the
 * neutral counter colour showing. `.od-scene` already declares that colour
 * under the image, so a failed counter needs no code at all.
 *
 * ONE VARIANT, NEVER BOTH. `toaster.webp` and `toaster-phone.webp` are 221 KB
 * and 233 KB of the same appliance; fetching both to use one is the waste this
 * ticket exists to prevent. The variant comes from the console's OWN content
 * width (issue #725) — never `window.innerWidth`, which inside `ct-app-shell`
 * is always the wider number and would pick the desktop plate for a console
 * that is on its phone step. An UNMEASURED console (width 0: first paint, a
 * hidden tabpanel, a host with no ResizeObserver) has no variant, and this
 * gate fetches nothing at all for it rather than guessing and then correcting
 * itself with a second fetch. `consoleSize` documents the same rule from the
 * layout side.
 *
 * NO TIMERS. N5's retry is a control the reviewer presses, not a backoff loop:
 * a plate that 404s would otherwise be re-requested forever behind a layout
 * that is already working. `retry()` is the only thing that starts another
 * attempt, and `orbit-diner-artwork.test.tsx` pins the absence of a timer in
 * this file.
 *
 * WHY `decode()` AND NOT `onload`. `decode()` settles when the plate is
 * decoded and paintable, which is the moment the scene may be shown, and it
 * rejects on exactly the failures N5 is about. It is also the honest
 * capability probe: a host that does not implement it (jsdom, any non-visual
 * renderer) has no image pipeline, so no plate there can ever load OR fail.
 * Gating on an answer that cannot arrive would strand such a host in the plain
 * layout permanently, which is why `canObservePlates()` is checked before
 * anything else and reports the console ready with nothing fetched. That
 * fallback decides nothing about a browser, where `decode()` has been
 * available for years and the gate runs in full.
 */
import { useCallback, useEffect, useState } from 'react';
import { consoleSize } from './consoleWidth';
import { platePath, type PlateName } from './plates';

/** Which photographed toaster the console is showing. */
export type ToasterVariant = 'desktop' | 'phone';

/**
 * `pending` — nothing to show yet, render the plain layout.
 * `ready` — the plates for `variant` are decoded; the scene may be shown.
 * `failed` — an essential plate did not arrive; render the plain layout and
 * offer the retry (N5).
 */
export type ArtworkStatus = 'pending' | 'ready' | 'failed';

export interface ArtworkGate {
  status: ArtworkStatus;
  /**
   * The variant that is SAFE to render right now — the last one fully
   * decoded, not necessarily the one the current width wants. A console that
   * has just been narrowed keeps showing the desktop plate it already has
   * until the phone plate is in, rather than blanking the scene mid-review.
   */
  variant: ToasterVariant;
  /** Start one more attempt at the current variant. Never called on a timer. */
  retry: () => void;
}

/**
 * The toaster variant for a console content width, or `null` when the console
 * has not been measured yet.
 *
 * `null` rather than a default, because the two ways of guessing are both
 * wrong: `width <= 560` reads an unmeasured 0 as a phone (issue #725 forbids
 * exactly that), and defaulting to desktop fetches 221 KB that a phone-width
 * console then has to follow with the other 233 KB.
 */
export function toasterVariant(width: number): ToasterVariant | null {
  if (width <= 0) return null;
  // Asked through `consoleSize` so this and the stylesheet can never hold two
  // opinions about where the phone step starts.
  return consoleSize(width) === 'phone' ? 'phone' : 'desktop';
}

/**
 * The plates a control's own surface depends on, for one toaster variant.
 * Order is not significant; the set is.
 */
export function essentialPlates(variant: ToasterVariant): PlateName[] {
  return [
    variant === 'phone' ? 'toaster-phone' : 'toaster',
    'register',
    'pad',
    'dial',
    'toast',
  ];
}

/**
 * Whether this host can tell the console that a plate arrived or failed.
 *
 * Constructing an `Image` fetches nothing until a `src` is assigned, so this
 * probe costs one detached element and no request.
 */
function canObservePlates(): boolean {
  if (typeof Image !== 'function') return false;
  try {
    return typeof new Image().decode === 'function';
  } catch {
    return false;
  }
}

/**
 * Load the essential plates for the variant `consoleWidth` selects, and report
 * whether the scene may be shown.
 *
 * Re-runs only when the wanted variant changes, when `base` changes, or when
 * `retry()` bumps the attempt — so a resize INSIDE one step (342px to 500px,
 * both phone) requests nothing.
 */
export function useArtworkGate(consoleWidth: number, base?: string): ArtworkGate {
  const wanted = toasterVariant(consoleWidth);
  const [loaded, setLoaded] = useState<ToasterVariant | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    // No image pipeline here (see the header): nothing can be fetched, nothing
    // can fail, and holding the console in the plain layout would be waiting
    // for an answer that cannot come. Nothing is requested on this path.
    if (!canObservePlates()) {
      setLoaded(wanted ?? 'desktop');
      setFailed(false);
      return undefined;
    }
    if (wanted === null) return undefined;
    let cancelled = false;
    const decoding: Promise<unknown>[] = [];
    for (const plate of essentialPlates(wanted)) {
      const image = new Image();
      image.src = platePath(plate, base);
      decoding.push(image.decode());
    }
    void Promise.all(decoding).then(
      () => {
        if (cancelled) return;
        setLoaded(wanted);
        setFailed(false);
      },
      () => {
        // N5. One essential plate is enough to lose the scene, and the console
        // falls back to the layout it was already showing before the switch.
        // Nothing about the review is touched: no status, no poll, no message.
        if (!cancelled) setFailed(true);
      },
    );
    // The in-flight decodes are abandoned, not aborted: a fetch already on the
    // wire is cheaper to let finish into the browser cache than to cancel, and
    // `cancelled` keeps its answer from reaching a console that has moved on.
    return () => {
      cancelled = true;
    };
  }, [wanted, base, attempt]);

  const retry = useCallback(() => {
    // Clear the failure first so the notice goes away the moment the reviewer
    // presses the key; it comes back if this attempt fails too.
    setFailed(false);
    setAttempt((current) => current + 1);
  }, []);

  return {
    status: failed ? 'failed' : loaded ? 'ready' : 'pending',
    variant: loaded ?? 'desktop',
    retry,
  };
}
