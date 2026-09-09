# Orbit Diner — final plan and build queue

> *Published from the project's planning history on 2026-09-09. This is a
> record of what was decided at the time, not current instructions. Issue
> numbers predate the tracker migration of 2026-09-08 and refer to the old
> private numbering, and any repository path or command shown may name the
> private origin this work was done in. Read both as history, not as links
> to follow.*

7 September 2026. Supersedes `2026-09-06-orbit-diner-review-tab-plan.md` for anything
they disagree on. Epic #729.

The design is settled and **no product decisions remain open**. The 04p1 patch and the
04p2 artwork package are both accepted and applied to the delivered kit. Owner decisions
H1–H9 and designer answers N1–N7 / O1–O4 are recorded below and folded into the tickets.
The queue is 21 loop-eligible issues plus one attended.

## Source of record

| Thing | Where |
|---|---|
| **Supplier source of record** (kit 04 + 04p1 + 04p2 artwork) | `frontend/vendor/orbit-diner/` — tracked, 2.3 MB, never edit |
| Designer rationale | `frontend/vendor/orbit-diner/docs/DESIGNER-ANSWERS-04p1.md`, `CHANGELOG-04p1.md` |
| Per-plate provenance and prompts | `.../docs/ART-PROVENANCE-04p1.json`, `ART-PROMPTS-04p2.json` |
| Patch baselines | `.../MANIFEST.sha256`, `.../docs/PATCH-MANIFEST-04p1.json`, `PATCH-MANIFEST-04p2-artwork.json` |
| Full supplier drops, archived | `~/Dev/_vendor-archive/contract-toaster/orbit-diner/2026-09-07/` (129 MB) |
| Our vendored copy (created by #717) | `frontend/src/orbit-diner/` |
| Round-3 asks sent to the designer | `2026-09-07-orbit-diner-designer-round3.md` |
| Mockup of the finished tab | <https://claude.ai/code/artifact/b844cbd8-2e9c-4fab-9022-72f5fc001fa5> |

Both supplier packages were applied with their own helpers after verifying every hash.
04p1: 24 declared baselines matched our kit-04 bytes, 21 declared-new files were
genuinely absent, 45 payloads matched. 04p2 artwork: 7/7 payloads matched and every
declared baseline matched our 04p1 kit exactly; check → apply → re-apply was clean and
idempotent. Backups of the pre-patch files sit beside the kit.

## The 04p2 artwork correction

My H9 report said the stray grey circle on the phone toaster was `.od-ready-lamp`
escaping its container. **That was wrong.** The fitting is painted into
`artwork/toaster.webp`. Verified by pixel diff of the old and new plates: a single
localised retouch at approximately x 738–796, y 810–866 removes it and rebuilds the
enamel; `PROMPTS.json` records the edit instruction and the coordinates.

The package supplies two toaster plates — `toaster.webp` (221,298 B, desktop/tablet,
original moulded recess) and `toaster-phone.webp` (233,432 B, wider and lower
photographed status recess) — an additive `toasterVariant` prop on `MaterialArt.tsx`,
and `orbit-artwork-04p2.css` carrying the finished positions for the status dot, the
lever label and the filename box. That CSS adds **no** new `audit:layout` findings; the
17 remain in `orbit.css` and #717 removes them by consolidation.

## Owner decisions, final

| Ref | Decision | Ticket |
|---|---|---|
| **H1** | Keep **Toast another slice** on the toaster. Add **Review details** beside the visible failure cause/fix; it opens the record overlay with Copy ID and, when available, Save original. Both reachable before retry clears the review. | #734 |
| **H2** | Keep the estimate **captured for that review**, labelled **Estimated review cost**, after completion. **Final review cost** only on complete numeric settlement. **Estimate unavailable** when there is no estimate. Never substitute the next review's estimate. | #735 |
| **H3** | Retain the second register glass as the operational message window. With no message it must look deliberately switched off — no duplicated status, no filler. | #737 |
| **H4** | On DONE the toaster says **Redline ready**. Decision, counts, critic detail and confidence stay inside **View receipt**. No outcome summary beneath the scene or in the second glass. Failure causes and manual-review guidance stay visible in their own states. | no ticket — this is the delivered behaviour |
| **H5** | Auto-select the playbook **with no explanation, confirmation or Undo control**. The name and dial simply reflect the choice. Manual override always wins; stale responses and non-active playbooks are ignored; a submitted review is never changed. | #730 |
| **H6** | `keyboardShortcuts` defaults to **false**. The standalone demo enables it explicitly. The app keeps its dispatcher. Local dialog Escape stays. | #720 |
| **H7** | Canonical `receiptLines()` wording is unchanged. **Playbook type** is the appliance label only; this integration does not rewrite historical receipt wording. | #721 |
| **H8** | Both house rules accepted, **no exemptions**. All five `clamp()` declarations removed and replaced with explicit sizes at console-width breakpoints; every bare `fr` becomes `minmax(0, …fr)`. | #717 |
| **H9** | Four defects confirmed and fixed in 04p2 — including the correction that the stray circle was baked into the plate, not a stray DOM element. | #717, #739 |

## Standing invariants for every ticket

Read once; they are not repeated in each issue.

- **The app stays the state owner.** The console fetches nothing, polls nothing,
  persists nothing, submits nothing and logs nothing. `onAction` calls existing guarded
  handlers; `onPreferences` calls existing setters.
- **Keep the backend contract.** File validation, the idempotency key, cancellation
  races, the once-only automatic download, preference error handling and the failure
  classification table all stay where they are.
- **Escaped text only.** No clause text on the receipt, no raw server exception in the
  status window, no document excerpt in a message.
- **Storage allowlist unchanged.** No new `localStorage` or `sessionStorage` key.
  `security-posture.test.tsx` must pass untouched.
- **14 px type floor (#600)** and the bare-`fr` rule (#457) hold; `audit:layout` is part
  of the gate.
- **`adminDaily` never reaches a non-admin**, and nothing calls `/api/admin/spend` on a
  reviewer's behalf.
- **Brand-free public surface.** `python3 tests/lint-brand-free.py` still passes.
- **Never edit `frontend/vendor/orbit-diner/`.** It is the supplier's source of record and the baseline the next patch verifies against. Our copy is `frontend/src/orbit-diner/`.

## The queue

Loop label `afk-orbit-diner`, gate `bash scripts/check-frontend.sh`, branch `main`.
21 loop-eligible tickets; one attended.

```
717 vendor ──┬── 732 asset guard
             └── 718 projection ── 719 render behind flag ──┬── 733 test-id contract
                                                            ├── 720 shortcuts
                                                            ├── 721 receipt
                                                            ├── 722 sound
                                                            ├── 723 motion rails
                                                            ├── 724 forced colours / plain
                                                            ├── 725 host CSS + width
                                                            ├── 726 new surfaces
                                                            ├── 734 H1 review details
                                                            ├── 735 H2 captured estimate
                                                            └── 730 H5 auto playbook
                                                                    │
                        727 delete replaced surfaces ◀──────────────┘
                                    │
                        728 flip and remove the flag
                                    │
                        736 attended acceptance  (not loop-labelled)

also under 719: 737 unlit glass · 739 filename shortening
also under 719+725: 738 variant / first paint / plate failure
also under 721: 740 receipt printing
```

**#736** is the only ticket outside the loop, and it is rung 3: a hardware GPU frame
trace, a real Windows screen reader and a real `.docx` through a live deployment have no
CLI, API, browser-automation or computer-control path. Everything around the human's part
is prepared; their job is to work the enumerated list and paste the numbers into one
comment.

#737 is no longer parked — the 04p2 response settles it: `register.webp` already contains
the finished unlit glass in both themes, so it is reused with an empty text layer and no
CSS fill is painted over it.

## Where the art lives now

**`frontend/vendor/orbit-diner/` is the tracked source of record** — 70 files, 2.3 MB: the
component source, the seven runtime plates, nine WOFF2 faces with their licences, the 19
audio one-shots, the supplier documentation and provenance, and a `MANIFEST.sha256`.
Sixty-seven of the seventy are byte-identical to the delivered kit; the other three are
the patch manifests and the art prompts. Its README carries the procedure for applying the
next supplier patch against a verified baseline.

Nothing there is compiled, tested or audited: `tsconfig.json` includes only `src`, both
CTDS audits root at `src`, and `vitest.config.ts` scopes collection to `src/**` (#731). It
is inert until #717 copies out of it.

The other 129 MB — PNG masters, the standalone preview bundle and its 59 MB of reference
renders, the untrimmed CC0 originals, their build tooling, and the three drop directories
— is archived at `~/Dev/_vendor-archive/contract-toaster/orbit-diner/2026-09-07/`, with a
README explaining what each piece is. Nothing was deleted that cannot be regenerated or
re-obtained, and the drops are kept intact so a disputed baseline can be reconstructed.

`frontend/orbit-diner-*` is gitignored so a future drop does not clutter `git status` or
get swept into a careless `git add -A`. It would not have blocked the loop — its sync gate
ignores `??` lines.

## Prerequisite already landed

`390be56` — `frontend/vitest.config.ts` now scopes collection to `src/**` (#731). The
gate was **red** before it: vitest was collecting
`orbit-diner-04p1-patch/files/tests/workflow.test.tsx`, a partial tree whose
`../src/motion` import cannot resolve, and quietly running the complete kit's 16 tests
as if they were ours. The loop cannot start on a red tree.

Baseline now: `bash scripts/check-frontend.sh` → `CHECK-FRONTEND: ALL GREEN`,
90 files, 956 tests.

## Running the loop

In a fresh thread, with a clean tree on `main`:

```
Workflow({
  scriptPath: "~/Dev/Skillz/workflow-loop/assets/workflow-loop.js",
  args: {
    repo: "exos-legal/contract-toaster",
    label: "afk-orbit-diner",
    branch: "main",
    checkCommand: "bash scripts/check-frontend.sh",
    setupCommand: "",
    reviewerEffort: "xhigh",
    coderEffort: "high",
    onBlocked: "skip",
    blockedLabel: "afk-blocked",
    reportIssue: "auto",
    dryRun: true
  }
})
```

Run once with `dryRun: true` to check the queue and dependency parsing, then again
without it. `reportIssue` must stay on: the head-blocker guard has no journal to read
without it, and that is the failure mode that starves an overnight queue.

`scripts/check.sh` (the Python gate) is not in `checkCommand` because every ticket here
is frontend-only; #728 runs it explicitly as its own verification step.

## Nothing is open with the designer

All of N1–N7 are answered and folded in:

| Ask | Answer | Ticket |
|---|---|---|
| N1 | Belongs upstream in 04p2, against the 04p1 baseline. No permanent app-level CSS delta — consolidate into `orbit.css`. | #717 |
| N2 | `register.webp` already holds the finished unlit glass; reuse with an empty text layer, no gradient box. | #737 |
| N3 | Identical to the pre-submit estimate. The label carries the distinction. Final review cost includes a genuine zero. | #735 |
| N4 | Functional plain layout while essential artwork loads; switch to the scene only on success, preserving state and never under a focused control. | #738 |
| N5 | Same plain fallback on plate failure, with "Illustration unavailable. All review controls are available." and one Retry illustration. Never an ERROR. | #738 |
| N6 | Phone keeps one line at 15 px with an end ellipsis; the picker shows full names. | #725 |
| N7 | Receipt printing only — never the counter scene. | #740 |
| O1 | Two lines max in the bread's central face; measured truncation preserving the start and `.docx`; no character-count limit. | #739 |
| O2 | Fitting removed from the artwork; one 8 px DOM dot in the capsule, one implementation. | #717 |
| O3 | Status background is photographed into the phone plate; the CSS readout stays transparent. No beige CSS capsule, and the provisional 145 × 32 px box is superseded. | #717 |
| O4 | Label centred on the lever track, 4 px below its visible end, stationary while the handle moves. | #717 |

Two earlier proposals were explicitly superseded and must not be implemented: the opaque
CSS phone capsule with a 145 × 32 px box (O3), and the conceptual gradient hex fills for
the off glass (N2).
