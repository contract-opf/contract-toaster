# Orbit Diner — round 3 for the designer

> *Published from the project's planning history on 2026-09-09. Issue numbers
> below predate the tracker migration of 2026-09-08 and refer to the old
> private numbering; read them as history, not as links.*

7 September 2026. Answers to H1–H9 are accepted and recorded; the plan is final and
the build queue is filed. Two of those answers asked us to send you material (H8, H9)
— it is below and in `docs/assets/orbit-diner-designer-round3/`. Five further asks
follow, all of them things we would otherwise have to invent.

---

## H8 — the layout audit: rules and exact output

Two house rules, both enforced by `frontend/scripts/layout-audit.mjs`, which runs in
`scripts/check-frontend.sh` and in CI GATE E. Your CSS produces **17 findings**.
`audit:focus` and `audit:contrast` pass unchanged.

Full output: **`docs/assets/orbit-diner-designer-round3/layout-audit-output.txt`**
(reproduce with the file copied under `frontend/src/`, then
`cd frontend && node scripts/layout-audit.mjs`).

**Rule 1 — flexible grid tracks must be `minmax(0, …fr)`, never a bare `…fr`** (our
issue #457). A bare `1fr` is `minmax(auto, 1fr)`, and that automatic minimum is the
*min-content* width of the track's contents, so one non-shrinkable child pins the
track open and the page scrolls sideways. We measured this on a real screen: at
375 px it held the app grid open at 835 px and pushed three tabs off-screen.

12 findings, all in `orbit.css`:

| Selector | Declaration |
|---|---|
| `.od-appliances` | `1fr 1.02fr` |
| `.od-appliances` | `1fr` (×2, in container blocks) |
| `.od-counter-papers` | `0.8fr 1.2fr` |
| `.od-counter-papers` | `1fr` (×2) |
| `.od-results` | `1fr` (×2) |
| `.od-shortcuts` | `minmax(130px, 0.8fr) 1.2fr` |
| `.od-shortcuts` | `1fr` |
| `.od-disposition-keys` | `1fr` |
| `.od-ticket-column` | `1fr auto` |

The fix is mechanical — wrap the flexible track: `minmax(0, 1fr)`,
`minmax(0, 1.02fr)`, `minmax(0, 0.8fr)`, `minmax(0, 1.2fr)`. It changes nothing
visually while content fits; it only stops a long word or a wide child from forcing
the track open. Several of your other tracks already do this
(`repeat(3, minmax(0, 1fr))`), so the convention is already in the file.

**Rule 2 — every font size must resolve to an absolute value at or above 14 px**
(our issue #600). The checker accepts a `--ct-text-*` token, a `px` literal, a `rem`
literal, or `inherit`. It rejects anything it cannot resolve from source, because a
relative or viewport-derived size compounds with its context and no source guard can
prove it clears the floor. The owner set this floor after measuring 12 px text he
could not read.

5 findings, all `clamp()`:

```
clamp(25px, 3.5vw, 48px)
clamp(18px, 2.1vw, 30px)
clamp(18px, 1.65vw, 26px)
clamp(15px, 1.6vw, 24px)
clamp(14px, 4.4vw, 18px)
```

Every one of those has a floor at or above 14 px, so they are *correct*; the checker
simply cannot prove it. Two ways forward, your call: replace each with a px literal
plus a container/media step, or tell us which of the five must stay fluid and we will
carry a documented per-selector exemption on our side. We would rather not weaken the
rule — it is the only thing standing between us and the 12 px text we already shipped
once.

The 14 px minimum holds in the current file: your smallest absolute size is 14 px.

---

## H9 — the render overlaps, with two corrections

You were right to ask. Measured in a live browser at the stated widths, one of my
three original claims was wrong and one was mis-described. Here is what is actually
there.

### O1 — the filename overflows the bread's crust · **confirmed**

- **Element:** `.od-toast-name` inside `[data-part="slice"]`
- **Width:** 1360 px viewport, 1320 px console (also at 1220 px and below)
- **State:** DONE — but the rule is state-independent; LOADED and ERROR are the same
- **Content:** `Mutual NDA draft-v3.docx` — 24 characters, your own fixture
- **Screenshot:** `overlap-2-desktop-toast-name-crust.png`

The two-line name is horizontally wider than the bread at the height it sits. In the
capture the leading `M` of "Mutual" and the trailing `-` of "draft-" both sit on the
star field, outside the crust.

Cause: `.od-toast-name` is positioned `left: 12%; right: 12%` against the slice's SVG
**bounding box**, which is the full 1254 × 1254 viewBox. The visible crust at
`top: 27%` is inset considerably more than 12%. Measured: name box 199 px wide inside
a 248 px slice box, but the crust at that height is narrower than 199 px.

What we need: the intended maximum, and what should happen past it. A tighter
`left`/`right` inset, a smaller `clamp()` floor, three lines, or a mid-word ellipsis
are all yours to choose. The accessible name already carries the full filename, so
truncation is safe.

### O2 — the ready lamp detaches from its pill on phone · **confirmed**

- **Elements:** `.od-ready-lamp` and `.od-ready`
- **Width:** 390 px viewport, 378 px console
- **State:** DONE
- **Screenshot:** `overlap-1-phone-ready-lamp-and-lever-label.png`

On desktop the lamp is a green dot inline before "Redline ready". On phone it renders
as a small unlabelled grey circle roughly 20 px *below* the pill, on the toaster's
lower face, with no relationship to the text. It reads as a stray screw head.

### O3 — the ready pill sits on the dial's bezel on phone · **confirmed, phone only**

- **Elements:** `.od-ready` over `.od-dial`
- **Overlap:** 96 × 11 CSS px at 390 px; 168 × 11 px at 1360 px
- **State:** DONE
- **Screenshot:** same as O2

At 1360 px this is not a defect — the pill sits in the toaster's moulded recess and
the graze against the dial's chrome ring reads as designed. At 390 px the dial is
proportionally larger, the pill's translucent `#eadcca80` fill is not enough, and
"Redline ready" is set directly on brushed metal.

### O4 — the lever's label detaches from the lever on phone · **re-described**

- **Elements:** `.od-lever-label` and `.od-lever`
- **Width:** 390 px, state DONE
- **Screenshot:** same as O2

My earlier report said "Start" was clipped by the silhouette. **That was wrong** —
measured, it sits 32 px inside the figure's box and about 10 px inside the visible
shell edge. Nothing is clipped. The real problem is placement: the word sits below and
right of the lever slot, on the flat of the toaster body, far enough from the lever
that it reads as a floating word rather than that control's label.

### Withdrawn

My earlier claim that the filename collides with the toaster's top chrome on desktop
does not reproduce. The name's bottom is at y 249 and the shell's visible top edge at
y 317. Sorry for the noise.

---

## New asks

**N1 — do H1 and H3 come as an 04p2, or do we implement them?**
H1 (Review details beside the visible failure cause/fix) and H3 (the switched-off
glass) are both changes to `OrbitDiner.tsx` and `orbit.css`. We are vendoring the
source, so we *can* make them — but you own the CSS and the composition, and a patch
keeps our copy and yours from diverging on the first change. Our preference is an
04p2 carrying H1, H3, H6 (`keyboardShortcuts` default `false`), the H8 fixes and the
H9 corrections. Tell us if you would rather we take them locally.

**N2 — what does "deliberately switched off" look like?**
H3 says the second register glass should look switched off when there is no message,
not filled with filler. We do not have that treatment. Please supply it for light,
dark and forced colours: the glass fill, whether the bezel/inner shadow changes,
whether any indicator remains, and whether the element stays in the accessibility tree
or becomes `aria-hidden` while off.

**N3 — the post-terminal estimate.**
H2 keeps the captured estimate for that review, labelled **Estimated review cost**,
after completion. Should it look identical to the pre-submit estimate, or read as
historical — dimmer digits, a caption, something? And confirm the four label states we
will implement: *Estimated review cost* (pre-submit and post-terminal), *Review
reservation*, *Final review cost* (settled only), *Estimate unavailable*.

**N4 — first paint on the Review route.**
Nothing in the kit covers what the console looks like while 1,491,984 bytes of plates
are still fetching. The route is lazy-loaded, so a cold visit has a real gap. Is there
an intended skeleton, a low-quality placeholder, or should the native controls simply
render on the counter colour until the plates land?

**N5 — what if a plate does not load?**
A 404, a cache miss behind a stale fingerprint, or a CSP block on a misconfigured
deployment. Today the `<image>` silently renders nothing and the controls float on the
page background. Should the console fall back to the plain layout automatically — the
same path forced colours already takes — or is there a different intended degraded
state?

**N6 — long playbook names in the phone glass.**
Desktop wraps the selected name to two lines. The phone glass is much narrower and the
fixture name is short. What happens to a 60-character playbook display name there? Our
admin UI accepts any string.

**N7 — is the Review tab meant to be printable?**
`orbit.css` carries a `@media print` block that hides the console and prints an open
dialog. Is printing an intended supported path (a printed receipt is plausible for a
legal file), or is that block defensive only?

---

## Recorded, no action needed from you

H4 (outcome stays inside View receipt), H5 (auto-select with no explanation,
confirmation or Undo; manual override always wins), H6 (`keyboardShortcuts` defaults
to `false`), H7 (canonical `receiptLines()` wording unchanged; "Playbook type" is the
appliance label only). All four are in the build queue under epic #729.
