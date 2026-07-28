/**
 * ct-toolbar — title/filters/actions row above ct-table (issue #392,
 * docs/frontend-design-system.md §6/§7). Replaces the ad-hoc
 * `.ct-toolbar`/`.ct-row`/`.ct-actions` panel-header layouts previously
 * used on AdminUsers.tsx / AdminRetention.tsx (the `.ct-toolbar` CSS
 * CLASS itself is untouched — ReviewSubmission.tsx still uses it and is
 * out of scope here; this is an unrelated TAG selector, not a rename).
 *
 * LIGHT DOM (§5.1), following ct-app-shell.ts's `brand` accessor and
 * ct-field.ts's `label` accessor exactly: `title` is a hand-rolled
 * accessor (not a Lit `static properties` entry) that builds a REAL
 * heading `<h2>` once in `connectedCallback` and mutates its
 * `textContent` on every set — same synchronous-DOM rationale as those
 * two docstrings: `@lit/react` assigns props inside a `useLayoutEffect`
 * that RTL's `render()` flushes synchronously, so the heading needs to
 * exist synchronously on first paint too.
 *
 * Deliberately shadows the inherited `HTMLElement.prototype.title`
 * accessor (`@lit/react`'s `createComponent` sets it as a JS property —
 * `node.title = value` — for any prop name already `in elementClass
 * .prototype`, which native `title` always is; see
 * node_modules/@lit/react/development/create-component.js's `setProperty`
 * / `k in elementClass.prototype` check). This accessor never calls
 * `super.title` and never touches the `title` CONTENT ATTRIBUTE, only
 * `textContent` on the hand-built heading — so hovering the toolbar never
 * triggers the browser's native title tooltip (which fires off the
 * *attribute*, not the property). If this ever reflected as an attribute,
 * every toolbar would grow a redundant native tooltip repeating its own
 * visible heading.
 *
 * `filters`/`actions` are plain React children tagged with a `slot="..."`
 * ATTRIBUTE (a CSS hook only — this element has no shadow root, so the
 * attribute has no native slotting behavior), selected in ct-toolbar.css
 * exactly like ct-app-shell.css's `[slot="identity"]` etc. Neither region
 * is ever touched by this component's own DOM code, so there is no
 * React/Lit reconciliation hazard (ct-banner.ts's docstring) — arbitrary
 * filter/action content is safe.
 */
import { LitElement } from 'lit';
import { defineOnce } from '../define';
import './ct-toolbar.css';

const TAG = 'ct-toolbar';

export class CtToolbar extends LitElement {
  createRenderRoot(): this {
    return this;
  }

  private _titleEl: HTMLHeadingElement | null = null;
  private _title = '';

  connectedCallback(): void {
    super.connectedCallback();
    if (this._titleEl) {
      return;
    }
    const heading = document.createElement('h2');
    heading.className = 'ct-toolbar__title';
    heading.textContent = this._title;
    this.insertBefore(heading, this.firstChild);
    this._titleEl = heading;
  }

  /**
   * Section heading text, e.g. "Users". Rendered as a real `<h2>` — never
   * the native `title` tooltip attribute (see module docstring).
   */
  get title(): string {
    return this._title;
  }

  set title(value: string) {
    this._title = value;
    if (this._titleEl) {
      this._titleEl.textContent = value;
    }
  }

  // No render() override — filters/actions regions are React-owned
  // content this component must never move or wrap (see module docstring).
}

defineOnce(TAG, CtToolbar);
