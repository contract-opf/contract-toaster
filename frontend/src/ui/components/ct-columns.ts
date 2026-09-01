/**
 * ct-columns — CTDS two-column layout primitive (issue #601,
 * docs/frontend-design-system.md §6/§11). Admin panels are today a single
 * full-width column (`ct-stack`, vertical only), so a control that needs a
 * handful of characters — the retention window, in days — spans the whole
 * viewport and every screen becomes a long vertical scroll. This primitive
 * gives a panel a second column to put related short fields side by side.
 *
 * LIGHT DOM (§5.1), same shape as ct-toolbar.ts/ct-app-shell.ts: a bare CSS
 * Grid on the host tag itself (ct-columns.css), styled entirely from
 * tokens. No `render()` override, no internal DOM built in
 * `connectedCallback` — children are never moved, wrapped, or templated,
 * they stay exactly where React put them in the DOM. That means the grid
 * only repositions them VISUALLY; source/DOM/tab order is untouched by
 * construction, so a two-column visual arrangement can never scramble
 * keyboard order the way an actual DOM reorder could (an acceptance
 * criterion of #601) — there is nothing here for React and Lit to fight
 * over either (ct-banner.ts's docstring names the same no-reconciliation-
 * hazard shape).
 *
 * Deliberately has no properties: "keep it small and general; this is not
 * a grid framework" (#601's own scope note). Exactly two columns at desktop
 * width, collapsing to one column at the same breakpoint ct-app-shell's own
 * outer grid uses (640px, issue #390) — the whole app goes single-column
 * together rather than this primitive breaking at a different width than
 * its parent shell. More than two children simply wrap onto additional
 * rows; there is no `span` API.
 *
 * A child that should NOT stretch to fill its column's width (a short
 * control, e.g. a number input) is not this component's concern — that is
 * `ct-field`'s own `narrow` prop (ct-field.ts). This primitive only ever
 * decides column COUNT; a child's own sizing is between that child and its
 * own styling.
 *
 * When NOT to use this: a single logical control (nothing to pair it with
 * — `ct-stack` alone is correct), or a form whose fields must be read in
 * strict top-to-bottom sequence (a two-column layout reads left-to-right
 * per row before moving down, which breaks a sequence-dependent read
 * order even though DOM/tab order stays correct — see
 * docs/frontend-design-system.md §6).
 */
import { LitElement } from 'lit';
import { defineOnce } from '../define';
import './ct-columns.css';

const TAG = 'ct-columns';

export class CtColumns extends LitElement {
  createRenderRoot(): this {
    return this;
  }

  // No render() override — children are React-owned content this
  // component must never move or wrap (see module docstring).
}

defineOnce(TAG, CtColumns);
