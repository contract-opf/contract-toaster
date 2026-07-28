/**
 * ui/react.ts — the ONLY import surface React code uses for ct-* components
 * (CTDS foundation, issue #385).
 *
 * React 18 handles custom-element *attributes* but not rich properties or
 * custom events. `@lit/react`'s `createComponent` wraps each element in a
 * real typed React component (props map to Lit reactive properties, custom
 * events map to `on*` callbacks). Importing a component module here also
 * registers it (via `defineOnce` in the component module) — so importing
 * from this file alone is sufficient. React code must never import a
 * `ui/components/*` module directly or write a raw `<ct-*>` JSX tag; doing
 * so bypasses this typed surface and the centralized event wiring it exists
 * to provide.
 */
import React from 'react';
import { createComponent } from '@lit/react';
import type { EventName } from '@lit/react';
import { CtChip as CtChipElement } from './components/ct-chip';
import type { CtChipVariant } from './components/ct-chip';
import { CtButton as CtButtonElement } from './components/ct-button';
import type { CtButtonSize, CtButtonType, CtButtonVariant } from './components/ct-button';
import { CtIconButton as CtIconButtonElement } from './components/ct-icon-button';
import { CtCard as CtCardElement } from './components/ct-card';
import type { CtCardPad } from './components/ct-card';
import { CtBanner as CtBannerElement } from './components/ct-banner';
import type { CtBannerVariant } from './components/ct-banner';
import { CtTabBar as CtTabBarElement } from './components/ct-tab-bar';
import type { CtTabDef } from './components/ct-tab-bar';
import { CtAppShell as CtAppShellElement } from './components/ct-app-shell';
import { CtField as CtFieldElement } from './components/ct-field';
import { CtTable as CtTableElement } from './components/ct-table';
import { CtToolbar as CtToolbarElement } from './components/ct-toolbar';
import { CtFileDrop as CtFileDropElement } from './components/ct-file-drop';
import { CtProgress as CtProgressElement } from './components/ct-progress';

export type { CtChipVariant, CtButtonVariant, CtButtonSize, CtButtonType, CtCardPad, CtBannerVariant, CtTabDef };

export const CtChip = createComponent({
  tagName: 'ct-chip',
  elementClass: CtChipElement,
  react: React,
});

// ---------------------------------------------------------------------------
// CtButton / CtIconButton — both wrap createComponent's output rather than
// re-exporting it directly, because their visible label must be routed
// through an internal (`attribute: false`) Lit property instead of passed
// straight through as light-DOM `children`. See ct-button.ts's docstring
// for the empirically-verified reason: React's custom-element child
// reconciliation does not tolerate the receiving element relocating nodes
// it handed over, which is exactly what a naive `<button><slot></slot>`
// wrapper would need to do.
// ---------------------------------------------------------------------------

const CtButtonComponent = createComponent({
  tagName: 'ct-button',
  elementClass: CtButtonElement,
  react: React,
});

export interface CtButtonProps
  extends Omit<React.ComponentProps<typeof CtButtonComponent>, 'text' | 'children'> {
  children: string;
}

export function CtButton({ children, ...rest }: CtButtonProps): React.ReactElement {
  return React.createElement(CtButtonComponent, { ...rest, text: children });
}

const CtIconButtonComponent = createComponent({
  tagName: 'ct-icon-button',
  elementClass: CtIconButtonElement,
  react: React,
});

export interface CtIconButtonProps
  extends Omit<React.ComponentProps<typeof CtIconButtonComponent>, 'text' | 'pressed' | 'children' | 'aria-pressed'> {
  children: string;
  label: string;
  'aria-pressed'?: boolean;
}

export function CtIconButton({
  children,
  'aria-pressed': ariaPressed,
  ...rest
}: CtIconButtonProps): React.ReactElement {
  return React.createElement(CtIconButtonComponent, { ...rest, text: children, pressed: ariaPressed });
}

// ---------------------------------------------------------------------------
// CtCard / CtBanner — neither one templates its children (ct-card projects
// them through a real shadow-DOM <slot>, ct-banner never overrides
// render() at all), so there's no reconciliation hazard to route around;
// createComponent's output is used directly.
// ---------------------------------------------------------------------------

export const CtCard = createComponent({
  tagName: 'ct-card',
  elementClass: CtCardElement,
  react: React,
});

export const CtBanner = createComponent({
  tagName: 'ct-banner',
  elementClass: CtBannerElement,
  react: React,
});

// ---------------------------------------------------------------------------
// CtTabBar / CtAppShell (issue #390) — shell & tab-bar extraction from
// App.tsx. Neither wraps createComponent's output: ct-tab-bar takes no
// React children at all (every tab button is generated from its `tabs`
// property, see ct-tab-bar.ts), and ct-app-shell never touches the
// children React passes it (see ct-app-shell.ts's docstring) — so there is
// no reconciliation hazard to route around for either, unlike
// CtButton/CtIconButton above.
// ---------------------------------------------------------------------------

export const CtTabBar = createComponent({
  tagName: 'ct-tab-bar',
  elementClass: CtTabBarElement,
  react: React,
  events: {
    onSelect: 'ct-select' as EventName<CustomEvent<{ id: string }>>,
  },
});

export const CtAppShell = createComponent({
  tagName: 'ct-app-shell',
  elementClass: CtAppShellElement,
  react: React,
});

// ---------------------------------------------------------------------------
// CtField (issue #391) — form field wrapper (label + control-slot + hint +
// error). Like CtBanner/CtCard/CtAppShell above, it never templates the
// children React hands it (see ct-field.ts's docstring), so there's no
// reconciliation hazard to route around; createComponent's output is used
// directly, and the slotted control (a real light-DOM `<input>` etc.) is
// exactly what React put there.
// ---------------------------------------------------------------------------

export const CtField = createComponent({
  tagName: 'ct-field',
  elementClass: CtFieldElement,
  react: React,
});

// ---------------------------------------------------------------------------
// CtTable / CtToolbar (issue #392) — data-surface upgrades for the admin
// panels. Neither wraps createComponent's output: ct-table never overrides
// `render()` at all (the slotted `<table>` stays exactly where React put
// it — see ct-table.ts's docstring), and ct-toolbar's `filters`/`actions`
// regions are untouched `slot="..."`-tagged children, the same
// no-reconciliation-hazard shape as ct-app-shell/ct-field above. See
// ct-toolbar.ts's docstring for why its `title` prop is safe to pass
// straight through despite shadowing the inherited `HTMLElement.title`.
// ---------------------------------------------------------------------------

export const CtTable = createComponent({
  tagName: 'ct-table',
  elementClass: CtTableElement,
  react: React,
});

export const CtToolbar = createComponent({
  tagName: 'ct-toolbar',
  elementClass: CtToolbarElement,
  react: React,
});

// ---------------------------------------------------------------------------
// CtFileDrop / CtProgress (issue #393) — review-flow redesign. Neither wraps
// createComponent's output: ct-file-drop takes no React children at all (its
// well/input/label/pill are built internally, see ct-file-drop.ts), and
// ct-progress is the same shape (track/fill/label all internal) — so there
// is no reconciliation hazard to route around for either.
// ---------------------------------------------------------------------------

export const CtFileDrop = createComponent({
  tagName: 'ct-file-drop',
  elementClass: CtFileDropElement,
  react: React,
  events: {
    onFiles: 'ct-files' as EventName<CustomEvent<{ files: File[] }>>,
  },
});

export const CtProgress = createComponent({
  tagName: 'ct-progress',
  elementClass: CtProgressElement,
  react: React,
});
