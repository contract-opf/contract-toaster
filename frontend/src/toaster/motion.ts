/**
 * Motion vocabulary and toaster state chart (issue #502, graphics phase 2).
 *
 * Every delight ticket (#494 lever, #496 theater, #497 favicon, #498 receipt,
 * #500 tray, #501 pop/steam/burnt) drives the hero through THIS module rather
 * than calling `element.animate` itself. That is the whole point: the rails
 * below — reduced motion, hidden tab, forced colors, no layout animation —
 * are enforced once, centrally, instead of being re-remembered at every call
 * site. A rail that each caller has to opt into is a rail that one caller
 * eventually forgets.
 *
 * It is also the replacement for the rejected Rive evaluation (decision
 * recorded in #493): a state machine written as a TypeScript union and a
 * transition table is reviewable in a diff and editable by anyone with a text
 * editor, which a `.riv` binary is not.
 *
 * ## What this module deliberately does NOT do
 *
 * - Decide anything. The chart is driven by real submission/poll state; it
 *   never advances on a timer, exactly as `StagedDoneness` never does.
 * - Animate layout. Every helper touches `transform`, `opacity` or `filter`
 *   only. Nothing here can trigger reflow in a loop.
 * - Own the DOM. Callers pass elements in; this module never queries for
 *   them, so two heroes on one page cannot collide (the reason #493's parts
 *   carry `data-part` and per-instance ids).
 */

// ---------------------------------------------------------------------------
// The state chart
// ---------------------------------------------------------------------------

/**
 * `toasting` carries the real `progress_stage` token so the theater (#496)
 * can vary a vignette per sub-stage without a second source of truth. `null`
 * is legal and means "running, but this deployment reports no stage" — the
 * same honest fallback `StagedDoneness` makes.
 */
export type ToasterState =
  | { readonly name: 'idle' }
  | { readonly name: 'loaded' }
  | { readonly name: 'toasting'; readonly stage: string | null }
  | { readonly name: 'pop' }
  | { readonly name: 'burnt' };

export type ToasterEvent =
  | { readonly type: 'file_selected' }
  | { readonly type: 'file_cleared' }
  | { readonly type: 'submit' }
  /** Reattaching to a review that was already running when the page loaded
   *  (#489). Without this the chart would have to lie and pretend a submit
   *  just happened, which would fire the lever ritual on a page refresh. */
  | { readonly type: 'resume'; readonly stage: string | null }
  | { readonly type: 'stage'; readonly stage: string | null }
  | { readonly type: 'done' }
  | { readonly type: 'error' }
  /** CANCELLED is not an error and must not burn the toast — it returns the
   *  appliance to rest. See backend/src/reviews.py's terminal statuses. */
  | { readonly type: 'cancel' }
  | { readonly type: 'reset' };

export class IllegalTransitionError extends Error {
  constructor(from: ToasterState['name'], event: ToasterEvent['type']) {
    super(`toaster state chart: no transition from "${from}" on "${event}"`);
    this.name = 'IllegalTransitionError';
  }
}

/**
 * The transition table. Exhaustive by construction: every state names every
 * event it accepts, and anything absent is illegal rather than ignored.
 *
 * `reduce` throws on an illegal transition in dev and returns the unchanged
 * state in production — a wrong animation is a cosmetic bug and must never
 * take the review panel down with it, but it also must not pass review
 * silently.
 */
export function reduce(state: ToasterState, event: ToasterEvent): ToasterState {
  switch (state.name) {
    case 'idle':
      if (event.type === 'file_selected') return { name: 'loaded' };
      if (event.type === 'resume') return { name: 'toasting', stage: event.stage };
      break;
    case 'loaded':
      if (event.type === 'file_cleared') return { name: 'idle' };
      if (event.type === 'submit') return { name: 'toasting', stage: null };
      if (event.type === 'resume') return { name: 'toasting', stage: event.stage };
      break;
    case 'toasting':
      if (event.type === 'stage') return { name: 'toasting', stage: event.stage };
      if (event.type === 'done') return { name: 'pop' };
      if (event.type === 'error') return { name: 'burnt' };
      if (event.type === 'cancel') return { name: 'idle' };
      break;
    case 'pop':
    case 'burnt':
      if (event.type === 'reset') return { name: 'idle' };
      if (event.type === 'file_selected') return { name: 'loaded' };
      break;
  }
  if (import.meta.env?.DEV) throw new IllegalTransitionError(state.name, event.type);
  return state;
}

export const INITIAL_STATE: ToasterState = { name: 'idle' };

// ---------------------------------------------------------------------------
// Easings
//
// Named for what they DO, not for their control points, so a call site reads
// as intent. The overshoot in the spring curves is what makes a lever feel
// mechanical rather than animated; `EASE_OUT` matches the existing
// `--ct-ease-out` token so new motion agrees with the CSS already shipping.
// ---------------------------------------------------------------------------

export const EASE_SPRING_STIFF = 'cubic-bezier(.16, 1.36, .3, 1)';
export const EASE_SPRING_SOFT = 'cubic-bezier(.22, 1.12, .36, 1)';
export const EASE_DETENT = 'cubic-bezier(.34, 1.56, .64, 1)';
export const EASE_OUT = 'cubic-bezier(.2, .8, .3, 1)';

// ---------------------------------------------------------------------------
// The rails
// ---------------------------------------------------------------------------

function matches(query: string): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
  try {
    return window.matchMedia(query).matches;
  } catch {
    // A test environment with a partial matchMedia stub must not crash the
    // panel. Failing to "not-reduced-motion" is the safe direction: it only
    // ever suppresses motion.
    return true;
  }
}

export function prefersReducedMotion(): boolean {
  return matches('(prefers-reduced-motion: reduce)');
}

export function forcedColors(): boolean {
  return matches('(forced-colors: active)');
}

/** True when motion must collapse to its end state instead of playing. */
export function motionSuppressed(): boolean {
  return prefersReducedMotion() || forcedColors();
}

const running = new Set<Animation>();
let visibilityBound = false;

function bindVisibility(): void {
  if (visibilityBound || typeof document === 'undefined' || !document.addEventListener) return;
  visibilityBound = true;
  document.addEventListener('visibilitychange', onVisibilityChange);
}

/**
 * Exported so the battery behaviour is a unit test rather than a claim: a
 * hidden tab must do zero animation work, and "zero" is checkable.
 */
export function onVisibilityChange(): void {
  const hidden = typeof document !== 'undefined' && document.visibilityState === 'hidden';
  for (const animation of running) {
    try {
      if (hidden) animation.pause();
      else animation.play();
    } catch {
      // A finished animation rejects pause/play; drop it rather than let one
      // stale handle stop the rest of the set from being paused.
      running.delete(animation);
    }
  }
}

/** Live animations this module started. Test seam; do not mutate. */
export function runningAnimations(): ReadonlySet<Animation> {
  return running;
}

export function stopAll(): void {
  for (const animation of running) {
    try {
      animation.cancel();
    } catch {
      /* already finished */
    }
  }
  running.clear();
}

/**
 * The single seam every helper below goes through.
 *
 * Returns `null` — never a fake Animation — when motion is suppressed or the
 * environment has no WAAPI, so a caller that wants to chain must handle the
 * no-motion case explicitly rather than await a promise that never settles.
 * The end state is applied first in that case, so the art still ARRIVES; only
 * the travel is skipped.
 */
export function play(
  element: Element,
  keyframes: Keyframe[],
  options: KeyframeAnimationOptions = {},
): Animation | null {
  const end = keyframes[keyframes.length - 1];
  if (motionSuppressed() || typeof (element as HTMLElement).animate !== 'function') {
    applyEndState(element, end);
    return null;
  }
  const animation = (element as HTMLElement).animate(keyframes, {
    easing: EASE_OUT,
    fill: 'forwards',
    ...options,
  });
  running.add(animation);
  bindVisibility();
  const forget = () => running.delete(animation);
  animation.addEventListener?.('finish', forget);
  animation.addEventListener?.('cancel', forget);
  if (typeof document !== 'undefined' && document.visibilityState === 'hidden') animation.pause();
  return animation;
}

/**
 * Writes a keyframe's properties onto the element as inline style. Only the
 * three non-layout properties this module allows are copied — a keyframe that
 * smuggles in `width` or `top` silently does nothing here, which is the
 * intended outcome rather than an oversight.
 */
export function applyEndState(element: Element, frame: Keyframe | undefined): void {
  if (!frame) return;
  const style = (element as HTMLElement).style;
  if (!style) return;
  for (const property of ['transform', 'opacity', 'filter'] as const) {
    const value = frame[property];
    if (value !== undefined && value !== null) style.setProperty(property, String(value));
  }
}

// ---------------------------------------------------------------------------
// The vocabulary
// ---------------------------------------------------------------------------

/**
 * The toast pop — the single highest-payoff animation in the set.
 *
 * A parabola, not an ease-out: the slice is thrown, so it decelerates on the
 * way up, hangs, and accelerates back down. A symmetric easing reads as a
 * slide. The small counter-rotation is what sells it as an object with mass
 * rather than a sprite on a path; the settle at the end is the spring.
 *
 * `height` is in the hero's own user-space units so callers reason in the
 * same coordinates as the art.
 */
export function popSlice(element: Element, { height = 58, duration = 620 } = {}): Animation | null {
  return play(
    element,
    [
      { transform: 'translateY(0) rotate(0deg)', opacity: 1, offset: 0 },
      { transform: `translateY(${-height}px) rotate(-3.5deg)`, opacity: 1, offset: 0.42 },
      { transform: `translateY(${-height * 0.86}px) rotate(-1.5deg)`, opacity: 1, offset: 0.58 },
      { transform: `translateY(${-height * 0.62}px) rotate(1deg)`, opacity: 1, offset: 0.82 },
      { transform: `translateY(${-height * 0.66}px) rotate(0deg)`, opacity: 1, offset: 1 },
    ],
    { duration, easing: EASE_SPRING_STIFF },
  );
}

/**
 * The lever's travel (#494). Down is a detent — it clicks into place and
 * stops dead; up is a release, which overshoots slightly because a real
 * spring does.
 */
export function leverTo(element: Element, down: boolean, { travel = 46, duration = 260 } = {}) {
  return play(
    element,
    down
      ? [
          { transform: 'translateY(0)' },
          { transform: `translateY(${travel * 1.04}px)` },
          { transform: `translateY(${travel}px)` },
        ]
      : [
          { transform: `translateY(${travel}px)` },
          { transform: `translateY(${-travel * 0.08}px)` },
          { transform: 'translateY(0)' },
        ],
    { duration, easing: down ? EASE_DETENT : EASE_SPRING_SOFT },
  );
}

/**
 * Steam. Each wisp gets its own delay and drift so the group never pulses in
 * unison — synchronised wisps read as a flashing shape, not vapour.
 *
 * `loop: false` is the first-toast, spawn-once variant (#501); `true` is the
 * fresh-pop variant that keeps going until the caller cancels it.
 */
export function steam(
  wisps: Iterable<Element>,
  { loop = false, duration = 2600 } = {},
): Animation[] {
  const out: Animation[] = [];
  let index = 0;
  for (const wisp of wisps) {
    const drift = 6 + index * 3;
    const animation = play(
      wisp,
      [
        { transform: 'translate(0, 0) scale(0.9)', opacity: 0, offset: 0 },
        { transform: `translate(${drift * 0.4}px, -14px) scale(1)`, opacity: 0.55, offset: 0.35 },
        { transform: `translate(${drift}px, -34px) scale(1.15)`, opacity: 0, offset: 1 },
      ],
      {
        duration,
        delay: index * 420,
        iterations: loop ? Infinity : 1,
        easing: 'ease-out',
        fill: 'forwards',
      },
    );
    if (animation) out.push(animation);
    index += 1;
  }
  return out;
}

/**
 * Heat shimmer over the slot region while a pass is genuinely running.
 *
 * Implemented by advancing an `feTurbulence` seed rather than by animating a
 * transform, because the effect is refraction, not movement. Deliberately
 * throttled: the displacement map is the most expensive thing on the page and
 * nobody can see the difference above ~20fps. Returns a stop function — the
 * caller MUST call it when the pass ends, since this is the one helper whose
 * work is not owned by an `Animation` the visibility rail can pause.
 */
export function heatShimmer(turbulence: Element, { fps = 20 } = {}): () => void {
  if (motionSuppressed() || typeof requestAnimationFrame !== 'function') return () => {};
  const interval = 1000 / fps;
  let seed = 0;
  let last = 0;
  let frame = 0;
  let stopped = false;
  const tick = (now: number) => {
    if (stopped) return;
    // A hidden tab gets no rAF callbacks at all in every browser that
    // matters, so this loop is already free when the tab is away; the check
    // covers the browser that fires one last frame on the way out.
    if (typeof document === 'undefined' || document.visibilityState !== 'hidden') {
      if (now - last >= interval) {
        last = now;
        seed = (seed + 1) % 1000;
        turbulence.setAttribute('seed', String(seed));
      }
    }
    frame = requestAnimationFrame(tick);
  };
  frame = requestAnimationFrame(tick);
  return () => {
    stopped = true;
    if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(frame);
  };
}

/**
 * Interior glow intensity, 0..1. Written as a CSS custom property rather than
 * an opacity so the theater (#496) can bind it to real progress and the art
 * decides for itself what "hot" looks like in each theme.
 */
export function glowRamp(host: Element, level: number): void {
  const clamped = Math.min(1, Math.max(0, level));
  (host as HTMLElement).style?.setProperty('--ct-glow-level', clamped.toFixed(3));
}

/**
 * Odometer digit roll (#501) — the digit strip translates by whole cells and
 * settles with a detent, the way a mechanical counter lands.
 */
export function odometerRoll(strip: Element, cells: number, { cellHeight = 15 } = {}) {
  return play(
    strip,
    [{ transform: 'translateY(0)' }, { transform: `translateY(${-cells * cellHeight}px)` }],
    { duration: 420 + cells * 40, easing: EASE_DETENT },
  );
}

/**
 * Receipt spool (#498) — a clip reveal, so the slip appears to emerge from
 * the slot rather than fade in on top of it.
 */
export function receiptSpool(element: Element, { duration = 900 } = {}) {
  return play(
    element,
    [
      { transform: 'translateY(-100%)', opacity: 1 },
      { transform: 'translateY(0)', opacity: 1 },
    ],
    { duration, easing: EASE_OUT },
  );
}

/**
 * Butter pat sliding across the toast ("Butter it", #499) — a straight
 * horizontal travel with a small settle at the end, the way a solid pat
 * decelerates once it starts to melt into the surface rather than
 * stopping dead. `distance` is in the hero's own user-space units, same
 * convention as `popSlice`'s `height` above.
 */
export function butterSlide(element: Element, { distance = 64, duration = 700 } = {}) {
  return play(
    element,
    [
      { transform: 'translateX(0) scale(1)', opacity: 1, offset: 0 },
      { transform: `translateX(${distance * 0.7}px) scale(1.02)`, opacity: 1, offset: 0.55 },
      { transform: `translateX(${distance}px) scale(1)`, opacity: 1, offset: 1 },
    ],
    { duration, easing: EASE_SPRING_SOFT },
  );
}
