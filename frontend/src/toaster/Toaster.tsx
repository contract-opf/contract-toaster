/**
 * Toaster — the owned, inline-SVG illustration set that is the hero of the
 * review experience. A near-photoreal chrome toaster whose rotating dial
 * selects the contract type, whose lever depresses while a review runs, whose
 * slots glow warm while toasting, and out of which a "contract" slice pops on
 * completion (a real download affordance when a handler is wired in).
 *
 * Design constraints (locked by tests + the Amplify CSP — do NOT relax):
 *   - All art is inline SVG in this file. No image files, no <img>, no
 *     external fonts/CDN — the CSP forbids remote loads. Animation is CSS only.
 *   - `ToasterStyles` renders a real inline <style> whose text includes a
 *     `@media (prefers-reduced-motion: reduce)` block that kills every toaster
 *     transition/animation. A test runs with CSS disabled and asserts a
 *     <style> element's text matches /prefers-reduced-motion:\s*reduce/.
 *   - The dial is a real ARIA `radiogroup` of `radio` stops with roving
 *     tabIndex + arrow-key navigation; the SVG knob/pointer is pure decoration
 *     (aria-hidden) that reflects `value`, never a replacement for the control.
 *     Coming-soon stops render de-emphasized and `aria-disabled`: visible,
 *     but not selectable by pointer OR arrow key. They are real published
 *     intent (the dial is the roadmap as well as the control), yet an
 *     unactivated playbook fails closed at load_playbook, so offering one as
 *     a choice could only ever 503. The backend stays authoritative — what's
 *     selectable is driven by the `status` the catalog itself reports, and a
 *     direct API call for an unactivated type still gets the same 503.
 *   - Theme-aware via `prefers-color-scheme` and the tokens.css vars
 *     (--ct-accent, --ct-glow, --ct-toast, --ct-toast-crust, …).
 *   - Content is only ever rendered as escaped text or static SVG — never
 *     injected as raw HTML — and nothing is persisted to web storage.
 *     (tests/test_frontend_xss_posture.py greps this tree for the raw-HTML
 *     injection prop by name, so don't spell it out here even to disavow it.)
 */
import { useCallback, useId, useRef, useState } from 'react';
import {
  STAGE_VIGNETTES,
  stageNumber,
  vignetteForStage,
  type ReviewStageToken,
  type StageVignette,
} from './stageTheater';

import {
  BROWNING_SETTINGS,
  browningSetting,
  DEFAULT_BROWNING,
  type BrowningLevel,
} from './browning';

// ---------------------------------------------------------------------------
// Shared stylesheet — a plain <style>, no CSS-in-JS dependency. Everything the
// SVG can't express as a static attribute lives here: the dial-pointer
// rotation, the lever slide, the warm slot-glow keyframes, the toast-pop
// spring, and the theme-aware chrome gradient stops. The whole lot is disabled
// under `prefers-reduced-motion: reduce`, and the chrome stops flip in dark
// mode via `prefers-color-scheme`.
// ---------------------------------------------------------------------------
export function ToasterStyles(): React.ReactElement {
  return (
    <style>{`
      /* Toaster-scoped chrome/metal palette — every hardcoded hex the hero SVG
         used to carry now routes through one of these, each with an
         SVG-safe var(--ct-x, #hex) fallback, so the appliance is
         theme-correct (chrome darkens, glow warms) without duplicating any
         shape. Defined once here; the dark block below overrides the values. */
      :root {
        --ct-chrome-edge: #8b9196;
        --ct-chrome-edge-soft: #7c8288;
        --ct-chrome-edge-strong: #83898e;
        --ct-knurl: #6f7479;
        --ct-lever-pin: #5a5f63;
        --ct-lever-pin-edge: #e8ebed;
        --ct-lever-knob-edge: #7a3a20;
        --ct-base-hi: #33373a;
        --ct-base-lo: #131517;
        --ct-slot-hi: #04060a;
        --ct-slot-mid: #161b21;
        --ct-slot-lo: #0a0e12;
        --ct-knob-hi: #f7f8f9;
        --ct-knob-mid: #cbd0d4;
        --ct-knob-lo: #9aa0a5;
        --ct-feet: #0e1012;
        --ct-contact-shadow: rgba(58, 34, 14, 0.32);
        --ct-contact-shadow-soft: rgba(58, 34, 14, 0.16);
        --ct-knob-shadow: rgba(20, 14, 8, 0.13);
        --ct-toast-seal-edge: #7a1712;
        /* Deep end of the DONENESS ramp (issue #447) — what a fully-toasted
           slice mixes toward. It is NOT --ct-toast-crust: at night that
           token is deliberately light (#f2ede2, the stroke that keeps the
           slice legible on a dark page), so mixing toward it would make the
           toast get PALER as the review progressed — the metaphor upside
           down. One token, overridden in the dark block below, keeps
           "further along = darker" true in both themes. */
        --ct-doneness-deep: #8a5a2b;

        /* --- Phase 1 art (issue #493) -------------------------------------
           The shell ramp replaces the old .ct-cr0…6 stop-color block: eight
           stops instead of seven, and routed through vars so the dark block
           overrides values rather than redeclaring a parallel set of classes
           (the #389 note). Brushed stainless is not a single grey — it is a
           run of alternating light and dark bands, which is what makes the
           banding read as anisotropic metal rather than a gradient. */
        --ct-shell-0: #fdfdfe;
        --ct-shell-1: #dfe4e8;
        --ct-shell-2: #f2f5f7;
        --ct-shell-3: #c2c9ce;
        --ct-shell-4: #e4e9ec;
        --ct-shell-5: #a4abb1;
        --ct-shell-6: #d2d8dc;
        --ct-shell-7: #8f979d;
        /* Bakelite — the warm phenolic the knob, the browning thumb and the
           lever cap are turned from. Deliberately NOT --ct-accent: the accent
           is the product's signal colour and must stay reserved for state,
           while this is a material that is simply always there. */
        --ct-bakelite-hi: #7a3f28;
        --ct-bakelite-mid: #4e2317;
        --ct-bakelite-lo: #2a120b;
        /* The countertop the appliance sits on and reflects into. */
        --ct-counter: #efe6d8;
        --ct-counter-far: #e3d7c5;
      }

      /* --- Legacy state illustrations (ProgressToaster/ToastUpToaster/SoberToaster) --- */
      .toaster-dial { display: flex; flex-wrap: wrap; gap: 0.5rem; padding: 0; margin: 0.25rem 0 0.5rem; list-style: none; justify-content: center; }
      .toaster-dial-stop {
        appearance: none; cursor: pointer; border-radius: 999px; padding: 0.35rem 0.85rem;
        font-size: 0.9rem; line-height: 1.2; border: 2px solid #8a8a8a; background: #f5f1e8; color: #2a2a2a;
        transition: transform 150ms ease, border-color 150ms ease, background-color 150ms ease;
        /* Printed appliance nameplate voice for the stop labels. */
        font-family: var(--ct-font-display, ui-sans-serif, system-ui, sans-serif);
        letter-spacing: 0.02em;
      }
      .toaster-dial-stop[aria-checked="true"] { border-color: var(--ct-accent, #af4b29); background: var(--ct-accent-soft, #ffe2b8); transform: scale(1.06); font-weight: 600; }
      .toaster-dial-stop[aria-disabled="true"], .toaster-dial-stop.toaster-dial-stop--coming-soon { opacity: 0.6; font-style: italic; }
      .toaster-dial-stop:focus-visible { outline: 2px solid #2a6bcc; outline-offset: 2px; }
      /* Browning readback (#495): quiet, permanent, and never truncated — the
         sentence it shows is the sentence the model is sent. */
      .toaster-browning__note { text-align: center; margin: 0.15rem 0 0; font-size: 0.85rem; max-width: 42ch; }

      /* --- The receipt (issue #498) --- */
      .toaster-receipt { display: flex; flex-direction: column; align-items: center; gap: 0.35rem; margin-top: 0.5rem; }
      .toaster-receipt__paper {
        width: min(100%, 34rem);
        padding: 1rem 1.25rem 1.5rem;
        background: var(--ct-receipt-paper, #fdfbf5);
        color: var(--ct-receipt-ink, #2a2119);
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.82rem; line-height: 1.55;
        box-shadow: 0 1px 2px rgba(20, 14, 8, 0.14);
        /* A torn bottom edge, drawn rather than imaged (the CSP forbids
           remote loads and this file carries no image files). */
        clip-path: polygon(0 0, 100% 0, 100% calc(100% - 6px),
          96% 100%, 92% calc(100% - 6px), 88% 100%, 84% calc(100% - 6px), 80% 100%,
          76% calc(100% - 6px), 72% 100%, 68% calc(100% - 6px), 64% 100%, 60% calc(100% - 6px),
          56% 100%, 52% calc(100% - 6px), 48% 100%, 44% calc(100% - 6px), 40% 100%,
          36% calc(100% - 6px), 32% 100%, 28% calc(100% - 6px), 24% 100%, 20% calc(100% - 6px),
          16% 100%, 12% calc(100% - 6px), 8% 100%, 4% calc(100% - 6px), 0 100%);
        animation: toaster-receipt-spool 520ms var(--ct-ease-out, cubic-bezier(.2, .8, .3, 1)) both;
      }
      .toaster-receipt__line { display: flex; justify-content: space-between; gap: 1rem; margin: 0; }
      .toaster-receipt__value { white-space: nowrap; }
      /* A full-width disclosure sentence (issue #570 follow-up) rather than
         a label/value pair: the fixed-width flex row above never lets its
         value shrink below its own content width (flex's default
         min-width: auto), so a sentence this long overflowed the paper's
         capped width and was clipped at its clip-path edge instead of
         wrapping. Falling back to a plain block sidesteps that -- there is
         no label to align against here, so there is nothing the flex row
         was buying. */
      .toaster-receipt__line--wrap { display: block; }
      .toaster-receipt__value--wrap {
        display: block; white-space: normal; overflow-wrap: break-word; text-align: left;
      }
      .toaster-receipt__rule { border: 0; border-top: 1px dashed var(--ct-receipt-ink, #2a2119); opacity: 0.45; margin: 0.4rem 0; }
      .toaster-receipt__actions { justify-content: center; }
      @keyframes toaster-receipt-spool {
        from { clip-path: inset(0 0 100% 0); transform: translateY(-8px); }
        to   { transform: translateY(0); }
      }
      @media (prefers-color-scheme: dark) {
        .toaster-receipt__paper { --ct-receipt-paper: #efe7d8; --ct-receipt-ink: #241c15; }
      }
      /* The slider glides between detents like the pointer sweeps the dial.
         The x presentation attribute is animatable, so this needs no transform
         bookkeeping. (No backticks in this block — it lives inside a template
         literal and one would terminate the whole stylesheet string.) */
      .toaster-browning-slider { transition: x 180ms var(--ct-ease-out, cubic-bezier(.2, .8, .3, 1)); }
      .toaster-illustration { display: block; margin: 0.5rem 0; }
      .toaster-illustration .toaster-body { fill: #c9c9c9; stroke: #4a4a4a; stroke-width: 2; }
      .toaster-illustration .toaster-slot { fill: #2a2a2a; }
      .toaster-illustration .toaster-coil { stroke: #d1602e; stroke-width: 2; fill: none; opacity: 0.35; }
      .toaster-illustration .toaster-coil--hot { opacity: 1; transition: opacity 400ms ease-in-out; }
      .toaster-illustration .toaster-toast { fill: var(--ct-toast, #d9a463); stroke: var(--ct-toast-crust, #8a5a2b); stroke-width: 2; transition: transform 400ms ease-out; }
      .toaster-illustration .toaster-toast--up { transform: translateY(-14px); }
      .toaster-illustration .toaster-toast--down { transform: translateY(6px); }
      .toaster-progress-track { fill: none; stroke: #d8d0c0; stroke-width: 6; }
      .toaster-progress-fill { fill: none; stroke: var(--ct-accent, #af4b29); stroke-width: 6; stroke-linecap: round; transition: stroke-dashoffset 600ms ease; }


      /* --- Toasting theater (issue #496) --- */
      .toaster-doneness__caption {
        font-size: 0.9rem; margin: 0.15rem 0 0; text-align: center;
        color: var(--ct-text, #2c2621);
      }
      .toaster-vignette { display: block; margin: 0.35rem auto 0; }
      .toaster-vignette__page { fill: var(--ct-surface, #fffdf9); stroke: var(--ct-border, #e6ddd0); stroke-width: 1.5; }
      .toaster-vignette__line { fill: var(--ct-border, #e6ddd0); stroke: var(--ct-border, #e6ddd0); }
      /* Two inks, and they must stay TELLABLE APART — the whole message of the
         critic vignette is "a different model wrote this one". Accent vs a
         cool counter-hue, never two shades of the same colour. */
      .toaster-vignette__ink-a { fill: var(--ct-accent, #af4b29); stroke: none; }
      .toaster-vignette__ink-b { fill: var(--ct-info-fg, #2a6bcc); stroke: none; }
      .toaster-vignette__nib { fill: var(--ct-neutral, #5c5c5c); }

      .toaster-vignette__pen { transform-box: fill-box; transform-origin: center; }
      .toaster-vignette--marking-up .toaster-vignette__pen--a {
        animation: toaster-pen-a calc(var(--ct-dur-slow, 400ms) * 5) var(--ct-ease-out, cubic-bezier(.2,.8,.3,1)) infinite;
      }
      .toaster-vignette--marking-up .toaster-vignette__mark {
        animation: toaster-mark-in calc(var(--ct-dur-slow, 400ms) * 5) linear infinite;
        transform-box: fill-box; transform-origin: left center;
      }
      .toaster-vignette--arguing .toaster-vignette__pen--b {
        animation: toaster-pen-b calc(var(--ct-dur-slow, 400ms) * 5) var(--ct-ease-out, cubic-bezier(.2,.8,.3,1)) infinite;
      }
      .toaster-vignette--arguing .toaster-vignette__strike {
        animation: toaster-mark-in calc(var(--ct-dur-slow, 400ms) * 5) linear infinite;
        transform-box: fill-box; transform-origin: left center;
      }
      .toaster-vignette--merging .toaster-vignette__merge-a {
        animation: toaster-merge-a calc(var(--ct-dur-slow, 400ms) * 6) var(--ct-ease-out, cubic-bezier(.2,.8,.3,1)) infinite;
      }
      .toaster-vignette--merging .toaster-vignette__merge-b {
        animation: toaster-merge-b calc(var(--ct-dur-slow, 400ms) * 6) var(--ct-ease-out, cubic-bezier(.2,.8,.3,1)) infinite;
      }
      .toaster-vignette--rolling .toaster-vignette__roll {
        animation: toaster-roll calc(var(--ct-dur-slow, 400ms) * 6) var(--ct-ease-out, cubic-bezier(.2,.8,.3,1)) infinite;
      }
      @keyframes toaster-pen-a {
        0%, 12% { transform: translate(0, 0); }
        45% { transform: translate(-46px, -14px); }
        70%, 100% { transform: translate(0, 0); }
      }
      @keyframes toaster-pen-b {
        0%, 12% { transform: translate(0, 0); }
        45% { transform: translate(58px, -8px); }
        70%, 100% { transform: translate(0, 0); }
      }
      @keyframes toaster-mark-in {
        0%, 20% { transform: scaleX(0); }
        50%, 90% { transform: scaleX(1); }
        100% { transform: scaleX(0); }
      }
      @keyframes toaster-merge-a {
        0%, 100% { transform: translateX(0); }
        50% { transform: translateX(11px); }
      }
      @keyframes toaster-merge-b {
        0%, 100% { transform: translateX(0); }
        50% { transform: translateX(-11px); }
      }
      @keyframes toaster-roll {
        0% { transform: translateX(56px); opacity: 0; }
        30% { opacity: 1; }
        100% { transform: translateX(0); opacity: 1; }
      }

      /* --- ToasterHero: the photoreal centerpiece --- */
      .toaster-hero { display: flex; flex-direction: column; align-items: center; gap: 0.75rem; }
      .toaster-hero__stage {
        position: relative; display: inline-block; line-height: 0;
        border-radius: var(--ct-radius-lg, 20px);
        padding: var(--ct-space-4, 16px);
        /* A subtle sunken counter mat under the appliance. */
        background: radial-gradient(120% 90% at 50% 65%, var(--ct-surface-sunken, #f1e9dc) 0%, transparent 72%);
        box-shadow: inset 0 0 0 1px var(--ct-border, #e6ddd0);
        transition: box-shadow var(--ct-dur, 200ms) var(--ct-ease-out, cubic-bezier(.2, .8, .3, 1));
      }
      /* Error state: keep the sober grayscale treatment on the appliance
         itself, but give the mat a deliberate danger-tinted edge so the
         state reads as intentional, not a rendering bug. */
      .toaster-hero--sober .toaster-hero__stage { box-shadow: inset 0 0 0 2px var(--ct-danger-border, #eab0ac); }
      .toaster-hero__svg { display: block; width: 100%; max-width: 360px; height: auto; }
      .toaster-hero--sober .toaster-hero__svg { filter: grayscale(0.85) brightness(0.82); opacity: 0.72; }

      /* Chrome gradient stops — brushed-stainless banding, theme-aware.
         Still used by the legacy state illustrations; the Phase 1 hero uses
         the eight-stop --ct-shell-* ramp below. */
      .ct-cr0 { stop-color: #fbfbfb; } .ct-cr1 { stop-color: #d9dde0; } .ct-cr2 { stop-color: #eef1f3; }
      .ct-cr3 { stop-color: #c3c9cd; } .ct-cr4 { stop-color: #dfe3e6; } .ct-cr5 { stop-color: #a9afb4; }
      .ct-cr6 { stop-color: #cfd4d8; }

      /* Phase 1 shell ramp (#493). */
      .ct-sh0 { stop-color: var(--ct-shell-0, #fdfdfe); } .ct-sh1 { stop-color: var(--ct-shell-1, #dfe4e8); }
      .ct-sh2 { stop-color: var(--ct-shell-2, #f2f5f7); } .ct-sh3 { stop-color: var(--ct-shell-3, #c2c9ce); }
      .ct-sh4 { stop-color: var(--ct-shell-4, #e4e9ec); } .ct-sh5 { stop-color: var(--ct-shell-5, #a4abb1); }
      .ct-sh6 { stop-color: var(--ct-shell-6, #d2d8dc); } .ct-sh7 { stop-color: var(--ct-shell-7, #8f979d); }
      .ct-bk-hi { stop-color: var(--ct-bakelite-hi, #7a3f28); }
      .ct-bk-mid { stop-color: var(--ct-bakelite-mid, #4e2317); }
      .ct-bk-lo { stop-color: var(--ct-bakelite-lo, #2a120b); }
      .ct-counter { stop-color: var(--ct-counter, #efe6d8); }
      .ct-counter-far { stop-color: var(--ct-counter-far, #e3d7c5); }

      /* forced-colors: the whole appliance is decoration that carries no
         information the DOM does not already state (the dial is a real
         radiogroup, the progress a real progressbar). In a forced palette the
         gradients collapse to flat system colours anyway, so rather than
         render a smear of ButtonFace we drop the art's own contrast work and
         let the browser substitute — the same posture as the sober filter. */
      @media (forced-colors: active) {
        .toaster-hero__svg { forced-color-adjust: none; }
      }

      /* Base/slot/knob gradient stops — single definition + var(), swapped in
         the dark block below (the Note in #389: avoid duplicating stop-color
         class blocks when a token will do). */
      .ct-base-hi { stop-color: var(--ct-base-hi, #33373a); }
      .ct-base-lo { stop-color: var(--ct-base-lo, #131517); }
      .ct-slot-hi { stop-color: var(--ct-slot-hi, #04060a); }
      .ct-slot-mid { stop-color: var(--ct-slot-mid, #161b21); }
      .ct-slot-lo { stop-color: var(--ct-slot-lo, #0a0e12); }
      .ct-knob-hi { stop-color: var(--ct-knob-hi, #f7f8f9); }
      .ct-knob-mid { stop-color: var(--ct-knob-mid, #cbd0d4); }
      .ct-knob-lo { stop-color: var(--ct-knob-lo, #9aa0a5); }

      /* Rotating dial pointer — reflects the selected entry's angle. fill-box
         + center origin keeps the pivot glued to the knob center regardless of
         viewport scaling (the group carries a transparent full-diameter disc). */
      /* --- The dial as a pointer control (issue #490) --- */
      .toaster-knob--interactive { cursor: grab; touch-action: none; }
      .toaster-knob--interactive:active { cursor: grabbing; }
      /* While a finger is on it the needle must track exactly, not ease 200ms
         behind — a lag on a direct-manipulation control reads as broken. */
      .toaster-pointer--dragging { transition: none; }

      .toaster-pointer {
        transition: transform var(--ct-dur, 200ms) var(--ct-ease-out, cubic-bezier(.2, .8, .3, 1));
        transform-box: fill-box; transform-origin: center;
      }

      /* Lever slides down its track whenever the toaster is doing anything. */
      .toaster-lever { transition: transform 250ms cubic-bezier(.4, 0, .3, 1); }
      .toaster-lever--down { transform: translateY(46px); }
      /* Issue #494: the lever becomes a real control. The transition is
         suppressed WHILE dragging (an inline transform is present) so the
         lever tracks the finger exactly instead of easing behind it —
         a 250ms lag on a direct-manipulation control reads as broken. */
      .toaster-lever[style*="translateY"] { transition: none; }
      .toaster-lever--interactive { cursor: grab; touch-action: none; }
      .toaster-lever--interactive:active { cursor: grabbing; }
      /* The focus ring has to be drawn on the SVG group itself; an outline on
         a <g> is honoured by every current engine, and this is the affordance
         a keyboard user actually lands on. */
      .toaster-lever--interactive:focus-visible {
        outline: 3px solid var(--ct-focus, #2a6bcc);
        outline-offset: 3px;
        border-radius: 12px;
      }

      /* Warm slot glow while toasting — a blurred halo that breathes. Timed
         off --ct-dur-slow (400ms) rather than a hardcoded duration; eased
         with --ct-ease-out per #389. */
      .toaster-glow {
        opacity: 0.6;
        animation: toaster-glow-pulse calc(var(--ct-dur-slow, 400ms) * 6) var(--ct-ease-out, cubic-bezier(.2, .8, .3, 1)) infinite;
      }
      @keyframes toaster-glow-pulse {
        0%, 100% { opacity: 0.32; }
        50% { opacity: 0.92; }
      }

      /* Toast springs out of the slot with a slight overshoot. */
      .toaster-hero__toast {
        position: absolute; left: 50%; top: 6%; transform: translate(-50%, 0);
        animation: toaster-pop 380ms var(--ct-ease-spring, cubic-bezier(.34, 1.56, .64, 1));
      }
      @keyframes toaster-pop {
        from { transform: translate(-50%, 46px); opacity: 0; }
        to { transform: translate(-50%, 0); opacity: 1; }
      }
      .toaster-hero__toast-btn {
        appearance: none; background: none; border: none; padding: 0; margin: 0;
        cursor: pointer; display: flex; flex-direction: column; align-items: center; gap: 0.15rem;
      }
      .toaster-hero__toast-btn:disabled { cursor: default; opacity: 0.6; }
      .toaster-hero__toast-btn:focus-visible { outline: 2px solid var(--ct-accent, #af4b29); outline-offset: 3px; border-radius: 8px; }
      .toaster-hero__toast-caption {
        font-size: 0.8rem; font-weight: 600; color: var(--ct-accent, #af4b29);
        text-decoration: underline; text-underline-offset: 2px;
      }
      /* Burnt slice + smoke (issue #501). The wisps rise and fade on a slow
         loop, staggered so they never read as one shape moving. */
      @keyframes toaster-smoke-rise {
        0%   { opacity: 0;    transform: translateY(6px)  scaleX(0.9); }
        25%  { opacity: 0.55; }
        100% { opacity: 0;    transform: translateY(-16px) scaleX(1.25); }
      }
      .toaster-smoke__wisp { transform-origin: 50% 100%; animation: toaster-smoke-rise 3.4s ease-out infinite; }
      .toaster-smoke__wisp--b { animation-duration: 4.1s; animation-delay: 0.7s; }
      .toaster-smoke__wisp--c { animation-duration: 3.8s; animation-delay: 1.4s; }
      .toaster-hero__sober {
        position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
        pointer-events: none;
      }
      .toaster-hero__progress { display: flex; flex-direction: column; align-items: center; gap: 0.25rem; }
      .toaster-hero__progress p { font-size: 0.85rem; margin: 0; }

      /* --- Staged doneness (issue #447): the toast slice IS the bar ---
         The slice darkens one step per real pipeline sub-stage, along a warm
         ramp mixed from --ct-toast toward --ct-doneness-deep (CTDS §5:
         tokens only, both themed above). The plain --ct-toast declaration
         before each color-mix is the fallback for an engine without
         color-mix — it simply shows an undarkened slice, and the step TEXT
         still carries the actual information, so nothing is lost but the
         delight. Colour is NEVER the sole carrier here. */
      .toaster-doneness__slice { transition: fill var(--ct-dur-slow, 400ms) var(--ct-ease-out, cubic-bezier(.2, .8, .3, 1)); }
      .toaster-doneness--step1 .toaster-doneness__slice { fill: var(--ct-toast, #d9a463); }
      .toaster-doneness--step2 .toaster-doneness__slice {
        fill: var(--ct-toast, #d9a463);
        fill: color-mix(in srgb, var(--ct-doneness-deep) 26%, var(--ct-toast));
      }
      .toaster-doneness--step3 .toaster-doneness__slice {
        fill: var(--ct-toast, #d9a463);
        fill: color-mix(in srgb, var(--ct-doneness-deep) 52%, var(--ct-toast));
      }
      .toaster-doneness--step4 .toaster-doneness__slice {
        fill: var(--ct-toast, #d9a463);
        fill: color-mix(in srgb, var(--ct-doneness-deep) 78%, var(--ct-toast));
      }
      /* Honest within-step motion: the heat shimmer says "still working on
         THIS step". It never advances the step — only a real stage token
         from the pipeline does that. */
      .toaster-doneness__heat {
        animation: toaster-doneness-shimmer calc(var(--ct-dur-slow, 400ms) * 5) var(--ct-ease-out, cubic-bezier(.2, .8, .3, 1)) infinite;
        transform-box: fill-box; transform-origin: center;
      }
      @keyframes toaster-doneness-shimmer {
        0%, 100% { opacity: 0.18; }
        50% { opacity: 0.62; }
      }
      .toaster-doneness__step { font-size: 0.85rem; font-weight: 600; margin: 0; text-align: center; }
      .toaster-doneness__hint { font-size: 0.78rem; margin: 0; opacity: 0.75; text-align: center; }

      @media (prefers-color-scheme: dark) {
        .toaster-dial-stop { background: #2e2a24; color: #f2ede2; border-color: #6b6b6b; }
        .toaster-illustration .toaster-body { fill: #4a4a4a; stroke: #d0d0d0; }
        /* Chrome darkens so the appliance still reads as metal against a dark page. */
        .ct-cr0 { stop-color: #6b7075; } .ct-cr1 { stop-color: #3f4448; } .ct-cr2 { stop-color: #565b60; }
        .ct-cr3 { stop-color: #34383c; } .ct-cr4 { stop-color: #4a4f54; } .ct-cr5 { stop-color: #26292c; }
        .ct-cr6 { stop-color: #3a3e42; }

        :root {
          --ct-chrome-edge: #5b6166;
          --ct-chrome-edge-soft: #4a4f54;
          --ct-chrome-edge-strong: #6b7075;
          --ct-knurl: #4a4f54;
          --ct-lever-pin: #babfc4;
          --ct-lever-pin-edge: #2a2d30;
          --ct-lever-knob-edge: #4a2410;
          /* The knob face darkens too — previously only the body chrome did,
             leaving the dial looking un-themed at night. */
          --ct-base-hi: #45494c;
          --ct-base-lo: #202325;
          --ct-slot-hi: #000000;
          --ct-slot-mid: #0c0f12;
          --ct-slot-lo: #060809;
          --ct-knob-hi: #cfd3d6;
          --ct-knob-mid: #9a9fa3;
          --ct-knob-lo: #6b7075;
          --ct-feet: #000000;
          /* Night shadows nearly disappear (§4.1) — warm contact shadow fades. */
          --ct-contact-shadow: rgba(0, 0, 0, 0.38);
          --ct-contact-shadow-soft: rgba(0, 0, 0, 0.2);
          --ct-knob-shadow: rgba(0, 0, 0, 0.28);
          --ct-toast-seal-edge: #4a0f0c;
          /* Night doneness deepens from the night --ct-toast (#b98246)
             toward this, so the slice still DARKENS with progress; the
             light --ct-toast-crust stroke keeps its silhouette readable
             against the dark page at every level. */
          --ct-doneness-deep: #5c3a1c;

          /* Night shell: the same eight-band structure, pulled down so the
             appliance still reads as brushed metal — a lit object in a dark
             room — instead of a white cut-out on a dark page. */
          --ct-shell-0: #b9c0c6;
          --ct-shell-1: #7d858b;
          --ct-shell-2: #a6aeb4;
          --ct-shell-3: #666e74;
          --ct-shell-4: #939ba1;
          --ct-shell-5: #4e565c;
          --ct-shell-6: #7b838a;
          --ct-shell-7: #3f474d;
          --ct-bakelite-hi: #6a3522;
          --ct-bakelite-mid: #401c12;
          --ct-bakelite-lo: #200d07;
          --ct-counter: #2a241d;
          --ct-counter-far: #1d1813;
        }
      }

      @media (prefers-reduced-motion: reduce) {
        /* The receipt APPEARS instead of spooling. The slip itself is the
           information; the spool is pure theatre, so it is the only part that
           goes. */
        .toaster-receipt__paper { animation: none !important; }
        /* Reduced motion keeps the smoke VISIBLE but still — the wisps are
           part of what says "burnt", so removing them would remove
           information, not just movement. Only the rising loop stops. */
        .toaster-smoke__wisp { animation: none !important; opacity: 0.4 !important; transform: none !important; }
        .toaster-browning-slider,
        .toaster-dial-stop,
        .toaster-illustration .toaster-coil--hot,
        .toaster-illustration .toaster-toast,
        .toaster-progress-fill,
        .toaster-pointer,
        .toaster-lever,
        .toaster-glow,
        .toaster-hero__toast,
        .toaster-hero__stage,
        /* Issue #447: the staged-doneness darkening + within-step heat
           shimmer are covered here explicitly. base.css's global
           universal-selector rule already neutralizes them, but this file's
           stylesheet is self-contained (it is what the reduced-motion test
           greps), so the toaster's own animations are all listed here rather
           than relying on a stylesheet that may not be loaded. */
        .toaster-doneness__slice,
        .toaster-doneness__heat,
        /* Issue #496: the theater vignettes. Listed by their inner parts, not
           by the <svg> wrapper, because the animations live on the groups.
           Reduced motion leaves each scene STATIC but present — the caption
           beside it is the message and still advances with the real stage, so
           nothing is lost, only the movement. */
        .toaster-vignette__pen,
        .toaster-vignette__mark,
        .toaster-vignette__strike,
        .toaster-vignette__merge-a,
        .toaster-vignette__merge-b,
        .toaster-vignette__roll {
          transition: none !important;
          animation: none !important;
        }
        /* With the scale animation off, the marks must still be VISIBLE — the
           toaster-mark-in keyframes start at scaleX(0), so leaving the
           transform un-neutralised would hide the very ink the scene is
           about. (No backticks in this stylesheet: it is a template literal,
           and one would end it mid-CSS.) */
        .toaster-vignette__mark,
        .toaster-vignette__strike,
        .toaster-vignette__roll {
          transform: none !important;
          opacity: 1 !important;
        }
        /* Reduced motion keeps the DONENESS LEVEL (it is information, not
           decoration) — only the transition between levels and the shimmer
           go away. The heat marks settle at a steady mid opacity. */
        .toaster-doneness__heat { opacity: 0.4 !important; }
        /* Reduced motion still shows a steady, mid-intensity glow — never a
           pulse — and the toast simply appears at rest. */
        .toaster-glow { opacity: 0.6 !important; }
        .toaster-hero__toast { opacity: 1 !important; transform: translate(-50%, 0) !important; }
      }
    `}</style>
  );
}

// ---------------------------------------------------------------------------
// DialEntry — one selectable contract type. `status !== 'active'` renders as a
// de-emphasized "coming soon" stop that is still selectable (backend decides).
// ---------------------------------------------------------------------------
export interface DialEntry {
  playbook_id: string;
  display_name: string;
  status: string;
}

interface ContractTypeDialProps {
  entries: DialEntry[];
  value: string;
  onChange: (playbookId: string) => void;
}

// ---------------------------------------------------------------------------
// ContractTypeDial — the accessible contract-type picker: a real ARIA
// `radiogroup` of `radio` stops with roving tabIndex + arrow-key navigation.
// ToasterHero renders this as the labeled stops beside its decorative knob.
// ---------------------------------------------------------------------------
export function ContractTypeDial({ entries, value, onChange }: ContractTypeDialProps): React.ReactElement {
  const groupRef = useRef<HTMLDivElement | null>(null);

  // Only a loaded ("active") playbook can actually be reviewed against — an
  // unactivated one fails closed at load_playbook, so selecting it could only
  // ever 503. Those stops still RENDER (de-emphasized, "(coming soon)"): the
  // dial is the product's roadmap as well as its control, and hiding a
  // registered-but-unloaded type would erase that signal. They are simply not
  // selectable — visible, not clickable — so nothing offers a guaranteed
  // failure dressed up as a choice. Pointer and keyboard agree: both route
  // through `selectable`.
  const selectable = entries.filter((entry) => entry.status === 'active');

  // Select the nth SELECTABLE stop (wrapping), and move focus to it. Indexing
  // `selectable` rather than `entries` is what keeps every keyboard route —
  // arrows, Home and End alike — off the coming-soon stops a mouse user can't
  // click either. `Math.abs` is not enough for the wrap: a negative index has
  // to come back around from the end, so the modulo is taken twice.
  const selectAt = useCallback(
    (index: number) => {
      if (selectable.length === 0) {
        return;
      }
      const next = selectable[((index % selectable.length) + selectable.length) % selectable.length];
      if (next) {
        onChange(next.playbook_id);
        // Move focus with selection (roving tabindex / ARIA radiogroup
        // pattern) so keyboard users can keep pressing arrow keys. Find the
        // button by dataset lookup rather than a CSS-attribute selector
        // (avoids relying on `CSS.escape`, which jsdom doesn't implement).
        const buttons = groupRef.current?.querySelectorAll<HTMLButtonElement>('button[data-playbook-id]');
        const nextButton = buttons
          ? Array.from(buttons).find((btn) => btn.dataset.playbookId === next.playbook_id)
          : undefined;
        nextButton?.focus();
      }
    },
    [selectable, onChange],
  );

  const moveSelection = useCallback(
    (delta: number) => {
      const currentIndex = Math.max(
        0,
        selectable.findIndex((e) => e.playbook_id === value),
      );
      selectAt(currentIndex + delta);
    },
    [selectable, value, selectAt],
  );

  // Arrows + Home/End, matching `ui/components/ct-tab-bar.ts`'s handler. Home
  // and End were missing here while the tab bar had both, so the two roving
  // widgets in the same app answered the keyboard differently — a gap the
  // live audit could not catch, because production has a single dial stop and
  // no key has anywhere to move (audit §E6, issue #450 item 1).
  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        event.preventDefault();
        moveSelection(1);
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        event.preventDefault();
        moveSelection(-1);
      } else if (event.key === 'Home') {
        event.preventDefault();
        selectAt(0);
      } else if (event.key === 'End') {
        event.preventDefault();
        selectAt(selectable.length - 1);
      }
    },
    [moveSelection, selectAt, selectable.length],
  );

  return (
    <div style={{ marginBottom: '0.5rem' }}>
      <span id="review-playbook-dial-label" style={{ display: 'block', marginBottom: '0.25rem', textAlign: 'center' }}>
        Contract type:
      </span>
      <div
        ref={groupRef}
        role="radiogroup"
        aria-labelledby="review-playbook-dial-label"
        data-testid="review-playbook-dial"
        className="toaster-dial"
        onKeyDown={handleKeyDown}
      >
        {entries.map((entry) => {
          const checked = entry.playbook_id === value;
          const comingSoon = entry.status !== 'active';
          return (
            <button
              key={entry.playbook_id}
              type="button"
              role="radio"
              aria-checked={checked}
              // aria-disabled (not the `disabled` attribute): a coming-soon
              // stop stays perceivable and focusable for assistive tech — it
              // is information, not dead chrome — while both click and arrow
              // keys refuse to select it. `disabled` would drop it out of the
              // a11y tree and hide the roadmap from exactly the users who
              // can't see the de-emphasized styling.
              aria-disabled={comingSoon || undefined}
              tabIndex={checked ? 0 : -1}
              data-playbook-id={entry.playbook_id}
              data-testid={`review-playbook-option-${entry.playbook_id}`}
              className={`toaster-dial-stop${comingSoon ? ' toaster-dial-stop--coming-soon' : ''}`}
              onClick={() => {
                if (!comingSoon) {
                  onChange(entry.playbook_id);
                }
              }}
            >
              {/* NOTE: textContent must be EXACTLY the display name (+ optional
                  " (coming soon)"). Do NOT add decorative text nodes here — a
                  test asserts stops.map(s => s.textContent). */}
              {comingSoon ? `${entry.display_name} (coming soon)` : entry.display_name}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// BrowningControl — the Light/Medium/Dark markup-intensity picker (issue #495).
//
// Same accessible shape as the dial above: a real ARIA `radiogroup` of `radio`
// stops with roving tabIndex and arrow/Home/End keys. The SVG slider on the
// hero is decoration that mirrors the value.
//
// The transparency rule lives here: whatever sentence this control is about to
// add to the review's instructions is shown, verbatim, right underneath it —
// and both the shown copy and the sent text come from the SAME constant in
// `browning.ts`. Nothing invisible reaches the model.
// ---------------------------------------------------------------------------
interface BrowningControlProps {
  value: BrowningLevel;
  onChange: (level: BrowningLevel) => void;
}

export function BrowningControl({ value, onChange }: BrowningControlProps): React.ReactElement {
  const groupRef = useRef<HTMLDivElement | null>(null);

  const selectAt = useCallback(
    (index: number) => {
      const count = BROWNING_SETTINGS.length;
      const next = BROWNING_SETTINGS[((index % count) + count) % count];
      if (!next) {
        return;
      }
      onChange(next.id);
      const buttons = groupRef.current?.querySelectorAll<HTMLButtonElement>('button[data-browning]');
      const nextButton = buttons
        ? Array.from(buttons).find((btn) => btn.dataset.browning === next.id)
        : undefined;
      nextButton?.focus();
    },
    [onChange],
  );

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      const current = Math.max(
        0,
        BROWNING_SETTINGS.findIndex((setting) => setting.id === value),
      );
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        event.preventDefault();
        selectAt(current + 1);
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        event.preventDefault();
        selectAt(current - 1);
      } else if (event.key === 'Home') {
        event.preventDefault();
        selectAt(0);
      } else if (event.key === 'End') {
        event.preventDefault();
        selectAt(BROWNING_SETTINGS.length - 1);
      }
    },
    [value, selectAt],
  );

  const setting = browningSetting(value);

  return (
    <div style={{ marginBottom: '0.5rem' }}>
      <span
        id="review-browning-label"
        style={{ display: 'block', marginBottom: '0.25rem', textAlign: 'center' }}
      >
        Markup intensity:
      </span>
      <div
        ref={groupRef}
        role="radiogroup"
        aria-labelledby="review-browning-label"
        data-testid="review-browning-control"
        className="toaster-dial"
        onKeyDown={handleKeyDown}
      >
        {BROWNING_SETTINGS.map((option) => (
          <button
            key={option.id}
            type="button"
            role="radio"
            aria-checked={option.id === value}
            tabIndex={option.id === value ? 0 : -1}
            data-browning={option.id}
            data-testid={`review-browning-option-${option.id}`}
            className="toaster-dial-stop"
            onClick={() => onChange(option.id)}
          >
            {option.label}
          </button>
        ))}
      </div>
      {/* The transparency readback. `setting.sentence` is the exact string
          submitted — not a paraphrase of it — so a reviewer can read what the
          model will be told before pressing the lever. */}
      <p className="ct-muted toaster-browning__note" data-testid="review-browning-note">
        {setting.note}
        {setting.sentence ? ' ' : ''}
        {setting.sentence ? <q data-testid="review-browning-sentence">{setting.sentence}</q> : null}
      </p>
    </div>
  );
}

// ===========================================================================
// ToasterHero — THE hero. One cohesive, near-photoreal chrome toaster: layered
// chrome gradients, a specular highlight, a soft ground shadow, a rotating
// dial pointer, a sliding lever, glowing slots while working, and a contract
// slice that pops out (a real download button) on completion.
// ===========================================================================
export type ToasterPhase = 'idle' | 'working' | 'done' | 'error';

// ---------------------------------------------------------------------------
// Staged review progress (issue #447), now derived from ONE map (issue #496).
//
// The four tokens are a WIRE CONTRACT with the backend — scripts/review_spine
// .py's PROGRESS_STAGES, written onto the reviews row by
// pipeline_runner._write_progress_stage as each sub-stage starts and projected
// by get_review_detail as `progress_stage`.
//
// `STAGE_VIGNETTES` (toaster/stageTheater.ts) is that map. It used to be
// duplicated here as `PROGRESS_STEPS`, which meant two independent
// enumerations of the same wire contract — and #496 needed a third surface
// (the tab title, #497) to read it too. Three copies of "which stages exist"
// is three chances to disagree about what the backend just said. These are
// now thin re-exports so every existing consumer keeps working unchanged.
// ---------------------------------------------------------------------------
export type ReviewProgressStage = ReviewStageToken;

export const PROGRESS_STEPS: ReadonlyArray<{ token: ReviewProgressStage; label: string }> =
  STAGE_VIGNETTES.map((stage) => ({ token: stage.token, label: stage.label }));

/** 1-based step number for a reported token, or 0 for "no honest step to show".
 *  An absent, null, or unrecognised token (an older runner, a deployment that
 *  reports no progress, a stage renamed on the backend) deliberately yields 0
 *  rather than a guess — the caller then shows the indeterminate treatment,
 *  which claims nothing, instead of a step number that might be a lie. */
export const progressStepNumber = stageNumber;

export interface ToasterHeroProps {
  entries: DialEntry[];
  value: string;
  onChange: (playbookId: string) => void;
  phase: ToasterPhase;
  /** When provided AND phase==='done', the toast becomes a real download button. */
  onDownload?: () => void;
  /** Disables that toast button while a download is preparing. */
  downloadDisabled?: boolean;
  /**
   * The review's CURRENT pipeline sub-stage, straight off the polled detail
   * (`progress_stage`). Drives the staged doneness treatment while
   * phase==='working'. Null/absent/unknown falls back to the indeterminate
   * ring — never a timer-driven guess at which step we are on.
   */
  progressStage?: string | null;
  /**
   * Issue #494 — pushing the lever down IS the submission.
   *
   * Absent leaves the lever pure decoration, which is what every caller that
   * only illustrates state wants. Present makes it a real control, alongside
   * (never instead of) the submit button the form already has.
   */
  onLeverPull?: () => void;
  /**
   * Whether pulling would actually be legitimate right now. The caller owns
   * this because the caller owns the guards — a file chosen, nothing in
   * flight, no review already running. A lever that clicks down and does
   * nothing is worse than one that will not move.
   */
  leverArmed?: boolean;
  /**
   * Markup intensity (issue #495). Optional: a caller that does not pass it
   * gets the decorative slider parked at the default and no control, which is
   * how the state-illustration wrappers below stay unchanged.
   */
  browning?: BrowningLevel;
  onBrowningChange?: (level: BrowningLevel) => void;
}

// Geometry constants for the hero SVG (user-space units; viewBox 0 0 420 340).
// Phase 1 (#493) moved the dial left and shrank it: at the old r=40 it filled
// most of the front face, which is why the appliance read as "a circle on a
// box". A real two-slice toaster's timer dial is small relative to the shell,
// and the space it frees is what the browning control and the counter window
// now occupy. The knurl/stop tick radii below are derived from DIAL_R, so they
// followed the change without their own constants.
const DIAL_CX = 146;
const DIAL_CY = 202;
const DIAL_R = 24;

// The shell silhouette — a flat-bottomed box whose top corners roll over into
// a curved deck. Declared once because four things must agree on it exactly:
// the fill, the clip that keeps the grain and speculars inside the body, the
// rim stroke, and the countertop reflection. Four hand-copied path strings
// would drift the first time the form is tuned.
const SHELL_PATH = 'M92 268 V150 Q92 106 138 104 H282 Q328 106 328 150 V268 Z';
// Same path, left open at the bottom: the rim highlight traces the silhouette
// but must not draw a bright line across the base, where the shell meets the
// plinth and the light does not reach.
const SHELL_PATH_OPEN = 'M92 268 V150 Q92 106 138 104 H282 Q328 106 328 150 V268';

// The viewBox, named (issue #490). Pointer coordinates arrive in client pixels
// and every angle the dial computes is in user space, so the conversion needs
// both dimensions; spelling them out beats two magic numbers in a trig
// expression, and a future viewBox change now has one place to be made.
const VIEWBOX_WIDTH = 420;
const VIEWBOX_HEIGHT = 340;
// Lever travel (issue #494), in the same user-space units, matching the
// `.toaster-lever--down` transform exactly — the drag and the state-driven
// position must land in the same place or the lever visibly jumps when the
// pointer is released.
const LEVER_TRAVEL = 46;
// Past this and the carriage latches; short of it, it springs back with no
// submission. A review spends real money, so "any downward movement commits"
// is not an option. Two thirds is far enough to be unmistakably deliberate
// and near enough that nobody has to fight the control.
const LEVER_COMMIT_TRAVEL = LEVER_TRAVEL * (2 / 3);
// Below this, the gesture was a click rather than a drag, and a click is a
// full pull. Requiring a drag would make the lever unusable on a trackpad.
const LEVER_DRAG_SLOP = 3;

// Browning-slider detents (issue #495), user-space x on the 197..299 track —
// one per BROWNING_SETTINGS entry, so the engraved ticks and the control can
// never disagree about how many intensities exist. Phase 1 drew four ticks as
// placeholder art; four ticks with three settings is the appliance lying about
// its own controls.
const BROWNING_DETENT_X = BROWNING_SETTINGS.map(
  (_, index) => 210 + (76 * index) / Math.max(1, BROWNING_SETTINGS.length - 1),
);

export function ToasterHero({
  entries,
  value,
  onChange,
  phase,
  onDownload,
  downloadDisabled,
  progressStage,
  onLeverPull,
  leverArmed = false,
  browning = DEFAULT_BROWNING,
  onBrowningChange,
}: ToasterHeroProps): React.ReactElement {
  // Namespace every gradient/filter id so multiple toasters on a page can't
  // collide on url(#…) references. useId is stable across renders; strip the
  // colons React emits so the ids are safe inside url() fragments.
  const uid = useId().replace(/:/g, '');
  const g = (name: string) => `${name}-${uid}`;

  const hasDial = entries.length > 0;
  const browningIndex = Math.max(
    0,
    BROWNING_SETTINGS.findIndex((setting) => setting.id === browning),
  );
  const browningX = BROWNING_DETENT_X[browningIndex] ?? BROWNING_DETENT_X[0];
  const leverDown = phase !== 'idle';
  const working = phase === 'working';

  // ---------------------------------------------------------------------
  // The lever as a control (issue #494)
  //
  // `dragOffset` is the lever's live position while a pointer is down, in
  // the SVG's own user-space units. null means "not dragging" and hands the
  // lever back to the CSS class that reflects `phase`, so a drag that springs
  // back leaves no residue on the state-driven position.
  //
  // A HALF-PULL MUST NOT SUBMIT. A review spends real money, so the drag is
  // deliberately not "any downward movement commits" — it commits at
  // LEVER_COMMIT_TRAVEL and springs back below it, which is also how a real
  // toaster's carriage latch behaves.
  // ---------------------------------------------------------------------
  const [dragOffset, setDragOffset] = useState<number | null>(null);
  const dragStartRef = useRef<{ clientY: number; scale: number } | null>(null);
  const leverInteractive = Boolean(onLeverPull) && leverArmed;

  const commitLever = useCallback(() => {
    if (!leverInteractive) return;
    onLeverPull?.();
  }, [leverInteractive, onLeverPull]);

  const onLeverPointerDown = useCallback(
    (event: React.PointerEvent<SVGGElement>) => {
      if (!leverInteractive) return;
      // The SVG scales with the layout, so a client-pixel delta means nothing
      // on its own. Capture the conversion factor at grab time rather than
      // per move: the element cannot resize mid-drag, and measuring once
      // keeps the move handler free of layout reads.
      const rect = event.currentTarget.ownerSVGElement?.getBoundingClientRect();
      const scale = rect && rect.height > 0 ? VIEWBOX_HEIGHT / rect.height : 1;
      dragStartRef.current = { clientY: event.clientY, scale };
      setDragOffset(0);
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    [leverInteractive],
  );

  const onLeverPointerMove = useCallback((event: React.PointerEvent<SVGGElement>) => {
    const start = dragStartRef.current;
    if (!start) return;
    const travelled = (event.clientY - start.clientY) * start.scale;
    // Clamped at both ends: the carriage cannot be pulled UP past its rest
    // position, and cannot go past its stop.
    setDragOffset(Math.min(LEVER_TRAVEL, Math.max(0, travelled)));
  }, []);

  const onLeverPointerUp = useCallback(
    (event: React.PointerEvent<SVGGElement>) => {
      const start = dragStartRef.current;
      if (!start) return;
      dragStartRef.current = null;
      const travelled = (event.clientY - start.clientY) * start.scale;
      setDragOffset(null);
      event.currentTarget.releasePointerCapture?.(event.pointerId);
      // A click (no meaningful travel) is a full pull. Someone who taps the
      // lever has expressed the same intent as someone who drags it, and
      // requiring a drag would make the control unusable on a trackpad.
      if (travelled >= LEVER_COMMIT_TRAVEL || travelled < LEVER_DRAG_SLOP) {
        commitLever();
      }
    },
    [commitLever],
  );

  const onLeverKeyDown = useCallback(
    (event: React.KeyboardEvent<SVGGElement>) => {
      if (!leverInteractive) return;
      if (event.key !== 'Enter' && event.key !== ' ' && event.key !== 'Spacebar') return;
      // Space scrolls the page by default, which is exactly wrong on a
      // control whose whole job is to move downward.
      event.preventDefault();
      commitLever();
    },
    [leverInteractive, commitLever],
  );

  // While dragging, the inline transform wins over the class-driven position;
  // the moment the drag ends it is removed and `phase` is back in charge.
  const leverStyle =
    dragOffset === null ? undefined : { transform: `translateY(${dragOffset}px)` };

  // The decorative pointer sweeps a -60°..+60° arc, one stop per entry index.
  const selectedIndex = Math.max(
    0,
    entries.findIndex((e) => e.playbook_id === value),
  );
  // stopAngle(i) is the "nameplate" angle (0 = pointing straight up, the
  // pointer's rest orientation before its CSS rotate()) for stop i, evenly
  // spread across the same -60°..+60° arc the pointer sweeps.
  const stopAngle = (index: number) =>
    entries.length > 1 ? -60 + (120 * index) / (entries.length - 1) : 0;
  const pointerAngle = stopAngle(selectedIndex);

  // Machined-knob texture: a dense knurled ring just outside the knob face
  // (purely decorative, evenly spaced), plus one engraved tick per dial stop
  // set at the exact angle the pointer lands on when that stop is selected —
  // converting the "pointing up = 0°, clockwise" nameplate angle to standard
  // SVG (x = cos, y = sin) coordinates needs a -90° offset (north is -90°).
  const KNURL_COUNT = 48;
  const knurlTicks = Array.from({ length: KNURL_COUNT }, (_, i) => {
    const a = ((i * 360) / KNURL_COUNT) * (Math.PI / 180);
    return {
      x1: DIAL_CX + (DIAL_R + 2) * Math.cos(a),
      y1: DIAL_CY + (DIAL_R + 2) * Math.sin(a),
      x2: DIAL_CX + (DIAL_R + 5) * Math.cos(a),
      y2: DIAL_CY + (DIAL_R + 5) * Math.sin(a),
    };
  });
  const stopTicks = entries.map((entry, i) => {
    const a = ((stopAngle(i) - 90) * Math.PI) / 180;
    return {
      key: entry.playbook_id,
      x1: DIAL_CX + (DIAL_R - 10) * Math.cos(a),
      y1: DIAL_CY + (DIAL_R - 10) * Math.sin(a),
      x2: DIAL_CX + (DIAL_R - 3) * Math.cos(a),
      y2: DIAL_CY + (DIAL_R - 3) * Math.sin(a),
    };
  });

  // -----------------------------------------------------------------------
  // The dial as a pointer control (issue #490)
  //
  // The radiogroup below the stage is untouched — same roles, same roving
  // tabIndex, same arrow/Home/End handling. This adds a POINTER route to the
  // same `onChange`, exactly as the lever adds a pointer route to submission.
  // A visually-hidden proxy control would be a second thing that can disagree
  // with the art the user is actually grabbing.
  //
  // Only ACTIVE entries are reachable by drag, for the same reason arrow keys
  // skip them: an unactivated playbook fails closed at load_playbook, so
  // offering one could only ever 503.
  // -----------------------------------------------------------------------
  const [dragAngle, setDragAngle] = useState<number | null>(null);
  const dialInteractive = entries.filter((e) => e.status === 'active').length > 1;

  /** The nameplate angle (0 = north, clockwise) of a client-space point. */
  const angleFromCentre = useCallback((event: React.PointerEvent<SVGGElement>) => {
    const svg = event.currentTarget.ownerSVGElement;
    const rect = svg?.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return null;
    // Client -> user space. The viewBox has no aspect distortion here, but the
    // two axes are scaled independently anyway so a future viewBox change
    // cannot silently skew the angle.
    const x = ((event.clientX - rect.left) / rect.width) * VIEWBOX_WIDTH - DIAL_CX;
    const y = ((event.clientY - rect.top) / rect.height) * VIEWBOX_HEIGHT - DIAL_CY;
    // atan2(x, -y) puts 0° at north and grows clockwise, matching stopAngle.
    return (Math.atan2(x, -y) * 180) / Math.PI;
  }, []);

  /** The index of the nearest ACTIVE stop to a nameplate angle, or null. */
  const nearestStopIndex = useCallback(
    (angle: number) => {
      let best: number | null = null;
      let bestDelta = Infinity;
      entries.forEach((entry, index) => {
        if (entry.status !== 'active') return;
        const delta = Math.abs(stopAngle(index) - angle);
        if (delta < bestDelta) {
          bestDelta = delta;
          best = index;
        }
      });
      return best;
    },
    // `entries` is the only input; stopAngle is derived from it in render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [entries],
  );

  const commitAngle = useCallback(
    (angle: number) => {
      const index = nearestStopIndex(angle);
      if (index === null) return;
      const entry = entries[index];
      if (entry && entry.playbook_id !== value) onChange(entry.playbook_id);
    },
    [entries, nearestStopIndex, onChange, value],
  );

  const onDialPointerDown = useCallback(
    (event: React.PointerEvent<SVGGElement>) => {
      if (!dialInteractive) return;
      const angle = angleFromCentre(event);
      if (angle === null) return;
      // Clamped to the arc the stops actually occupy: dragging past the last
      // stop must park ON it, not wrap around to the first one.
      setDragAngle(Math.max(-60, Math.min(60, angle)));
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    [dialInteractive, angleFromCentre],
  );

  const onDialPointerMove = useCallback(
    (event: React.PointerEvent<SVGGElement>) => {
      if (dragAngle === null) return;
      const angle = angleFromCentre(event);
      if (angle === null) return;
      setDragAngle(Math.max(-60, Math.min(60, angle)));
    },
    [dragAngle, angleFromCentre],
  );

  const onDialPointerUp = useCallback(
    (event: React.PointerEvent<SVGGElement>) => {
      if (dragAngle === null) return;
      const angle = angleFromCentre(event) ?? dragAngle;
      setDragAngle(null);
      event.currentTarget.releasePointerCapture?.(event.pointerId);
      // Commit on release, never during the drag. A dial that fired onChange
      // on every pointermove would submit-adjacent state changes at ~60Hz and
      // make "which type did I actually pick" a race with the mouse.
      commitAngle(Math.max(-60, Math.min(60, angle)));
    },
    [dragAngle, angleFromCentre, commitAngle],
  );

  // While dragging, the needle tracks the finger; the moment it is released it
  // snaps back to the SELECTED stop's angle — which, if the commit landed, is
  // the stop under the finger. Selection stays the single source of truth.
  // A nameplate on the hero was built and then removed: at 360px the hero's
  // user-space units render at 0.857 scale, so text small enough to fit
  // between the dial and the receipt slot lands under 7 real pixels AND
  // overlaps the slot. Present but unreadable is worse than absent, and the
  // pill row below is already the labelled readout of exactly this value.
  const needleAngle = dragAngle ?? pointerAngle;

  return (
    <div className={`toaster-hero${phase === 'error' ? ' toaster-hero--sober' : ''}`}>
      <div className="toaster-hero__stage">
        <svg
          className="toaster-hero__svg"
          viewBox="0 0 420 340"
          role="img"
          aria-hidden="true"
          focusable="false"
        >
          {/* ---------------------------------------------------------------
              PART MAP (issue #493) — every group below carries a stable
              `data-part` as well as a `useId`-scoped `id`. Follow-on tickets
              (#494 lever, #496 theater, #502 motion, #490 dial, #498 receipt,
              #501 odometer) target `data-part`, NOT the id: two ToasterHeroes
              on one page must not collide, so the ids are per-instance and
              cannot be written into a stylesheet or a querySelector.

                countertop     the surface, plus the appliance's own reflection
                ground-shadow  contact shadow (tight core + wide haze)
                base           plinth and feet
                shell          the brushed-steel body, its grain and speculars
                deck           the rolled top surface the slots sit in
                slot-left      left opening, its AO and inner shadow
                slot-right     right opening
                glow           warm interior light rising through both slots
                knob           the timer dial (decoration; the radiogroup is
                               the control — see this file's header)
                browning       the heat-setting slider (#495)
                odometer       the lifetime-count window (#501)
                receipt-slot   where the provenance slip emerges (#498)
                crumb-tray     the drawer at the base (#500, parked)
                lever-track    the slot the lever rides in
                lever          the lever itself (#494)
                toast-slice    the slice that pops on done (#496)

              LIGHTING — one key light, top-left, everywhere. Every specular
              sits up-left of its form's centre and every ambient-occlusion
              shadow sits down-right of it. That single rule is what makes the
              separate parts read as one object; break it in one part and the
              whole appliance goes flat.
              --------------------------------------------------------------- */}
          <defs>
            {/* Brushed stainless — eight alternating bands, not a two-stop
                ramp. Real brushed metal alternates light and dark along the
                brush axis; that alternation IS the material. */}
            <linearGradient id={g('shell')} x1="0.10" y1="0" x2="0.24" y2="1">
              <stop offset="0" className="ct-sh0" />
              <stop offset="0.10" className="ct-sh1" />
              <stop offset="0.26" className="ct-sh2" />
              <stop offset="0.44" className="ct-sh3" />
              <stop offset="0.60" className="ct-sh4" />
              <stop offset="0.76" className="ct-sh5" />
              <stop offset="0.90" className="ct-sh6" />
              <stop offset="1" className="ct-sh7" />
            </linearGradient>
            {/* The rolled top deck catches the key light more directly than
                the front face, so it gets its own lighter ramp. */}
            <linearGradient id={g('deck')} x1="0" y1="0" x2="0.3" y2="1">
              <stop offset="0" className="ct-sh2" />
              <stop offset="0.5" className="ct-sh0" />
              <stop offset="1" className="ct-sh3" />
            </linearGradient>
            {/* Cylindrical falloff across the width. The shell is a rounded
                box: both flanks turn away from the viewer and darken, and the
                left one carries the light's own reflection just inside the
                edge. Without this the body reads as a flat rectangle no
                amount of banding can rescue. */}
            <linearGradient id={g('flank')} x1="0" y1="0" x2="1" y2="0">
              <stop offset="0" stopColor="#000" stopOpacity="0.34" />
              <stop offset="0.08" stopColor="#000" stopOpacity="0.08" />
              <stop offset="0.26" stopColor="#fff" stopOpacity="0.18" />
              <stop offset="0.50" stopColor="#fff" stopOpacity="0.02" />
              <stop offset="0.78" stopColor="#000" stopOpacity="0.12" />
              <stop offset="1" stopColor="#000" stopOpacity="0.38" />
            </linearGradient>
            {/* Anisotropic brush grain: turbulence stretched almost flat on
                one axis, so the noise becomes horizontal streaking rather
                than clouds. `numOctaves={2}` is a deliberate ceiling — the
                filter runs on every paint and the third octave buys grain
                nobody can see at 360px. */}
            <filter id={g('brush')} x="0" y="0" width="100%" height="100%">
              <feTurbulence type="fractalNoise" baseFrequency="0.010 1.1" numOctaves="2" seed="7" result="n" />
              <feColorMatrix in="n" type="saturate" values="0" />
              <feComponentTransfer>
                <feFuncA type="linear" slope="0.13" intercept="0" />
              </feComponentTransfer>
            </filter>
            {/* Specular blade down the left shoulder — bright core, fast
                falloff. A soft even glow here would read as a smudge. */}
            <linearGradient id={g('spec')} x1="0" y1="0" x2="1" y2="0">
              <stop offset="0" stopColor="#fff" stopOpacity="0" />
              <stop offset="0.40" stopColor="#fff" stopOpacity="0.80" />
              <stop offset="0.58" stopColor="#fff" stopOpacity="0.26" />
              <stop offset="1" stopColor="#fff" stopOpacity="0" />
            </linearGradient>
            {/* The room the chrome mirrors back — one soft diagonal sweep. */}
            <linearGradient id={g('env')} x1="0" y1="0" x2="0.9" y2="1">
              <stop offset="0" stopColor="#fff" stopOpacity="0" />
              <stop offset="0.38" stopColor="#fff" stopOpacity="0.18" />
              <stop offset="0.50" stopColor="#fff" stopOpacity="0.20" />
              <stop offset="0.62" stopColor="#fff" stopOpacity="0" />
              <stop offset="1" stopColor="#fff" stopOpacity="0" />
            </linearGradient>
            <linearGradient id={g('base')} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" className="ct-base-hi" />
              <stop offset="1" className="ct-base-lo" />
            </linearGradient>
            <linearGradient id={g('slot')} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" className="ct-slot-hi" />
              <stop offset="0.55" className="ct-slot-mid" />
              <stop offset="1" className="ct-slot-lo" />
            </linearGradient>
            {/* Bakelite — deep, warm, and lit from up-left like everything
                else. Used for the three turned parts a real appliance would
                mould rather than press: dial, browning thumb, lever cap. */}
            <radialGradient id={g('bakelite')} cx="0.32" cy="0.26" r="0.88">
              <stop offset="0" className="ct-bk-hi" />
              <stop offset="0.52" className="ct-bk-mid" />
              <stop offset="1" className="ct-bk-lo" />
            </radialGradient>
            <radialGradient id={g('knob')} cx="0.34" cy="0.28" r="0.82">
              <stop offset="0" className="ct-knob-hi" />
              <stop offset="0.52" className="ct-knob-mid" />
              <stop offset="1" className="ct-knob-lo" />
            </radialGradient>
            <radialGradient id={g('glow')} cx="0.5" cy="0.5" r="0.5">
              <stop offset="0" stopColor="var(--ct-glow, #ff7a1a)" stopOpacity="0.9" />
              <stop offset="0.55" stopColor="var(--ct-glow, #ff7a1a)" stopOpacity="0.3" />
              <stop offset="1" stopColor="var(--ct-glow, #ff7a1a)" stopOpacity="0" />
            </radialGradient>
            <radialGradient id={g('glowCore')} cx="0.5" cy="0.5" r="0.5">
              <stop offset="0" stopColor="#fff6ec" stopOpacity="0.9" />
              <stop offset="0.5" stopColor="var(--ct-glow, #ff7a1a)" stopOpacity="0.55" />
              <stop offset="1" stopColor="var(--ct-glow, #ff7a1a)" stopOpacity="0" />
            </radialGradient>

            {/* Countertop. It has to end SOMEWHERE, and a hard rectangular
                edge would read as a beige slab pasted behind the appliance —
                so both the surface and its reflection are masked by a
                horizontal fade that dissolves them into the stage. */}
            <linearGradient id={g('counterFade')} x1="0" y1="0" x2="1" y2="0">
              <stop offset="0" stopColor="#fff" stopOpacity="0" />
              <stop offset="0.18" stopColor="#fff" stopOpacity="1" />
              <stop offset="0.82" stopColor="#fff" stopOpacity="1" />
              <stop offset="1" stopColor="#fff" stopOpacity="0" />
            </linearGradient>
            <mask id={g('counterMask')}>
              <rect x="0" y="270" width="420" height="70" fill={`url(#${g('counterFade')})`} />
            </mask>
            <linearGradient id={g('counter')} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" className="ct-counter-far" />
              <stop offset="0.35" className="ct-counter" />
              <stop offset="1" stopColor="var(--ct-counter, #efe6d8)" stopOpacity="0" />
            </linearGradient>
            <linearGradient id={g('reflFade')} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#fff" stopOpacity="0.30" />
              <stop offset="0.45" stopColor="#fff" stopOpacity="0.07" />
              <stop offset="1" stopColor="#fff" stopOpacity="0" />
            </linearGradient>
            <mask id={g('reflMask')}>
              <rect
                x="0"
                y="288"
                width="420"
                height="52"
                fill={`url(#${g('reflFade')})`}
                mask={`url(#${g('counterMask')})`}
              />
            </mask>

            <filter id={g('soft')} x="-40%" y="-40%" width="180%" height="180%">
              <feGaussianBlur stdDeviation="7" />
            </filter>
            <filter id={g('tight')} x="-40%" y="-40%" width="180%" height="180%">
              <feGaussianBlur stdDeviation="2.2" />
            </filter>
            {/* Ambient occlusion: the soft dark that collects wherever two
                surfaces meet. Reused by every part rather than tuned per
                part, so the contact shadows agree with one another. */}
            <filter id={g('ao')} x="-40%" y="-40%" width="180%" height="180%">
              <feGaussianBlur stdDeviation="3" />
            </filter>
            <filter id={g('haze')} x="-60%" y="-60%" width="220%" height="220%">
              <feGaussianBlur stdDeviation="6" />
            </filter>
            <filter id={g('bloom')} x="-60%" y="-60%" width="220%" height="220%">
              <feGaussianBlur stdDeviation="2.5" />
            </filter>
            <filter id={g('reflBlur')} x="-20%" y="-20%" width="140%" height="140%">
              <feGaussianBlur stdDeviation="3.2" />
            </filter>
            <clipPath id={g('shellClip')}>
              <path d={SHELL_PATH} />
            </clipPath>
          </defs>

          {/* ============ countertop + the appliance's reflection ============ */}
          <g id={g('countertop')} data-part="countertop">
            <g mask={`url(#${g('counterMask')})`}>
              <rect x="0" y="270" width="420" height="70" fill={`url(#${g('counter')})`} />
              <rect x="0" y="270" width="420" height="1" fill="#fff" opacity="0.35" />
            </g>
            {/* A suggestion of the appliance, not a second copy of the art:
                the silhouette and the two slots, flipped about the contact
                line, blurred and faded. Anything more detailed would compete
                with the object casting it. */}
            <g mask={`url(#${g('reflMask')})`} filter={`url(#${g('reflBlur')})`}>
              <g transform="translate(0,556) scale(1,-1)">
                <path d={SHELL_PATH} fill={`url(#${g('shell')})`} />
                <rect x="126" y="96" width="76" height="20" rx="7" fill="var(--ct-slot-mid, #161b21)" />
                <rect x="218" y="96" width="76" height="20" rx="7" fill="var(--ct-slot-mid, #161b21)" />
              </g>
            </g>
          </g>

          {/* ============ contact shadow ============ */}
          <g id={g('groundShadow')} data-part="ground-shadow">
            <ellipse cx="210" cy="280" rx="146" ry="13" fill="var(--ct-contact-shadow, #2a140a)" filter={`url(#${g('soft')})`} />
            {/* The tight core is what actually glues the feet to the counter;
                the wide haze alone reads as hovering. */}
            <ellipse cx="210" cy="277" rx="98" ry="6" fill="var(--ct-contact-shadow, #2a140a)" filter={`url(#${g('tight')})`} opacity="0.85" />
          </g>

          {/* ============ base plinth + feet ============ */}
          <g id={g('basePlate')} data-part="base">
            <rect x="84" y="248" width="252" height="24" rx="10" fill={`url(#${g('base')})`} />
            <rect x="84" y="248" width="252" height="2" rx="1" fill="#fff" opacity="0.18" />
            <rect x="104" y="268" width="30" height="9" rx="3" fill="var(--ct-feet, #0e1012)" />
            <rect x="286" y="268" width="30" height="9" rx="3" fill="var(--ct-feet, #0e1012)" />
          </g>

          {/* ============ the shell ============ */}
          <g id={g('shellGroup')} data-part="shell">
            <path d={SHELL_PATH} fill={`url(#${g('shell')})`} />
            <g clipPath={`url(#${g('shellClip')})`}>
              <rect x="92" y="104" width="236" height="164" filter={`url(#${g('brush')})`} opacity="0.9" />
              <rect x="92" y="104" width="236" height="164" fill={`url(#${g('flank')})`} />
              <rect x="92" y="104" width="236" height="164" fill={`url(#${g('env')})`} />
              <rect x="106" y="118" width="22" height="146" rx="11" fill={`url(#${g('spec')})`} />
              {/* The shoulder seam — where the curved deck rolls into the
                  flat front face. A dark band with a bright line just above
                  it is the whole trick: it turns one silhouette into two
                  planes meeting at an edge. */}
              <path d="M92 146 Q210 138 328 146" fill="none" stroke="#000" strokeOpacity="0.10" strokeWidth="6" />
              <path d="M92 143 Q210 135 328 143" fill="none" stroke="#fff" strokeOpacity="0.30" strokeWidth="1.4" />
              <ellipse cx="210" cy="270" rx="126" ry="14" fill="#000" opacity="0.32" filter={`url(#${g('ao')})`} />
            </g>
            <path d={SHELL_PATH_OPEN} fill="none" stroke="var(--ct-chrome-edge, #8b9196)" strokeWidth="1.5" />
            <path d="M140 105 H280" stroke="#fff" strokeOpacity="0.8" strokeWidth="1.8" strokeLinecap="round" />
            <path d="M93 158 V250" stroke="#fff" strokeOpacity="0.22" strokeWidth="1.4" strokeLinecap="round" />
          </g>

          {/* ============ top deck ============ */}
          <g id={g('deckGroup')} data-part="deck">
            <path d="M104 122 Q210 108 316 122 L316 132 Q210 118 104 132 Z" fill={`url(#${g('deck')})`} opacity="0.5" />
          </g>

          {/* ============ slots ============ */}
          <g id={g('slotLeft')} data-part="slot-left">
            <ellipse cx="164" cy="112" rx="44" ry="8" fill="#000" opacity="0.20" filter={`url(#${g('ao')})`} />
            <rect x="126" y="97" width="76" height="18" rx="6" fill={`url(#${g('slot')})`} stroke="var(--ct-chrome-edge-soft, #7c8288)" strokeWidth="1.4" />
            <rect x="129" y="99" width="70" height="3" rx="1.5" fill="#000" opacity="0.6" />
          </g>
          <g id={g('slotRight')} data-part="slot-right">
            <ellipse cx="256" cy="112" rx="44" ry="8" fill="#000" opacity="0.20" filter={`url(#${g('ao')})`} />
            <rect x="218" y="97" width="76" height="18" rx="6" fill={`url(#${g('slot')})`} stroke="var(--ct-chrome-edge-soft, #7c8288)" strokeWidth="1.4" />
            <rect x="221" y="99" width="70" height="3" rx="1.5" fill="#000" opacity="0.6" />
          </g>

          {/* ============ warm interior light, rising through both slots ============ */}
          {working && (
            <g id={g('glowGroup')} data-part="glow" className="toaster-glow">
              <g filter={`url(#${g('haze')})`}>
                <ellipse cx="164" cy="103" rx="44" ry="18" fill={`url(#${g('glow')})`} />
                <ellipse cx="256" cy="103" rx="44" ry="18" fill={`url(#${g('glow')})`} />
              </g>
              <g filter={`url(#${g('bloom')})`}>
                <ellipse cx="164" cy="101" rx="24" ry="6" fill={`url(#${g('glowCore')})`} />
                <ellipse cx="256" cy="101" rx="24" ry="6" fill={`url(#${g('glowCore')})`} />
              </g>
            </g>
          )}

          {/* ============ the timer dial ============
              A POINTER route to the same selection the radiogroup below the
              stage owns (issue #490). The radiogroup is untouched — same
              roles, same roving tabIndex, same arrow/Home/End — and remains
              the keyboard and screen-reader surface; this is the affordance
              that makes the appliance the thing you set.

              Un-interactive with a single active stop: there is nowhere to
              turn to, and a knob that moves and changes nothing is a dead
              affordance dressed as a live one. */}
          {hasDial && (
            <g
              id={g('knobGroup')}
              data-part="knob"
              data-testid="toaster-dial-knob"
              className={dialInteractive ? 'toaster-knob toaster-knob--interactive' : 'toaster-knob'}
              onPointerDown={dialInteractive ? onDialPointerDown : undefined}
              onPointerMove={dialInteractive ? onDialPointerMove : undefined}
              onPointerUp={dialInteractive ? onDialPointerUp : undefined}
              onPointerCancel={dialInteractive ? () => setDragAngle(null) : undefined}
            >
              {/* The grab surface. The knob face is r=24 in a 420-unit
                  viewBox — about 20 real pixels at 360px wide — so the ring
                  the user actually aims at has to be bigger than the art. */}
              {dialInteractive && (
                <circle
                  cx={DIAL_CX}
                  cy={DIAL_CY}
                  r={DIAL_R + 14}
                  fill="transparent"
                  style={{ cursor: 'grab' }}
                />
              )}
              <ellipse cx={DIAL_CX} cy={DIAL_CY + 4} rx={DIAL_R + 5} ry={DIAL_R + 3} fill="#000" opacity="0.20" filter={`url(#${g('ao')})`} />
              {/* The bakelite grip ring the knurling is cut into. */}
              <circle cx={DIAL_CX} cy={DIAL_CY} r={DIAL_R + 4} fill={`url(#${g('bakelite')})`} />
              {knurlTicks.map((t, i) => (
                <line key={i} x1={t.x1} y1={t.y1} x2={t.x2} y2={t.y2} stroke="var(--ct-knurl, #6f7479)" strokeWidth="1" opacity="0.55" />
              ))}
              <circle cx={DIAL_CX} cy={DIAL_CY} r={DIAL_R} fill={`url(#${g('knob')})`} stroke="var(--ct-chrome-edge-strong, #83898e)" strokeWidth="1.3" />
              {/* Own specular: an offset crescent up-left, plus a much
                  weaker bounce down-right. A centred dot would read as a
                  bubble rather than a domed face. */}
              <ellipse cx={DIAL_CX - 8} cy={DIAL_CY - 10} rx="11" ry="6" fill="#fff" opacity="0.46" transform={`rotate(-30 ${DIAL_CX - 8} ${DIAL_CY - 10})`} />
              <ellipse cx={DIAL_CX + 9} cy={DIAL_CY + 11} rx="8" ry="4" fill="#fff" opacity="0.10" transform={`rotate(-30 ${DIAL_CX + 9} ${DIAL_CY + 11})`} />
              {stopTicks.map((t) => (
                <line key={t.key} x1={t.x1} y1={t.y1} x2={t.x2} y2={t.y2} stroke="var(--ct-chrome-edge-strong, #83898e)" strokeWidth="1.6" strokeLinecap="round" opacity="0.85" />
              ))}
              {/* Rotating pointer group. The transparent full-diameter disc
                  fixes the fill-box centre on the knob so CSS rotation pivots
                  exactly at (DIAL_CX, DIAL_CY). */}
              <g
                className={`toaster-pointer${dragAngle !== null ? ' toaster-pointer--dragging' : ''}`}
                style={{ transform: `rotate(${needleAngle}deg)` }}
                data-testid="toaster-dial-needle"
                data-angle={String(Math.round(needleAngle))}
              >
                <circle cx={DIAL_CX} cy={DIAL_CY} r={DIAL_R} fill="transparent" />
                <path d={`M ${DIAL_CX - 2.5} ${DIAL_CY} L ${DIAL_CX + 2.5} ${DIAL_CY} L ${DIAL_CX} ${DIAL_CY - (DIAL_R - 4)} Z`} fill="var(--ct-accent, #af4b29)" />
                <circle cx={DIAL_CX} cy={DIAL_CY} r="4.5" fill="var(--ct-lever-pin, #5a5f63)" stroke="var(--ct-lever-pin-edge, #e8ebed)" strokeWidth="1.2" />
              </g>
            </g>
          )}

          {/* ============ browning control (#495) ============
              Decoration that MIRRORS the real radiogroup below the appliance —
              never a replacement for it. One detent per setting, so the art
              cannot imply a fourth intensity the control does not offer. */}
          <g id={g('browning')} data-part="browning" data-browning-level={browning}>
            <rect x="196" y="190" width="104" height="11" rx="5.5" fill="#000" opacity="0.24" />
            <rect x="197" y="191" width="102" height="9" rx="4.5" fill={`url(#${g('slot')})`} />
            <g opacity="0.5">
              {BROWNING_DETENT_X.map((x) => (
                <line key={x} x1={x} y1="184" x2={x} y2="188" stroke="var(--ct-chrome-edge-strong, #83898e)" strokeWidth="1.4" />
              ))}
            </g>
            <ellipse cx={browningX} cy="197" rx="11" ry="10" fill="#000" opacity="0.22" filter={`url(#${g('ao')})`} />
            <rect
              data-testid="toaster-browning-slider"
              className="toaster-browning-slider"
              x={browningX - 8.5}
              y="187"
              width="17"
              height="18"
              rx="5"
              fill={`url(#${g('bakelite')})`}
              stroke="var(--ct-lever-knob-edge, #7a3a20)"
              strokeWidth="1"
            />
            <rect x={browningX - 5.5} y="190" width="4" height="12" rx="2" fill="#fff" opacity="0.30" />
          </g>

          {/* ============ lifetime-count window (#501 fills it) ============ */}
          <g id={g('odometer')} data-part="odometer">
            <rect x="200" y="216" width="58" height="17" rx="4" fill="#000" opacity="0.26" />
            <rect x="201" y="217" width="56" height="15" rx="3" fill="var(--ct-slot-mid, #161b21)" />
            <g opacity="0.32">
              <rect x="205" y="221" width="7" height="7" rx="1" fill="#fff" />
              <rect x="215" y="221" width="7" height="7" rx="1" fill="#fff" />
              <rect x="225" y="221" width="7" height="7" rx="1" fill="#fff" />
              <rect x="235" y="221" width="7" height="7" rx="1" fill="#fff" />
              <rect x="245" y="221" width="7" height="7" rx="1" fill="#fff" />
            </g>
            {/* Glass: one bright band across the top of the recess. */}
            <rect x="201" y="217" width="56" height="5" rx="2.5" fill="#fff" opacity="0.10" />
          </g>

          {/* ============ receipt slot (#498 feeds it) ============ */}
          <g id={g('receiptSlot')} data-part="receipt-slot">
            <rect x="142" y="238" width="136" height="5" rx="2.5" fill="#000" opacity="0.26" />
            <rect x="142" y="237.2" width="136" height="1.2" rx="0.6" fill="#fff" opacity="0.20" />
          </g>

          {/* ============ crumb tray (#500, parked — the drawer is art only) ============ */}
          <g id={g('crumbTray')} data-part="crumb-tray">
            <rect x="100" y="254" width="220" height="11" rx="4" fill="#000" opacity="0.28" />
            <rect x="100" y="254" width="220" height="1.5" rx="0.75" fill="#fff" opacity="0.16" />
            <rect x="196" y="258" width="28" height="4" rx="2" fill="var(--ct-chrome-edge, #8b9196)" opacity="0.65" />
          </g>

          {/* ============ lever (#494 animates it) ============ */}
          <g id={g('leverTrack')} data-part="lever-track">
            <rect x="329" y="146" width="13" height="74" rx="6.5" fill="#000" opacity="0.24" />
            <rect x="330" y="147" width="11" height="72" rx="5.5" fill={`url(#${g('slot')})`} />
          </g>
          {/*
            The lever is a real control when the caller wires one up (#494),
            and pure decoration otherwise. `role="button"` + `tabIndex` on a
            <g> is deliberate: the thing the user grabs IS this art, and a
            visually-hidden proxy button elsewhere would be a second control
            that can disagree with it. The form's submit button remains the
            keyboard-first path — this is an addition, never a replacement.
          */}
          <g
            id={g('lever')}
            data-part="lever"
            data-testid="toaster-lever"
            className={`toaster-lever${leverDown && dragOffset === null ? ' toaster-lever--down' : ''}${
              leverInteractive ? ' toaster-lever--interactive' : ''
            }`}
            style={leverStyle}
            role={leverInteractive ? 'button' : undefined}
            tabIndex={leverInteractive ? 0 : undefined}
            aria-label={leverInteractive ? 'Push the lever down to start the review' : undefined}
            aria-disabled={onLeverPull && !leverArmed ? true : undefined}
            onPointerDown={leverInteractive ? onLeverPointerDown : undefined}
            onPointerMove={leverInteractive ? onLeverPointerMove : undefined}
            onPointerUp={leverInteractive ? onLeverPointerUp : undefined}
            onPointerCancel={
              leverInteractive
                ? () => {
                    dragStartRef.current = null;
                    setDragOffset(null);
                  }
                : undefined
            }
            onKeyDown={leverInteractive ? onLeverKeyDown : undefined}
          >
            {/* An invisible grab target. The visible lever is a 26x11 bar and
                a r=10 cap — a hit area small enough to be genuinely annoying
                on touch, and well under the 44px minimum once the hero scales
                down. This rect is the control's real surface. */}
            {leverInteractive && (
              <rect x="312" y="140" width="52" height="46" fill="transparent" style={{ cursor: 'grab' }} />
            )}
            <rect x="318" y="152" width="26" height="11" rx="5.5" fill={`url(#${g('shell')})`} stroke="var(--ct-chrome-edge-strong, #83898e)" strokeWidth="1" />
            <ellipse cx="348" cy="159" rx="12" ry="11" fill="#000" opacity="0.22" filter={`url(#${g('ao')})`} />
            <circle cx="347" cy="157" r="10" fill={`url(#${g('bakelite')})`} stroke="var(--ct-lever-knob-edge, #7a3a20)" strokeWidth="1.2" />
            <ellipse cx="343" cy="153" rx="4.5" ry="2.6" fill="#fff" opacity="0.42" transform="rotate(-30 343 153)" />
          </g>
        </svg>

        {/* DONE — the contract slice pops out of the slot. When onDownload is
            wired in it is a real, focusable download button with a visible
            "Click to download" caption; otherwise it is decorative only. */}
        {phase === 'done' && (
          <div data-testid="toaster-state-done" className="toaster-hero__toast">
            <ToastSlice onDownload={onDownload} downloadDisabled={downloadDisabled} />
          </div>
        )}

        {/* ERROR — the burnt slice (issue #501). The appliance still wears
            the sober, unplugged treatment via the --sober wrapper; what
            changed is that the failure now looks like something the MACHINE
            did rather than a generic error glyph.

            The rule this art is under: burnt is never cute INSTEAD of
            informative. This is decoration only — aria-hidden, no text — and
            the classified cause and next step stay exactly where they were,
            in the danger banner. If the art ever became the explanation, a
            screen-reader user would be told nothing at all. */}
        {phase === 'error' && (
          <div data-testid="toaster-state-sober" className="toaster-hero__sober">
            <svg viewBox="0 0 120 120" width="120" height="120" aria-hidden="true" focusable="false">
              {/* Smoke: three wisps rising and fading. The animation is
                  defined in ToasterStyles and killed under reduced motion,
                  where they settle at a static, low opacity. */}
              <g className="toaster-smoke" data-testid="toaster-burnt-smoke">
                <path className="toaster-smoke__wisp toaster-smoke__wisp--a" d="M46 44 q-7 -11 0 -21 q7 -10 0 -19" fill="none" stroke="var(--ct-neutral, #5c5c5c)" strokeWidth="3.5" strokeLinecap="round" opacity="0.5" />
                <path className="toaster-smoke__wisp toaster-smoke__wisp--b" d="M60 40 q-8 -13 0 -24 q8 -11 0 -20" fill="none" stroke="var(--ct-neutral, #5c5c5c)" strokeWidth="3" strokeLinecap="round" opacity="0.42" />
                <path className="toaster-smoke__wisp toaster-smoke__wisp--c" d="M74 44 q-6 -10 0 -19 q6 -9 0 -17" fill="none" stroke="var(--ct-neutral, #5c5c5c)" strokeWidth="2.6" strokeLinecap="round" opacity="0.34" />
              </g>
              {/* The slice itself: charred, not merely dark — a scorched
                  crust edge, a blistered face, and one corner burnt through. */}
              <g data-testid="toaster-burnt-slice">
                <path
                  d="M36 56 h48 a6 6 0 0 1 6 6 v34 a6 6 0 0 1 -6 6 h-48 a6 6 0 0 1 -6 -6 v-34 a6 6 0 0 1 6 -6 z"
                  fill="var(--ct-burnt, #2b211c)"
                  stroke="var(--ct-burnt-crust, #120c09)"
                  strokeWidth="2.5"
                />
                {/* Blistering — irregular char bubbles, deliberately uneven. */}
                <circle cx="48" cy="70" r="4.5" fill="var(--ct-burnt-crust, #120c09)" opacity="0.85" />
                <circle cx="66" cy="80" r="6" fill="var(--ct-burnt-crust, #120c09)" opacity="0.8" />
                <circle cx="55" cy="90" r="3.5" fill="var(--ct-burnt-crust, #120c09)" opacity="0.7" />
                <circle cx="76" cy="66" r="3" fill="var(--ct-burnt-crust, #120c09)" opacity="0.6" />
                {/* Burnt through at one corner: a hole to the countertop. */}
                <path d="M78 92 q7 -4 10 4 q-6 6 -12 2 z" fill="var(--ct-counter, #d7cbb6)" opacity="0.55" />
              </g>
            </svg>
          </div>
        )}
      </div>

      {/* The accessible dial (radiogroup of stops) — only when there are
          entries. The SVG knob above is decoration that mirrors `value`. */}
      {hasDial && <ContractTypeDial entries={entries} value={value} onChange={onChange} />}

      {/* The browning control, beside the dial as designed. Rendered only when
          a handler is wired in — the state-illustration wrappers below show
          the appliance, not a live control that changes nothing. */}
      {onBrowningChange && <BrowningControl value={browning} onChange={onBrowningChange} />}

      {/* WORKING — staged doneness when the pipeline reports where it is;
          the indeterminate ring when it does not. */}
      {working && (
        <div data-testid="toaster-state-progress" className="toaster-hero__progress">
          <StagedDoneness progressStage={progressStage} />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// StagedDoneness — the progress indicator, and the reason this ticket exists.
//
// The appliance IS the bar: a slice of toast that darkens one step per REAL
// pipeline sub-stage (issue #447), not a line that flashes. The visual is the
// delight; the "Step 2 of 4 · Adversarial critic" text beneath it is the
// information and the accessibility, and it always ships with the darkening.
//
// The one rule that makes this honest: the step only ever advances when the
// backend reports a new `progress_stage` token. Nothing here is on a timer.
// The within-step heat shimmer is the only motion that runs on its own, and
// it says "still working on THIS step", which is true by construction.
//
// No reported stage (a runner that predates the seam, a deployment that
// reports nothing, a token this build doesn't know) -> the old indeterminate
// ring, unchanged. An honest "working" beats an invented "step 3 of 4".
// ---------------------------------------------------------------------------
function StagedDoneness({ progressStage }: { progressStage?: string | null }): React.ReactElement {
  const step = progressStepNumber(progressStage);

  if (step === 0) {
    return (
      <>
        <DonenessRing />
        <p data-testid="review-progress-indeterminate">Toasting your review…</p>
      </>
    );
  }

  const label = PROGRESS_STEPS[step - 1].label;
  const stepText = `Step ${step} of ${PROGRESS_STEPS.length} · ${label}`;
  // Issue #496. The vignette and its caption come from the same map the tab
  // title will read (#497), so the glass and the tab can never disagree about
  // what is happening. Null here is impossible given step > 0, but the type
  // says it can be and the fallback costs nothing.
  const vignette = vignetteForStage(progressStage);

  return (
    <>
      <div
        className={`toaster-doneness toaster-doneness--step${step}`}
        data-testid="review-progress-stage"
        data-progress-stage={progressStage ?? undefined}
        data-progress-step={String(step)}
        role="progressbar"
        aria-label="Review progress"
        aria-valuemin={1}
        aria-valuemax={PROGRESS_STEPS.length}
        aria-valuenow={step}
        aria-valuetext={stepText}
      >
        <DonenessSlice step={step} />
      </div>
      {/* The step text is the actual information — announced on change, and
          never replaced by the darkening alone. */}
      <p className="toaster-doneness__step" data-testid="review-progress-step-text" aria-live="polite">
        {stepText}
      </p>
      {/* Issue #496: the vignette is decoration for a caption that is itself
          the message. It is aria-hidden and the caption is NOT in a live
          region — the step text above already announces every transition, and
          a second polite region firing on the same commit is the #510 defect. */}
      {vignette && (
        <>
          <StageVignetteArt art={vignette.art} />
          <p className="toaster-doneness__caption" data-testid="review-stage-caption">
            {vignette.caption}
          </p>
        </>
      )}
    </>
  );
}


// ---------------------------------------------------------------------------
// StageVignetteArt — the small scene under the glass (issue #496).
//
// Each one depicts what the pipeline is ACTUALLY doing at that moment, which
// is the whole point: a spinner says "wait", and these say "a second model is
// arguing with the first one about your contract". The architecture is the
// reassurance; the art just makes it visible.
//
// Everything here is decoration. `aria-hidden`, and the caption beside it
// carries the meaning — a screen-reader user loses nothing by not seeing it.
//
// Motion is CSS, gated by the SAME `prefers-reduced-motion` block in
// ToasterStyles that every other toaster animation is, so reduced motion
// leaves the scenes static and the captions still advance. Nothing here
// loops on a timer in JS, so a hidden tab costs nothing: the browser stops
// painting CSS animations on its own.
// ---------------------------------------------------------------------------
function StageVignetteArt({ art }: { art: StageVignette['art'] }): React.ReactElement {
  return (
    <svg
      className={`toaster-vignette toaster-vignette--${art}`}
      viewBox="0 0 120 72"
      width="120"
      height="72"
      aria-hidden="true"
      focusable="false"
      data-testid="review-stage-vignette"
      data-vignette={art}
    >
      {/* The page every scene happens on. */}
      <rect x="34" y="6" width="52" height="60" rx="3" className="toaster-vignette__page" />
      {[16, 24, 32, 40, 48, 56].map((y) => (
        <rect key={y} x="40" y={y} width={y === 56 ? 22 : 40} height="2.5" rx="1.25" className="toaster-vignette__line" />
      ))}

      {art === 'marking-up' && (
        <>
          {/* One pen, one colour, working down the page. */}
          <g className="toaster-vignette__mark">
            <rect x="40" y="23" width="40" height="4" rx="2" className="toaster-vignette__ink-a" />
          </g>
          <g className="toaster-vignette__pen toaster-vignette__pen--a">
            <path d="M92 40 L104 28 L108 32 L96 44 Z" className="toaster-vignette__ink-a" />
            <path d="M92 40 L96 44 L90 46 Z" className="toaster-vignette__nib" />
          </g>
        </>
      )}

      {art === 'arguing' && (
        <>
          {/* The first pen's mark is already there; a SECOND pen, in a
              different colour, strikes through it. Two colours is the entire
              message — one model checking another. */}
          <rect x="40" y="23" width="40" height="4" rx="2" className="toaster-vignette__ink-a" />
          <g className="toaster-vignette__strike">
            <rect x="38" y="24" width="44" height="2" rx="1" className="toaster-vignette__ink-b" />
          </g>
          <g className="toaster-vignette__pen toaster-vignette__pen--b">
            <path d="M18 34 L6 22 L2 26 L14 38 Z" className="toaster-vignette__ink-b" />
            <path d="M18 34 L14 38 L20 40 Z" className="toaster-vignette__nib" />
          </g>
        </>
      )}

      {art === 'merging' && (
        <>
          {/* The two sets of marks slide together into one. */}
          <g className="toaster-vignette__merge-a">
            <rect x="40" y="23" width="18" height="4" rx="2" className="toaster-vignette__ink-a" />
          </g>
          <g className="toaster-vignette__merge-b">
            <rect x="62" y="23" width="18" height="4" rx="2" className="toaster-vignette__ink-b" />
          </g>
          <rect x="40" y="39" width="40" height="4" rx="2" className="toaster-vignette__ink-a" opacity="0.35" />
        </>
      )}

      {art === 'rolling' && (
        <>
          {/* The finished page rolls tight. */}
          <rect x="40" y="23" width="40" height="4" rx="2" className="toaster-vignette__ink-a" />
          <rect x="40" y="39" width="30" height="4" rx="2" className="toaster-vignette__ink-b" />
          <g className="toaster-vignette__roll">
            <rect x="30" y="4" width="10" height="64" rx="5" className="toaster-vignette__page" />
            <line x1="35" y1="10" x2="35" y2="62" className="toaster-vignette__line" strokeWidth="2" />
          </g>
        </>
      )}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// DonenessSlice — a small slice of toast whose crust deepens with `step`. The
// fill comes from `.toaster-doneness--step{n}` in ToasterStyles (a color-mix
// of the existing --ct-toast / --ct-toast-crust ramp, CTDS §5: tokens only);
// the inline `fill` here is only the no-CSS fallback. Purely decorative — the
// progressbar wrapper and the step text carry every accessible fact.
// ---------------------------------------------------------------------------
function DonenessSlice({ step }: { step: number }): React.ReactElement {
  // One heat mark per completed-or-current step, so the slice reads as
  // "further along" even where colour alone would not (colour is never the
  // sole carrier — the step text is authoritative).
  const marks = Array.from({ length: step }, (_, i) => 26 + i * 15);
  return (
    <svg viewBox="0 0 96 96" width="64" height="64" aria-hidden="true" focusable="false" style={{ display: 'block' }}>
      <path
        className="toaster-doneness__slice"
        d="M12 34 Q12 12 34 10 Q48 6 62 10 Q84 12 84 34 L84 84 Q84 88 80 88 L16 88 Q12 88 12 84 Z"
        fill="var(--ct-toast, #d9a463)"
        stroke="var(--ct-toast-crust, #8a5a2b)"
        strokeWidth="3"
      />
      <g className="toaster-doneness__heat" fill="var(--ct-glow, #ff7a1a)">
        {marks.map((y) => (
          <rect key={y} x="26" y={y} width="44" height="7" rx="3.5" />
        ))}
      </g>
    </svg>
  );
}

// ---------------------------------------------------------------------------
// DonenessRing — the indeterminate "doneness" sweep shown while a review runs.
// No numeric percentage is claimed (the pipeline reports none).
// ---------------------------------------------------------------------------
function DonenessRing(): React.ReactElement {
  const radius = 34;
  const circumference = 2 * Math.PI * radius;
  return (
    <svg viewBox="0 0 80 80" width="48" height="48" aria-hidden="true" focusable="false" style={{ display: 'block' }}>
      <circle className="toaster-progress-track" cx="40" cy="40" r={radius} />
      <circle
        className="toaster-progress-fill"
        cx="40"
        cy="40"
        r={radius}
        strokeDasharray={circumference}
        strokeDashoffset={circumference * 0.35}
        transform="rotate(-90 40 40)"
      />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// ToastSlice — the popped-out "contract" slice: toasted-bread fill with a
// crust edge, ruled text lines, a signature squiggle, and a small red seal.
// When `onDownload` is given it renders as a real focusable download button
// with a visible "Click to download" caption; otherwise it is decorative.
// ---------------------------------------------------------------------------
function ToastSlice({
  onDownload,
  downloadDisabled,
}: {
  onDownload?: () => void;
  downloadDisabled?: boolean;
}): React.ReactElement {
  const art = (
    <svg viewBox="0 0 120 132" width="96" height="106" aria-hidden="true" focusable="false" style={{ display: 'block' }}>
      {/* Bread slice: rounded, slightly domed top like a real toast. */}
      <path
        d="M14 44 Q14 16 42 13 Q60 8 78 13 Q106 16 106 44 L106 120 Q106 126 100 126 L20 126 Q14 126 14 120 Z"
        fill="var(--ct-toast, #d9a463)"
        stroke="var(--ct-toast-crust, #8a5a2b)"
        strokeWidth="3"
      />
      {/* Crumb texture speckled across the exposed crust margin — a toast
          slice, not a flat card. */}
      <g fill="var(--ct-toast-crust, #8a5a2b)" opacity="0.4">
        <circle cx="20" cy="52" r="1.3" />
        <circle cx="19" cy="90" r="1" />
        <circle cx="24" cy="112" r="1.4" />
        <circle cx="99" cy="46" r="1.1" />
        <circle cx="101" cy="80" r="1.5" />
        <circle cx="97" cy="108" r="1" />
        <circle cx="60" cy="20" r="1.2" />
        <circle cx="46" cy="16" r="1" />
      </g>
      {/* A lighter inner "page" area so the ruled contract reads clearly. */}
      <rect x="26" y="34" width="68" height="84" rx="6" fill="#ffffff" opacity="0.28" />
      {/* Ruled text lines — the "contract" body. */}
      <line x1="34" y1="48" x2="86" y2="48" stroke="var(--ct-toast-crust, #8a5a2b)" strokeWidth="2.5" opacity="0.75" />
      <line x1="34" y1="60" x2="86" y2="60" stroke="var(--ct-toast-crust, #8a5a2b)" strokeWidth="2" opacity="0.5" />
      <line x1="34" y1="72" x2="86" y2="72" stroke="var(--ct-toast-crust, #8a5a2b)" strokeWidth="2" opacity="0.5" />
      <line x1="34" y1="84" x2="78" y2="84" stroke="var(--ct-toast-crust, #8a5a2b)" strokeWidth="2" opacity="0.5" />
      {/* Signature squiggle. */}
      <path
        d="M34 106 q6 -10 12 0 t12 0 t12 0"
        fill="none"
        stroke="var(--ct-toast-crust, #8a5a2b)"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      {/* Red wax seal. */}
      <circle cx="86" cy="106" r="8" fill="var(--ct-danger, #b3261e)" stroke="var(--ct-toast-seal-edge, #7a1712)" strokeWidth="1.5" />
    </svg>
  );

  if (!onDownload) {
    // Decorative-only slice (e.g. idle preview / no download gate wired in).
    return <div aria-hidden="true">{art}</div>;
  }

  return (
    <button
      type="button"
      aria-label="Download redlined document"
      className="toaster-hero__toast-btn"
      disabled={downloadDisabled}
      onClick={onDownload}
    >
      {art}
      <span className="toaster-hero__toast-caption">Click to download</span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Decorative toaster-body SVG shared by the legacy status illustrations below.
// Kept for backwards compatibility with any caller still importing them.
// ---------------------------------------------------------------------------
function ToasterBody({ children }: { children?: React.ReactNode }): React.ReactElement {
  return (
    <svg
      className="toaster-illustration"
      viewBox="0 0 160 110"
      width="160"
      height="110"
      aria-hidden="true"
      focusable="false"
    >
      <rect className="toaster-body" x="10" y="20" width="140" height="70" rx="14" />
      <rect className="toaster-slot" x="35" y="10" width="35" height="14" rx="3" />
      <rect className="toaster-slot" x="90" y="10" width="35" height="14" rx="3" />
      {children}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// PENDING/RUNNING — legacy "doneness" progress treatment.
// ---------------------------------------------------------------------------
export function ProgressToaster(): React.ReactElement {
  return (
    <div data-testid="toaster-state-progress">
      <ToasterBody>
        <line className="toaster-coil toaster-coil--hot" x1="40" y1="24" x2="40" y2="55" />
        <line className="toaster-coil toaster-coil--hot" x1="55" y1="24" x2="55" y2="55" />
        <line className="toaster-coil toaster-coil--hot" x1="95" y1="24" x2="95" y2="55" />
        <line className="toaster-coil toaster-coil--hot" x1="110" y1="24" x2="110" y2="55" />
      </ToasterBody>
      <DonenessRing />
      <p style={{ fontSize: '0.85rem', margin: '0.25rem 0 0' }}>Toasting your review…</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DONE — legacy toast-up treatment. Now accepts optional download props so it
// can render the same real download slice ToasterHero uses.
// ---------------------------------------------------------------------------
export function ToastUpToaster({
  onDownload,
  downloadDisabled,
}: {
  onDownload?: () => void;
  downloadDisabled?: boolean;
} = {}): React.ReactElement {
  return (
    <div data-testid="toaster-state-done">
      <ToasterBody>
        <rect className="toaster-toast toaster-toast--up" x="42" y="0" width="30" height="22" rx="3" />
        <rect className="toaster-toast toaster-toast--up" x="88" y="0" width="30" height="22" rx="3" />
      </ToasterBody>
      {onDownload ? <ToastSlice onDownload={onDownload} downloadDisabled={downloadDisabled} /> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ERROR / MANUAL_REVIEW_REQUIRED — legacy sober (non-cute) treatment.
// ---------------------------------------------------------------------------
export function SoberToaster(): React.ReactElement {
  return (
    <div data-testid="toaster-state-sober">
      <svg
        className="toaster-illustration"
        viewBox="0 0 160 110"
        width="160"
        height="110"
        aria-hidden="true"
        focusable="false"
      >
        <rect x="10" y="20" width="140" height="70" rx="14" fill="none" stroke="currentColor" strokeWidth="2" />
        <rect x="35" y="10" width="35" height="14" rx="3" fill="currentColor" opacity="0.5" />
        <rect x="90" y="10" width="35" height="14" rx="3" fill="currentColor" opacity="0.5" />
        <line x1="70" y1="42" x2="90" y2="68" stroke="currentColor" strokeWidth="3" />
        <line x1="90" y1="42" x2="70" y2="68" stroke="currentColor" strokeWidth="3" />
      </svg>
    </div>
  );
}
