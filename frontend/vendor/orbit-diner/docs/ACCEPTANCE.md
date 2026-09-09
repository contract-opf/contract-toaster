# Verification and remaining app integration

> Updated by kit **04p1**. The [designer answers](DESIGNER-ANSWERS-04p1.md) and [patch changelog](CHANGELOG-04p1.md) supersede earlier placement, completion-cost, plain-mode and overlay guidance below. Original renders in `preview/renders` document kit 04; current verification renders are in `preview/patch-renders`.

## Executed on the delivered kit

- TypeScript strict typecheck passed. Production source and the independent preview build successfully.
- Ten Vitest/jsdom tests pass: missing/settling money, unknown stage, cancel/completion race, no replay on resume, selected-file submission guard, calm manual status, completed/cover/disposition/input-download controls, disabled catalog entries, escaped text, two-instance SVG ID uniqueness, permitted animation properties and reduced-motion cancellation. Several related assertions share a test case.
- Actual Chromium 133 browser renders were inspected at 1600 px desktop, 1050 px tablet, 390 px phone and 320 px narrow phone. The recorded browser check reports no horizontal page overflow and a 14 px minimum among visible console text elements. Containers below 1220 px stack the appliances so register keys retain their size.
- Light, dark, plain-controls and Windows-style forced-colours browser emulation were rendered. The long-instruction field grows without an internal vertical scroll region. The 24-entry catalog fixture filters correctly; 1/4/8 entries are also selectable in the fixture toolbar.
- A real native dialog opened and closed with focus returning to Browse playbooks. Submit emitted one submit action and entered SUBMITTING. Cancel emitted its request and stayed RUNNING with Stopping until confirmation. Typing `m` into instructions did not toggle sound.
- Receipt Save image produced an actual PNG download and a confirmation in the open receipt dialog. The exported PNG is included in `preview/renders/receipt-export.png`.
- The recorded palette passed 19 format, size and hash checks. Every effect is mono, 44.1 kHz MP3 and below 10,000 bytes. `audio/format-checks.json` records the results. Source licence checks and edit recipes are recorded separately.
- The silent motion reel records actual browser frames while explicit synthetic fixture actions change state. It is not a generated animation of the screenshot, a real backend run or a frame-rate benchmark.

Evidence: `preview/browser-checks.json`, `preview/interaction-checks.json`, `preview/capture-checks.json`, `audio/format-checks.json`, `docs/build-sizes.json` and the rendered screens.

## Scope of those checks

The browser used here is headless Chromium with software rendering. Real Safari/iOS, Firefox, Windows assistive technology, mobile audio unlock and a hardware GPU performance trace have not been tested in this environment. The selected recordings were technically inspected and cut from verified source files; the audition page includes both edited clips and the source recordings for human listening. Do not describe that as a completed listening panel or as acoustic measurement against the app's unavailable original clips.

This is a finished presentation/integration kit, not a deployed or backend-integrated app. The fixture toolbar is intentionally labelled and must not ship as production product UI.

## Required connection gates

1. Connect all 22 rows through the existing state owner and guarded handlers. Retain backend file validation, idempotency, cancellation races, once-only auto-download, preference error handling and the failure classification table.
2. Keep all review content as escaped text. Retain the storage allowlist: no new preference or document storage was introduced here. Never put clause text in receiptLines or raw server exceptions in the status window.
3. Update only the intentional old presentation assertions: this scene uses local photographic plates; the playbook control is an unbounded native select plus catalog; the visible lever is the conventional native Start/Stop button. Keep input/action test IDs or adapt the tests to their new element roles. Retain the CSP script policy and the inline reduced-motion rail assertion.
4. Exercise the host's existing global shortcuts with the new native dialogs and all modifier chords. Verify local handling does not duplicate global submit/download calls. Confirm completion never steals typing focus.
5. Connect original lever/tick/pop to one central audio owner, retain mute/notification silence, and ensure the existing timer does not keep another independent full-volume voice running underneath the new palette.
6. Do not expose `adminDaily` to an unauthorized reviewer. Only set document-model basis, numeric hold, settled total, run-again availability or odometer when the corresponding server contract is implemented. A failed/cancelled review has no automatic zero-cost inference.
7. Test one real end-to-end review, a classified submit rejection, a poll failure, cancellation/completion race, manual-review handoff, download failure, cover-note retry and disposition retry in the target application.
8. Run the finite-event and five-minute idle/review performance measurements on a real desktop and phone using the targets in MOTION-AND-SOUND. The static register adds no continuous shimmer.

No gate above asks the connecting engineer to draw an appliance, design a paper surface, select typography or invent animation timing.
