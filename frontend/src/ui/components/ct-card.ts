/**
 * ct-card — surface panel (issue #388, docs/frontend-design-system.md §6).
 *
 * Shadow DOM (§5.2): ALL content is slotted, so real content stays in
 * light DOM for @testing-library queries and screen readers (same pattern
 * as ct-chip.ts) while the shadow tree owns only the surface chrome
 * (border/shadow/radius/padding) via `:host`. Unlike ct-button/
 * ct-icon-button, there's no internal element that needs to CONTAIN
 * React's children — `<slot>` projection inside a REAL shadow root does
 * not move/reparent the light-DOM nodes React owns (verified empirically;
 * see ct-button.ts's docstring for what goes wrong when a light-DOM
 * component tries to relocate them instead), so there's no reconciliation
 * hazard here and arbitrary/complex/changing children are safe.
 */
import { LitElement, css, html } from 'lit';
import { defineOnce } from '../define';

export type CtCardPad = 'none' | 'md' | 'lg';

const TAG = 'ct-card';

export class CtCard extends LitElement {
  static properties = {
    pad: { type: String, reflect: true },
  };

  declare pad: CtCardPad;

  constructor() {
    super();
    this.pad = 'md';
  }

  static styles = css`
    :host {
      display: block;
      background: var(--ct-surface);
      border: 1px solid var(--ct-border);
      border-radius: var(--ct-radius);
      box-shadow: var(--ct-shadow-1);
    }
    :host([pad='md']) {
      padding: var(--ct-space-4) calc(var(--ct-space-4) + 2px);
    }
    :host([pad='lg']) {
      padding: var(--ct-space-5) var(--ct-space-6);
    }
    :host([pad='none']) {
      padding: 0;
    }
  `;

  render() {
    return html`<slot></slot>`;
  }
}

defineOnce(TAG, CtCard);
