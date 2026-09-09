# Review workflow control inventory — 2026-09-05, revised at #728

> *Published from the project's planning history on 2026-09-09. Issue numbers
> below predate the tracker migration of 2026-09-08 and refer to the old
> private numbering; read them as history, not as links.*

Originally the input to the counter/register/waitress-pad design, read from the
code at `e510718`. **Revised at issue #728** (epic #729) to describe the tab
that actually shipped: the Orbit Diner console is the Review tab, the old
`ToasterHero`/`ToastReceipt` tree and the `.ct-review-console` grid went with
#727, and the build-time switch that used to choose between the two went with
#728. Nothing below is planned or assumed unless marked **PLANNED**.

The allocation is no longer "under test" — it is what renders. Toaster =
document and review operation; register = configuration, money, receipt;
waitress pad = special instructions; one overlay dialog = everything a finished
review has to say.

Symbols: `RS` = `frontend/src/ReviewSubmission.tsx` (state owner, adapter),
`OD` = `frontend/src/orbit-diner/OrbitDiner.tsx` (the console),
`PR` = `frontend/src/orbit-diner/projection.ts` (`toReviewModel`, the one
read-only projection between them), `RR` = `backend/src/review_routes.py`.

The seam is one-way and total: the console fetches nothing, polls nothing,
persists nothing and submits nothing. Every action is a hand-off to a guarded
`RS` handler through `connectReviewSubmission`.

## 1. Complete inventory of inputs and actions

Primary inputs (all captured at submit time, none locked during a run):

| # | Input | Surface as shipped | Test id |
|---|---|---|---|
| I1 | File (choose / drop / replace / clear) | The toaster's own slot: `review-intake-slot` owns dragover/drop, and the empty slot's "Choose or drop a .docx" label opens the picker. Escape clears in idle; `U` or Cmd+U/O opens it | `review-intake-slot`, `review-file-input` |
| I2 | Contract type (playbook) | A native `<select>` behind the playbook readout, the knob key beside it (advances to the next ACTIVE playbook), and "Browse playbooks" — a searchable catalog in the overlay dialog. `P` focuses the select; `R` jumps to the preflight-recommended one | `review-playbook-dial` |
| I3 | Markup intensity (browning: light / medium / dark) | Three radio keys on the register; `[`/`-` and `]`/`=`/`+` step them | `review-browning-control` |
| I4 | Footnote audience (notes mode: none / external / internal / both) | Radio fieldset on the register; `1`..`4` select; saved server-side as a user preference | `review-notes-mode-control` |
| I5 | Instructions (free text) | Textarea on the waitress pad; `G` focuses it | `review-guidance-field` / `review-guidance-input` |

Actions:

| # | Action | Surface as shipped | Guard |
|---|---|---|---|
| A1 | Submit | The lever — ONE control that reads Start / Uploading / Stop / Stopping. Drag past two-thirds of 46 units or click; Cmd/Ctrl+Enter. In the plain (non-illustrated) layout it is two keys instead | file chosen, an ACTIVE playbook selected, not submitting, not working |
| A2 | Cancel | The same lever, in its Stop state; `review-cancel-button` in the plain layout | working; inert while the submit request is still in flight or a cancel is already requested |
| A3 | Retry after failure ("Toast another slice") | Key on the operations rail | status ERROR |
| A4 | Reset (clear file) | Escape, or choosing a new file | idle only |
| A5 | Download redline | "Save redline" on the operations rail, Cmd/Ctrl+D/S; also automatic once on completion (see section 3) | `has_output`, not downloading |
| A6 | Download input document | "Save original" in the review-id row — now ON the Review tab, not History-only | `has_input` |
| A7 | Butter it (generate cover note) | Key in the register's till drawer, beside the receipt printer | decision REQUEST_CHANGE and `has_output` |
| A8 | Cover note: copy / regenerate / retry | Keys on the cover-note card, in the overlay | draft present / not loading |
| A9 | Record disposition (Accepted / Accepted with edits / Rejected) plus optional note | Three keys and a textarea in the overlay's record view | terminal dispositionable status |
| A10 | Receipt: copy as text / save PNG / **print** | Keys in the overlay's receipt view. Print (#740) is offered only when there are lines to print, and prints the receipt, not the counter scene | status DONE |
| A11 | Copy review id | Key in the review-id row | any review |
| A12 | Sound mute toggle | Key in the register's till drawer; `M` | always |
| A13 | Browser notification opt-in | Key in the till drawer | only when `Notification` is supported |
| A14 | Keyboard-shortcuts cheat sheet | "Shortcuts" key in the till drawer; `?` / Cmd+/ press that same key, so the sheet's open state has exactly one owner | always |
| A15 | Switch to preflight-recommended playbook | Key on the preflight card; `R`. Since #730 the recommendation is normally applied automatically and silently, so this is the answer for the cases the auto-choice declines | preflight classification ok and a different active playbook matches |
| A16 | Retry saving the notes-mode preference | "Retry" on the preference message | after a failed PUT |
| A17 | Review details | Key that opens the overlay's record view | a review id exists |
| A18 | Retry a failed poll / catalog load | "Check now" and "Reload contract types" on the status window's messages | after the respective failure |
| A19 | Open History / choose next document / switch plain ↔ illustrated | Three link keys on the counter footer. The switch is disabled under forced colours or when the plates did not decode — there is then nothing to switch to (#738) | always |

Not present anywhere on the Review tab: rerun/"run again" (issue #710, PLANNED — the console can render the affordance, and `RS` hands it no history strip, so its callback opens the History tab that owns the real control), estimated time remaining (#711, PLANNED), cost actuals (see section 6). The console has an odometer element; `RS` supplies no `odometer` value, so it never renders (#501 part 2, PLANNED). Resume after reload shipped (#698): `RS` reattaches to a non-terminal review on mount.

## 2. Options and constraints per control

| Control | Field / type | Options and default | Validation and limits | Helper text |
|---|---|---|---|---|
| File | multipart `file` (required) | `.docx` only (`accept=".docx"`) | 25 MiB hard cap in `upload_validation.MAX_UPLOAD_SIZE_BYTES`; WAF body cap 50 MiB; OOXML gauntlet (zip bomb 200 MiB decompressed, macros, field codes, AV) with format-specific 4xx copy | Slot button "Choose or drop a .docx" |
| Playbook | form `playbook_id` (string, default = registry default) | Dynamic catalog from `GET /api/playbooks`: `{playbook_id, display_name, status}`; only `status === 'active'` selectable; others are `disabled` options suffixed "· coming soon" | No length limit on `display_name` anywhere (admin rename accepts any string); no cap on catalog size — and the shipped controls do not need one. The old 120° dial arc is gone: the value is a native `<select>` (so a long catalog scrolls and a long label wraps the way the platform does it), the knob key is a next-active-playbook stepper, and "Browse playbooks" opens a **searchable** list in the overlay for catalogs too long to step through | Readout above the select; the browse list carries the search field |
| Browning | today folded into `toaster_guidance` as a sentence; PLANNED enum form field `markup_intensity` (#694) | `light` / `medium` (default) / `dark`; persisted in localStorage `contract-toaster:last-browning` (`lastBrowning.ts`) | Closed vocabulary; medium contributes nothing. Three radio keys, not a slider — no detent geometry left to honour | `review-browning-note` quotes the exact sentence sent ("This adds to your instructions:") or "The playbook drives."; a screen-reader readback carries the same text |
| Notes mode | form `notes_mode` (omitted when equal to default) | `none`, `external` (default), `internal`, `both`; `internal`/`both` only when the deployment enables internal notes (`notesModeInternalAvailable`) | Backend `reviews.resolve_notes_mode` 400s on anything else; PUT `/api/me/preferences` stores `notes_mode` | Per-mode note, plus the internal-notes disclosure sentence when `internal`/`both` |
| Instructions | form `toaster_guidance` (string, default "") | Free text; omitted from the request when whitespace-only | No `maxLength` on the textarea and no length check in the backend or the prompt assembler; the only bound is the WAF body cap. `rows={6}`, no autosize | Label `guideLabel` — "Special instructions", or "Special instructions · next review" while working or terminal — with a separate "Optional" span; hint = precedence copy: instructions "govern over the playbook's positions wherever the two conflict — but never over rules the playbook marks as hard requirements" |
| Disposition | JSON `{outcome, note?}` to `POST /api/reviews/{id}/disposition` | ACCEPTED / EDITED / REJECTED; note optional | note ≤ 4000 chars server-side, and the textarea sets `maxLength={4000}`; reason/topic lists ≤ 20 items of ≤ 64 chars (API only, no UI) | Label "Optional disposition note" (`disposition.ts`'s `DISPOSITION_PROMPT_COPY` / `DISPOSITION_RECORD_COPY` are exported but no longer rendered) |
| Cover note | JSON `{regenerate?: bool}` to `POST /api/reviews/{id}/cover-note` | Cached draft returned unless `regenerate` | Response carries `cost_usd_cents` and `cached`; 502 is surfaced as a real error | Cost line under the card |

## 3. Controls by state

There are now two derivations, and they answer different questions.

`RS` still derives a four-value `phase` (`idle` / `working` / `done` / `error`) from `reviewId` and the polled `detail.status`, but it is read only by the tab title and the favicon (`useTabTheater`). CANCELLED maps to `idle`, deliberately: a review the reviewer stopped rests, it does not burn.

The console reads `model.status` — a fuller vocabulary projected by `PR`: `EMPTY`, `LOADED`, `SUBMITTING`, `PENDING`, `RUNNING`, `DONE`, `CANCELLED`, `ERROR`, `MANUAL_REVIEW_REQUIRED`, `ERROR_MANUAL_REVIEW_REQUIRED`. `state.ts` holds the predicates: `isWorking`, `isManual`, `canSubmit`, `canDispose`, `statusText`.

**Issue #458 is answered on this surface.** A manual-review status reads "Needs a human" and gets its own treatment; only `ERROR` reads "That one burnt." The two are no longer one burnt state.

| State | Visible | Editable | Notes |
|---|---|---|---|
| Empty (no file) | Toaster with an empty slot ("Choose or drop a .docx"), playbook select + knob + browse, browning, notes mode, instructions, cost estimate, counter footer | All inputs | Lever inert; status reads "Choose a document" |
| File selected (LOADED) | As above, with the filename on the bread — measured and shortened to at most two lines (#739); preflight card appears when the preflight returns | All inputs; Escape clears the file | Lever armed once an ACTIVE playbook is selected; otherwise "Choose an active playbook" |
| Preflight running | Same as file selected; the card is absent until the response lands (about 8 s, advisory) | All | Never blocks submit. On return, #730 may apply the recommended playbook silently |
| Submitting | Lever reads "Uploading"; both operations keys disabled | Inputs remain editable but the request has already been built | No abort during upload (#693 PLANNED) |
| Queued / running (PENDING, RUNNING) | Progress (indeterminate until the first stage token, then the staged toast + caption in `review-stage-caption`), lever in its Stop state, review-id row, poll-error message in the status window with a "Check now" retry | Playbook, browning, notes mode, instructions stay enabled but changes do not affect the running review | Ticking sound, tab title and favicon browning |
| Cancellation requested | Lever reads "Stopping"; it and the plain Stop key are inert | As above | 409 → "This review finished before it could be stopped." |
| Cancelled | Back to an operable idle console; the outcome is stated rather than burnt | All inputs, file retained | No burnt art, no clunk |
| Completed (DONE) | Popped toast, "Save redline" on the operations rail, the receipt printer lit, the till's Butter it / Record outcome / sound / alerts / shortcuts keys, and the result — outcome, decision copy, applied guidance, confidence band, critic delta, meta line — on the page and again inside the overlay's record view | Download, butter, disposition, receipt actions; form inputs still editable for the next run | Pop sound, notification, automatic download once (below) |
| Download preparing | "Save redline" reads "Preparing…" and is disabled | | Download-error message on failure |
| Cover note generating | Butter/regenerate disabled | | Failure offers retry; a 502 shows the real error, and the two are different message ids |
| Error | Burnt slice + steam, failure card with headline, cause, fix, failing stage and reason code, "Toast another slice" | Retry clears detail, review id, file, errors | Clunk sound |
| Manual review required | "Needs a human", its own result card with the same diagnosis fields and a "Review details" key beside it | Disposition is available (`canDispose`) | Distinct from ERROR — see #458 above |

Settings changeable during a review: all four are technically editable; only notes mode has a side effect (it saves the preference immediately). None of them affects the review in flight.

### The once-only automatic download

Confirmed at #728 and unchanged by the console. It lives in `RS`, in the completion-handoff effect, and it fires **once per completed review**:

- The key is `handedOffReviewRef.current === detail.review_id`. The effect returns early when the ref already names this review, and stamps it before doing anything else, so a re-render or another poll of the same DONE review cannot fire a second save.
- It requires `detail.has_output`. No output, no save — the announcement says so instead.
- The output-contract download gate still holds: a review whose critic delta carries content dings, announces and focuses "Save redline", and saves nothing until a human presses it.
- Failure is not silent: the same download-error message the manual key uses appears, and only a resolved fetch is allowed to set `autoSaved` / upgrade the announcement to "saved".
- **The console adds no second one.** Every download path in `OrbitDiner.tsx` is a press: the "Save redline" key, "Save original", and the kit's own Cmd/Ctrl+D/S branch — which is dead here, because `RS` passes `keyboardShortcuts={false}` (owner decision H6) and its own dispatcher owns the vocabulary. There is no effect, no mount hook and no status watcher that dispatches `download`. `model.downloadStarted` only *reports* that the automatic save happened ("Download started automatically. Save redline repeats it."); it triggers nothing.

## 4. Reusable components, handlers, and state ownership

State is still owned entirely by `RS` and passed down. `OrbitDiner` is a controlled component whose own state is cosmetic and local: which overlay is open, the catalog search string, the plain/illustrated override, the live lever drag offset, and the measured console width. Nothing it owns survives a reload, and it writes no storage key.

The seam is three pieces, and there is exactly one of each:

- `PR`'s `toReviewModel(state)` — a pure projection from `RS`'s render-time values to one `ReviewModel`. Read-only, no fetch, no memo of its own.
- `PR`'s `connectReviewSubmission(callbacks)` — turns `RS`'s guarded handlers into the console's `onAction` / `onPreferences` / message-retry props.
- `toaster/sounds.ts`'s `playMotionEvent` — the ONE audio owner. `onSound` routes every console `MotionEvent` there rather than to the kit's `createSoundBus`, which would be a second `AudioContext` with its own budget and its own idea of the mute flag.

Because the console is one view over one owner, "two appliances reflecting the same value" needs no duplicate state: the toaster, the register and the pad all read the same `model` and call the same setters — `choosePlaybookManually`, `applyBrowningChange`, `handleNotesModeChange`, `setToasterGuidance`, `submitReview`, `handleCancel`, `handleDownload`, `handleButterIt`, `handleRecordDisposition`.

`RS`'s own helpers: `useSoundMuted` (`toaster/sounds.ts`), `useNotifyPreference` (`toaster/notify.ts`), `useTabTheater` (`toaster/tabChrome.ts`). Pure modules on the app side: `browning.ts` (`composeGuidance`), `notesMode.ts`, `stageTheater.ts`, `toaster/receipt.ts` (`receiptLines`, `receiptText`), `preflight.ts`, `coverNote.ts` (`butterIt`), `disposition.ts` (`recordDisposition`), `outcome.ts` (`describeOutcome`). Pure modules inside `src/orbit-diner/`: `state.ts` (status predicates and copy), `autoPlaybook.ts` (#730's decision), `consoleWidth.ts`, `filename.ts` (#739's measurement), `artworkGate.ts` (#732/#738), `plates.ts`, `motion.ts`, `receipt.ts`.

Behaviour tied to geometry or DOM:

- Lever: pointer capture on the lever button; drag measured in the same 46-unit travel the kit draws, committing past two-thirds with a 3-unit slop; a click commits too. In the plain layout it is two ordinary keys instead, which is what forced-colours and no-artwork users get.
- Playbook: a native `<select>` plus a knob key that steps to the next ACTIVE entry. No angles, no arc, no hit-testing.
- Browning and notes mode: ordinary radio inputs. No detent positions.
- Console width: `useConsoleWidth` observes the console's own **content box**, never `window.innerWidth` — the console sits inside `ct-app-shell` and is always narrower than the viewport, so a viewport reading would switch at the wrong moment. `orbit.css` asks the same question through `@container od-console`, and `consoleWidth.ts` holds the same numbers; they are a pair, not two opinions. An unmeasured width (first paint, hidden tabpanel, no `ResizeObserver`) resolves to `wide`, never `phone`.
- Motion targets `data-part` attributes and per-instance ids from `useId`.
- Keyboard shortcuts stay `RS`'s, and it reaches the console's controls the way a person would — `document.querySelector` on `data-testid="review-file-input"`, `review-playbook-dial`, `review-guidance-field textarea`, `review-shortcuts-key` (pressed, so the cheat sheet has one owner and one open state) and `review-download-button` (focused on the completion handoff). Moving a control must keep its test id.
- The kit's own shortcut handler is inert: `keyboardShortcuts={false}` (owner decision H6). Its modal Escape is local to its dialog and unaffected.

## 5. The instructions field, precisely

Plain text `<textarea>` (`review-guidance-input`) on the waitress pad, `rows={6}`, no maxlength, no autosize, controlled by `toasterGuidance` in `RS`. The applied guidance comes back as a `<details>` ("Sent with this review", `review-applied-guidance`) on the pad rather than as a banner elsewhere. Value is read at submit time only: `composeGuidance(browning, toasterGuidance)` = browning sentence, blank line, trimmed typed text; whitespace-only becomes no field at all. Sent as multipart `toaster_guidance`; the backend stores it verbatim on the review row, injects it into both model passes as trusted first-party instruction text (deliberately NOT wrapped in untrusted-input delimiters), and projects it back on `GET /api/reviews/{id}` so the result block shows an "applied guidance" banner and History shows it. After completion, cancellation, or retry the textarea keeps its text (only the file, detail, review id, and errors are cleared); nothing ever clears it except the user. It is not persisted across reloads. Shortcuts while typing: only modifier chords fire (Cmd/Ctrl+Enter submit, Cmd+U/O picker, Cmd+D/S download, Cmd+/ cheat sheet, Cmd+Shift+P/G/M/R); bare keys are ignored while an input, textarea, select, or contenteditable has focus; Escape closes the cheat sheet but does not clear the file from inside an input. After #694 the browning sentence leaves this field and becomes `markup_intensity`.

## 6. What the register displays — and what it must never be handed

The register renders exactly what `PR`'s `projectCost` puts on the model, and the label names which figure it is (`costLabel` in `OD`): "Estimated review cost" before and during, and — owner decision H2, #735 — a finished review keeps the estimate that was **captured at submit time** under that same honest label, rather than reading "FINAL REVIEW COST —" for a settlement this deployment does not project. "Final review cost" appears only for a settlement that is genuinely complete, which nothing produces today.


| Figure | Exists | Endpoint / field | Access | Update timing | Missing-data behaviour |
|---|---|---|---|---|---|
| Expected cost of the next review | Yes | `GET /api/review-cost-estimate` → `estimated_usd_cents` | any signed-in user | fetched once on mount of the Review tab | absent on the Bedrock path; UI hides the line |
| Worst-case reservation | Yes, server-side only | `reviews.compute_worst_case_reservation_usd_cents`; reserved on submit against the daily cap | not exposed to non-admins | at submit | 429 with copy when the cap would be exceeded |
| Actual spend per review | Partially | Per-pass rows in the model-invocation ledger (tokens, priced cents); summed only for the admin outlier flag | admin (`/api/admin/releases`) | per pass attempt | no per-review total on `GET /api/reviews/{id}` — PLANNED |
| Daily budget (reserved / settled / cap) | Yes | `GET /api/admin/spend` (`daily_spend` table) | admin only | on request | — |
| Cover-note cost | Yes | `POST .../cover-note` → `cost_usd_cents`, `cached` | owner or admin | per call | shown under the card |
| Completed-review count (odometer) | No | — | — | — | The console has the element; `RS` supplies no `odometer`, so it never renders. PLANNED (#501 part 2) |
| Duration of this review | Yes | `created_at` / `updated_at` on the record ("Toasted in") | owner or admin | at terminal | dropped from the receipt when absent |
| Time-remaining estimate | No | — | — | — | PLANNED (#711) |
| Receipt | Yes | `receiptLines(detail)` → `model.receiptLines`; copy as text, save as PNG, or print (#740) | owner or admin | on DONE | lines dropped when a source field is absent; the print key is absent when there are no lines |

A register visible to every reviewer shows: the estimate (live, or the one captured for this review), this review's duration, cover-note cost, and the receipt. Daily budget and actuals still require admin scope or a new owner-safe projection (a "your spend today" figure is a new endpoint, PLANNED).

### `adminDaily` is not the register's to ask for

Three independent guards, and the criterion holds if any one of them does:

1. **`RS` hands over neither.** `isAdmin` and `adminDaily` are deliberately absent from the state it builds — nothing on the Review panel holds an authorized spend aggregate, and the console must never be the reason one is fetched for a reviewer. Nothing on this tab calls `/api/admin/spend`.
2. **`PR` refuses to project one anyway.** `projectCost` copies `adminDaily` onto the model only when `state.isAdmin === true` **and** an aggregate is present. An aggregate handed in without the flag is dropped, not rendered.
3. **`OD` renders the budget block only under `m.cost.adminDaily`**, which by (2) exists only for an admin.

Covered by `orbit-diner-projection.test.ts` ("omits adminDaily for a non-admin even when an aggregate is handed in").

## 7. Interactions needing special treatment

- Drag and drop: `review-intake-slot` owns dragover/drop; the empty slot's own label opens the picker. Drag-over lights the slot glow by class.
- Lever drag versus click: both commit; drag past two-thirds of the 46-unit travel with a 3-unit slop; the lever is one control for Start and Stop, so its label and its action move together and there is no separate cancel row to keep in step.
- Playbook keyboard: an ordinary `<select>`, so the platform's own listbox behaviour applies and disabled options are unreachable by construction. The knob key steps to the next ACTIVE entry; "Browse playbooks" opens a searchable list for a catalog too long to step.
- Shortcuts: two tiers (modifier chords global; bare keys only outside inputs); the cheat sheet lists them, and `?` / Cmd+/ press the console's own key so there is one sheet with one open state. Any new surface that takes typing must be an input/textarea so bare-key shortcuts stay suppressed. The kit's own handler stays disabled.
- Confirmations: none on submit or cancel today; disposition is one press, with copy warning it becomes part of the record; the design system has a confirm-step pattern (§14) for destructive admin actions only.
- Focus: on completion `review-ready-announcement` (visually hidden, `aria-live`) is written **and** focus moves to `review-download-button`. Focus moves before the automatic save is attempted, because the focused key is the path that always works and must not wait on a network round trip. The register's second glass is decorative and `aria-hidden`; the polite announcer beside it (`review-register-announcement`) stays mounted and empty for the whole session, because a region that only exists once it has content is a new region, and a new region is not announced.
- The plain ↔ illustrated switch (#738, N4) may not pull a control out from under anyone: while focus is on the lever, the plain operations keys, a textarea or a select, the switch defers until focus leaves. The layout it defers in is fully operable, so waiting costs nothing.
- Duplicate submission: guarded client-side by `canSubmit` (a file, a filename, an ACTIVE playbook, not working) and by `RS`'s own `submitting`/phase checks on the shortcut path, and server-side by an idempotency key derived from owner, file sha256 and active bundle hash (a duplicate returns the existing review with `resumed: true`).
- Conventional fallbacks are not a parallel tree to maintain — they are what the console already renders: a real `<input type=file>`, a real `<select>`, real radio inputs, a real textarea, real buttons, a real `<dialog>`, and the plain layout for forced colours or a failed plate decode. Text alternatives accompany every readout.

## 8. Layout constraints

- Breakpoints: the app shell still collapses at 640 px, but the console's own steps are **container** queries on its content width, not viewport media queries — `phone` ≤ 560 px, `narrow` ≤ 1220 px (the two appliances stack, owner A2), `wide` above. `CONSOLE_PHONE_MAX_PX` / `CONSOLE_STACK_MAX_PX` in `consoleWidth.ts` and the `@container od-console` rules in `orbit.css` carry the same two numbers and must be changed together.
- Fonts: Space Grotesk (display), Instrument Sans (text), IBM Plex Mono (mono, used by the receipt), all self-hosted; **minimum text size 14 px** (`--ct-text-sm`), enforced by `npm run audit:layout` (#600).
- Assets: seven same-origin `.webp` plates, imported through `orbit-diner/plates.ts` by `OrbitDiner.tsx`, `MaterialArt.tsx` and `artworkGate.ts` — reached from `App.tsx` → `ReviewSubmission.tsx` — so Vite emits them content-hashed into the production graph. No remote art, no data: URIs, no eighth plate — `orbit-diner-assets.test.ts` (#732) pins the set and their hashes and fails on any absolute `http(s)://` in `src/orbit-diner/`. The receipt PNG is drawn on a canvas at 2×, 420 px wide.
- Gates: `layout-audit.mjs` requires flexible grid tracks to be `minmax(0, …fr)` and never a bare `…fr` (#457), and requires absolutely-positioned visually-hidden helpers to sit under a positioned owner. `focus-audit` and `contrast-audit` are source guards, not selector-bound. Keep every `data-testid` when moving a control; `consoleSurface.ts` is where a test asks WHERE a control lives, so placement changes there, once.
- Motion and CSP: unchanged (inline SVG, WAAPI on transform/opacity/filter, reduced-motion kill switch, no remote assets).
- Flexible regions rather than fixed keys: the instructions textarea (unbounded text), the playbook select and browse list (1..N entries with unbounded labels), the filename on the bread (measured and shortened to at most two lines, #739 — never truncated by a fixed character count), the preflight card, the failure card (cause + fix sentences), the cover-note card (paragraphs), the receipt (variable line count), and the disposition note. Fixed-size keys are fine for: browning (3), notes mode (4), submit, cancel, download, copy id, sound, alerts, shortcuts, the three disposition keys.
- Storage: the console adds no `localStorage` or `sessionStorage` key. The allowlist is what `security-posture.test.tsx` already pins.

## State / action map

```
EMPTY ──file_selected──▶ LOADED ──submit──▶ SUBMITTING ──accepted──▶ PENDING/RUNNING
                                                                     (stage: null|primary_pass|
                                                                      critic_pass|reconciliation|redline)
LOADED ──file_cleared──▶ EMPTY
RUNNING ──done──▶ DONE              (pop, receipt printer lit, auto-download once, butter/disposition)
RUNNING ──error──▶ ERROR            (clunk, burnt slice + steam; "Toast another slice" → EMPTY)
RUNNING ──manual──▶ MANUAL_REVIEW_REQUIRED / ERROR_MANUAL_REVIEW_REQUIRED
                                    ("Needs a human" — NOT the burnt state; disposition available)
RUNNING ──cancel──▶ CANCELLED       (stated, not burnt; file retained)
page load with running review ──resume(stage)──▶ RUNNING   (no lever ritual, no motion replay)
DONE/ERROR/CANCELLED ──reset──▶ EMPTY
```

Actions by state: EMPTY/LOADED → choose file, playbook (select / knob / browse), browning, notes, instructions, submit (LOADED with an active playbook only), preflight switch, sound, alerts, shortcuts, plain ↔ illustrated. SUBMITTING/PENDING/RUNNING → stop, copy id, retry poll, the same preference controls (editable but inert for the running review). DONE → save redline, save original, butter (REQUEST_CHANGE), cover-note copy/regenerate, record outcome, receipt copy/save/print, review details, copy id. ERROR → retry, review details, copy id. Manual → record outcome, review details, copy id.

## Representative fixtures (synthetic)

Catalog:
```json
{"playbooks":[
 {"playbook_id":"nda-mutual","display_name":"Mutual NDA","status":"active"},
 {"playbook_id":"msa-services","display_name":"Master Services Agreement (customer paper)","status":"active"},
 {"playbook_id":"dpa-eu","display_name":"Data Processing Addendum","status":"coming_soon"}]}
```
Cost estimate: `{"estimated_usd_cents": 31}`.

Preflight: `{"word_count":4180,"page_estimate":11,"paragraph_count":96,"title":"MUTUAL NON-DISCLOSURE AGREEMENT","classification":"ok","agreement_type_guess":"nda","paper_side":"counterparty","confidence":0.83,"one_line_summary":"Two-way confidentiality with a three-year term and carve-outs.","match":"likely","injection_scan":null}`.

Running detail: `{"review_id":"5f1c…","status":"RUNNING","decision":null,"message":null,"has_output":false,"progress_stage":"critic_pass","cancel_requested":false,"created_at":"2026-09-05T14:02:11Z","playbook_id":"nda-mutual","toaster_guidance":"Keep the markup light: flag only material issues, accept reasonable counterparty positions, minimal ink.\n\nDo not touch the governing-law clause."}`.

Done detail: `{"review_id":"5f1c…","status":"DONE","decision":"REQUEST_CHANGE","has_output":true,"has_input":true,"confidence_band":"high","created_at":"2026-09-05T14:02:11Z","updated_at":"2026-09-05T14:06:48Z","playbook_id":"nda-mutual","playbook_version":"1.3.0","instructions_version":2,"primary_model_id":"synthetic-primary-model","critic_model_id":"synthetic-critic-model","issues":[{"issue_id":"i1"},{"issue_id":"i2"},{"issue_id":"i3"}],"critic_delta":{"contested_issue_ids":["i2"],"added_issues":[]},"original_filename":"Synthetic-NDA-draft-v3.docx"}` → receipt "Toasted in 4 min 37 s", "Changes requested 3", "Contested by the critic 1".

Burnt detail: `{"review_id":"9a0e…","status":"ERROR","decision":null,"has_output":false,"failing_stage":"primary_pass","reason":"model_timeout","created_at":"2026-09-05T15:10:00Z"}` → headline "That one burnt.", cause/fix from the `model_timeout` entry, retry button.

Cover note: `{"draft":"Dear counsel, …","cost_usd_cents":4,"cached":false}`.
