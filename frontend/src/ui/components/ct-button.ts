/**
 * ct-button — the core CTDS button (issue #388,
 * docs/frontend-design-system.md §6).
 *
 * LIGHT DOM by design (§5.1): the component renders a REAL `<button>` as
 * its only content, so it keeps native keyboard activation, native
 * click-a-submit-button-submits-its-form activation, and `css:false`
 * @testing-library queries (`getByRole('button')`, `toBeDisabled()`)
 * working with zero ceremony.
 *
 * Two things are deliberately NOT done the "normal" Lit way, both
 * verified empirically against this repo's actual test patterns (see
 * git history / issue #388 for the throwaway probes) rather than assumed:
 *
 * 1. The label is a plain property (`text`), not slotted `children`.
 *    React (via `@lit/react`'s `createComponent`) inserts JSX `children`
 *    as REAL, React-owned DOM children of the light-DOM host — the same
 *    way it would for a plain `<div>`. A `<button><slot></slot></button>`
 *    template (or manually reparenting those children into the rendered
 *    `<button>`) breaks: React's own reconciler does not tolerate the
 *    receiving element relocating nodes it handed over — a follow-up
 *    label-only text update wipes the whole subtree instead of updating
 *    in place. `ui/react.ts`'s `CtButton` wrapper maps the public
 *    `children` prop to this internal `text` property instead, so
 *    nothing ever needs to move a React-owned node.
 *
 * 2. `disabled`/`loading`/`type`/`text` are hand-rolled accessors that
 *    mutate the (once, synchronously, in `connectedCallback`) already-
 *    built `<button>` directly — they are NOT Lit `static properties`
 *    going through the normal declarative `render()` cycle. Lit's update
 *    cycle is inherently asynchronous (`requestUpdate()` always defers to
 *    a microtask, even for the very first render — there is no supported
 *    synchronous-first-render hook). But `@lit/react` assigns these
 *    values as JS properties inside a `useLayoutEffect`, which RTL's
 *    `render()` flushes synchronously — and this repo's existing tests
 *    (e.g. `password-auth.test.tsx`) call `fireEvent.click(getByTestId(...))`
 *    immediately after `render()`, with no `await` in between. If the
 *    real `<button>` (and its `data-testid`/`disabled`/`type`) only
 *    existed after Lit's async render landed, those clicks would still
 *    be hitting the (non-interactive) host — breaking native submit
 *    activation on every such test. Building the button eagerly and
 *    updating it via plain accessors keeps this component's DOM
 *    synchronously consistent with its properties at all times, matching
 *    what a plain `<button>` gave those tests before this migration.
 *    `variant`/`size` stay ordinary Lit reactive properties (reflected
 *    attributes only used for CSS) since nothing needs them synchronously.
 */
import { LitElement } from 'lit';
import { defineOnce } from '../define';
import './ct-button.css';

export type CtButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type CtButtonSize = 'sm' | 'md';
export type CtButtonType = 'button' | 'submit' | 'reset';

const TAG = 'ct-button';

export class CtButton extends LitElement {
  createRenderRoot(): this {
    return this;
  }

  static properties = {
    variant: { type: String, reflect: true },
    size: { type: String, reflect: true },
  };

  declare variant: CtButtonVariant;
  declare size: CtButtonSize;

  private _btn: HTMLButtonElement | null = null;
  private _label: HTMLSpanElement | null = null;
  private _spinner: HTMLSpanElement | null = null;
  private _loading = false;
  private _disabled = false;
  private _type: CtButtonType = 'button';
  private _text = '';

  constructor() {
    super();
    this.variant = 'secondary';
    this.size = 'md';
  }

  connectedCallback(): void {
    super.connectedCallback();
    if (this._btn) {
      return;
    }

    const btn = document.createElement('button');
    btn.className = 'ct-button__el';
    btn.type = this._type;
    btn.disabled = this._disabled;
    if (this._loading) {
      btn.setAttribute('aria-busy', 'true');
    }

    // See module docstring point 2 — data-testid is a plain host attribute
    // React sets before Lit ever gets a turn; move it onto the real button
    // synchronously so getByTestId always resolves to an interactive,
    // form-participating element.
    const testId = this.getAttribute('data-testid');
    if (testId !== null) {
      btn.setAttribute('data-testid', testId);
      this.removeAttribute('data-testid');
    }

    const label = document.createElement('span');
    label.className = 'ct-button__label';
    label.textContent = this._text;
    this._label = label;

    const spinner = document.createElement('span');
    spinner.className = 'ct-button__spinner';
    spinner.setAttribute('aria-hidden', 'true');
    this._spinner = spinner;

    if (this._loading) {
      btn.appendChild(spinner);
    }
    btn.appendChild(label);

    this.appendChild(btn);
    this._btn = btn;
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

  get loading(): boolean {
    return this._loading;
  }

  set loading(value: boolean) {
    this._loading = value;
    if (!this._btn || !this._spinner) {
      return;
    }
    if (value) {
      this._btn.setAttribute('aria-busy', 'true');
      if (!this._spinner.isConnected) {
        this._btn.insertBefore(this._spinner, this._btn.firstChild);
      }
    } else {
      this._btn.removeAttribute('aria-busy');
      this._spinner.remove();
    }
  }

  get type(): CtButtonType {
    return this._type;
  }

  set type(value: CtButtonType) {
    this._type = value;
    if (this._btn) {
      this._btn.type = value;
    }
  }

  /** Internal-only channel from ui/react.ts's CtButton wrapper — see module docstring. */
  get text(): string {
    return this._text;
  }

  set text(value: string) {
    this._text = value;
    if (this._label) {
      this._label.textContent = value;
    }
  }

  // No render() override: this component's real content is the button
  // built once in connectedCallback and mutated directly by the accessors
  // above, never through lit-html — see module docstring point 2.
  // LitElement's default render() returns `noChange`, so lit-html never
  // touches `this`'s children (same rationale as ct-banner.ts).
}

defineOnce(TAG, CtButton);
