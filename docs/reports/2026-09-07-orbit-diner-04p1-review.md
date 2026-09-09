# Orbit Diner 04p1 patch — review

> *Published from the project's planning history on 2026-09-09. This is a
> record of what was decided at the time, not current instructions. Issue
> numbers predate the tracker migration of 2026-09-08 and refer to the old
> private numbering, and any repository path or command shown may name the
> private origin this work was done in. Read both as history, not as links
> to follow.*

2026-09-07. Reviewed `frontend/orbit-diner-04p1-patch` against the delivered kit 04
and against the 31 questions in `2026-09-06-orbit-diner-questions-for-designer.md`.

**Verdict: accept the patch.** It answers all 31 questions, fixes the one blocking
accessibility defect in code rather than in prose, and is materially quieter on the
completed screen. Seven new findings below; two are defects, two are owner
decisions the patch makes on our behalf, and three are ours to carry.

## Integrity and build — verified

| Check | Result |
|---|---|
| 24 declared baselines against our kit-04 bytes | all match |
| 21 declared-new files | none already present |
| 45 payload files against `manifest.json` after-hashes | all match |
| `apply_patch.py --target …` (check) | 45 payloads verified, 45 applicable |
| `--apply`, then re-run | applied with backup; re-run idempotent |
| `npx tsc --noEmit` on the patched kit | clean |
| `npx vitest run` on the patched kit | 16 files/tests, 16 pass |
| `npm run audit:focus`, `audit:contrast` with the new `orbit.css` | pass |
| `npm run audit:layout` with the new `orbit.css` | **17 failures** (12 bare `fr` tracks, 5 `clamp()` font sizes — was 15) |

Applied to a disposable copy; `frontend/orbit-diner-kit-04` is untouched.

## Answers spot-checked against the code, not the prose

- **B1** — `aria-valuenow={stage?.step}` with `aria-valuemin/max` 1–4, inside a
  `working`-only block. Unknown tokens leave it undefined. As stated.
- **B2** — the illustrated lever keeps one id at a time; `plainView` renders
  separate `review-submit-button` and `review-cancel-button` keys. The two modes
  never both render, so no duplicate ids.
- **B3/B4** — `review-cover-note-butter`, `notify-toggle`, `sound-toggle`,
  `review-cost-estimate`, `review-outcome`, `review-preflight-card`,
  `review-receipt-paper`, `toaster-state-progress`, `review-receipt-text` and
  `review-browning-option-{light,medium,dark}` are all present.
- **C2** — `keyboardShortcuts` prop exists; the handler returns early on
  `!keyboardShortcuts || e.defaultPrevented`.
- **F1** — `plainView = forcedColours || (plainOverride ?? plain)`, driven by a
  `matchMedia('(forced-colors: active)')` listener. The new forced-colours render
  shows every control with a system-colour border, separate Start/Stop, no
  decorative SVG, and a checked-radio indicator. The defect is genuinely fixed.
- **F2** — footer toggle, `onPlainChange`, session-only React state, no storage
  write. Forced colours disables returning to the illustration.
- **G3** — `ART-PROVENANCE-04p1.json` gives per-plate generator, reference hash,
  master and runtime hashes, export settings and terms, and states plainly that no
  model revision or seed was preserved. It does not claim CC0 or pick our licence.

## Findings

**P1 — On a failed review the review ID is unreachable.**
`files/src/OrbitDiner.tsx:973–986`. The toaster base rail has one slot after
Browse playbooks: `hasOutput → Save redline`, else `ERROR → Toast another slice`,
else `reviewId → Review details`. On ERROR the retry key wins, and `open("record")`
has no other call site — so Review id, Copy ID and Save original cannot be reached.
`CHANGELOG-04p1.md` states the opposite ("Before completion or on failure/manual
handoff, Review details opens the support record"). This is the one case where a
reviewer most needs the id to report a failure, and where Save original matters
because retry clears the file. Designer question **H1**.

**P2 — Every completed review will read "FINAL REVIEW COST —".**
`files/src/state.ts` `costText` now returns an em dash for all five terminal
statuses unless `kind:'settled' && settlementComplete && cents != null`. We have no
settlement projection and no ticket that delivers one, so this is 100% of reviews
until a new backend field lands. Today the reviewer at least sees the estimate.
The designer's reasoning (C1, A4) is sound — an estimate must not be dressed as a
bill — but the middle option was not taken: keep the label **Estimated review
cost** after a terminal status and keep showing the estimate, switching to **Final
review cost** only when a settled number exists. Owner decision **H2**.

**P3 — The register's second glass renders empty on a normal terminal review.**
`registerLine` falls through to `""` when the status is terminal, there is no
submit/catalog/preference/cancel/poll message, and settlement is not pending. A
lit, empty display bar sits under the cost glass on every DONE screen. **H3**.

**P4 — The outcome is now behind a click.**
On DONE the counter says "Redline ready". The decision, changes-requested count,
critic delta and confidence band exist only inside View receipt. That follows the
owner's "redundant completion paragraphs" feedback, but the answer to *what did the
tool decide* is no longer on screen. **H4**.

**P5 — Automatic playbook selection is a product change, not a presentation one.**
`preflightPlaybookChoice` lets a cheap-model preflight classification change which
playbook a review runs under, where today the app only *offers* a switch. That
changes the standard the contract is reviewed against and what the reviewer is
billed for, on an advisory signal. The changelog attributes it to owner feedback,
so it is in scope — but it needs its own ticket, a visible statement that the
choice was automatic, and a decision on whether it should land before #694/#695.
**H5**.

**P6 — The layout-audit failures are ours, and unchanged.**
17 now, up from 15. I never asked the designer to fix them; they are a house rule
(#457, #600) rather than a kit defect. Either we carry a vendored delta forever or
we ask for them upstream. **H8**.

**P7 — Small visual overlaps in the delivered renders.**
The toast filename's second line crosses the toaster's top chrome on desktop and
burnt; on the phone render "Redline ready" sits on the dial's lower bezel and
"Start" is clipped by the silhouette edge. **H9**.

## What the answers change in the filed plan

| Issue | Change |
|---|---|
| #717 | Audit count 15 → 17. Instrument Sans 600 confirmed as the intended face; register it once, do not duplicate app faces. Provenance now supplied (G3 closed). |
| #719 | Ids largely restored, so the migration shrinks. New placement rules: result details live in the receipt overlay, preflight is absent after a terminal status, disposition and cover note live in overlays. `review-submission` root id is still ours to add. |
| #720 | Resolved by design: pass `keyboardShortcuts={false}`, keep our dispatcher, use the kit dialog as the only cheat sheet. Our dispatcher must ignore `dialog[open]` targets and already-prevented events, including for modifier chords. |
| #721 | Unchanged. Note the fixture receipt says "Playbook type" where ours says "Contract type" — H7. |
| #722 | Answered: deployment config picks `completion:'register'`, no new user preference; keep our tick at ~446 ms and duck it; reduced motion only fills an *absent* mute preference. |
| #724 | **Unblocked.** F1 fixed in code; `plain` is a footer toggle with session-only state and an `onPlainChange` escape hatch. Remaining work is our own Windows/AT acceptance. |
| #725 | **Unblocked.** Keep the 1320 px cap and the existing warm shell; no dark takeover; console caps itself at 1320 px and stacks at ≤1220 px container width. |
| #726 | History drawer **removed** — the footer navigates to the existing History tab. Save original and Copy ID move into the receipt/record overlay, which raises P1. |
| new | Automatic playbook selection (P5). |

## Round-two questions

H1 ERROR and the review id (P1) — where does Review details go, given one base slot?
H2 The terminal em dash (P2) — owner: dash on every review, or keep "Estimated"?
H3 The empty second glass (P3) — hide it, or is blank intended?
H4 Outcome behind a click (P4) — confirm; if so, should the toaster badge carry the
decision ("Changes requested") rather than "Redline ready"?
H5 Automatic playbook (P5) — what does the reviewer see, and can they undo it before
submit? Is `playbookSelection` meant to render anything today?
H6 `keyboardShortcuts` defaults to `true`; the answer says we should pass `false`.
Should the default flip, so forgetting is not a double-fire?
H7 Receipt field label — do you want our `receiptLines()` relabelled to "Playbook
type"? It changes exported provenance wording for past reviews.
H8 Layout-audit rules (P6) — take `minmax(0, …fr)` and non-`clamp` sizes upstream,
or do we carry the delta?
H9 The three render overlaps in P7.
