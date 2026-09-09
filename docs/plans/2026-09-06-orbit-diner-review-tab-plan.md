# Orbit Diner kit 04 — review-tab replacement plan

> *Published from the project's planning history on 2026-09-09. This is a
> record of what was decided at the time, not current instructions. Issue
> numbers predate the tracker migration of 2026-09-08 and refer to the old
> private numbering, and any repository path or command shown may name the
> private origin this work was done in. Read both as history, not as links
> to follow.*

2026-09-06. Companion to the control inventory the kit was built against
(`2026-09-05-review-workflow-control-inventory.md`) and to the designer questions
(`2026-09-06-orbit-diner-questions-for-designer.md`).

- **Kit:** `frontend/orbit-diner-kit-04` (untracked; delivered 6 Sep 2026, hashes in `SHA256SUMS.txt`).
- **Epic:** #729. Steps #717–#728, ordered.
- **Mockup:** <https://claude.ai/code/artifact/b844cbd8-2e9c-4fab-9022-72f5fc001fa5>
  (rebuild locally with `python3 docs/assets/orbit-diner-mockup/build.py`).

## Verified on the delivered kit

Run on 2026-09-06 from `frontend/orbit-diner-kit-04`:

```
npm ci && npx tsc --noEmit                     # clean
npx vitest run --maxWorkers=1 --minWorkers=1   # 1 file, 10 tests, all pass
python3 -m http.server 4173                    # preview/ renders live
```

Everything else in the handoff is the supplier's own claim. In particular the
browser checks ran in headless Chromium with software rendering: real Safari/iOS,
Firefox, Windows assistive technology, mobile audio unlock and hardware GPU
tracing were not tested there, and are gates on our side (#728).

## Measured against this repo

`orbit.css` copied under `frontend/src/` and the audits run:

| Audit | Result |
|---|---|
| `npm run audit:layout` | **FAIL, 15 findings** — 12 bare `fr` grid tracks (#457's rule) in `.od-appliances` ×3, `.od-counter-papers` ×3, `.od-results` ×2, `.od-shortcuts` ×2, `.od-disposition-keys`, `.od-ticket-column`; and 3 `clamp()` font sizes (#600's rule) that the resolver cannot prove clear the 14 px floor |
| `npm run audit:focus` | pass |
| `npm run audit:contrast` | pass |

The kit's smallest absolute font size is 14 px, so the floor itself is respected.

Test-id coverage: the kit ships 11 ids; the suite references about 60. Full list
and use counts in #719.

`scripts/focus-audit.mjs` hardcodes `frontend/src/toaster/Toaster.tsx` and reads
it, so deleting that file breaks the audit (#727).

## Decisions this plan assumes, and who owns them

| Ref | Decision | Owner | Blocks |
|---|---|---|---|
| A1 | What sits behind the console — warm shell, dark Review tab, or dark shell | designer | #725 |
| A2 | 1720 px console versus our 1320 px `--ct-maxw` | designer + owner | #725 |
| A3 | What the Review history drawer is for, given the History tab | designer + owner | #726 (part) |
| C2 | Which keyboard-shortcut list is canonical | designer | #720 |
| E1–E3 | Completion identity, the running tick, mute default under reduced motion | designer + owner | #722 |
| F1 | Forced-colours corrections | designer | #724 |
| F2 | Whether `plain` becomes a user preference (#504 implies yes) | owner | #724 |
| G3 | Per-plate provenance for the open-source cut (#282, #341) | designer | #717 (part) |

## Two reversals of earlier direction needing owner sign-off

1. **Raster art on the hero.** Six same-origin WebP plates, 1,491,984 bytes,
   replacing the vector-only rule. Lazy Review route, outside the app JS chunk.
   No animation runtime, CDN or third-party telemetry is added.
2. **One Start control.** The lever is itself a native text-labelled button and
   becomes Stop during a run, so the previously required separate Start button
   goes away.

## Related issues

Closes #458. Delivers #504. Supersedes #490. Lays out but does not implement
#501, #710, #711. Interacts with #694 (intensity as a wire field). Makes #713's
split of `ReviewSubmission.tsx` easier — the projection in #718 is the seam.
