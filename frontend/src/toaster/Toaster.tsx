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
import { useCallback, useId, useRef } from 'react';

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
      /* --- Legacy state illustrations (ProgressToaster/ToastUpToaster/SoberToaster) --- */
      .toaster-dial { display: flex; flex-wrap: wrap; gap: 0.5rem; padding: 0; margin: 0.25rem 0 0.5rem; list-style: none; justify-content: center; }
      .toaster-dial-stop {
        appearance: none; cursor: pointer; border-radius: 999px; padding: 0.35rem 0.85rem;
        font-size: 0.9rem; line-height: 1.2; border: 2px solid #8a8a8a; background: #f5f1e8; color: #2a2a2a;
        transition: transform 150ms ease, border-color 150ms ease, background-color 150ms ease;
      }
      .toaster-dial-stop[aria-checked="true"] { border-color: var(--ct-accent, #c0522d); background: var(--ct-accent-soft, #ffe2b8); transform: scale(1.06); font-weight: 600; }
      .toaster-dial-stop[aria-disabled="true"], .toaster-dial-stop.toaster-dial-stop--coming-soon { opacity: 0.6; font-style: italic; }
      .toaster-dial-stop:focus-visible { outline: 2px solid #2a6bcc; outline-offset: 2px; }
      .toaster-illustration { display: block; margin: 0.5rem 0; }
      .toaster-illustration .toaster-body { fill: #c9c9c9; stroke: #4a4a4a; stroke-width: 2; }
      .toaster-illustration .toaster-slot { fill: #2a2a2a; }
      .toaster-illustration .toaster-coil { stroke: #d1602e; stroke-width: 2; fill: none; opacity: 0.35; }
      .toaster-illustration .toaster-coil--hot { opacity: 1; transition: opacity 400ms ease-in-out; }
      .toaster-illustration .toaster-toast { fill: var(--ct-toast, #d9a463); stroke: var(--ct-toast-crust, #8a5a2b); stroke-width: 2; transition: transform 400ms ease-out; }
      .toaster-illustration .toaster-toast--up { transform: translateY(-14px); }
      .toaster-illustration .toaster-toast--down { transform: translateY(6px); }
      .toaster-progress-track { fill: none; stroke: #d8d0c0; stroke-width: 6; }
      .toaster-progress-fill { fill: none; stroke: var(--ct-accent, #c0522d); stroke-width: 6; stroke-linecap: round; transition: stroke-dashoffset 600ms ease; }

      /* --- ToasterHero: the photoreal centerpiece --- */
      .toaster-hero { display: flex; flex-direction: column; align-items: center; gap: 0.75rem; }
      .toaster-hero__stage { position: relative; display: inline-block; line-height: 0; }
      .toaster-hero__svg { display: block; width: 100%; max-width: 360px; height: auto; }
      .toaster-hero--sober .toaster-hero__svg { filter: grayscale(0.85) brightness(0.82); opacity: 0.72; }

      /* Chrome gradient stops — brushed-stainless banding, theme-aware. */
      .ct-cr0 { stop-color: #fbfbfb; } .ct-cr1 { stop-color: #d9dde0; } .ct-cr2 { stop-color: #eef1f3; }
      .ct-cr3 { stop-color: #c3c9cd; } .ct-cr4 { stop-color: #dfe3e6; } .ct-cr5 { stop-color: #a9afb4; }
      .ct-cr6 { stop-color: #cfd4d8; }

      /* Rotating dial pointer — reflects the selected entry's angle. fill-box
         + center origin keeps the pivot glued to the knob center regardless of
         viewport scaling (the group carries a transparent full-diameter disc). */
      .toaster-pointer { transition: transform 200ms ease; transform-box: fill-box; transform-origin: center; }

      /* Lever slides down its track whenever the toaster is doing anything. */
      .toaster-lever { transition: transform 250ms cubic-bezier(.4, 0, .3, 1); }
      .toaster-lever--down { transform: translateY(46px); }

      /* Warm slot glow while toasting — a blurred halo that breathes. */
      .toaster-glow { opacity: 0.6; animation: toaster-glow-pulse 2.5s ease-in-out infinite; }
      @keyframes toaster-glow-pulse {
        0%, 100% { opacity: 0.32; }
        50% { opacity: 0.92; }
      }

      /* Toast springs out of the slot with a slight overshoot. */
      .toaster-hero__toast {
        position: absolute; left: 50%; top: 6%; transform: translate(-50%, 0);
        animation: toaster-pop 380ms cubic-bezier(.34, 1.56, .64, 1);
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
      .toaster-hero__toast-btn:focus-visible { outline: 2px solid var(--ct-accent, #c0522d); outline-offset: 3px; border-radius: 8px; }
      .toaster-hero__toast-caption {
        font-size: 0.8rem; font-weight: 600; color: var(--ct-accent, #c0522d);
        text-decoration: underline; text-underline-offset: 2px;
      }
      .toaster-hero__sober {
        position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
        pointer-events: none;
      }
      .toaster-hero__progress { display: flex; flex-direction: column; align-items: center; gap: 0.25rem; }
      .toaster-hero__progress p { font-size: 0.85rem; margin: 0; }

      @media (prefers-color-scheme: dark) {
        .toaster-dial-stop { background: #2e2a24; color: #f2ede2; border-color: #6b6b6b; }
        .toaster-illustration .toaster-body { fill: #4a4a4a; stroke: #d0d0d0; }
        /* Chrome darkens so the appliance still reads as metal against a dark page. */
        .ct-cr0 { stop-color: #6b7075; } .ct-cr1 { stop-color: #3f4448; } .ct-cr2 { stop-color: #565b60; }
        .ct-cr3 { stop-color: #34383c; } .ct-cr4 { stop-color: #4a4f54; } .ct-cr5 { stop-color: #26292c; }
        .ct-cr6 { stop-color: #3a3e42; }
      }

      @media (prefers-reduced-motion: reduce) {
        .toaster-dial-stop,
        .toaster-illustration .toaster-coil--hot,
        .toaster-illustration .toaster-toast,
        .toaster-progress-fill,
        .toaster-pointer,
        .toaster-lever,
        .toaster-glow,
        .toaster-hero__toast {
          transition: none !important;
          animation: none !important;
        }
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

  const moveSelection = useCallback(
    (delta: number) => {
      if (selectable.length === 0) {
        return;
      }
      // Arrow keys cycle the SELECTABLE stops only, so a keyboard user can
      // never land on a coming-soon stop a mouse user can't click either.
      const currentIndex = Math.max(
        0,
        selectable.findIndex((e) => e.playbook_id === value),
      );
      const nextIndex = (currentIndex + delta + selectable.length) % selectable.length;
      const next = selectable[nextIndex];
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
    [selectable, value, onChange],
  );

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        event.preventDefault();
        moveSelection(1);
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        event.preventDefault();
        moveSelection(-1);
      }
    },
    [moveSelection],
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

// ===========================================================================
// ToasterHero — THE hero. One cohesive, near-photoreal chrome toaster: layered
// chrome gradients, a specular highlight, a soft ground shadow, a rotating
// dial pointer, a sliding lever, glowing slots while working, and a contract
// slice that pops out (a real download button) on completion.
// ===========================================================================
export type ToasterPhase = 'idle' | 'working' | 'done' | 'error';

export interface ToasterHeroProps {
  entries: DialEntry[];
  value: string;
  onChange: (playbookId: string) => void;
  phase: ToasterPhase;
  /** When provided AND phase==='done', the toast becomes a real download button. */
  onDownload?: () => void;
  /** Disables that toast button while a download is preparing. */
  downloadDisabled?: boolean;
}

// Geometry constants for the hero SVG (user-space units; viewBox 0 0 420 340).
const DIAL_CX = 168;
const DIAL_CY = 202;
const DIAL_R = 40;

export function ToasterHero({
  entries,
  value,
  onChange,
  phase,
  onDownload,
  downloadDisabled,
}: ToasterHeroProps): React.ReactElement {
  // Namespace every gradient/filter id so multiple toasters on a page can't
  // collide on url(#…) references. useId is stable across renders; strip the
  // colons React emits so the ids are safe inside url() fragments.
  const uid = useId().replace(/:/g, '');
  const g = (name: string) => `${name}-${uid}`;

  const hasDial = entries.length > 0;
  const leverDown = phase !== 'idle';
  const working = phase === 'working';

  // The decorative pointer sweeps a -60°..+60° arc, one stop per entry index.
  const selectedIndex = Math.max(
    0,
    entries.findIndex((e) => e.playbook_id === value),
  );
  const pointerAngle =
    entries.length > 1 ? -60 + (120 * selectedIndex) / (entries.length - 1) : 0;

  // Knurled tick marks around the dial rim (every 30°).
  const ticks = Array.from({ length: 12 }, (_, i) => {
    const a = ((i * 30) * Math.PI) / 180;
    return {
      x1: DIAL_CX + (DIAL_R - 4) * Math.cos(a),
      y1: DIAL_CY + (DIAL_R - 4) * Math.sin(a),
      x2: DIAL_CX + (DIAL_R + 2) * Math.cos(a),
      y2: DIAL_CY + (DIAL_R + 2) * Math.sin(a),
    };
  });

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
          <defs>
            {/* Brushed-stainless body — vertical multi-stop banding. */}
            <linearGradient id={g('chrome')} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" className="ct-cr0" />
              <stop offset="0.12" className="ct-cr1" />
              <stop offset="0.32" className="ct-cr2" />
              <stop offset="0.5" className="ct-cr3" />
              <stop offset="0.7" className="ct-cr4" />
              <stop offset="0.88" className="ct-cr5" />
              <stop offset="1" className="ct-cr6" />
            </linearGradient>
            {/* Narrow specular highlight band down the left shoulder. */}
            <linearGradient id={g('spec')} x1="0" y1="0" x2="1" y2="0">
              <stop offset="0" stopColor="#ffffff" stopOpacity="0" />
              <stop offset="0.5" stopColor="#ffffff" stopOpacity="0.6" />
              <stop offset="1" stopColor="#ffffff" stopOpacity="0" />
            </linearGradient>
            {/* Dark base / feet. */}
            <linearGradient id={g('base')} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#33373a" />
              <stop offset="1" stopColor="#131517" />
            </linearGradient>
            {/* Inner-shadow of a slot opening. */}
            <linearGradient id={g('slot')} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#04060a" />
              <stop offset="0.55" stopColor="#161b21" />
              <stop offset="1" stopColor="#0a0e12" />
            </linearGradient>
            {/* Domed dial knob face. */}
            <radialGradient id={g('knob')} cx="0.38" cy="0.34" r="0.75">
              <stop offset="0" stopColor="#f7f8f9" />
              <stop offset="0.55" stopColor="#cbd0d4" />
              <stop offset="1" stopColor="#9aa0a5" />
            </radialGradient>
            {/* Warm slot glow. */}
            <radialGradient id={g('glow')} cx="0.5" cy="0.5" r="0.5">
              <stop offset="0" stopColor="var(--ct-glow, #ff7a1a)" stopOpacity="0.95" />
              <stop offset="0.6" stopColor="var(--ct-glow, #ff7a1a)" stopOpacity="0.35" />
              <stop offset="1" stopColor="var(--ct-glow, #ff7a1a)" stopOpacity="0" />
            </radialGradient>
            {/* Soft ground shadow + ambient occlusion under the body. */}
            <filter id={g('soft')} x="-40%" y="-40%" width="180%" height="180%">
              <feGaussianBlur stdDeviation="7" />
            </filter>
            {/* Halo blur for the glow. */}
            <filter id={g('haze')} x="-60%" y="-60%" width="220%" height="220%">
              <feGaussianBlur stdDeviation="6" />
            </filter>
          </defs>

          {/* Ground shadow the appliance casts. */}
          <ellipse cx="212" cy="298" rx="150" ry="16" fill="#000000" opacity="0.28" filter={`url(#${g('soft')})`} />

          {/* Base plate + feet. */}
          <rect x="74" y="252" width="272" height="30" rx="12" fill={`url(#${g('base')})`} />
          <rect x="96" y="278" width="34" height="12" rx="4" fill="#0e1012" />
          <rect x="290" y="278" width="34" height="12" rx="4" fill="#0e1012" />

          {/* Chrome body. */}
          <rect x="82" y="104" width="256" height="156" rx="30" fill={`url(#${g('chrome')})`} stroke="#8b9196" strokeWidth="1.5" />
          {/* Ambient occlusion where the body meets the base. */}
          <ellipse cx="210" cy="258" rx="120" ry="12" fill="#000000" opacity="0.16" filter={`url(#${g('soft')})`} />
          {/* Specular highlight band. */}
          <rect x="104" y="118" width="26" height="128" rx="13" fill={`url(#${g('spec')})`} />
          {/* Rounded top deck where the slots sit. */}
          <rect x="94" y="104" width="232" height="26" rx="20" fill="#ffffff" opacity="0.14" />

          {/* Two slots — dark insets with an inner-shadow gradient. */}
          <rect x="126" y="96" width="76" height="20" rx="7" fill={`url(#${g('slot')})`} stroke="#7c8288" strokeWidth="1.5" />
          <rect x="218" y="96" width="76" height="20" rx="7" fill={`url(#${g('slot')})`} stroke="#7c8288" strokeWidth="1.5" />

          {/* Warm pulsing glow rising from the slots while toasting. */}
          {working && (
            <g className="toaster-glow" filter={`url(#${g('haze')})`}>
              <ellipse cx="164" cy="104" rx="48" ry="24" fill={`url(#${g('glow')})`} />
              <ellipse cx="256" cy="104" rx="48" ry="24" fill={`url(#${g('glow')})`} />
            </g>
          )}

          {/* Dial: knurled knob + a needle that rotates to the selected stop. */}
          {hasDial && (
            <g>
              <circle cx={DIAL_CX} cy={DIAL_CY} r={DIAL_R + 6} fill="#00000022" />
              <circle cx={DIAL_CX} cy={DIAL_CY} r={DIAL_R} fill={`url(#${g('knob')})`} stroke="#83898e" strokeWidth="1.5" />
              {ticks.map((t, i) => (
                <line key={i} x1={t.x1} y1={t.y1} x2={t.x2} y2={t.y2} stroke="#6f7479" strokeWidth="1.5" />
              ))}
              {/* Rotating pointer group. The transparent full-diameter disc
                  fixes the fill-box center on the knob so CSS rotation pivots
                  exactly at (DIAL_CX, DIAL_CY). */}
              <g className="toaster-pointer" style={{ transform: `rotate(${pointerAngle}deg)` }}>
                <circle cx={DIAL_CX} cy={DIAL_CY} r={DIAL_R} fill="transparent" />
                <path
                  d={`M ${DIAL_CX - 4} ${DIAL_CY} L ${DIAL_CX + 4} ${DIAL_CY} L ${DIAL_CX} ${DIAL_CY - (DIAL_R - 6)} Z`}
                  fill="var(--ct-accent, #c0522d)"
                />
                <circle cx={DIAL_CX} cy={DIAL_CY} r="6" fill="#5a5f63" stroke="#e8ebed" strokeWidth="1.5" />
              </g>
            </g>
          )}

          {/* Lever on a track down the right flank; slides down when busy. */}
          <rect x="340" y="150" width="12" height="72" rx="6" fill="#20242700" stroke="#7c8288" strokeWidth="1.5" />
          <line x1="346" y1="156" x2="346" y2="216" stroke="#5a5f63" strokeWidth="2" opacity="0.5" />
          <g className={`toaster-lever${leverDown ? ' toaster-lever--down' : ''}`}>
            <rect x="330" y="156" width="24" height="10" rx="5" fill={`url(#${g('chrome')})`} stroke="#83898e" strokeWidth="1" />
            <circle cx="352" cy="161" r="9" fill="var(--ct-accent, #c0522d)" stroke="#7a3a20" strokeWidth="1.5" />
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

        {/* ERROR — sober, "unplugged" treatment. Muted body (via the --sober
            wrapper class) plus the plain X mark. No glow, no toast: legal
            software, failure reads as serious. */}
        {phase === 'error' && (
          <div data-testid="toaster-state-sober" className="toaster-hero__sober">
            <svg viewBox="0 0 120 120" width="120" height="120" aria-hidden="true" focusable="false">
              <circle cx="60" cy="60" r="34" fill="none" stroke="var(--ct-neutral, #5c5c5c)" strokeWidth="4" opacity="0.7" />
              <line x1="46" y1="46" x2="74" y2="74" stroke="var(--ct-neutral, #5c5c5c)" strokeWidth="5" strokeLinecap="round" />
              <line x1="74" y1="46" x2="46" y2="74" stroke="var(--ct-neutral, #5c5c5c)" strokeWidth="5" strokeLinecap="round" />
            </svg>
          </div>
        )}
      </div>

      {/* The accessible dial (radiogroup of stops) — only when there are
          entries. The SVG knob above is decoration that mirrors `value`. */}
      {hasDial && <ContractTypeDial entries={entries} value={value} onChange={onChange} />}

      {/* WORKING — doneness ring + copy. */}
      {working && (
        <div data-testid="toaster-state-progress" className="toaster-hero__progress">
          <DonenessRing />
          <p>Toasting your review…</p>
        </div>
      )}
    </div>
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
      <circle cx="86" cy="106" r="8" fill="var(--ct-danger, #b3261e)" stroke="#7a1712" strokeWidth="1.5" />
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
