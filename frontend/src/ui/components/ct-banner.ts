/**
 * ct-banner — inline status surface (issue #388,
 * docs/frontend-design-system.md §6). Replaces `.ct-error`/`.ct-status`/
 * `.ct-note`. `variant` drives both the tinted surface and the computed
 * `role` (`alert` for `danger`, `status` otherwise — screen readers should
 * be interrupted for an error but merely informed of a status change).
 *
 * Light DOM (§5.1), but unlike ct-button/ct-icon-button this component
 * does NOT render any internal wrapper element around its content and
 * does NOT override `render()` at all — LitElement's default `render()`
 * returns `noChange`, so lit-html never touches `this`'s children.
 * React's children therefore stay exactly where React put them (direct
 * children of the host), which sidesteps the reconciliation hazard
 * documented in ct-button.ts's docstring entirely: there is nothing here
 * for React and Lit to fight over. Lit only ever touches the HOST's own
 * attributes (`variant`, and the derived `role`) — the same shape as
 * ct-chip's `:host` attribute-selector styling, just without a shadow
 * root. Styling is the co-located `ct-banner.css`, keyed off
 * `ct-banner[variant=...]`.
 */
import { LitElement } from 'lit';
import { defineOnce } from '../define';
import './ct-banner.css';

export type CtBannerVariant = 'ok' | 'warn' | 'danger' | 'info' | 'muted';

const TAG = 'ct-banner';

export class CtBanner extends LitElement {
  createRenderRoot(): this {
    return this;
  }

  static properties = {
    variant: { type: String, reflect: true },
  };

  declare variant: CtBannerVariant;

  constructor() {
    super();
    this.variant = 'info';
  }

  willUpdate(): void {
    this.setAttribute('role', this.variant === 'danger' ? 'alert' : 'status');
  }
}

defineOnce(TAG, CtBanner);
