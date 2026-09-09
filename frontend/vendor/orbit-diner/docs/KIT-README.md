# Orbit Diner — finished integration kit 04

> Updated by kit **04p1**. The [designer answers](docs/DESIGNER-ANSWERS-04p1.md) and [patch changelog](docs/CHANGELOG-04p1.md) supersede earlier placement, completion-cost, plain-mode and overlay guidance below. Original renders in `preview/renders` document kit 04; current verification renders are in `preview/patch-renders`.

6 September 2026 · Contract Toaster review workflow

This is a working presentation layer: finished material artwork, native controls, responsive layouts, live typography, state-driven animation, recorded sound edits and callback adapters. The browser captures in `preview/renders` show the supplied implementation. The preview uses explicitly labelled synthetic fixtures; it does not upload a document or call the review backend.

## Open the working kit

`START-HERE.html` links the visual gallery, motion reel, recorded-sound audition and handoff.

Unzip the entire folder, then run from that folder:

```sh
python3 -m http.server 4173
```

Open `http://localhost:4173/preview/`. No build or package install is needed to view the supplied preview. The View menu selects real component states; Accept upload and Next stage explicitly change synthetic state. Nothing advances on a review timer. `audio/audition.html` plays the recorded palette and its source recordings on request.

For source development:

```sh
npm ci
npm run typecheck
npm test -- --maxWorkers=1 --minWorkers=1
npm run build
```

React is a peer dependency of the production component. The prebuilt preview includes its own React copy only so the kit can be viewed independently. Do not import `preview/app.js` into the app.

## Connect it

Import `OrbitDiner` from `src/index.ts`. Pass one `ReviewModel`, `onFile`, `onPreferences`, `onAction` and the optional `onSound`. `src/connect.ts` provides the complete action and preference dispatch adapters. The existing app remains the owner of review state, polling, request guards, preferences, notifications, auto-download and receipt facts.

Copy the six runtime WebP plates and the used fonts to same-origin assets. Preserve the relative font URLs when importing the CSS, or let Vite process them. Set `assetBase` to the same-origin artwork folder. Lazy-load the component with the Review route. No drawing, positioning, font selection or animation design is left to the connecting engineer.

Read these files in order:

1. `docs/CONTROL-COVERAGE.md` — every item 1–22, including its guards and backend connection.
2. `docs/INTEGRATION.md` — fields, callback wiring and existing application behavior to retain.
3. `docs/ART-DIRECTION.md` — exact delivered art decision, responsive behavior and source resolutions.
4. `docs/MOTION-AND-SOUND.md` — event bindings, durations, rails and mixing.
5. `docs/ACCEPTANCE.md` — executed checks and remaining integration gates.
6. `audio/SOURCES.md` — all recording licences, cuts, gains and derivation commands.

## The deliberate art decision

This kit changes the earlier SVG-only hero rule. It uses six same-origin photographic WebP material plates, clipped in inline SVG, with native HTML text/inputs and source-controlled SVG/CSS moving parts. It is not a vector-only deliverable. This is the explicit tradeoff chosen to carry the reference image's material quality into a functioning interface. There is no animation runtime, remote asset dependency or binary state machine.

The runtime image plates total 1,491,984 bytes before HTTP compression. PNG masters and the reference image are handoff materials, not production assets. See `artwork/assets.json` for native source dimensions. A 4K browser capture is a high-resolution composite, not a claim that every photographic master is natively 4K.

## What is complete, and what still needs the app

All 22 requested workflow surfaces are implemented. Butter it, cover-note copy/regenerate/retry, disposition and its note, input download, receipt actions, notification states, preference retry, preflight switching and History are included. Run again, actual settled spend, owner-safe holds, daily budget and odometer are fully laid out but only appear or become available when their real fields/feature flags are supplied.

The coder still connects existing guarded handlers and authorized data. They must not invent a document-sensitive price from the current typical-review estimate, mark a download as definitely saved, treat a failed poll as a failed review, or print estimates on the receipt.

The previous interactive studies were mockups. This package contains the actual rendered controls and styling. The assembled scene intentionally adapts the approved image for native text entry, arbitrary content and mobile operation; it is not a pixel-identical reproduction of that single image.
