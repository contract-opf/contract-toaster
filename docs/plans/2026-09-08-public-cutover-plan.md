# Public cutover — develop in the open, keep exos-legal as the overlay

8 September 2026. Written to be read cold. This plan takes the project from
"private trunk, periodic sanitised cut" to "public trunk, private Exos
overlay". Owner decisions are recorded in *Decisions*; nothing below assumes
context from the conversation that produced it.

## Decisions

- **D1 · Topology.** `contract-opf/contract-toaster` becomes the development
  trunk. `exos-legal/contract-toaster` stays private and is stripped down to
  the overlay: tenant config, the real playbook bundle and gold set, the
  corpus, the access request — **plus any Exos-specific AWS implementation
  code**, which is a deliberate widening of today's overlay contract (see
  *The overlay seam*).
- **D2 · Planning docs.** Publish a curated subset, not the whole directory.
- **D3 · Issues — REVISED 2026-09-08.** The backlog moves to the public
  tracker, in full. The original decision kept it private and filed only new
  work publicly; that was wrong for the model actually being adopted — if the
  grind happens in public, the backlog has to be where the code is. 61 issues
  were migrated and renumbered; 6 overlay-only issues stayed private.
- **D4 · Playbook.** The published `contract-opf/eiaa-playbook` stays the
  public position set. The two extra hard rejections in the private copy
  (`no-exos-payment`, `no-one-way-confidentiality`) stay private.

## Where things actually stand

The cutover is less of a leap than it sounds, because most of the machinery
already exists and the target architecture is already written down in
`overlay/README.md` (in the private overlay).

- `contract-opf/contract-toaster` is **already public and live**, with its own
  fresh history: eight `Public cut of private main @ <sha>` commits landed by
  pull request, most recently 2026-09-01 from `caa0408`. It is seven days and
  one Orbit Diner epic behind private `main`.
- The org already publishes four sibling repos: `opf` (the format),
  `playbook-engine`, `playbooks`, and `eiaa-playbook`.
- `scripts/public-cut.sh` builds the clean tree, scrubs the manifest, runs the
  gates, and diffs against the public repo. It never pushes. It works.
- `public-cut-exclude.txt` is the written-down scrub list, gated by
  `tests/lint-public-cut-exclude.py`.
- **The private history can never be published** and this plan does not try:
  119 real `.docx` were added over its life, plus 22 commits carrying the
  pre-de-branding playbook. Fresh-history publication stays the only mechanism.

## What gates publication

Six findings, in the order they should be dealt with.

### F1 · A counterparty name was four days from shipping — fixed

`docs/task3_block_map_determinism_walkthrough.md` and
`docs/whitespace_and_block_transcript_plan.md`, both added 2026-09-04, quoted
a real counterparty agreement filename. They sit in `docs/`, which is **not**
in the exclusion manifest, so the next cut would have published it. The live
public repo was clean only because the last cut predated them.

**Done:** both sanitised to "identity withheld". The live public repo was
verified clean of the name before and after.

### F2 · Nothing detects a counterparty name — BUILT 2026-09-08

This is the finding that matters most, because it is the one the model change
makes dangerous. Today's safety rests on a human scrubbing at cut time. Once
the trunk is public, **a push is a publication** and there is no scrub step.

The existing gates do not close this:

| Gate | Catches | Misses |
|---|---|---|
| `lint-brand-free.py` | the corporate domain; the private org in a functional position | every counterparty name |
| `lint-public-cut-exclude.py` | manifest paths that stopped existing | content typed into a non-excluded file |
| `public-cut.sh` scans | AWS keys, private keys, `.env`, non-synthetic `.docx` | prose |

F1 passed all three.

**The design constraint that makes this interesting: the denylist is itself
the secret.** A list of 44 counterparty names cannot live in the public repo.
So the detector splits:

- **Public** — the scanner logic, in the engine, with no data. It takes a
  token-list path and fails on a match.
- **Private** — the token list, derived from the corpus folder names by a
  script in the overlay repo, never committed to the engine.
- **Wiring** — a `pre-push` hook in the developer's engine clone that runs the
  scanner when an overlay checkout is present, and a scheduled job in the
  private repo that scans the published tree and alerts if one ever lands.

Fail-open on a missing token list is deliberate: an outside contributor
without the overlay must still be able to push.

**Built and landed 2026-09-08 (#341, closed).**

| Piece | Where |
|---|---|
| Scanner, public, data-free, self-testing | `tests/lint-counterparty-names.py` |
| Token generator, private | `overlay/gen-counterparty-tokens.py` |
| Token list, private, 32 entries | `overlay/counterparty-tokens.txt` |
| Regression tests, 14 checks | `tests/test_counterparty_scanner_341.py` |
| Hard check on the publish path | `scripts/public-cut.sh` §4d |
| CI gate | `.github/workflows/counterparty-name-gate.yml` |
| Pre-push hook | `.pre-commit-config.yaml` (`--hook-type pre-push`) |
| Scheduled scan of the published tree | `.github/workflows/published-tree-scan.yml` |

Verified end-to-end: planting a real token in a surface file fails the gate,
the failure log prints `file:line — matches private token #N` and never the
token itself, and removing it passes again. The live public repo scans clean.

Phase 0 is therefore complete and the trunk move is unblocked.

### F3 · Personal domain on the public surface — fixed

The live deployment's personal hostname appeared in `.github/workflows/dts-image-publish.yml`
and two docs. Genericised to "the live Coolify deployment". Four
`docs/planning/` files still carry it and must be sanitised if published (D2).

### F4 · The overlay is documented but not wired

`overlay/README.md` describes the destination precisely — and nothing
implements it. No code, CI job, or runbook step reads `overlay/`.
`overlay/ENGINE_VERSION` pins `ENGINE_SHA=e72cef4`, the **second** public cut
(2026-07-26); four cuts have landed since and nothing noticed. A pin nothing
enforces is a comment.

### F5 · `#NNN` references survived a renumbering — RESOLVED 2026-09-08

The docs and code comments are dense with issue numbers, and migrating the
backlog renumbered every migrated issue (private #58–#741 became public
#19–#79). Three things keep that navigable:

- Every migrated issue carries a footer naming its original private number.
- Cross-references **between migrated issues** were rewritten to the new
  numbers (17 issues needed it).
- Everything else — references to already-closed private work, and the issue
  numbers embedded in code comments and docs — refers to the pre-migration
  history and is left alone. `AGENTS.md` says so once, so nobody chases a
  wrong public issue.

### F6 · Two public-repo first impressions

`#689` (main is red) and `#225` (not deployable from clone — placeholder
account IDs, unprovisioned secrets). Neither is a leak; both are what a first
visitor sees. See *Sequencing*.

## The overlay seam

D1 widens the overlay contract. Today `overlay/README.md` says "Engine source
does not belong here". That stays true in its important sense and gains an
exception:

- **Editing a file that also exists in the engine** — still forbidden. It is a
  divergence to reconcile at every cut. Fix it upstream and re-pin.
- **Adding an Exos-only file that imports engine constructs** — this is the
  overlay's purpose and the thing to build for.

The seams that make this work mostly exist already:

- **Infra.** `infra/cdk.context.example.json` plus
  `overlay/cdk.context.json.template` already separate identity from code, and
  `infra/lib/` is CDK TypeScript that an Exos-only stack can import. Target
  layout: `infra-exos/` in the private repo, depending on the engine's
  constructs, deployed with `--context-file overlay/cdk.context.json`.
- **Playbooks.** `playbooks/registry.json` already resolves a playbook by id
  with no code edit, which is exactly the extension point the real bundle
  needs.
- **Config.** Every deployment-specific value already resolves from context
  rather than a literal, enforced by `lint-brand-free.py`.

What is missing is the **build**: something that composes engine + overlay into
a deployable. That is the main new engineering this plan calls for.

## Sequencing

### Phase 0 — close the control gap (blocking)

1. Build the F2 counterparty detector: public scanner, private token list,
   pre-push hook, scheduled private scan of the published tree.
2. Extend `public-cut.sh` to run it, so the two paths share one control.
3. Land the F1/F3 sanitisation (done in the working tree; needs to ship).

Nothing else should start before this. It is the only item that is genuinely
irreversible if skipped.

### Phase 1 — make the public repo the trunk

4. Cut private `main` to public (seven days of work including Orbit Diner),
   through the existing reviewed-PR path.
5. Switch development to a fresh clone of the public repo. Land the AGENTS.md
   and `docs/INDEX.md` work with it.
6. **Done.** The backlog moved to the public tracker (61 issues, renumbered);
   `AGENTS.md` records how to read a `#NNN` written before the migration.
7. Publish the D2 curated planning subset, sanitised. Proposed set: the
   long-range plan, the review-workflow control inventory, and the Orbit Diner
   design chain (questions, plan, review, designer rounds, completion handoff)
   — the material a contributor can actually use. Held back: the agent-loop
   prompts (owner tooling, stale local paths and an old repo name) and the
   audit diagnostics (dense with private issue numbers).

### Phase 2 — build the overlay for real

8. Strip `exos-legal/contract-toaster` to overlay contents. History stays; the
   engine files leave the tree.
9. Wire `ENGINE_VERSION`: a check that fails when the pin is stale, and a
   documented bump procedure.
10. Stand up `infra-exos/` and the compose step, then move the corpus out of
    git into document storage.

### Phase 3 — routine

11. Grind the remaining backlog in public.

## The 85 open issues

The question was whether to grind them before cutting over. The data says
grind a defined few, not all 85.

- They are a live working backlog, not accumulated debt: 58 of 85 were touched
  in the last 30 days.
- **26 of 85 cannot be ground without you** — they carry `needs-human`,
  `needs-design`, `needs-discussion`, `legal-review-required`, `type:spike`,
  or `epic`. "Grind the backlog first" therefore has no autonomous finish line.
- The five `phase:5-cutover` issues (`#96`–`#100`) are about **production
  go-live** — production account bootstrap, custom domain, GA readiness review,
  pilot parallel-run, go-live checklist. They are not about publishing the
  repository. Do not let the shared word "cutover" couple them.
- Three `oss-launch` issues already describe this work: `#282` (the epic —
  Apache-2.0, fresh public repo cut, sanitisation inventory), `#281`
  (community playbooks repo), `#292` (wave order and standing rules).
- Under D1, some infra issues belong to the private overlay rather than the
  public trunk — notably `#96` and `#97`. Grinding them first would be
  grinding work this very plan reassigns.

**Recommended pre-publication set: `#689` and `#225`.** A red `main` and a repo
that will not run from a clone are the two things a first visitor meets.
Everything else grinds better in public, where the backlog is visible and
someone else might take a ticket.

The mock review pipeline (`#243`, `#244`) is deliberately **not** on that list.
`docs/REVIEW-GUIDE.md` already discloses it honestly, and an honest disclosure
is a better public artefact than a delayed launch.

## Parked for you

- **`Q1` The corpus in git.** It should leave version control entirely — 213
  files of real signed agreements is document management, not source control.
  Phase 2 step 10, but the destination is your call.
- **`Q2` Renaming `dts`.** The letters are a legacy container-name artefact.
  Renaming touches published GHCR image names, env defaults, and about ten
  test files, and would break the live Coolify pull. Left alone; `docs/CONTEXT.md`
  now explains the name instead.
- **`Q3` CODEOWNERS.** It routes review to two teams in the private org, which
  do not exist in `contract-opf`. It is manifest-excluded today. The public repo
  needs its own, or none.
- **`Q4` Contribution posture.** Nothing yet says whether outside pull requests
  are welcome, or what review they get. Worth deciding before the trunk moves.
