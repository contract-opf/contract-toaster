import type { MotionEvent } from "./types";
export const easings = {
  stiff: "cubic-bezier(.2,.9,.25,1.2)",
  soft: "cubic-bezier(.2,.8,.25,1)",
  detent: "cubic-bezier(.2,.8,.2,1)",
  out: "cubic-bezier(0,0,.2,1)",
};
export const motionStyles = `@media (prefers-reduced-motion: reduce){.od-console *{animation:none!important;transition:none!important;scroll-behavior:auto!important}}@media (forced-colors: active){.od-console *{animation:none!important;transition:none!important}}.od-console[data-motion-paused="true"] *{animation:none!important;transition:none!important}`;
export interface MotionController {
  play: (event: MotionEvent, target?: Element) => void;
  preview: (element: HTMLElement, x: number) => void;
  dispose: () => void;
}
/* Local addition to the supplier kit (#723). The kit calls `element.animate`
   inline inside `createMotion`; the seam was added while a second hero was
   still alive, and "one reviewed motion owner" has to stay a fact a `grep` can
   check rather than a convention. #727 deleted that second hero
   (`toaster/motion.ts`), so this module is now the only animator in the
   frontend — the seam stays, because that is what the grep checks. Re-apply
   this when merging the next supplier patch. */
/**
 * The single call to `element.animate` in the whole frontend.
 *
 * Every animation goes through here. Since #727 removed the legacy hero this
 * controller's own vocabulary is all there is, and this is the frontend's only
 * `element.animate` call. The seam owns that call and its two failure modes and
 * nothing else: it does NOT decide whether motion is allowed. The rails stay
 * with the caller, which is what let the two owners differ while both existed.
 *
 * Returns `null` — never a fake `Animation` — when the environment has no WAAPI
 * or the call throws, so a caller that wants to chain has to handle the
 * no-animation case rather than await a promise that never settles.
 */
export function animateElement(
  element: Element,
  keyframes: Keyframe[],
  options: KeyframeAnimationOptions,
): Animation | null {
  if (typeof element.animate !== "function") return null;
  try {
    return element.animate(keyframes, options);
  } catch {
    /* Cosmetics never affect review actions. */
    return null;
  }
}
export function createMotion(root: HTMLElement): MotionController {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)"),
    forced = window.matchMedia("(forced-colors: active)");
  const ghosts = new Set<Element>();
  const running = new Set<Animation>();
  const allowed = () => !reduce.matches && !forced.matches && !document.hidden;
  const rail = () => {
    root.dataset.motionPaused = String(!allowed());
    if (!allowed()) {
      for (const a of running) a.cancel();
      running.clear();
      for (const ghost of ghosts) ghost.remove();
      ghosts.clear();
    }
  };
  const animate = (
    element: Element | null,
    frames: Keyframe[],
    duration: number,
    easing = easings.out,
  ) => {
    if (!element || !allowed()) return;
    const a = animateElement(element, frames, { duration, easing, fill: "none" });
    if (!a) return;
    running.add(a);
    a.finished.catch(() => {}).finally(() => running.delete(a));
    return a;
  };
  const part = (name: string) => root.querySelector(`[data-part="${name}"]`);
  let sliceSnapshot = part("slice")?.cloneNode(true) as HTMLElement | undefined;
  const play = (event: MotionEvent, target?: Element) => {
    switch (event) {
      case "key":
        animate(
          target ?? root.querySelector(".od-key"),
          [
            { transform: "translateY(0)" },
            { transform: "translateY(2px)" },
            { transform: "translateY(0)" },
          ],
          130,
          easings.detent,
        );
        break;
      case "file-removed":
        if (allowed() && sliceSnapshot) {
          const ghost = sliceSnapshot.cloneNode(true) as HTMLButtonElement;
          ghost.dataset.part = "slice-exit";
          ghost.setAttribute("aria-hidden", "true");
          ghost.disabled = true;
          ghost.tabIndex = -1;
          root.querySelector(".od-toaster-figure")?.append(ghost);
          ghosts.add(ghost);
          const clean = () => {
            ghost.remove();
            ghosts.delete(ghost);
          };
          const a = animate(
            ghost,
            [
              { transform: "translateY(0)", opacity: 1 },
              { transform: "translateY(-58px)", opacity: 0 },
            ],
            280,
            easings.soft,
          );
          if (a) void a.finished.then(clean, clean);
          else clean();
        }
        sliceSnapshot = undefined;
        break;
      case "pad-focus":
        animate(
          part("instructions-pad"),
          [
            { filter: "brightness(1)" },
            { filter: "brightness(1.025)", offset: 0.4 },
            { filter: "brightness(1)" },
          ],
          220,
        );
        break;
      case "guidance-readback":
        break;
      case "file-loaded":
        animate(
          part("slice"),
          [
            { transform: "translateY(-28px)", opacity: 0 },
            { transform: "translateY(0)", opacity: 1 },
          ],
          420,
          easings.soft,
        );
        break;
      case "submit-accepted":
        animate(
          part("lever-handle"),
          [
            {
              transform: `translateY(-${(part("lever-handle")?.parentElement?.clientHeight ?? 102) * 0.45}px)`,
            },
            { transform: "translateY(0)" },
          ],
          180,
          easings.detent,
        );
        break;
      case "done":
        animate(
          part("slice"),
          [
            { transform: "translateY(58px)" },
            { transform: "translateY(-6px)", offset: 0.8 },
            { transform: "translateY(0)" },
          ],
          620,
          easings.stiff,
        );
        animate(
          part("receipt-paper"),
          [
            { transform: "translateY(-35px)", opacity: 0.4 },
            { transform: "translateY(0)", opacity: 1 },
          ],
          560,
          easings.out,
        );
        break;
      case "cancelled":
        animate(
          part("lever-handle"),
          [
            {
              transform: `translateY(${(part("lever-handle")?.parentElement?.clientHeight ?? 102) * 0.45}px)`,
            },
            { transform: "translateY(-4px)", offset: 0.8 },
            { transform: "translateY(0)" },
          ],
          260,
          easings.stiff,
        );
        break;
      case "stage":
        animate(part("slot-glow"), [{ opacity: 0.4 }, { opacity: 1 }], 240);
        break;
      case "error":
        animate(
          part("steam"),
          [
            { transform: "translateY(15px)", opacity: 0 },
            { transform: "translateY(0)", opacity: 0.65 },
          ],
          420,
        );
        break;
      case "manual":
        animate(part("ready-lamp"), [{ opacity: 0.3 }, { opacity: 1 }], 260);
        break;
      case "cover-ready":
        animate(
          part("butter"),
          [{ transform: "translateX(-64px)" }, { transform: "translateX(0)" }],
          700,
          easings.soft,
        );
        break;
      case "receipt-open":
        animate(
          part("receipt-dialog"),
          [
            { transform: "translateY(12px) scale(.98)", opacity: 0.7 },
            { transform: "translateY(0) scale(1)", opacity: 1 },
          ],
          220,
        );
        break;
      case "receipt-copy":
      case "receipt-save":
        animate(
          part("receipt-paper"),
          [
            { transform: "translateX(0)" },
            { transform: "translateX(3px)", offset: 0.6 },
            { transform: "translateX(0)" },
          ],
          160,
        );
        break;
      case "disposition-saved":
        animate(
          part("disposition-stamp"),
          [
            { transform: "scale(1.12)", opacity: 0 },
            { transform: "scale(1)", opacity: 1 },
          ],
          240,
          easings.detent,
        );
        break;
      case "reservation-confirmed":
      case "settled":
        animate(
          part("till-drawer"),
          [
            { transform: "translateY(0)" },
            { transform: "translateY(5px)", offset: 0.45 },
            { transform: "translateY(0)" },
          ],
          420,
          easings.soft,
        );
        break;
      case "odometer":
        animate(
          part("odometer"),
          [
            { transform: "translateY(-15px)", opacity: 0.3 },
            { transform: "translateY(0)", opacity: 1 },
          ],
          240,
          easings.detent,
        );
        break;
    }
    if (event !== "file-removed" && part("slice"))
      sliceSnapshot = part("slice")!.cloneNode(true) as HTMLElement;
  };
  reduce.addEventListener("change", rail);
  forced.addEventListener("change", rail);
  document.addEventListener("visibilitychange", rail);
  rail();
  return {
    play,
    preview(element, y) {
      if (allowed()) element.style.transform = `translateY(${y}px)`;
    },
    dispose() {
      for (const a of running) a.cancel();
      running.clear();
      for (const ghost of ghosts) ghost.remove();
      ghosts.clear();
      reduce.removeEventListener("change", rail);
      forced.removeEventListener("change", rail);
      document.removeEventListener("visibilitychange", rail);
    },
  };
}
