# Owner decisions — contract-toaster, September 2026

> **ANSWERED 2026-09-08.** All ten decisions were taken. This document is now
> the decision record: each section keeps the question and options as they were
> put, and the outcome is in *Decisions taken* below. What each decision changed
> in the tracker and the docs is listed there too.

**Read this without the codebase.** Everything needed to decide is quoted
inline. It is written to be worked through with an advisor who has no access to
the repository.

**Internal. Not for publication** — it discusses production accounts, a client
pilot, and a private document corpus. Keep it out of any public cut.

---

## What this project is

A contract-review tool. A lawyer uploads a counterparty's edited draft of an
agreement; the tool compares it against a **playbook** — the organisation's
codified negotiating positions, written as machine-readable data — and returns
either an ACCEPT decision or a Word document with tracked changes and footnoted
reasons for each one. It never gives a legal verdict; every output is framed as
a tool recommendation for an attorney to approve.

It runs on two deployment targets from one codebase: an AWS stack, and a Docker
Compose stack. The AWS path is not yet running the real review pipeline — it
still returns a pre-baked result — while the Docker Compose path runs the real
one. That gap is a deliberate, documented deferral, not an accident.

## The decision context

The project is moving from a private repository to developing in public. The
public repository already exists and is live; the private one becomes a thin
"overlay" holding what cannot be published: the real production configuration,
the real playbook, and a corpus of real signed agreements whose folder names
alone identify counterparties.

Eighty-five open issues were read and triaged. Twenty were closed as already
done. Sixty go into a pre-publication grind. Six are private-overlay-only. The
ten below need a human decision before anyone can act on them.

Each carries a recommendation. Disagreeing with it is the point of reviewing.

---

## D1 · What does the "Phase 2 acceptance gate" mean now? (issue #86)

**Situation.** A blocking gate was written early on: the full real review
pipeline must run green end-to-end against a curated set of known-answer
documents, driven from a browser, before Phase 2 counts as done.

The problem is that the sentence assumed one pipeline. There are now two. The
Docker Compose deployment runs the real pipeline today. The AWS deployment
still runs a placeholder that returns a canned result, because replacing it was
deliberately deferred past the open-source launch by an owner decision in July.

So the gate as written cannot pass, and it is not obvious that it should be
expected to.

**Options**

- **A. Scope the gate to the Docker Compose target.** It passes there today.
  Add a matching AWS gate later, attached to the deferred work that would make
  it meaningful. *Consequence:* the gate becomes true and useful now; "Phase 2
  done" no longer implies the AWS path works.
- **B. Keep one gate covering both targets.** *Consequence:* Phase 2 stays open
  until the deferred AWS work is undone, which contradicts the July decision.
- **C. Retire the gate.** Rely on the per-change test suite instead.
  *Consequence:* loses the only end-to-end, browser-driven check.

**Recommended: A.** A gate that cannot pass is not a gate. Splitting it makes
both halves honest and keeps the July deferral intact.

---

## D2 · Production readiness review (issue #98) · *overlay-only*

**Situation.** A verification sweep in which every security and data-handling
control the documentation claims is demonstrated working in the real production
environment — data residency, audit-log immutability, access fail-closed
behaviour, output-leakage blocking, legal-hold protection, alarms reaching a
real tested address, an incident rehearsal. It is a blocking gate before the
tool touches real work, and it needs legal review.

**It depends on a production environment that does not exist yet.** Standing
that up is separate work, also in the overlay bucket.

**Options**

- **A. Sequence it after production exists, and treat it as the gate before any
  real use.** *Consequence:* honest ordering; nothing real runs unverified.
- **B. Run a reduced version now against the existing non-production
  deployment.** *Consequence:* earlier signal, but it proves things about an
  environment that is not the one carrying the risk.
- **C. Fold it into the pilot (D3) as a precondition checklist.**
  *Consequence:* one gate instead of two, at the cost of making the pilot's
  start depend on a long verification.

**Recommended: A**, with the explicit note that it blocks D3.

---

## D3 · The pilot and the go-live decision (issue #99) · *overlay-only*

**Situation.** Before the tool informs real negotiations, legal runs 10–15 real
inbound agreements through it **in parallel with** normal manual review, across
at least three counterparty types including at least one where the organisation
supplied the paper. For each, the attorney records what the tool caught, what
it missed that manual review caught, and what it raised that manual review did
not. Misses become new test cases. Exit criteria are agreed with the General
Counsel **before** the pilot starts.

**The decision is the exit criteria themselves.** The issue proposes examples —
zero false ACCEPTs, a cap on false-positive findings, attorney time saved — but
they are not agreed.

Relevant evidence for setting them: see D9, which measured the tool producing
between zero and nine deletions on four runs of the *same* document with the
same settings.

**Options**

- **A. Zero false ACCEPTs as an absolute bar, plus a false-positive ceiling and
  a time-saved target.** *Consequence:* the strictest reading — one missed
  problem fails the pilot. Defensible for a legal tool; may be hard to clear.
- **B. Zero false ACCEPTs on a defined severity class only**, with lower-severity
  misses recorded but not fatal. *Consequence:* more achievable, requires
  agreeing a severity taxonomy first.
- **C. No pass/fail bar; the GC makes a judgement call on the evidence.**
  *Consequence:* fastest, but the pre-agreed criteria exist precisely to stop
  the bar moving after the results are in.

**Recommended: B.** A is the right instinct but needs the severity class to be
meaningful; C gives up the control that makes the pilot worth running.

---

## D4 · Go-live cutover checklist (issue #100) · *overlay-only*

**Situation.** The close-out: switch the team's default workflow to tool-first
review (with the attorney approval step unchanged), onboard every reviewer, hand
the operating documentation to whoever runs it day to day, purge pilot test data
from production, and schedule a recurring recertification.

**No real decision is open here** — it is a checklist that executes after D3.
It is listed so it is not mistaken for outstanding work.

**Recommended:** confirm it stays blocked on D3 and needs no separate decision.

---

## D5 · Community playbooks policy text (issue #281)

**Situation.** A playbook encodes negotiating positions. Published ones invite
adopters to treat them as advice. The decision already taken: community-
contributed playbooks live in a **separate** public repository, not the main
one, under a CC-BY 4.0 licence (chosen deliberately over a share-alike licence
so that private adaptations carry no disclosure obligation). That repository
exists.

**What is outstanding is the policy text itself**, which needs GC sign-off: the
not-legal-advice disclaimer, the statement that no attorney-client relationship
is created, per-playbook provenance requirements, and the review policy for
accepting a contribution.

**Options**

- **A. GC drafts the disclaimer and review policy; engineering builds the
  scaffolding around it.** *Consequence:* correct division; gated on GC time.
- **B. Engineering drafts from standard open-source practice; GC edits.**
  *Consequence:* faster start, and a lawyer reviewing a draft is usually
  quicker than a lawyer facing a blank page.
- **C. Accept no community contributions for now**; publish only the
  organisation's own examples. *Consequence:* removes the question entirely
  until there is demand.

**Recommended: C now, B when demand appears.** There is no contribution queue
yet, and an unused policy still has to be maintained and honoured.

---

## D6 · Measure what an optional model tool costs (issue #581)

**Situation.** The tool sends the model a compressed summary of the playbook
rather than its full text, because the full evidence measured about a million
tokens on a real corpus and cannot fit. A "look up the full text of this one
clause" function was designed as the way back to detail. Two thirds of it are
built. What is missing is letting the model actually call it.

There is one existing measurement of tool-calling on this stack, and it was
unfavourable: **50% valid output versus 100%, +30% cost, +68% tokens.** But it
measured something structurally different — *forcing* the entire review to come
back through a tool call. The proposal here is an *optional* lookup the model
may call zero or more times, with the review returned normally. The earlier
failure mode may not apply, or there may be a new one: extra round trips.

The proposed experiment: same documents, same playbook, same model, varying only
whether the lookup is offered. At least five documents. Measure validity, tokens,
wall-clock, cost, how often the model calls it, and whether what it fetched
changed the proposed replacement text.

**Options**

- **A. Run the measurement, then decide.** *Consequence:* costs a small amount
  of real model spend and some time; produces evidence rather than an argument.
- **B. Skip it and build the tool loop anyway.** *Consequence:* faster; risks
  repeating a known-bad pattern.
- **C. Drop the tool-calling idea; keep the compressed summary alone.**
  *Consequence:* no new cost or complexity; accepts that the model cannot reach
  detail it might need.

**Recommended: A.** The whole reason the question is open is that the one number
anyone has does not transfer.

---

## D7 · A stale version stamp on the live deployment (issue #613)

**Situation.** The live deployment reports the wrong version. After a deploy
that genuinely shipped new code, the running container still reported the
*previous* deploy's version stamp, while genuinely running the new image.

Two explanations were ruled out with direct evidence from build logs — the
build arguments were correct, and the deployment configuration overrides
nothing. The actual cause was read from the build log: the build cache reused
every layer including the ones that bake in the version, so a correct new value
was passed to a build that never rebuilt those layers.

**Why it needs a person:** it was judged not verifiable offline. Confirming a
fix means doing a real deploy and reading what the live service reports.

**Options**

- **A. Fix the caching so version layers always rebuild, and verify on the next
  real deploy.** *Consequence:* correct; slightly slower builds; confirmation
  waits for a deploy that was going to happen anyway.
- **B. Stop baking the version into the image; read it at runtime instead.**
  *Consequence:* removes the failure class permanently rather than patching it;
  larger change.
- **C. Accept it and remove the version display.** *Consequence:* cheapest;
  loses the ability to tell what is actually running, which matters during an
  incident.

**Recommended: B.** A fixes this instance; B makes the bug impossible. The
version display earns its keep during incidents, so C is a real loss.

---

## D8 · A low-confidence clock-handling spike (issue #640)

**Situation.** One module reads the system clock through a different mechanism
than every other module, and specifically not through the one the tests can
control. It enforces a **daily** download limit — behaviour whose interesting
cases are all at day boundaries.

The person who filed it froze the clock at eight instants including month
rollover, year rollover, a daylight-saving transition, and a leap day. All eight
passed identically. As they noted, that is consistent with two very different
things: the logic is genuinely correct, **or** the test never saw the frozen
clock at all because the code reads a different source.

They explicitly did not claim a defect, and flagged that their own harness was
subject to the same problem.

**Options**

- **A. Change the module to read the clock the same way as everything else, then
  re-run the boundary tests.** *Consequence:* small, cheap, and settles the
  question — the tests either still pass (correct all along) or now fail
  (a real bug found).
- **B. Investigate first, change second.** *Consequence:* more rigorous,
  noticeably more effort for the same end state.
- **C. Close it.** *Consequence:* accepts an untested day-boundary in a limit
  that governs document downloads.

**Recommended: A.** The consistency fix is worth making regardless, and it
converts an unanswerable question into a test result for almost no cost.

---

## D9 · The same document produces very different redlines (issue #684)

**This is the most consequential decision here.**

**Situation.** The same real agreement, the same settings, run four times,
produced these results:

| Run | Decision | Insertions | Deletions | Findings |
|---|---|---|---|---|
| 1 | REQUEST_CHANGE | 1 | 1 | 2 |
| 2 | REQUEST_CHANGE | 2 | 9 | 2 |
| 3 | REQUEST_CHANGE | 0 | **6** | 1 |
| 4 | REQUEST_CHANGE | 0 | **0** | 2 |

Deletion counts across four identical runs: **0, 1, 6, 9.**

Two things are stable and one is not. The *decision* was the same every time.
The *number of findings* stayed in a narrow band, 1–2. What varied wildly was
**how much text an edit touches**. On a synthetic test document the same
measurement stayed in a 1–3 band, so this is not a general property of the
system — it is much wider on real paper.

**The specific product concern.** Run 3 produced six deletions and zero
insertions: a redline that strikes the counterparty's language and proposes
nothing in its place. Occasionally that is right — some clauses should simply
not exist. Usually it is not: a counterparty who receives text struck through
with no replacement has nothing to accept, and the negotiation stalls.

Run 4 produced no edits at all while still saying REQUEST_CHANGE.

**This is a decision about what the product should do, not a bug with an obvious
fix.** It is also currently invisible to the quality scoring, which measures
whether the right issues were identified, not how the edits are shaped.

**Options**

- **A. Require a replacement for every deletion.** Any edit that removes text
  must propose language, unless the finding is explicitly of a
  "delete-this-clause" type. *Consequence:* the strongest guarantee that output
  is usable in a negotiation; needs the playbook to distinguish the two cases.
- **B. Leave the model free, but flag all-deletion output for attorney
  attention** before it is sent. *Consequence:* less rigid, keeps a human in the
  loop where it matters; does nothing about the underlying variance.
- **C. Reduce the variance directly** — lower the sampling temperature, or run
  twice and reconcile. *Consequence:* attacks the cause rather than the symptom;
  costs more per review, and consistency is not the same as correctness.
- **D. Accept it.** The attorney reviews everything anyway. *Consequence:* no
  work; the tool's output quality stays unpredictable on the documents that
  matter most.

**Recommended: A plus B.** A makes the common case right by construction; B
catches what A cannot anticipate. C is worth measuring separately but should not
be the primary answer — making a system reliably produce the same mediocre
redline is not the goal. This should also become an explicit criterion in the
pilot (D3).

---

## D10 · Attended acceptance testing on real hardware (issue #736)

**Situation.** The review interface was rebuilt to a new design. Its automated
tests all pass, but a specific list of things cannot be proven by automated
tests running in a simulated browser: one real review from upload to finished
document; eight distinct error routes (wrong file format, oversized file,
malformed document, no playbook configured, daily limit reached, a polling
failure, a cancel-versus-complete race, a failed download); animation smoothness
against a stated performance budget on a real desktop **and** a mid-range phone;
a real screen reader on Windows; and Safari on iOS.

This is irreducibly a person operating a real deployment on real hardware.

**Options**

- **A. Do it before the public cutover.** *Consequence:* the interface is proven
  before strangers see it; costs a focused session across several devices.
- **B. Do it after, treating it as launch-week work.** *Consequence:* removes a
  serial dependency from the cutover; accepts that the first public visitors may
  find something.
- **C. Split it** — the eight error routes and one real review before, the
  performance trace and assistive-technology passes after. *Consequence:*
  covers what would actually embarrass, defers what needs specific hardware.

**Recommended: C.** Correctness before strangers arrive; the device matrix can
follow.

---

## Decisions taken

| # | Decision | Outcome | Landed as |
|---|---|---|---|
| D1 | Phase 2 acceptance gate | **A**, with milestones renamed so it cannot read as weakening the promise | #86 scoped to Docker Compose; milestone renamed "Phase 2 — Docker Compose real-pipeline acceptance"; new milestone "AWS real-pipeline acceptance — deferred" and #742, deliberately unsatisfied; `README.md` and `docs/REVIEW-GUIDE.md` corrected |
| D2 | Production readiness review | **A**, and it does not hold up publication. Per-implementation, so the public repo ships a **template**, not an issue | `docs/adopters/production-readiness-review.md`; #98 narrowed to the Exos AWS deployment |
| D3 | Pilot exit criteria | **C** — no bar imposed by the repo — but the criteria written down and published as **advisory** | `docs/adopters/pilot-and-validation-guide.md`; #99 narrowed to the Exos pilot |
| D4 | Go-live checklist | Confirmed: an execution checklist, no product decision outstanding | #100 annotated |
| D5 | Community playbook policy | Claude drafts; **accepted on creation unless the GC objects** | `docs/adopters/playbook-publication-policy.md`; #281 annotated. Not yet pushed to `contract-opf/playbooks` |
| D6 | Optional model tool cost | **A**, with a much stronger design — repeats, paired runs, pre-agreed thresholds | #581 rewritten |
| D7 | Stale version stamp | **A**, plus real artifact provenance: OCI `image.revision`, revision as an explicit cache input, artifact identity separated from deployment identity | #613 rewritten |
| D8 | Clock-handling spike | **A**, as an **injected clock**, and settle what "daily" means (default UTC) | #640 rewritten |
| D9 | Redline variance | Goal is stable issue identification and usable output, not identical wording — and it is **post-launch**, not a launch gate | #684 relabelled `post-launch`; sequencing note added to the adopter pilot guide |
| D10 | Attended acceptance | **C**, and **automated** rather than human-in-the-loop. "Public cutover" means publishing source, not exposing a hosted interface | #736 rewritten to browser-automated functional routes (pre-publication); #743 split out for the device/accessibility matrix, gated on public hosting |

## What changed in the buckets

Three decisions moved work out of the launch path rather than into it: D9 made
redline variance a post-launch goal, D10 separated source publication from
hosted-service exposure, and D2/D3 converted two internal gates into public
adopter guidance. Two decisions added issues (#742, #743) whose purpose is to
stay visibly open.

The `owner-decision` label is retired.
