# Wiring contract

> Updated by kit **04p1**. The [designer answers](DESIGNER-ANSWERS-04p1.md) and [patch changelog](CHANGELOG-04p1.md) supersede earlier placement, completion-cost, plain-mode and overlay guidance below. Original renders in `preview/renders` document kit 04; current verification renders are in `preview/patch-renders`.

## Keep one state owner

Retain ReviewSubmission or the extracted preferences/polling hooks. The kit does not fetch review data, persist preferences, log content, schedule polling or submit a document. `onAction` should call the existing guarded handlers; `onPreferences` should call existing setters/change handlers. The preview is an example owner using fixtures, not production business logic.

```tsx
import {OrbitDiner, connectActions, connectPreferences} from './orbit-diner/src';

<OrbitDiner
  model={viewModel}
  assetBase="/assets/orbit-diner"
  onFile={handleFileSelectedOrCleared}
  onPreferences={connectPreferences({
    playbook: setPlaybookId,
    intensity: handleBrowningChange,
    notesMode: handleNotesModeChange,
    instructions: setToasterGuidance,
    dispositionNote: setDispositionNote,
  })}
  onAction={connectActions(actionHandlers)}
  onSound={event => soundBus.event(event, viewModel.stage)}
/>
```

Every member of `ActionHandlers` is required by TypeScript. Connect submit/cancel/download/butter/disposition to the handlers named in the inventory. Connect local copy/receipt/History actions to their existing helpers. `runAgain` remains a handler but the UI never invokes it while `runAgainAvailable` is false. Wire `onFile(null)` to the real existing clear/reset behavior; do not retain a stale File object behind an empty toast.

## ReviewModel projection

| Kit property | Source / conversion |
|---|---|
| `status` | No review + no file → EMPTY; chosen local file → LOADED; outstanding upload → SUBMITTING; otherwise preserve PENDING/RUNNING/DONE/ERROR/CANCELLED and both MANUAL_REVIEW_REQUIRED spellings. Do not map manual statuses through the old generic error phase. |
| `fileSelected` | `selectedFile instanceof File` (or the app's equivalent actual retained-file test). Required independently of display filename. A resumed record with an original filename does not arm submit. |
| `filename`, `fileBytes` | Selected local File metadata; for a restored/result-only view the record's original filename may supply display text, while `fileSelected` stays false. Never place a duplicate filename plate on the shell. |
| `reviewId`, `stage`, `hasInput`, `hasOutput` | `review_id`, `progress_stage`, `has_input`, `has_output`. Unknown stage strings deliberately pass through. |
| `cancelRequested`, `downloading`, `downloadStarted`, `resumed` | Existing request flags and one-time download-start state. Clear stale cancel request state after terminal responses; terminal outcomes also take precedence defensively in the kit. |
| `preferences` | Existing playbook, intensity, notes, guidance and disposition-note state. Do not derive editable instructions from an old record on every poll. |
| `browningReadback`, `notesDisclosure` | Exact current strings from browning/notes modules. Keep current identity tests against composeGuidance until the enum lands. |
| `playbooks`, `internalNotesAvailable` | Current registry response and deployment setting. Retain returned display names without a hard count limit. |
| `preflight` | Rename word_count/page_estimate/paragraph_count/one_line_summary to camelCase; classification/match unchanged; normalized recommended ID/name only when the matched catalog entry is active; injection count and rule IDs only. Do not turn confidence into a new verdict. |
| `result` | Existing describeOutcome, safe failure table and critic-delta projections. Only supply counts with an actual field. Metadata is a label/value list of approved version/model identifiers. `appliedGuidance` is the actual returned `toaster_guidance`. |
| `cover` | Loading/error/ready from existing cover-note state; exact draft string, cached flag and classified retryability. Keep cost separately at `cost.coverCents`. |
| `disposition` | Current saving flag and recorded outcome. One record per review; failure resets saving and supplies a message. |
| `receiptLines` | Existing pure `receiptLines(detail)` result, only for DONE. Do not rebuild these facts from visible labels. |
| `muted`, `notification` | Existing hooks. Notification enum is unsupported/off/granted/denied; denied is explanatory UI, not a new permission request loop. |
| `history`, `runAgainAvailable` | Authorized retained-history summaries and deployed feature flag. No new browser storage. |
| `odometer` | Optional real deployment lifetime completed-review count, never session impressions. Omit until #501 data exists. |
| `messages` | Classified, safe title/detail pairs with a stable ID, scope and optional retry action. Receipt confirmations use scope receipt so they remain visible inside the modal. |

## Submission, cancellation and download

Keep the current multipart composer. Omit whitespace-only guidance and default notes mode exactly as today. Before #694, intensity remains in composeGuidance; after it, send `markup_intensity` and use the new prompt block. The art layer must not send both representations.

The native lever is a button with the existing submit/cancel test IDs. Its drag uses 46 logical units, scaled to 45% of the new lever's physical height; 3 logical units distinguish a drag, and two-thirds travel commits. A normal click or Enter/Space commits without a drag. Unarmed input cannot move the lever. Cancel is never a second submit, and SUBMITTING cannot cancel until the app exposes the planned upload abort separately.

Keep the backend idempotency key. `resumed: true` suppresses the fresh lever sound/motion and should add “Picked up your earlier review” through messages. Do not play lever audio both in the old submit handler and this event adapter.

Keep the existing once-only automatic download keyed by review ID and output availability. The kit only repeats on a manual button action. A browser-triggered download can be described as started, not definitely saved to disk. Render the existing fixed storage copy on output errors and retain DONE status.

## Money and receipt truth

Use `cost: {kind:'estimated', cents:estimated_usd_cents, basis:'typical'}` for today's endpoint. `basis:'document-model'` is reserved for a backend estimate actually computed for this document and active model policy. Model/length changes must invalidate that future estimate; do not calculate a proportional estimate from preflight words in the component.

A confirmed reservation without an owner-readable amount is `{kind:'held'}` and displays Held. Do not display the estimate as the held amount or multiply it by four. Only use `holdCents` from an authorized server projection.

A final total requires all invocation attempts to be priced and reconciliation/settlement complete. Use `{kind:'settled', cents:actual, settlementComplete:true}` then. Terminal pending/missing settlement displays an em dash; a known pending settlement may add the status Settlement pending. `$0.00` is only valid when an explicit complete ledger total is zero. Cancellation and failure can incur model cost; neither automatically gets a no-charge tag. Cover-note regeneration remains an additional billed action; do not silently fold its cost into a previously exported review receipt.

`adminDaily` is an optional authorized aggregate already in memory. Do not call `/api/admin/spend` on behalf of an ordinary reviewer. A “your spend today” figure needs a new owner-safe projection. Missing cap/actual/count means no corresponding ornament or number.

The receipt dialog displays the exact canonical text array; copying uses that array and PNG export draws it at 2×. Original filename may be added from `original_filename`; arbitrary filenames wrap and remain escaped. Actual cost and odometer lines require new canonical fields. Preflight word count can become Words in only after the pipeline records the exact normalized document-count basis; an estimate or preflight summary does not belong on provenance. Words changed needs a defined redline-transcript token metric and a recorded field. Do not infer it from issue or clause counts. Keep the existing normalized-tracked-changes and re-quote disclosures when supplied by receiptLines.

`created_at`/`updated_at` support the existing duration line only if updated_at still denotes terminal completion. If later cover/disposition operations mutate it, the backend must preserve `completed_at` before the receipt can promise a stable duration.

## Motion and sound wiring

Mount the component once. Pass changing data, not changing React keys; repeated remounting would lose cosmetic references and dialog focus. Initial render and a resumed review do not replay transition theater. Explicit receipt success can be signalled with a new `motionEvent: {id, type:'receipt-copy'|'receipt-save'}`. IDs are unique per actual event, not regenerated on each render.

The optional sound bus can load the app's original lever/tick/pop URLs through `existing`. Point `failure` at the existing low-clunk route or reuse its original lever recording with the existing pitch/gain treatment. Keep the original SOURCES entry. Prime audio in a pointer/keyboard gesture capture handler and call `syncMute` when mute changes. The kit has no access to the original three sound files, so they are intentionally not replaced in this package. The independent preview uses the included recorded register-completion alternative and has no running timer tick.

Prefer integrating the new IDs into the existing central sound module. Route all voices through one limiter. If using `createSoundBus` intact, route the existing tick trigger to `bus.play('tick')`; there is no new interval in this kit. Avoid running two sound buses for the same event.

## Host CSS and accessibility

Allow the new console to fill the existing warm Review shell up to 1320 px. Remove the old `max-width:360px` hero limit and the old grid-area placement for the replaced hero/form. The component owns its internal grid and changes arrangement according to container width. In a container under 1220 px, appliances stack; at phone widths, native register keys reflow and the glass receives a dedicated bezel. The textarea always stays in normal layout.

Keep the app's existing global shortcut guards. Events handled inside this console stop propagation for the implemented modifier chords, preventing duplicate submissions. Existing selectors `review-file-input`, `review-playbook-dial` and `review-guidance-field textarea` remain. The playbook target is now a native select; update tests that require a radio role specifically. Within its native dialog, Escape closes the dialog without ejecting the file. Test the host's global shortcuts with the new dialog mounted before release.

The decorative artwork is `aria-hidden`; real labels and controls carry semantics. The lever is the conventional button on its surface, not an SVG group pretending to be one. A native select plus the searchable dialog replaces fixed playbook radios. `plain` switches the same DOM to conventional presentation; forced colours hide imagery and preserve controls. Preserve the positioned root for screen-reader helpers and the inline reduced-motion style block.

No diagram or artwork changes are needed for these connections.

Use the exported `stages` / `stageInfo` as the shared labels/captions for the scene and tab signals, or have the existing stage module re-export those values. Keep the existing favicon-frame token mapping. Do not maintain independently edited caption tables after integration.
