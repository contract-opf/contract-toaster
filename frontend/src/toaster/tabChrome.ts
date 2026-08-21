/**
 * tabChrome.ts — favicon browning + tab-title theater (issue #497).
 *
 * The wait is minutes long and the review keeps running when the reviewer
 * tabs away — `sounds.ts`'s ticking already says so out loud, but nothing
 * upstream of this file said so to the EYE for someone who tabbed away, or
 * restored the tab to something honest when they tab back. This module is
 * the two structural things that fix that:
 *
 *   1. `document.title` mirrors the same `stageTheater.ts` vignette the
 *      hero's glass shows (#496) — specifically its `caption`, whose own
 *      doc comment already earmarks it for exactly this ("The plain-
 *      language sentence shown under the glass AND IN THE TAB TITLE"),
 *      written when #496 landed, ahead of this ticket. Every caption
 *      already ends in an ellipsis, which is also why the honest "no
 *      reported stage" fallback below is `'Toasting…'` rather than a bare
 *      "Toasting" — it reads as one family with the four real ones, not a
 *      differently-voiced placeholder.
 *   2. The favicon browns through the same four frames (`faviconFrames.ts`),
 *      keyed by the identical stage token — one map, so the glass, the tab
 *      title, and the tab icon can never disagree about what stage a review
 *      is in.
 *
 * ## Terminal state: title restores immediately, the favicon may not
 *
 * The title always restores the instant a review goes terminal — there is
 * nothing left to narrate once the pipeline stops, and a tab titled
 * "Writing your redline…" after it is done is a small lie. The favicon is
 * different on purpose: if the tab was HIDDEN at the moment a review finished
 * or failed, the reviewer cannot see the result panel either, so the icon
 * becomes a ✓/! badge (which the ding in `ReviewSubmission.tsx` accompanies)
 * and STAYS a badge until the tab is focused again — that is the entire
 * point of a badge. If the tab was already visible when the review went
 * terminal, the reviewer is looking at the real result already and the
 * favicon goes straight back to the static default; badging a tab someone
 * is already looking at would just be noise.
 *
 * ## Never on a timer
 *
 * Exactly like `stageTheater.ts` and `motion.ts`'s state chart: every change
 * here is driven by a real `phase`/`stage` transition the caller passes in,
 * never by an interval. The one thing that runs on its own is the
 * focus/visibility listener that clears a terminal badge, and it is removed
 * — not left running — the moment it fires or the effect re-runs for any
 * other reason (a leak here would mean a review five toasts ago still
 * quietly holding a `focus` listener hostage).
 */
import { useEffect } from 'react';
import { vignetteForStage } from './stageTheater';
import { FAVICON_BADGE_DONE, FAVICON_BADGE_FAILED, FAVICON_STAGE_FRAMES } from './faviconFrames';
import type { ToasterPhase } from './Toaster';

// ---------------------------------------------------------------------------
// Tab title
// ---------------------------------------------------------------------------

/** Captured once, on the first override — by then `App.tsx` has already set
 *  `document.title` to the configured product name (issue #274), so this
 *  needs no import of its own to know what to restore and stays correct
 *  under a `VITE_PRODUCT_NAME` override without being told the value. */
let originalTitle: string | null = null;

function baseTitle(): string {
  if (originalTitle === null) {
    originalTitle = typeof document !== 'undefined' ? document.title : '';
  }
  return originalTitle;
}

/**
 * `setTabTitle('Toasting…')` -> "Toasting… — Contract Toaster";
 * `setTabTitle(null)` restores the title this module found before its first
 * override. Exported for the leak/inert tests; ordinary callers only need
 * `useTabTheater` below.
 */
export function setTabTitle(prefix: string | null): void {
  if (typeof document === 'undefined') return;
  const base = baseTitle();
  document.title = prefix ? `${prefix} — ${base}` : base;
}

// ---------------------------------------------------------------------------
// Favicon
// ---------------------------------------------------------------------------

const ICON_LINK_SELECTOR = 'link[rel="icon"]';

interface CapturedIcon {
  el: HTMLLinkElement;
  href: string;
  type: string | null;
}

let capturedIcons: CapturedIcon[] | null = null;

function icons(): CapturedIcon[] {
  if (capturedIcons) return capturedIcons;
  if (typeof document === 'undefined') return (capturedIcons = []);
  capturedIcons = Array.from(document.querySelectorAll<HTMLLinkElement>(ICON_LINK_SELECTOR)).map(
    (el) => ({ el, href: el.getAttribute('href') ?? '', type: el.getAttribute('type') }),
  );
  return capturedIcons;
}

/**
 * `applyFaviconFrame('data:image/png;base64,...')` repoints EVERY existing
 * `<link rel="icon">` at the same data URI (index.html ships two — an SVG and
 * an .ico, and a browser that prefers the SVG one would otherwise keep
 * showing the static art the whole time a PNG frame swap only touched the
 * .ico link). `applyFaviconFrame(null)` restores each link's original
 * `href`/`type` exactly, captured the first time this module ever touches
 * the DOM.
 */
export function applyFaviconFrame(dataUri: string | null): void {
  for (const icon of icons()) {
    if (dataUri) {
      icon.el.setAttribute('href', dataUri);
      icon.el.setAttribute('type', 'image/png');
    } else {
      icon.el.setAttribute('href', icon.href);
      if (icon.type) icon.el.setAttribute('type', icon.type);
      else icon.el.removeAttribute('type');
    }
  }
}

export function restoreFavicon(): void {
  applyFaviconFrame(null);
}

function tabHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden';
}

// ---------------------------------------------------------------------------
// The hook — the one seam `ReviewSubmission.tsx` calls.
// ---------------------------------------------------------------------------

/**
 * Drives the tab title and favicon off the SAME `phase`/`stage` pair the
 * hero (`ToasterHero`) already renders from, so this can never show a
 * different story than the appliance on screen.
 *
 * `idle`/`loaded` (AC: "both inert when no review is running"): title and
 * favicon are restored and stay that way — nothing here ever fires with
 * nothing to report.
 */
export function useTabTheater(phase: ToasterPhase, stage: string | null): void {
  useEffect(() => {
    if (phase === 'working') {
      const vignette = vignetteForStage(stage);
      setTabTitle(vignette ? vignette.caption : 'Toasting…');
      applyFaviconFrame(vignette ? FAVICON_STAGE_FRAMES[vignette.token] : null);
      return undefined;
    }

    if (phase === 'done' || phase === 'error') {
      // The title's job ends the instant the review does — see the module
      // docstring on why this is unconditional while the favicon below is
      // not.
      setTabTitle(null);

      if (!tabHidden()) {
        restoreFavicon();
        return undefined;
      }

      applyFaviconFrame(phase === 'done' ? FAVICON_BADGE_DONE : FAVICON_BADGE_FAILED);

      const clearBadge = (): void => {
        if (document.visibilityState === 'visible') {
          restoreFavicon();
          cleanup();
        }
      };
      const onFocus = (): void => {
        restoreFavicon();
        cleanup();
      };
      function cleanup(): void {
        window.removeEventListener('focus', onFocus);
        document.removeEventListener('visibilitychange', clearBadge);
      }
      window.addEventListener('focus', onFocus);
      document.addEventListener('visibilitychange', clearBadge);
      return cleanup;
    }

    // idle / loaded — no review in flight; nothing should be overridden.
    setTabTitle(null);
    restoreFavicon();
    return undefined;
  }, [phase, stage]);

  // Belt-and-suspenders: a caller that unmounts mid-review (navigating away
  // from the SPA entirely, or a test tearing down) must never leave a stale
  // title/icon behind for whatever renders next, regardless of which branch
  // above was active when it happened.
  useEffect(() => {
    return () => {
      setTabTitle(null);
      restoreFavicon();
    };
  }, []);
}
