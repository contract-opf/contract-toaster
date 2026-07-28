/**
 * ct-table — styled table wrapper (issue #392,
 * docs/frontend-design-system.md §6/§7). Replaces the ad-hoc
 * `.ct-table-scroll` wrapper div previously used on AdminUsers.tsx /
 * AdminRetention.tsx.
 *
 * LIGHT DOM (§5.1), and unlike ct-field/ct-app-shell/ct-toolbar this
 * component doesn't even need to build or move any DOM of its own: the
 * HOST element itself becomes the horizontal-scroll container
 * (`overflow-x: auto` in ct-table.css, same role `.ct-table-scroll` used
 * to play), and the slotted `<table>` stays exactly where React put it as
 * a direct child, styled purely through descendant selectors
 * (`ct-table table`, `ct-table thead th`, …) in ct-table.css. No
 * `render()` override, no `connectedCallback` DOM-building — there is
 * nothing here for React and Lit to fight over (same principle as
 * ct-banner.ts, taken to its simplest form).
 *
 * Native `<table>/<thead>/<tbody>` markup stays real and slotted, so RTL's
 * `getByRole('table')`/`'row'`/`'cell'` and screen readers see full table
 * semantics — this component only ever adds visual chrome around it.
 *
 * `.ct-table__mono` is an opt-in class for id/digest cells (review IDs,
 * cognito subs, hashes) — apply it to a `<td>` to switch that cell to
 * `--ct-font-mono`.
 */
import { LitElement } from 'lit';
import { defineOnce } from '../define';
import './ct-table.css';

const TAG = 'ct-table';

export class CtTable extends LitElement {
  createRenderRoot(): this {
    return this;
  }

  // No render() override, no connectedCallback DOM-building — see the
  // module docstring: the slotted <table> is React-owned content this
  // component must never move or wrap.
}

defineOnce(TAG, CtTable);
