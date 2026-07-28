/**
 * ct-field — CTDS form field wrapper (issue #391,
 * docs/frontend-design-system.md §6): label + control-slot + hint + error
 * with wired `for`/`aria-describedby`, so RTL's `getByLabelText` (and
 * screen readers) resolve the slotted control through a REAL `label[for]`
 * pointing at the control's `id`.
 *
 * LIGHT DOM, mandatorily (§5.1, and the ticket's own note): `label[for]`
 * and `aria-describedby` are ID references, which only resolve within a
 * single tree scope — a shadow root is a separate tree scope from its
 * light-DOM children, so if the label lived in shadow DOM the `for` wiring
 * would silently stop resolving. `getByLabelText` also does not pierce
 * shadow roots (§3.2).
 *
 * `label`/`hint`/`error` are hand-rolled accessors, NOT Lit `static
 * properties` — same rationale and the same empirically-verified failure
 * mode as ct-button.ts's docstring point 2: `@lit/react` assigns these as
 * plain JS properties inside a `useLayoutEffect` that RTL's `render()`
 * flushes synchronously, but Lit's reactive-property update cycle always
 * defers to a microtask (even for the very first render). This repo's
 * tests call `getByLabelText`/`getByTestId` immediately after `render()`,
 * with no `await` — if the `for`/`aria-describedby`/`aria-invalid` wiring
 * only existed after Lit's async update landed, those queries would miss
 * on the very first pass. Hand-rolled accessors mutate the real DOM
 * synchronously in the setter, keeping every render pass consistent
 * immediately, the same way ct-button/ct-icon-button/ct-app-shell do.
 *
 * The slotted control itself is never moved or wrapped: this component
 * builds its OWN label/hint/error elements once in `connectedCallback` and
 * never overrides `render()`, so lit-html never touches the host's
 * children (same principle as ct-banner.ts/ct-app-shell.ts) — there is
 * nothing here for React and Lit to fight over. Visual order (label,
 * control, hint, error) is achieved with CSS `order` on a flex host
 * (ct-field.css) rather than DOM position, since where the control ends up
 * among the host's children is React's own append order, not something
 * this component may rearrange.
 *
 * A `MutationObserver` on the host's `childList` re-runs the wiring
 * whenever the control child is added, replaced, or removed — the closest
 * light-DOM equivalent of a shadow `<slot>`'s `slotchange` event (the
 * ticket's "on slot change").
 */
import { LitElement } from 'lit';
import { defineOnce } from '../define';
import './ct-field.css';

const TAG = 'ct-field';

let nextFieldId = 0;

export class CtField extends LitElement {
  createRenderRoot(): this {
    return this;
  }

  private _labelEl: HTMLLabelElement | null = null;
  private _hintEl: HTMLParagraphElement | null = null;
  private _errorEl: HTMLParagraphElement | null = null;
  private _control: HTMLElement | null = null;
  private _observer: MutationObserver | null = null;
  private readonly _id = `ct-field-${++nextFieldId}`;

  private _label = '';
  private _hint = '';
  private _error = '';

  connectedCallback(): void {
    super.connectedCallback();

    if (!this._labelEl) {
      const label = document.createElement('label');
      label.className = 'ct-field__label';
      label.textContent = this._label;
      this.insertBefore(label, this.firstChild);
      this._labelEl = label;

      const hint = document.createElement('p');
      hint.className = 'ct-field__hint';
      hint.id = `${this._id}-hint`;
      hint.textContent = this._hint;
      hint.hidden = !this._hint;
      this.appendChild(hint);
      this._hintEl = hint;

      const error = document.createElement('p');
      error.className = 'ct-field__error';
      error.id = `${this._id}-error`;
      error.setAttribute('role', 'alert');
      error.textContent = this._error;
      error.hidden = !this._error;
      this.appendChild(error);
      this._errorEl = error;
    }

    this._wireControl();
    this._observer ??= new MutationObserver(() => this._wireControl());
    this._observer.observe(this, { childList: true });
  }

  disconnectedCallback(): void {
    this._observer?.disconnect();
    super.disconnectedCallback();
  }

  /** Field label text, e.g. "Username". */
  get label(): string {
    return this._label;
  }

  set label(value: string) {
    this._label = value;
    if (this._labelEl) {
      this._labelEl.textContent = value;
    }
  }

  /** Muted helper text below the control; hidden entirely when empty. */
  get hint(): string {
    return this._hint;
  }

  set hint(value: string) {
    this._hint = value;
    if (this._hintEl) {
      this._hintEl.textContent = value;
      this._hintEl.hidden = !value;
    }
    this._updateDescribedBy();
  }

  /**
   * Validation error text. A non-empty value shows the error line (danger
   * tokens, `role="alert"`) and marks the control `aria-invalid="true"`.
   */
  get error(): string {
    return this._error;
  }

  set error(value: string) {
    this._error = value;
    if (this._errorEl) {
      this._errorEl.textContent = value;
      this._errorEl.hidden = !value;
    }
    this._updateDescribedBy();
    this._updateInvalid();
  }

  // Finds the one non-Lit-owned child (the slotted control), gives it a
  // stable id if it doesn't already have one, and wires the label/
  // aria-describedby/aria-invalid attributes onto it. Re-run on every
  // childList mutation so a control React swaps out stays wired.
  private _wireControl(): void {
    const control = Array.from(this.children).find(
      (el) => el !== this._labelEl && el !== this._hintEl && el !== this._errorEl,
    ) as HTMLElement | undefined;

    this._control = control ?? null;
    if (!control) {
      return;
    }

    if (!control.id) {
      control.id = `${this._id}-control`;
    }
    if (this._labelEl) {
      this._labelEl.htmlFor = control.id;
    }
    this._updateDescribedBy();
    this._updateInvalid();
  }

  private _updateDescribedBy(): void {
    if (!this._control) {
      return;
    }
    const ids: string[] = [];
    if (this._hint && this._hintEl) {
      ids.push(this._hintEl.id);
    }
    if (this._error && this._errorEl) {
      ids.push(this._errorEl.id);
    }
    if (ids.length > 0) {
      this._control.setAttribute('aria-describedby', ids.join(' '));
    } else {
      this._control.removeAttribute('aria-describedby');
    }
  }

  private _updateInvalid(): void {
    if (!this._control) {
      return;
    }
    if (this._error) {
      this._control.setAttribute('aria-invalid', 'true');
    } else {
      this._control.removeAttribute('aria-invalid');
    }
  }

  // No render() override — see the module docstring: the slotted control is
  // React-owned content this component must never move or wrap.
}

defineOnce(TAG, CtField);
