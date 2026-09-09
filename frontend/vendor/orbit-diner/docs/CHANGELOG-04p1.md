# Kit 04 → 04p1: visual integration patch

The base artwork, six plates, recorded sound pack, guarded action contract and review-state source remain in place. This patch changes the presentation and supplies the coder’s missing design decisions.

| Owner feedback | Implemented change |
|---|---|
| Redline ready repeated | One visible status badge on the toaster. No terminal progress panel or mirrored register headline. |
| Redundant completion/result paragraphs | Completed result facts are inside View receipt; extended review details are a disclosure within that overlay. No completed result cards below the counter. |
| Actual-spend explanation clutter | Removed. Final review cost replaces Settled review cost; it never displays an old estimate, hold or partial actual as final. |
| View receipt looked bolted on | Transparent ink layer follows the existing photographed tape; no rectangle or separate hanging Receipt label. Phone uses a small attached paper strip with a printer slot. |
| Save redline duplicated | One labelled key in the toaster base. The toast remains an accessible download target with the filename only and a tooltip. No register download key. |
| Butter it belongs on an appliance | The register’s physical service row now contains Butter it. Hover/accessibility text describes the cover note. Existing drafts open without a new billed request; Regenerate remains explicit in the overlay. |
| Record outcome too busy | A register key opens the native overlay with three disposition buttons, note, save state and error handling. |
| Preflight not useful after completion | No preflight ticket after DONE, ERROR, either manual status or CANCELLED. It returns for a newly selected document. |
| Review id / original download take space | Inside the receipt overlay after DONE. Before completion or on failure/manual handoff, Review details opens the support record. |
| Dial overlaps labels | Reduced diameter and moved within its face. Browse playbooks relocated to the lower key rail. |
| Contract type is not the playbook | Label changed to Playbook type. Catalog remains arbitrary-size and searchable, with disabled entries excluded. |
| Automatic choice with manual override | A pure preflight adapter helper rejects stale-file and in-flight changes; manual override wins. Dial reflects the parent-selected id. |
| Markup label over edge | Configuration bed inset farther right and narrowed to fit the register panel. |
| Sound off vs Notify off unclear | App sounds / Muted or On, and Browser alerts / Off, On or Blocked. Hover and accessible descriptions explain page sounds versus away-tab completion notifications. |
| Keep the pad | Native growing pad retained; fixed top/bottom skin sections stop the metal clip stretching over text. After terminal status its editable field is labelled for the next review; Sent with this review remains a separate readback. |
| Coder’s forced-colours blocker | Plain geometry, visible system-colour borders, hidden decorative SVG and separate guarded Start/Stop buttons. |
| Shell and desktop sizing unresolved | Existing warm shell, 1320 px ceiling, no full-page dark takeover; appliances stack by container size. |
| Extra history surface | Removed drawer; History goes directly to the existing tab. |

## All 22 surfaces remain accounted for

| Inventory | Patch location or connection |
|---|---|
| 1 Intake | Toast slot/drop, native file input and intake actions; Choose next document in the quiet footer after terminal status. |
| 2 Submit | Native lever in illustration; separate Start toaster button in plain mode. |
| 3 Progress | Toaster status badge plus working-only Step N/caption readout. |
| 4 Stop | Same physical lever while working; separate Stop review in plain mode. |
| 5 Ready state | Toaster status text, with restrained state dot on desktop. |
| 6 Redline download | Single labelled base key plus filename toast target. |
| 7 Burnt/manual | One status headline and visible classified next-step panel before the pad. |
| 8 Retry | Toast another slice in toaster base on ERROR. |
| 9 Review ID | Receipt overlay; Review details for non-DONE records. |
| 10 Playbook | Toaster dial/select, base Browse playbooks key and searchable overlay. |
| 11 Intensity | Register radios; exact supplied instruction readback before terminal, with accessible/title readback thereafter. |
| 12 Footnotes | Register radios, deployment gating, disclosure and preference warning/retry. |
| 13 Spend | One register glass; complete settlement gates final amount. |
| 14 Receipt | Printed View receipt tape; native-text overlay; canonical copy/PNG export. |
| 15 Preflight | Advisory counter ticket while preparing/working, absent after terminal status. |
| 16 Cover note | Butter it on register; draft, Copy, Regenerate, Retry and real last cost in overlay. |
| 17 Disposition | Record outcome on register; three keys/note, pending state and recorded stamp in overlay. |
| 18 Sound/notify/shortcuts | Register service row. |
| 19 Other messages | Scoped main message area or current overlay; operational summary only in register glass. |
| 20 Instructions | Native growing pad, preserved editable text, distinct applied-guidance readback. |
| 21 Result | Receipt overlay for DONE; visible next-step panel for error/manual, record overlay for supporting details. |
| 22 History / Run again | Existing History tab owns these; footer navigates to it. No new unavailable action in this scene. |

## Integration deltas

- Existing callbacks remain; new optional props are `onPlainChange` and `keyboardShortcuts`.
- Optional `Message.tone` distinguishes local success confirmations from errors without guessing from wording.
- Optional `ReviewModel.playbookSelection` describes automatic versus user choice.
- The exported `preflightPlaybookChoice` helper selects an id without owning state, fetching data or writing preferences.
- One native dialog is active at a time. Receipt export still uses only `receiptLines`; the adjacent support actions and extended review detail are not inserted into the provenance slip or its PNG.
- Retain original filenames and all existing provenance disclosures in the app’s receipt function. Change the field label to Playbook type there if desired; the scene does not rewrite its input facts.
- Added radio/test hooks and changed DOM placement require the app’s own assertions to be updated. The supplied fixture tests exercise the new state and overlay boundaries.
- `plain` is session-only unless the app supplies an already approved preference path. No new localStorage writes were added.
- No dependencies, model data, remote assets, font families, audio recordings or new timers were added.

## Apply and verify

Use the patch ZIP’s `README-PATCH.md`. This change is against the original kit 04, not against the application commit. The coder still connects the existing cancellation, idempotency, polling, download and audio owners. Updated review copies must be checked in the target app’s own browser and accessibility gates.
