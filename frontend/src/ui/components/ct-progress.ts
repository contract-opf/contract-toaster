/**
 * ct-progress — indeterminate warm shimmer progress bar (issue #393,
 * docs/frontend-design-system.md §6/§7). Used beneath the toaster hero
 * during upload/poll (non-terminal review states).
 *
 * LIGHT DOM (§5.1): a plain track + shimmer fill plus an optional muted
 * caption, in the same synchronous-hand-rolled-accessor style as
 * ct-field.ts/ct-toolbar.ts (see those docstrings) — `label` mutates
 * already-built DOM directly from its setter rather than going through
 * Lit's async `render()` cycle, so it stays consistent with whatever
 * `@lit/react`'s synchronous `useLayoutEffect` property assignment lands
 * immediately after `render()`.
 *
 * `role="progressbar"` with NO `aria-valuenow` signals an indeterminate
 * progressbar per the ARIA APG; `aria-valuetext` carries the caption when
 * one is set, so a screen reader announces the phase ("Reviewing your
 * document…") instead of a bare, silent bar.
 *
 * The shimmer sweep is a real CSS `animation`, so it is already covered by
 * base.css's global `prefers-reduced-motion: reduce` guard (§3.2/§4.4) —
 * no local guard needed here.
 */
import { LitElement } from 'lit';
import { defineOnce } from '../define';
import './ct-progress.css';

const TAG = 'ct-progress';

export class CtProgress extends LitElement {
  createRenderRoot(): this {
    return this;
  }

  private _labelEl: HTMLParagraphElement | null = null;
  private _label = '';

  connectedCallback(): void {
    super.connectedCallback();
    if (this._labelEl) {
      return;
    }

    this.setAttribute('role', 'progressbar');

    const track = document.createElement('div');
    track.className = 'ct-progress__track';

    const fill = document.createElement('div');
    fill.className = 'ct-progress__fill';
    fill.setAttribute('aria-hidden', 'true');
    track.appendChild(fill);

    const label = document.createElement('p');
    label.className = 'ct-progress__label';
    label.textContent = this._label;
    label.hidden = !this._label;

    this.append(track, label);
    this._labelEl = label;
    this._syncValueText();
  }

  /** Optional muted caption, e.g. "Reviewing your document…". */
  get label(): string {
    return this._label;
  }

  set label(value: string) {
    this._label = value ?? '';
    if (this._labelEl) {
      this._labelEl.textContent = this._label;
      this._labelEl.hidden = !this._label;
    }
    this._syncValueText();
  }

  private _syncValueText(): void {
    if (this._label) {
      this.setAttribute('aria-valuetext', this._label);
    } else {
      this.removeAttribute('aria-valuetext');
    }
  }

  // No render() override — see module docstring.
}

defineOnce(TAG, CtProgress);
