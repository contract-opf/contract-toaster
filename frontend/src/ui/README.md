# frontend/src/ui — CTDS component library

Contributor guide, condensed from `docs/frontend-design-system.md` (**the
authority** — this file links to it rather than duplicating it; if the two
ever disagree, the doc wins). See the live component gallery
(`npm run dev` → `/gallery.html`, dev-only) for every component in every
variant/state, in both themes, plus the token sheet.

## Light-DOM doctrine

Components render into **light DOM by default**
(`createRenderRoot() { return this; }`), styled by a co-located
`ct-<name>.css` with classes namespaced `ct-<name>__*`. This is load-bearing,
not a style preference: `vitest.config.ts` runs jsdom with `css: false`
(bundled stylesheets are never loaded) and @testing-library's queries don't
pierce shadow roots — light DOM keeps both working with zero ceremony, and
keeps ARIA `id` references (`aria-controls`, `label[for]`,
`aria-describedby`) resolving, since those only resolve within one tree
scope and a shadow root is a separate one.

**Shadow DOM is allowed only for self-contained leaves** where every
meaningful piece of content is slotted (so it stays in light DOM for tests
and screen readers) and no ARIA id reference crosses the boundary —
`ct-chip` and `ct-card` are the only two. If a test would ever need to
assert something the component renders internally, it must be light DOM.

Several light-DOM components (`ct-button`, `ct-icon-button`, `ct-app-shell`,
`ct-field`, `ct-toolbar`, `ct-file-drop`, `ct-progress`) build their real DOM
once in `connectedCallback` and mutate it through hand-rolled property
accessors instead of Lit's declarative `render()` cycle. This is
intentional, not a shortcut: `@lit/react` assigns props inside a
`useLayoutEffect` that React Testing Library's `render()` flushes
synchronously, but Lit's reactive-property updates always defer to a
microtask — even on first render. This repo's tests fire events immediately
after `render()` with no `await`, so a component whose interactive DOM only
existed after Lit's async update lands would silently miss the very first
interaction. See `ct-button.ts`'s module docstring for the fully-worked
rationale (including why React `children` route through an internal `text`
property rather than a `<slot>`, to avoid React/Lit fighting over the same
node).

## `defineOnce`

Every component registers via `defineOnce(tag, klass)`
(`ui/define.ts`), never the `@customElement` decorator — vitest re-imports
component modules across test files, and a bare `customElements.define`
throws on the second registration. `defineOnce` makes re-evaluation a
harmless no-op in tests, dev/HMR, and the gallery alike.

## The `react.ts` wrapper rule

`ui/react.ts` is the **only** import surface React code may use for `ct-*`
elements — it wraps each one with `@lit/react`'s `createComponent` (typed
props, custom events mapped to `on*` callbacks). React code must never
import a `ui/components/*` module directly or write a raw `<ct-*>` JSX tag.
The dev gallery (`src/gallery/`) is the one deliberate exception: it has no
React and exercises the elements directly via `document.createElement`,
which doubles as a no-React smoke test of the library.

## Tokens only

Component CSS references `--ct-*` custom properties exclusively
(`frontend/src/styles/tokens.css` is the source of truth) —
`transparent`/`currentColor` are the only literal-value exceptions. No
`--pico-*`, no hardcoded hex.

## Testing pattern

- Tests live in `frontend/src/__tests__/ui-<name>.test.tsx`, rendering the
  **React wrappers** (`ui/react.ts`) — that's how the app consumes them.
- `render()` (RTL) does not wait for a custom element's own
  microtask-scheduled first update. Locate the host, `await
  (host as unknown as LitElement).updateComplete`, then query — see any
  existing `ui-*.test.tsx` file's `settled()`/`settleHost()` helper.
- `css: false`: never assert computed styles. Assert structure, attributes,
  ARIA roles, and testids instead.
- Every interactive component's test covers keyboard operability and ARIA
  wiring.

## Add-a-component checklist

1. Check `docs/frontend-design-system.md` §6 (inventory) — extend an
   existing component before adding one; new visual decisions must route
   through existing tokens.
2. Scaffold `components/ct-<name>.ts` (+ `.css` if light DOM). Pick DOM mode
   per the doctrine above — when in doubt, light DOM.
3. Register via `defineOnce`, export from `index.ts`, wrap in `react.ts`
   with typed props/events.
4. Style with `--ct-*` tokens only; namespace classes `ct-<name>__part`;
   add `:focus-visible { box-shadow: var(--ct-focus-ring) }` on every
   interactive part; reduced motion is handled globally by `base.css`'s
   guard (add a local guard only for inline `<style>` blocks, per the
   toaster hero's exception).
5. Add the component to the gallery (`src/gallery/sections.ts`) in every
   variant/state, with a usage snippet showing the `react.ts` consumption
   pattern.
6. Write `ui-<name>.test.tsx`. Run `bash scripts/check-frontend.sh`.
7. Never: web storage, `dangerouslySetInnerHTML`, tenant-brand strings, raw
   endpoint strings in copy, CDN anything, `--pico-*`, bare `npm install`
   (lockfile procedure: `docs/frontend-design-system.md` §3.3).

## The gallery entry (`frontend/gallery.html` + `src/gallery/`)

A second Vite entry, included in `build.rollupOptions.input` only when
`mode !== 'production'` (`vite.config.ts`) — it never ships. `npm run dev`
always serves it at `/gallery.html` regardless (Vite's dev server resolves
any `.html` under root on request). `src/gallery/` is covered by the same
`tsconfig.json` `"include": ["src"]` as the rest of the app, so `tsc` stays
green without a separate config.

Full doctrine: `docs/frontend-design-system.md`.
