/**
 * ct-tab-bar — the app's ARIA tablist (issue #390,
 * docs/frontend-design-system.md §6). Extracted behavior-identical from
 * `App.tsx:203-289` (pre-#390): same `role="tablist"`/`role="tab"` markup,
 * same `id="tab-{id}"`/`data-tab-id="{id}"`/`aria-controls="panel-{id}"`
 * wiring (panels stay in React-rendered light DOM — see the module note
 * below), same roving-tabindex + arrow/Home/End keyboard behavior.
 *
 * LIGHT DOM, mandatorily (§9 Notes on issue #390): this component's
 * `aria-controls` values reference panel ids that live in React-rendered
 * light DOM outside this element. ARIA ID-reference attributes only
 * resolve within a single tree scope — a shadow root is a separate tree
 * scope from the document — so if the `<button role="tab">` elements were
 * rendered inside a shadow root, `aria-controls`/`aria-selected` wiring to
 * the panels would silently stop resolving. Rendering into light DOM (this
 * element's own children, via `createRenderRoot() { return this; }`) keeps
 * everything in the same tree scope as the panels.
 *
 * This component takes no React children — every tab button is generated
 * by `render()` from the `tabs` property — so there is no reconciliation
 * hazard to route around (contrast ct-button.ts, which DOES receive React
 * children and hand-rolls its DOM for exactly that reason). A normal
 * lit-html `render()` into the light-DOM host is safe here.
 *
 * Controlled component: `active` is owned by the caller (App.tsx keeps
 * `activeTab` in React state), never mutated internally. Selecting a tab
 * (click or keyboard) only dispatches `ct-select {detail: {id}}` — the
 * caller decides whether/how to update `active`. Keyboard activation also
 * moves DOM focus to the target tab button synchronously (matching the
 * pre-#390 `focusTab` behavior) even though that button's `aria-selected`/
 * `tabindex` won't flip to reflect the new `active` value until the
 * caller's prop update round-trips back down — `.focus()` doesn't require
 * `tabindex="0"`, only the browser's native Tab-key cycle does.
 */
import { LitElement, html, type PropertyValues } from 'lit';
import { defineOnce } from '../define';
import './ct-tab-bar.css';

export interface CtTabDef {
  id: string;
  label: string;
}

const TAG = 'ct-tab-bar';

export class CtTabBar extends LitElement {
  createRenderRoot(): this {
    return this;
  }

  static properties = {
    tabs: { type: Array, attribute: false },
    active: { type: String },
  };

  declare tabs: CtTabDef[];
  declare active: string;

  constructor() {
    super();
    this.tabs = [];
    this.active = '';
  }

  connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener('resize', this._onResize);
  }

  disconnectedCallback(): void {
    window.removeEventListener('resize', this._onResize);
    super.disconnectedCallback();
  }

  protected updated(changed: PropertyValues): void {
    if (changed.has('tabs') || changed.has('active')) {
      this._positionIndicator();
    }
  }

  render() {
    return html`
      <div class="ct-tab-bar__track" role="tablist" aria-label="Sections" @keydown=${this._onKeyDown}>
        ${this.tabs.map((tab) => this._renderTab(tab))}
        <span class="ct-tab-bar__indicator" aria-hidden="true"></span>
      </div>
    `;
  }

  private _renderTab(tab: CtTabDef) {
    const selected = tab.id === this.active;
    return html`<button
      type="button"
      role="tab"
      id="tab-${tab.id}"
      data-tab-id=${tab.id}
      class="ct-tab-bar__tab"
      aria-selected=${selected ? 'true' : 'false'}
      aria-controls="panel-${tab.id}"
      tabindex=${selected ? '0' : '-1'}
      @click=${() => this._activate(tab.id, false)}
    >${tab.label}</button>`;
  }

  // Roving-tabindex + arrow-key navigation, ported 1:1 from the pre-#390
  // App.tsx `handleTabKeyDown`/`activateAt` pair.
  private _onKeyDown = (event: KeyboardEvent): void => {
    const currentIndex = Math.max(
      0,
      this.tabs.findIndex((t) => t.id === this.active),
    );
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      event.preventDefault();
      this._activateAt(currentIndex + 1);
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      event.preventDefault();
      this._activateAt(currentIndex - 1);
    } else if (event.key === 'Home') {
      event.preventDefault();
      this._activateAt(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      this._activateAt(this.tabs.length - 1);
    }
  };

  private _activateAt(index: number): void {
    if (this.tabs.length === 0) {
      return;
    }
    const next = this.tabs[(index + this.tabs.length) % this.tabs.length];
    if (next) {
      this._activate(next.id, true);
    }
  }

  private _activate(id: string, moveFocus: boolean): void {
    this.dispatchEvent(new CustomEvent('ct-select', { detail: { id }, bubbles: true, composed: true }));
    if (moveFocus) {
      const target = Array.from(this.querySelectorAll<HTMLButtonElement>('[data-tab-id]')).find(
        (btn) => btn.dataset.tabId === id,
      );
      target?.focus();
    }
  }

  private _onResize = (): void => {
    this._positionIndicator();
  };

  // Purely decorative sliding accent bar under the active tab
  // (aria-hidden — §9 Notes). Positioned via measured geometry rather than
  // CSS so it can animate between arbitrary tab widths/positions.
  private _positionIndicator(): void {
    const track = this.querySelector<HTMLElement>('.ct-tab-bar__track');
    const indicator = this.querySelector<HTMLElement>('.ct-tab-bar__indicator');
    if (!track || !indicator) {
      return;
    }
    const activeBtn = Array.from(track.querySelectorAll<HTMLButtonElement>('[data-tab-id]')).find(
      (btn) => btn.dataset.tabId === this.active,
    );
    if (!activeBtn) {
      indicator.style.opacity = '0';
      return;
    }
    const trackRect = track.getBoundingClientRect();
    const btnRect = activeBtn.getBoundingClientRect();
    indicator.style.opacity = '1';
    indicator.style.transform = `translateX(${btnRect.left - trackRect.left}px)`;
    indicator.style.width = `${btnRect.width}px`;
  }
}

defineOnce(TAG, CtTabBar);
