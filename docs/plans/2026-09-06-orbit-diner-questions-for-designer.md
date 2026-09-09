# Orbit Diner kit 04 — questions for the designer

> *Published from the project's planning history on 2026-09-09. This is a
> record of what was decided at the time, not current instructions. Issue
> numbers predate the tracker migration of 2026-09-08 and refer to the old
> private numbering, and any repository path or command shown may name the
> private origin this work was done in. Read both as history, not as links
> to follow.*

2026-09-06. Read against `frontend/orbit-diner-kit-04` at the delivered SHA256SUMS,
and against the app at `7bd1b42`. Nothing below is a request to change the direction.
These are the blanks we deliberately did not fill in ourselves.

Verified before writing: `tsc --noEmit` clean, 10/10 kit tests pass, the preview
serves and renders at `http://localhost:4173/preview/`.

Answer inline; each item is numbered so we can quote the number back.

---

## A. Placement and page context

**A1 — What is behind the console?**
Every render puts the console on a deep navy page (`#0c1e2d`) with the counter
scene bleeding to a rounded edge. Our app shell is warm off-white
(`--ct-bg: #faf7f2`) with a nameplate, a tab strip and a footer above and below.
Which do you intend: (a) the console sits inside the existing warm shell as one
card, (b) the Review tab alone goes full-bleed dark to match the preview, or
(c) the whole shell adopts the darker surround?

**A2 — 1720 px versus our 1320 px shell.**
`docs/ART-DIRECTION.md` asks the console to fill its parent up to 1720 px. Our
shell caps every tab at `--ct-maxw: 1320px`. Should Review break out of that cap,
should the cap move for every tab, or is 1320 px an acceptable ceiling for this
composition?

**A3 — What is the Review history drawer for?**
The kit's **Review history** key opens a dialog listing `model.history` with a
**Run again** key per row, plus an "Open History tab" key. We already have a full
History *tab* with paging, filters, expandable rows, per-row disposition,
cover-note and download. Is the drawer (a) a shortcut to the last N reviews,
(b) intended to replace the History tab, or (c) removable in favour of just
navigating there? If (a): how many rows, sorted how, showing which fields?

**A4 — How should the unbuilt readouts look while they are absent?**
`odometer`, `cost.adminDaily`, `cost.holdCents`, settled totals and
`runAgainAvailable` have no backend yet (#501, #710, and a new owner-safe spend
projection). The contract says they simply do not render. Confirm that the
`Done` and `Ready` renders we have *are* the intended finished layout with those
absent — that no gap, dash or placeholder is meant to sit where they will go.

---

## B. Controls and behaviour

**B1 — Progress is text-only.**
The progress glass is `role="progressbar"` with `aria-valuetext` and never sets
`aria-valuenow`, including for the four known stage tokens. Assistive tech will
therefore report it as indeterminate for the whole review. `CONTROL-COVERAGE.md`
row 3 says "unknown/null is indeterminate, without `aria-valuenow`", which reads
as though the four known tokens *should* carry a value. Which is intended?

**B2 — One lever, two jobs.**
The lever carries `review-submit-button` when idle and `review-cancel-button`
when working — the same element, never both at once. Today Start and Stop are
separate controls. Is a single control also the intent in `plain` mode and under
forced colours, where there is no lever imagery to make the state obvious?

**B3 — Which test ids are part of the design contract?**
The kit keeps eleven. Our suite references about sixty. Some of the gaps look
deliberate (`toaster-state-progress` / `-sober` become status-named ids); others
look incidental (`review-preflight-card`, `review-cover-note-butter`,
`review-receipt-paper`, `review-cost-estimate`, `review-outcome`,
`notify-toggle`). We are happy to add the missing ones ourselves — we only need to
know whether any of them are absent *on purpose* because the control they named
no longer exists as a single thing.

**B4 — Markup-intensity keys have no test id; footnote keys do.**
`review-notes-mode-option-{value}` is present, but the three intensity radios
carry nothing. Deliberate, or an oversight?

**B5 — The register's status glass shows `messages[0]` regardless of scope.**
So a receipt confirmation ("Receipt image download started") can appear in the
register glass while the receipt dialog is open. Intended, or should the glass
filter to submit/poll/cancel scopes?

**B6 — "That one burnt." appears three times at once.**
On the toaster's status glass, in the register's status glass, and as the paper
card's headline. Is the repetition intended emphasis, or should two of the three
say something else?

**B7 — Stage signal without imagery.**
On the appliance the only stage signal is the browning tint on the bread plus the
caption. Under forced colours the bread is hidden. Is the caption alone the
intended fallback, or should something else carry the stage there?

---

## C. Copy

**C1 — Cost caption.**
Today the tab says: *"About $0.31 of model spend for a document of ordinary size,
on the models this deployment is set to. A long one costs more."* The kit says
**ESTIMATED REVIEW COST · $0.31** with *"Typical-review estimate for the active
model policy."* The kit's is shorter and drops "a long one costs more". Confirm
the kit's copy wins, or give us the wording you want.

**C2 — Which keyboard-shortcut list is canonical?**
The kit's cheat sheet lists `Cmd/Ctrl+Enter`, `Cmd/Ctrl+U/O`, `Cmd/Ctrl+D/S`,
bare `P`/`G`, `[`/`]`, `1`–`4`, `M`/`R`, `Escape`. Our app additionally binds
`Cmd+Shift+P`, `Cmd+Shift+G`, `Cmd+Shift+M` and `Cmd+Shift+R`, and `?` / `Cmd+/`
for the sheet itself. Should the kit's dialog become the only cheat sheet (and
gain the four chords), or does the app's modal survive?

**C3 — Manual review copy.**
The kit shows "Needs a human" on the lamp and the panel, and "A person needs to
review this result." in the glass. Both `MANUAL_REVIEW_REQUIRED` and
`ERROR_MANUAL_REVIEW_REQUIRED` map here. Confirm that copy is final — it is the
user-visible fix for issue #458 and we would rather not paraphrase it.

**C4 — Download confirmation.**
The kit says *"Download started automatically. Save redline repeats it."* Today
we render a fixed saved line. Which wording ships?

---

## D. Art and assets

**D1 — Instrument Sans 600.**
`orbit.css` declares Instrument Sans 400 and 600, Space Grotesk 500, IBM Plex
Mono 400. The app self-hosts Instrument Sans 400/500/700, Space Grotesk 500/700,
IBM Plex Mono 400. Should we add the 600 face, or restyle the keys to 500 or 700?

**D2 — Plate variants for phones.**
The six WebP plates are 1,491,984 bytes at one resolution. Is a smaller variant
(or an AVIF pair, or a `srcset`) available for phone widths, or is one resolution
intended everywhere?

**D3 — Clip paths versus container shape.**
The silhouettes are fixed paths in `MaterialArt.tsx` against fixed viewBoxes. If
the console ends up in a narrower or differently proportioned container than the
one you designed against (see A2), do the paths still line up, or would you re-cut
them?

**D4 — Things the approved scene has and the kit does not.**
`references/approved-scene.png` has a pen on the counter, the preflight card on a
metal stand, a separate **Start Toaster** and **Stop** pair, a lower filename
plate on the toaster body, and the wording "TYPICAL REVIEW ESTIMATE". The kit
drops all five. We read those as deliberate. Confirm — especially the wording
change, since it is the one a reviewer will read.

**D5 — Dark mode.**
`desktop-dark.png` uses the same plates with a darkened surround. Is there a dark
art variant coming, or is that the intended dark treatment?

**D6 — Tab chrome.**
We brown the favicon per stage today (`toaster/faviconFrames.ts`) and change the
document title. Does this scene come with its own favicon/tab treatment, or do we
keep ours unchanged?

---

## E. Sound

**E1 — Completion identity.**
The kit says the original `pop` stays the default and the new `ka-ching` is an
alternate. Who picks: a deployment setting, a user preference, or is it simply
never used? Same question for `failure` — the doc says point it at our existing
low clunk *or* reuse the lever recording pitched down.

**E2 — The running tick.**
The kit has no tick and no timer. We tick every ~445.6 ms during a review. Keep
our tick and duck it to 12% per the mixing contract, or drop the tick entirely
now that four stage cues exist?

**E3 — Mute default under reduced motion.**
`MOTION-AND-SOUND.md` says default the persisted mute flag to muted under reduced
motion. That changes existing behaviour for users who already have a stored
preference. Should the default only apply to a *new* user with no stored flag?

---

## F. Accessibility and fallbacks

**F1 — Forced colours needs another pass.** *(the one blocking item)*
In your own `forced-colors.png`: the contract-type `<select>` has no visible box
(only the label text shows), the receipt button renders as bare red text with no
box, the instructions textarea has no visible border, the dial is an empty circle
with "Browse playbooks" overlapping its edge, and the lever-handle SVG still
paints its cream gradient because `SmallArt` carries no `.od-material` class. The
contract says forced colours "hide imagery and preserve controls". Can you supply
the corrected rules, or confirm the intended appearance so we can write them?

**F2 — Who owns `plain`?**
It is a prop with no consumer in the app; only the preview toolbar sets it. Is it
(a) a user-visible, persisted preference, (b) a media-query fallback, or (c) a
development aid we should not expose? Issue #504 called for a "plain-controls
escape hatch", which suggests (a).

**F3 — Touch targets on phone.**
At 390 px the register key bed reflows to three intensity keys, footnotes
two-by-two, and three service keys. Do all of those meet 44×44 CSS px, and is
that a constraint you designed to?

**F4 — Escape, focus and the four dialogs.**
Escape inside a dialog closes it without ejecting the file; Escape outside a text
field ejects the file. With four dialogs (receipt, playbooks, history, shortcuts)
and our own global Escape handler, we want to be sure of the intended precedence
and where focus returns from each.

---

## G. Delivery

**G1 — How do we take delivery of the source?**
Do you want us to vendor `src/` into the repo (as `frontend/src/orbit-diner/`),
or consume it as a versioned package? Vendoring means your future revisions
arrive as diffs against our copy, which is fine — we just want to agree it now
rather than after the first revision.

**G2 — Is there a changelog against study 02?**
`orbit-diner-study-02.zip` is what we saw last. A list of what moved would save us
re-deriving it.

**G3 — Provenance for the six plates.**
`THIRD-PARTY-NOTICES.md` says they were "generated for this integration kit using
the user's supplied reference". For the open-source cut (#282, #341) we need a
per-plate provenance line: what generated them, from what, and what rights come
with the output. Can you supply that?

---

## Things we are *not* asking you about

We will handle these ourselves and do not need design input:

- Adding back the missing `data-testid` hooks and migrating the test suite.
- Choosing one owner for the keyboard shortcuts and deleting the duplicate.
- Choosing one receipt exporter (`toaster/receipt.ts` stays the source of facts).
- Wiring every callback to the existing guarded handlers, preserving idempotency,
  cancellation races, once-only auto-download and the failure classification table.
- Keeping `adminDaily` away from non-admin reviewers.
