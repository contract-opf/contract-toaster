/**
 * Toaster — the owned SVG illustration set for issue #280 ("static toaster +
 * dial"): a toaster whose dial selects the contract type, a slot for the
 * uploaded `.docx`, doneness-style progress while a review runs, and the
 * toast-up DONE state.
 *
 * Design constraints (see issue #280's Grind notes):
 *   - All art is inline SVG React components in this file — no image files,
 *     no external fonts/CDN (the CSP forbids remote loads).
 *   - Animation is CSS transitions only, guarded by
 *     `@media (prefers-reduced-motion: reduce)`.
 *   - Theme-aware via `prefers-color-scheme` — no hardcoded light/dark
 *     assumption.
 *   - The dial (`ContractTypeDial`) is a real `radiogroup` of `radio` stops
 *     with roving `tabIndex`/arrow-key navigation and `aria-checked`; the
 *     SVG is decoration (`aria-hidden`) layered behind/around it, never a
 *     replacement for the accessible control. Coming-soon stops are shown
 *     visually de-emphasized but stay selectable — the backend, not the
 *     frontend, is authoritative on whether a type can actually run right
 *     now (issue #272), and playbook-selector.test.tsx locks that in.
 *   - Out of scope (post-launch): lever-press/browning animation, sound,
 *     mascot-ification.
 */
import { useCallback, useRef } from 'react';

// ---------------------------------------------------------------------------
// Shared stylesheet — plain <style>, no CSS-in-JS dependency. Colors are
// theme-aware (prefers-color-scheme); transitions are guarded by
// prefers-reduced-motion so a reduced-motion user gets instant state
// changes instead of animation.
// ---------------------------------------------------------------------------
export function ToasterStyles(): React.ReactElement {
  return (
    <style>{`
      .toaster-dial { display: flex; flex-wrap: wrap; gap: 0.5rem; padding: 0; margin: 0.25rem 0 0.5rem; list-style: none; }
      .toaster-dial-stop {
        appearance: none; cursor: pointer; border-radius: 999px; padding: 0.35rem 0.85rem;
        font-size: 0.9rem; line-height: 1.2; border: 2px solid #8a8a8a; background: #f5f1e8; color: #2a2a2a;
        transition: transform 150ms ease, border-color 150ms ease, background-color 150ms ease;
      }
      .toaster-dial-stop[aria-checked="true"] { border-color: #c0522d; background: #ffe2b8; transform: scale(1.06); font-weight: 600; }
      .toaster-dial-stop[aria-disabled="true"], .toaster-dial-stop.toaster-dial-stop--coming-soon { opacity: 0.6; font-style: italic; }
      .toaster-dial-stop:focus-visible { outline: 2px solid #2a6bcc; outline-offset: 2px; }
      .toaster-illustration { display: block; margin: 0.5rem 0; }
      .toaster-illustration .toaster-body { fill: #c9c9c9; stroke: #4a4a4a; stroke-width: 2; }
      .toaster-illustration .toaster-slot { fill: #2a2a2a; }
      .toaster-illustration .toaster-coil { stroke: #d1602e; stroke-width: 2; fill: none; opacity: 0.35; }
      .toaster-illustration .toaster-coil--hot { opacity: 1; transition: opacity 400ms ease-in-out; }
      .toaster-illustration .toaster-toast { fill: #d9a463; stroke: #8a5a2b; stroke-width: 2; transition: transform 400ms ease-out; }
      .toaster-illustration .toaster-toast--up { transform: translateY(-14px); }
      .toaster-illustration .toaster-toast--down { transform: translateY(6px); }
      .toaster-progress-track { fill: none; stroke: #d8d0c0; stroke-width: 6; }
      .toaster-progress-fill { fill: none; stroke: #c0522d; stroke-width: 6; stroke-linecap: round; transition: stroke-dashoffset 600ms ease; }

      @media (prefers-color-scheme: dark) {
        .toaster-dial-stop { background: #2e2a24; color: #f2ede2; border-color: #6b6b6b; }
        .toaster-dial-stop[aria-checked="true"] { border-color: #e8a75b; background: #4a2e18; }
        .toaster-illustration .toaster-body { fill: #4a4a4a; stroke: #d0d0d0; }
        .toaster-illustration .toaster-toast { fill: #b98246; stroke: #f2ede2; }
      }

      @media (prefers-reduced-motion: reduce) {
        .toaster-dial-stop,
        .toaster-illustration .toaster-coil--hot,
        .toaster-illustration .toaster-toast,
        .toaster-progress-fill {
          transition: none !important;
          animation: none !important;
        }
      }
    `}</style>
  );
}

// ---------------------------------------------------------------------------
// ContractTypeDial — the accessible contract-type picker (issue #272's
// catalog, redesigned as a dial for #280). A real ARIA `radiogroup` of
// `radio` stops; the SVG toaster body/pointer around it is pure decoration.
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

export function ContractTypeDial({ entries, value, onChange }: ContractTypeDialProps): React.ReactElement {
  const groupRef = useRef<HTMLDivElement | null>(null);

  const moveSelection = useCallback(
    (delta: number) => {
      if (entries.length === 0) {
        return;
      }
      const currentIndex = Math.max(
        0,
        entries.findIndex((e) => e.playbook_id === value),
      );
      const nextIndex = (currentIndex + delta + entries.length) % entries.length;
      const next = entries[nextIndex];
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
    [entries, value, onChange],
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
      <span id="review-playbook-dial-label" style={{ display: 'block', marginBottom: '0.25rem' }}>
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
              tabIndex={checked ? 0 : -1}
              data-playbook-id={entry.playbook_id}
              data-testid={`review-playbook-option-${entry.playbook_id}`}
              className={`toaster-dial-stop${comingSoon ? ' toaster-dial-stop--coming-soon' : ''}`}
              onClick={() => onChange(entry.playbook_id)}
            >
              {entry.display_name}
              {comingSoon ? ' (coming soon)' : ''}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Decorative toaster-body SVG shared by every status illustration. Purely
// decorative (aria-hidden) — never conveys information the accessible
// controls/copy don't already carry.
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
// PENDING/RUNNING — "doneness" progress treatment: a filling ring plus dim
// heating coils. No numeric percentage is claimed (the pipeline doesn't
// report one) — just an indeterminate-feeling, slowly advancing sweep.
// ---------------------------------------------------------------------------
export function ProgressToaster(): React.ReactElement {
  const radius = 34;
  const circumference = 2 * Math.PI * radius;
  return (
    <div data-testid="toaster-state-progress">
      <ToasterBody>
        <line className="toaster-coil toaster-coil--hot" x1="40" y1="24" x2="40" y2="55" />
        <line className="toaster-coil toaster-coil--hot" x1="55" y1="24" x2="55" y2="55" />
        <line className="toaster-coil toaster-coil--hot" x1="95" y1="24" x2="95" y2="55" />
        <line className="toaster-coil toaster-coil--hot" x1="110" y1="24" x2="110" y2="55" />
      </ToasterBody>
      <svg
        viewBox="0 0 80 80"
        width="48"
        height="48"
        aria-hidden="true"
        focusable="false"
        style={{ display: 'block' }}
      >
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
      <p style={{ fontSize: '0.85rem', margin: '0.25rem 0 0' }}>Toasting your review…</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DONE — toast-up treatment. Purely decorative header above the existing,
// unchanged #255/#271 result markup (confidence band, critic delta,
// download gate, watermark all render exactly as before this ticket).
// ---------------------------------------------------------------------------
export function ToastUpToaster(): React.ReactElement {
  return (
    <div data-testid="toaster-state-done">
      <ToasterBody>
        <rect className="toaster-toast toaster-toast--up" x="42" y="0" width="30" height="22" rx="3" />
        <rect className="toaster-toast toaster-toast--up" x="88" y="0" width="30" height="22" rx="3" />
      </ToasterBody>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ERROR / MANUAL_REVIEW_REQUIRED / ERROR_MANUAL_REVIEW_REQUIRED — a
// distinct, sober (non-cute) treatment. Legal software: failure states do
// not get the toast-up metaphor. The existing designed copy renders
// unchanged next to this; this illustration adds no text of its own.
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
