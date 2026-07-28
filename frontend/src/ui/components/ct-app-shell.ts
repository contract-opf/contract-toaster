/**
 * ct-app-shell — the app's outer chrome (issue #390,
 * docs/frontend-design-system.md §6/§7): appliance-nameplate header
 * (brand + identity cluster over a hairline), the tab strip, the
 * max-width content column, and a mono footer.
 *
 * LIGHT DOM (§5.1). Unlike ct-button/ct-icon-button, this component never
 * needs to CONTAIN React's children inside a node it builds — the panels
 * it wraps carry ARIA `id`s that ct-tab-bar's `aria-controls` references,
 * and reparenting them into a Lit-built wrapper is exactly the hazard
 * ct-button.ts's docstring documents empirically ("manually reparenting
 * those children ... breaks" on a follow-up React update). So this
 * component does the opposite of ct-button: it never overrides `render()`
 * and never moves a single React-owned node (same principle as
 * ct-banner.ts, generalized to more than one content region).
 *
 * React composes the four regions as plain children of `<ct-app-shell>`,
 * each tagged with a plain `slot="..."` ATTRIBUTE — not a real shadow-DOM
 * `<slot>` (this element has no shadow root, so that attribute has no
 * native browser behavior here). It exists purely as a CSS hook:
 * `ct-app-shell.css` is a CSS Grid with named areas, and
 * `[slot="identity"]` / `[slot="tabs"]` / `[slot="footer"]` /
 * `:not([slot])` (the default region — the tabpanels) select each
 * region's grid-area. Zero DOM manipulation, so there is nothing for Lit
 * and React to fight over regardless of how often any region re-renders.
 *
 * The one thing this component DOES own and render itself is the brand
 * nameplate text (a `brand` property, e.g. "Contract Toaster Review
 * Tool") — built once as a real `<span>` in `connectedCallback` and
 * mutated via a hand-rolled accessor, the same synchronous-DOM pattern
 * ct-button.ts uses for its `text` property (see that file's docstring
 * point 2) and for the identical reason: `@lit/react` assigns properties
 * inside a `useLayoutEffect` that RTL's `render()` flushes synchronously,
 * so the node needs to exist synchronously too. Because this span is
 * Lit's OWN node — never a node React created — moving/mutating it is
 * safe; it just needs a `grid-area` of its own in the CSS (`brand`),
 * distinct from the React-owned regions above.
 */
import { LitElement } from 'lit';
import { defineOnce } from '../define';
import './ct-app-shell.css';

const TAG = 'ct-app-shell';

export class CtAppShell extends LitElement {
  createRenderRoot(): this {
    return this;
  }

  private _brandEl: HTMLSpanElement | null = null;
  private _brand = '';

  connectedCallback(): void {
    super.connectedCallback();
    if (this._brandEl) {
      return;
    }
    const brand = document.createElement('span');
    brand.className = 'ct-app-shell__brand';
    brand.textContent = this._brand;
    this.insertBefore(brand, this.firstChild);
    this._brandEl = brand;
  }

  /** The nameplate text, e.g. "Contract Toaster Review Tool". */
  get brand(): string {
    return this._brand;
  }

  set brand(value: string) {
    this._brand = value;
    if (this._brandEl) {
      this._brandEl.textContent = value;
    }
  }

  // No render() override — see the module docstring: every region besides
  // the brand span is React-owned content this component must never touch.
}

defineOnce(TAG, CtAppShell);
