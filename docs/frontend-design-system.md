# Contract Toaster Design System (CTDS)

**Status:** approved design, implementation tracked in GitHub issues (label `afk`, milestone: design-system)
**Date:** 2026-07-18
**Replaces:** the Pico CSS (`@picocss/pico` pumpkin build) base layer described in `docs/design-notes.md`

This document is the single source of truth for the frontend design system: the
visual language, the token vocabulary, the Lit component architecture, the hard
constraints every component must honor, and the migration plan away from Pico.
**If you are implementing a design-system issue, read the sections it cites
before writing code.** The contributor how-to lives in §11.

---

## 1. Vision

Contract Toaster's UI should feel like a **precision kitchen appliance**:
warm, tactile, and playful where the brand lives (the toaster hero), and
**instrument-panel clear** where decisions happen (status, confidence,
admin controls). Today's Pico-based UI reads as an unstyled prototype —
default element styling, floating controls with no visual hierarchy, a
hero stranded in whitespace. The redesign closes that gap.

Design principles, in priority order:

1. **Status is sacred.** A legal-review tool lives and dies on unambiguous
   state communication. Status colors, chips, and banners follow a strict
   semantic palette (§4.3) and never decorate anything non-semantic.
   (This is the OpenBridge influence — see §12.)
2. **Warmth without whimsy.** The toaster brand supplies warmth (palette,
   radii, the hero, micro-interactions). Everything else is quiet, dense
   enough for professional use, and typographically disciplined.
3. **One vocabulary.** Every visual decision routes through tokens (§4).
   Components never hardcode colors, spacing, shadows, or timing.
4. **Accessible by construction.** Focus rings, reduced-motion guards,
   ARIA patterns, and contrast are built into the components, not
   retrofitted per screen.
5. **Boring to extend.** Adding a component or a screen should require no
   novel decisions — the doctrine in §5 and the checklist in §11 decide
   everything structural in advance.

## 2. Architecture decision

**We build a first-party Lit component library (`frontend/src/ui/`,
custom-element prefix `ct-`) consumed by the existing React 18 shell via
`@lit/react` wrappers. Pico is removed at the end of the migration. We do
not adopt OpenBridge's components as a dependency, and we do not rewrite
the React shell.**

Rationale:

- **Why Lit at all:** the goal is a durable, framework-agnostic design
  system. Custom elements outlive React versions and can be consumed by
  any future shell (or server-rendered pages) unchanged. Lit 3 is tiny
  (~5 KB), has no runtime dependencies, and builds cleanly into our
  same-origin Vite bundle (CSP-safe, §3.1).
- **Why not a React component library:** couples the design system to
  React 18's lifetime; the stated direction for this codebase is Lit.
- **Why not OpenBridge components (`@oicl/openbridge-webcomponents`):**
  it is a maritime instrument library — 200+ components of which we'd use
  a handful of generic ones, restyled beyond recognition. Its **license is
  AGPL for the first 6 months after each release, Apache-2.0 only after**
  — an unacceptable drift risk for a commercially deployed product. We
  borrow its *ideas* (semantic status discipline, day/night palettes,
  token architecture) and none of its code. See §12.
- **Why not a full React→Lit shell rewrite now:** the shell (auth
  branching, tab state, polling) is stable and heavily tested. The
  design system delivers the visual overhaul without destabilizing it.
  Because the components are custom elements, a later shell migration
  (React → Lit or anything else) reuses them as-is; that path stays open
  and is deliberately out of scope here.
- **React interop:** React 18 handles custom-element *attributes* but not
  rich properties/events. `@lit/react`'s `createComponent` produces real
  React components with typed props and event mapping. All React code
  imports components **only** from `frontend/src/ui/react.ts` — never
  raw tag names — so typing and event wiring stay centralized.

## 3. Hard constraints (violating any of these breaks CI or production)

Every one of these is enforced by an existing test or a production
environment. Issues cite them; do not "clean them up."

### 3.1 CSP: everything same-origin, no CDN, ever
The deployed CSP (`infra/lib/nested/frontend-stack.ts:213`) is
`default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
img-src 'self' data:; font-src 'self' data:; connect-src 'self' <cognito> <api>`.
Consequences:
- No CDN scripts, stylesheets, or fonts. **Fonts ship as npm packages**
  (`@fontsource/*`) whose woff2 files Vite emits same-origin (§4.5).
- No `media-src` → audio falls back to `default-src 'self'`; mp3s must
  remain real same-origin files. `vite.config.ts` pins
  `assetsInlineLimit` so audio is never inlined as `data:`. Do not touch.
- Inline `<style>` elements are allowed (`'unsafe-inline'` in style-src)
  — the toaster's inline stylesheet depends on this.
- `tests/test_frontend_xss_posture.py` (CI) asserts the CSP exists.

### 3.2 The test harness cannot see shadow DOM or bundled CSS
`vitest.config.ts` runs jsdom with **`css: false`** (imported stylesheets
are ignored) and the suite uses @testing-library/react, whose queries
(`getByTestId`, `getByText`, `getByRole`) **do not pierce shadow roots**.
This drives the light-DOM doctrine in §5. Additionally,
`toaster-states.test.tsx:163-172` asserts a live inline `<style>` element
matching `/prefers-reduced-motion:\s*reduce/` — which is why the
toaster's animation CSS must stay in the inline `ToasterStyles` block
(`frontend/src/toaster/Toaster.tsx`), not a bundled `.css` file.

### 3.3 npm lockfile: never run bare `npm install` in `frontend/`
Local npm is 11.x; the Docker build (`deploy/dts/frontend.Dockerfile`,
node:20-slim / npm 10.8.2) runs `npm ci`, which hard-fails on npm-11
lockfile rewrites. **To add a dependency:** edit `package.json`, then
regenerate the lock with the same npm version the container uses:

```bash
cd frontend && git checkout package-lock.json
npx npm@10.8.2 install --package-lock-only   # pin the container's npm; no Docker needed
npx npm@10.8.2 ci --ignore-scripts --no-audit --no-fund   # prove the Docker build's `npm ci` will pass
```

(If Docker is available, `docker run --rm -v "$(pwd)":/app -w /app
node:20-slim npm install --package-lock-only` is equivalent.) Either way
the result is a small additive diff. A 500-line lockfile diff means you
did it wrong.

### 3.4 Behavioral invariants locked by tests
- **All tabpanels stay mounted**, toggled via the `hidden` attribute —
  never conditionally unmount (`security-posture.test.tsx` +
  polling-state survival; see `App.tsx:294-300`).
- **No web storage.** `localStorage.setItem`/`sessionStorage.setItem` are
  banned by a source grep in `security-posture.test.tsx`; tokens and
  mute state are in-memory only.
- **No `dangerouslySetInnerHTML`** anywhere in `frontend/src` (same test
  + `tests/test_frontend_xss_posture.py`).
- **No tenant-brand strings strings** in UI or code (white-label release;
  asserted in `toaster-states.test.tsx` and resilience tests). Voicing is
  "your" ("your review", "your document").
- **Error copy never leaks internals** — no `/api/` paths, no `HTTP <n>`
  in rendered text (`resilience-a11y.test.tsx`).
- **Existing `data-testid`s and ARIA roles are contract.** The tab
  shell's roving-tabindex tablist, the toaster dial `radiogroup`
  (`review-playbook-dial`), `toaster-state-{progress,done,sober}`,
  `sound-toggle`, login testids — migrations must preserve them so the
  suite keeps passing unmodified unless an issue explicitly says a test
  changes.

## 4. Design tokens (v2)

`frontend/src/styles/tokens.css` remains the single source of truth,
extended (existing `--ct-*` names keep their meaning; components never
reference `--pico-*`, which disappears in the removal phase). All tokens
are CSS custom properties on `:root`, so they pierce shadow DOM freely.

### 4.1 Themes
Two themes, automatic via `prefers-color-scheme` with a `[data-theme]`
attribute override (already the pattern in tokens.css):

- **Day ("counter-top")** — warm off-white app background `#faf7f2`,
  cream card surfaces, soft warm-gray borders, charcoal text `#26221c`.
  Shadows are warm-tinted, low-opacity.
- **Night ("midnight kitchen")** — warm-tinted dark surfaces (never pure
  gray): app `#191512`, cards `#221d18`, raised `#2b241e`. Depth comes
  from surface steps + 1px borders; shadows nearly disappear.

### 4.2 Surfaces & elevation
```
--ct-bg              app background
--ct-surface         card / panel background
--ct-surface-raised  menus, popovers, the active tab
--ct-surface-sunken  wells, table headers, the toaster slot backdrop
--ct-border          hairline borders (1px everywhere)
--ct-border-strong   input borders, emphasized dividers
--ct-shadow-1        resting card        (warm-tinted, subtle)
--ct-shadow-2        hover lift / dropdowns
--ct-shadow-3        modal / toast-pop moments
```
Cards = `--ct-surface` + 1px `--ct-border` + `--ct-shadow-1` +
`--ct-radius`. Elevation changes are *both* shadow and surface steps so
dark mode still reads depth.

### 4.3 Color: brand + semantic status
Brand ramp stays anchored on toaster orange (existing values):
`--ct-accent #af4b29`, `--ct-accent-strong`, `--ct-accent-soft`,
`--ct-glow`, `--ct-toast`, `--ct-toast-crust`. New:
`--ct-accent-contrast` (text on accent, `#fff8f2`).

Status palette (existing `--ct-ok/warn/danger/info/neutral` + `-bg`
pairs) gains a `-border` step per status for chips/banners
(`--ct-ok-border`, etc.). **Discipline rule:** status colors appear only
on semantic state (review status, validation, holds, key state) — never
as decoration. Interactive affordance is always the accent ramp. All
text/background pairings must meet WCAG AA (4.5:1); the a11y-hardening
issue audits both themes.

### 4.4 Rhythm, radii, focus, motion
```
--ct-space-1..8      4 8 12 16 24 32 48 64px   (replaces ad-hoc gaps;
                     --ct-gap* kept as aliases during migration)
--ct-radius-sm 8px   --ct-radius 12px   --ct-radius-lg 20px   --ct-radius-full 999px
--ct-maxw 960px      (unchanged)
--ct-focus-ring      0 0 0 2px var(--ct-bg), 0 0 0 4px var(--ct-accent)
                     (two-layer ring; every interactive element uses
                     :focus-visible { box-shadow: var(--ct-focus-ring) })
--ct-ease-out        cubic-bezier(.2,.8,.3,1)
--ct-ease-spring     cubic-bezier(.34,1.56,.64,1)   (toast pop, chip enter)
--ct-dur-fast 120ms  --ct-dur 200ms  --ct-dur-slow 400ms
```
Every animation/transition in the system uses these tokens and sits
behind the global reduced-motion guard in `base.css`
(`@media (prefers-reduced-motion: reduce)` zeroes durations). The
toaster's inline stylesheet keeps its own guard (§3.2).

### 4.5 Typography
Self-hosted via `@fontsource` packages (OFL-licensed, bundled woff2 —
CSP-safe):

- **Display / brand:** `Space Grotesk` — header brand, tab labels,
  section titles, the hero caption. Gives the "appliance nameplate" voice.
- **UI / body:** `Instrument Sans` — everything else.
- **Mono:** `IBM Plex Mono` — review IDs, digests, key hints, version.

```
--ct-font-display / --ct-font-sans / --ct-font-mono   (each with system fallbacks)
--ct-text-xs 12px  --ct-text-sm 14px  --ct-text-md 16px
--ct-text-lg 20px  --ct-text-xl 25px  --ct-text-2xl 31px
--ct-leading-tight 1.25   --ct-leading 1.55
```
Load only latin subsets, weights 400/500/700 (display: 500/700) — keep
the font payload under ~120 KB total.

## 5. Component doctrine: light-DOM-first Lit

This is the load-bearing architectural rule; it exists because of §3.2.

1. **Default: components render into light DOM.** Override
   `createRenderRoot() { return this; }`. Styling comes from a
   co-located `<component>.css` file (imported by the component module,
   bundled by Vite) using classes namespaced `ct-<component>__*`.
   Light DOM keeps @testing-library queries, ARIA `aria-controls`/
   `aria-labelledby` ID references, form participation, and the
   `css:false` test harness all working with zero ceremony.
2. **Shadow DOM is allowed only for self-contained leaves** where *all
   meaningful content is slotted* (slotted children stay in light DOM, so
   tests and screen readers still see them) and no ARIA ID reference
   crosses the boundary. If a test would ever need to assert an element
   the component renders internally, it must be light DOM.
3. **Registration:** every component guards registration through
   `defineOnce(tag, klass)` (`frontend/src/ui/define.ts`) instead of the
   `@customElement` decorator — vitest re-imports modules across test
   files and a bare `customElements.define` throws on the second call.
4. **React consumption:** `frontend/src/ui/react.ts` exports a
   `createComponent` wrapper per element (typed props, custom events
   mapped to `on*` callbacks). React code never writes `<ct-*>` tags
   directly.
5. **Naming & files:** tag `ct-<name>`; class `Ct<Name>`; files
   `frontend/src/ui/components/ct-<name>.ts` + `ct-<name>.css`; events
   are lowercase `ct-` prefixed CustomEvents (`ct-change`, `ct-select`,
   `ct-files`) with `detail` payloads, `bubbles: true, composed: true`.
6. **Tokens only.** Component CSS references `--ct-*` exclusively — a
   grep for `--pico-` or hex colors in `frontend/src/ui/` should return
   nothing (allowed exception: `transparent`/`currentColor`).

## 6. Component inventory

Phase-1 inventory (each maps to an implementation issue; APIs are the
contract — extend, don't rename):

| Element | DOM | Purpose / notes |
|---|---|---|
| `ct-chip` | shadow (slotted label) | Status pill. `variant: ok\|warn\|danger\|info\|muted`, optional `dot`. Replaces `.ct-chip*` classes. |
| `ct-button` | light | `variant: primary\|secondary\|ghost\|danger`, `size: sm\|md`, `loading` (spinner + `aria-busy`), renders a real `<button>` (form participation, testids land on it). |
| `ct-icon-button` | light | Square hit-target ≥44px, `label` (required, becomes `aria-label`). |
| `ct-card` | shadow (slotted) | Surface + border + shadow-1 + radius; `pad: none\|md\|lg`. Replaces `.ct-card`. |
| `ct-banner` | light | Inline status surface. `variant` as chip; `role="status"` or `"alert"` (danger). Replaces `.ct-error/.ct-status/.ct-note`. |
| `ct-tab-bar` | light | ARIA tablist with roving tabindex + arrow/Home/End nav, extracted from `App.tsx:203-289` **behavior-identical** (same roles, `data-tab-id`, `aria-controls` to light-DOM panels). Emits `ct-select {id}`. Panels stay in React and stay mounted (§3.4). Animated active indicator (token motion). |
| `ct-app-shell` | light | Header (brand nameplate in display face, identity, role badge, sign-out slot), max-width content column on `--ct-bg`, footer (version in mono). Slots: `header-actions`, default, `footer`. |
| `ct-field` | light | Label + control-slot + hint + error with wired `for`/`aria-describedby`; error text in `role="alert"` context per existing copy rules. |
| `ct-table` | light | Styled table wrapper: sunken header row, hairline rows, hover tint, `.ct-table-scroll` behavior built in (horizontal scroll wrapper). |
| `ct-toolbar` | light | Row layout for filters/actions above tables; replaces `.ct-toolbar/.ct-row/.ct-actions` usage in admin panels. |
| `ct-file-drop` | light | Drag-and-drop + click-to-browse upload. Accept list, max size, selected-file pill (name/size/clear), keyboard + SR accessible (`<input type=file>` under the hood), emits `ct-files {files}`. Visually rhymes with the toaster slot: sunken well, accent glow on dragover. |
| `ct-progress` | light | Indeterminate warm shimmer bar + optional phase caption; used during upload/poll alongside the hero. |

Deliberately not building: modal/dialog (no current use), toast/snackbar
(banners + hero cover it), router (tabs are state), icon system beyond
inline SVG (keep art inline per current pattern).

## 7. Screen direction (what "five stars" means per surface)

- **Shell:** warm `--ct-bg` page, single 960px column, appliance-
  nameplate header (Space Grotesk brand + thin hairline), pill role
  badge, quiet mono footer. Tabs get a sliding active indicator and
  clear hover/focus states instead of bare links.
- **Review tab:** the hero finally gets a stage — a `ct-card` "counter"
  with the toaster centered on a subtle sunken mat, dial caption in
  display face. Below it, one coherent submission row: `ct-file-drop`
  (not a naked `Choose File`), primary `ct-button` "Toast it", sound
  `ct-icon-button`. Status area becomes `ct-banner` + `ct-chip` row
  (review ID in mono inside a muted chip) with the confidence band and
  critic-delta presented as labeled chips above the download button.
  Existing testids/copy preserved (§3.4).
- **Admin tabs:** every panel is a `ct-card` with a `ct-toolbar` header
  (title + actions), `ct-table` for lists, `ct-field` forms, statuses as
  chips. Retention/legal-hold states get the full semantic treatment.
- **Login (password mode):** centered `ct-card` on the warm background,
  nameplate brand above, `ct-field` inputs — first impression matches
  the rest.
- **Empty/loading:** every async surface gets a quiet skeleton or
  `ct-progress`; no layout jumps between states.

## 8. Toaster hero v2

The hero stays the brand centerpiece and keeps its entire behavioral
contract: `ToasterHero` props and `ToasterPhase` API, the ARIA dial
radiogroup, all `data-testid`s, sounds wiring, and the **inline
`ToasterStyles` constraint (§3.2)**. The upgrade is visual:

- Chrome body re-rendered with richer multi-stop gradients, a soft
  environment reflection, tighter bezel highlights, and a warm contact
  shadow onto the card mat (instead of floating in white).
- Dial becomes a machined knob: knurled ring, engraved tick marks,
  accent pointer; stops read as printed labels.
- Slot glow gains depth (inner gradient + bloom) while `working`; the
  `done` toast pop gets crumb texture and uses `--ct-ease-spring`; the
  `error` state keeps the sober grayscale treatment but sits on a
  `danger`-tinted mat edge so it reads as a state, not a rendering bug.
- All colors route through tokens so the hero is theme-correct in night
  mode (chrome darkens, glow warms).
- The `ct-file-drop` well sits directly beneath the slot, visually
  "feeding" the toaster — drag-over lights the slot glow (pure CSS class
  toggle; no new test-visible behavior changes).

## 9. File layout & wiring

```
frontend/src/ui/
  README.md            contributor guide (§11 condensed + examples)
  define.ts            defineOnce(tag, klass)
  index.ts             side-effect element registrations
  react.ts             @lit/react wrappers — the ONLY React import surface
  components/
    ct-button.ts / ct-button.css
    ct-chip.ts   (shadow: styles in `static styles`)
    ...
frontend/src/styles/
  tokens.css           token source of truth (§4)
  base.css             NEW: element base layer replacing Pico (typography,
                       forms, buttons fallback, links, tables baseline)
  app.css              shrinks as components absorb its classes; deleted
                       classes are removed, not left as dead code
frontend/gallery.html + frontend/src/gallery/   dev-only component gallery
```
`main.tsx` import order during migration: pico → tokens.css → base.css →
app.css → amplify. After Pico removal: tokens.css → base.css → app.css →
amplify. New deps: `lit`, `@lit/react`, `@fontsource/*` (lockfile
procedure §3.3 applies every time).

**Gallery:** `frontend/gallery.html` is a second Vite entry rendering
every component in every variant/state in both themes, plus the token
sheet. Dev-only: included in `rollupOptions.input` only when
`mode !== 'production'`, so the prod bundle is unaffected. It is the
visual QA surface and the living documentation for future workers.

## 10. Testing the design system

- Component tests live in `frontend/src/__tests__/ui-<name>.test.tsx`,
  written with @testing-library/react against the **React wrappers**
  (that's how the app consumes them). Await Lit's completion with
  `await (el as LitElement).updateComplete` (or RTL `findBy*`) before
  asserting.
- Light-DOM components: standard queries work. Shadow leaves: assert
  slotted content and host attributes only — if you need more, the
  component should be light DOM (§5.2).
- Every interactive component's test asserts keyboard operability and
  ARIA wiring (the tab bar test inherits the existing App.tsx keyboard
  cases when extracted).
- Migration issues run the **existing** suite untouched as their
  regression gate — that is the point of preserving testids/roles.
- Gates: `bash scripts/check-frontend.sh` (tsc + vite build + vitest)
  and `.venv/bin/python tests/test_frontend_xss_posture.py`
  (source-posture greps). Both offline.

## 11. Contributor guide: adding or changing a component

1. **Check the inventory (§6).** Extend an existing component before
   inventing a new one; new visual decisions must route through existing
   tokens — if a value isn't expressible in tokens, propose a token
   first, in its own commit.
2. Scaffold `components/ct-<name>.ts` (+ `.css` if light DOM). Choose
   DOM mode by the doctrine (§5.1–5.2) — when in doubt, light DOM.
3. Register via `defineOnce`, export from `index.ts`, wrap in
   `react.ts` with typed props/events.
4. Style with `--ct-*` tokens only; namespace classes
   `ct-<name>__part`; include `:focus-visible { box-shadow:
   var(--ct-focus-ring) }` on interactive parts; respect reduced motion
   (inherit the base.css guard; add a local guard only for inline
   `<style>` blocks).
5. Add the component to the gallery page in all variants/states.
6. Write `ui-<name>.test.tsx`: rendering, variants, keyboard, ARIA,
   events. Run `bash scripts/check-frontend.sh`.
7. Never: web storage, `dangerouslySetInnerHTML`, the tenant name, raw endpoint
   strings in copy, CDN anything, `--pico-*` references, bare
   `npm install` (§3.3).

## 12. References & licensing

- **Lit 3** — BSD-3-Clause, npm `lit`. **@lit/react** — BSD-3-Clause.
- **OpenBridge** (design-language reference only): guidelines at
  openbridge.no; components repo
  `Ocean-Industries-Concept-Lab/openbridge-webcomponents`. **Do not add
  it as a dependency** — releases are AGPL for their first 6 months
  before relaxing to Apache-2.0, which is incompatible with this
  product's distribution posture. Borrow patterns (status discipline,
  day/night theming), never code.
- **Fonts** — Space Grotesk, Instrument Sans, IBM Plex Mono: all SIL OFL
  via `@fontsource/*` packages (MIT packaging).
- Prior art in-repo: `docs/design-notes.md` (Pico-era system, superseded
  by this doc), `docs/threat-model.md` (CSP rationale),
  `frontend/src/assets/sounds/SOURCES.md` (audio provenance).

## 13. Migration plan (maps 1:1 to the `afk` issue queue)

Strangler pattern; Pico stays until every screen is migrated, then one
removal commit. Each phase lands green on `main`.

1. **Foundation** — deps (`lit`, `@lit/react`), `ui/` scaffold,
   `defineOnce`, first component (`ct-chip`) proving the
   Lit-under-vitest pattern end-to-end.
2. **Tokens v2 + base layer** — §4 tokens, fonts, `base.css`.
3. **Core controls** — `ct-button`, `ct-icon-button`, `ct-card`,
   `ct-banner`; migrate all usages.
4. **Shell & tabs** — `ct-app-shell`, `ct-tab-bar`; `App.tsx` migrates.
5. **Forms** — `ct-field` + input styling; login + model-key screens.
6. **Data surfaces** — `ct-table`, `ct-toolbar`; both admin panels.
7. **Review flow** — `ct-file-drop`, `ct-progress`, status/confidence/
   download redesign.
8. **Toaster hero v2** — §8.
9. **Pico removal** — drop the import, purge `--pico-*`, shrink app.css.
10. **Gallery + docs** — §9 gallery entry, `ui/README.md`.
11. **Theme & a11y hardening** — contrast audit, focus audit,
    reduced-motion audit, night-mode QA across every screen.
12. **(Infra, parallel)** DTS nginx CSP parity with the Amplify CSP.
