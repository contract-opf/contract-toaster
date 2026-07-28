/**
 * ct-icon-button — square, compact-content button (issue #388,
 * docs/frontend-design-system.md §6). A required `label` attribute becomes
 * the inner button's `aria-label`; the hit target is a square ≥44px
 * regardless of content size.
 *
 * Same light-DOM/real-`<button>`, synchronous-hand-rolled-accessor
 * architecture as ct-button — see that file's module docstring for why
 * (verified empirically): the visible content routes through a plain
 * `text` property fed from React `children` by `ui/react.ts`'s
 * `CtIconButton` wrapper (never slotted, to avoid React/Lit fighting over
 * the same node), and `disabled`/`type`/`text`/`pressed` mutate the
 * button that gets built once, synchronously, in `connectedCallback` —
 * because this repo's existing tests click a button immediately after
 * `render()`, with no `await` for Lit's inherently-async update cycle.
 */
import { LitElement } from 'lit';
import { defineOnce } from '../define';
import './ct-icon-button.css';

const TAG = 'ct-icon-button';

export class CtIconButton extends LitElement {
  createRenderRoot(): this {
    return this;
  }

  private _btn: HTMLButtonElement | null = null;
  private _label = '';
  private _disabled = false;
  private _type: 'button' | 'submit' | 'reset' = 'button';
  private _text = '';
  private _pressed: boolean | undefined;

  connectedCallback(): void {
    super.connectedCallback();
    if (this._btn) {
      return;
    }

    const btn = document.createElement('button');
    btn.className = 'ct-icon-button__el';
    btn.type = this._type;
    btn.disabled = this._disabled;
    if (this._label) {
      btn.setAttribute('aria-label', this._label);
    }
    if (this._pressed !== undefined) {
      btn.setAttribute('aria-pressed', this._pressed ? 'true' : 'false');
    }
    btn.textContent = this._text;

    const testId = this.getAttribute('data-testid');
    if (testId !== null) {
      btn.setAttribute('data-testid', testId);
      this.removeAttribute('data-testid');
    }

    this.appendChild(btn);
    this._btn = btn;
  }

  /** Required — becomes the inner button's aria-label. */
  get label(): string {
    return this._label;
  }

  set label(value: string) {
    this._label = value;
    if (this._btn) {
      if (value) {
        this._btn.setAttribute('aria-label', value);
      } else {
        this._btn.removeAttribute('aria-label');
      }
    }
  }

  get disabled(): boolean {
    return this._disabled;
  }

  set disabled(value: boolean) {
    this._disabled = value;
    if (this._btn) {
      this._btn.disabled = value;
    }
  }

  get type(): 'button' | 'submit' | 'reset' {
    return this._type;
  }

  set type(value: 'button' | 'submit' | 'reset') {
    this._type = value;
    if (this._btn) {
      this._btn.type = value;
    }
  }

  /** Internal-only channel from ui/react.ts's CtIconButton wrapper. */
  get text(): string {
    return this._text;
  }

  set text(value: string) {
    this._text = value;
    if (this._btn) {
      this._btn.textContent = value;
    }
  }

  /** Internal-only channel from ui/react.ts's CtIconButton wrapper for aria-pressed. */
  get pressed(): boolean | undefined {
    return this._pressed;
  }

  set pressed(value: boolean | undefined) {
    this._pressed = value;
    if (this._btn) {
      if (value === undefined) {
        this._btn.removeAttribute('aria-pressed');
      } else {
        this._btn.setAttribute('aria-pressed', value ? 'true' : 'false');
      }
    }
  }

  // No render() override — see ct-button.ts's module docstring point 2.
}

defineOnce(TAG, CtIconButton);
