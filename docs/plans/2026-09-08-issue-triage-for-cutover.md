# Issue triage for the public cutover

8 September 2026. Every open issue in `exos-legal/contract-toaster` was read in
full and assigned. Companion to
[2026-09-08-public-cutover-plan.md](2026-09-08-public-cutover-plan.md); owner
decisions are carried separately in
[2026-09-08-owner-decisions-handoff.md](2026-09-08-owner-decisions-handoff.md),
which is written to be reviewed without the codebase.

Owner instruction: put as much as possible into the pre-publication grind, and
the rest into the private overlay.

## Result

Started at 85 open. Twenty were verifiably finished or deliberately parked
and are now closed, and one new issue was refiled. The 68 that remain are labelled in the tracker, so the
split is operational and not only written down here.

| Bucket | Label | Count |
|---|---|---|
| Pre-publication grind | `pre-pub` | 60 |
| Private overlay only | `overlay-only` | 6 |
| Post-launch refinement | `post-launch` | 2 |
| Closed in this pass | — | 20 |

All ten `owner-decision` issues were answered on 2026-09-08 and the label is
retired. The decisions and what each one changed are recorded in
[2026-09-08-owner-decisions-handoff.md](2026-09-08-owner-decisions-handoff.md);
three of them added issues (#742, #743) or moved one out of the launch path
(#684), which is why the totals moved.

## Closed: 20

Each was closed with a comment recording the evidence.

| Issue | Why |
|---|---|
| #28 | Retrieval spike — superseded; RAG is dormant by owner decision, documented with a revival playbook. |
| #48 | 2026-06-11 review remediation — 46 of 47 children already closed, the last closed here. |
| #101 | v1 phase plan — superseded twice, now by the cutover plan. |
| #185 | 2026-07 dual-repo audit epic — 57 of 64 closed; the remaining 7 are individually tracked. |
| #217 | OPF conversion gap — resolved as #278 and #279, both closed. |
| #292 | oss-launch grind plan — superseded; the launch model changed. |
| #383 | AFK run log — a journal, not a work item. |
| #405 | De-branding — withdrawn by the 2026-08-21 owner ruling; brand-free gates pass. |
| #408 | Empty-shell + develop-in-public epic — its own close-out note listed #405 as the last child. |
| #421 | Model-path robustness — 14 of 15 closed; the live smoke-eval supplied the evidence it awaited. |
| #436 | Frontend release-readiness — all 14 children closed. |
| #500 | Crumb tray — owner decision 2026-08-02: "Do not build." |
| #503 | Precedent whispers — owner-parked 2026-08-02; mechanism changed 2026-08-03. |
| #504 | "The toaster IS the Review tab" — superseded by #729; the Orbit Diner console replaced it wholesale. |
| #519 | Notes-mode — built and shipping; #648 enabled it in the deployment. |
| #689 | main is red — `main` is green at `cf005f4` across every gate. |
| #729 | Orbit Diner epic — all 20 children closed. |
| #731 | vitest collecting supplier drops — fixed; `include` pinned to `src/`. |
| #458 | MANUAL_REVIEW vs ERROR conflation — fixed by the Orbit Diner projection, which keeps both manual statuses distinct. |
| #490 | Toaster dial as contract-type selector — built; the console's dial resolves to a `playbook_id` with drag, click and arrow keys. |

## Rewritten: 2, refiled: 1

- **#341** was "allowlist export manifest + deny-scanner". The export half is
  obsolete — the path mechanism shipped as `public-cut-exclude.txt` plus
  `scripts/public-cut.sh`. Rewritten to the half that was never built and that
  the public trunk makes blocking: a **content** deny-scanner for counterparty
  names, with the token list held privately because the denylist is itself the
  secret. Raised to `priority:p0`.
- **#282** was the one-shot OSS-launch epic. Rewritten to the develop-in-public
  model, pointing at this plan set, recording what survived from the 2026-07-12
  decisions and what changed.
- **#741** is new: the two round-3 Orbit Diner follow-ups that were deferred
  inside #739 and would have been lost when #729 closed.

## How the split was drawn

`overlay-only` is deliberately narrow. It holds work that cannot exist in the
public engine because it operates on Exos's own production account, domain,
legal process, or client corpus. Everything else — including all of the
2026-09-05 audit and stability series, the whole frontend backlog, and the
security and dependency work — is engine work and belongs in the public trunk,
so it is in the pre-publication grind.

## Pre-publication grind (60)

Land before the trunk moves.

| Issue | Title |
|---|---|
| #58 | End-to-end smoke test |
| #67 | Playbook schema hardening + release-bundle governance |
| #68 | One-click release-bundle rollback + quarantine of affected reviews |
| #78 | Playbook admin UI: list, version history, diff view, draft upload |
| #85 | Reviewer UI: upload, status states, result view with provenance and critic deltas, disposition captu |
| #86 | Phase 2 acceptance gate — the real pipeline, end-to-end, on the Docker Compose target [BLOCKING GATE |
| #91 | Admin dashboard: spend ledger, pipeline health, manual-review queue, release activity |
| #93 | Audit explorer: implement the audit-queries.md catalogue as tested, efficient saved queries |
| #95 | Release operations admin UI: snapshots, bundles, activate/rollback, quarantine lists |
| #225 | Repo is not deployable from clone: placeholder account IDs, unprovisioned secrets, and a long undocu |
| #243 | afk(demo): replace the mock review lambda with the real stage + wire pipeline |
| #244 | afk(demo): failure-stage attribution + reachable manual-review statuses |
| #281 | Community playbooks: separate repo with disclaimer, provenance, and review policy (GC-owned) |
| #282 | Epic: cut over to developing in public (contract-opf trunk, exos-legal overlay) |
| #341 | cut-safety: a counterparty-name deny-scanner with a private token list (the gate no existing check p |
| #459 | Spend ledger drifts if the model selection changes mid-review |
| #501 | feat(frontend,backend): personality in the edges — burnt-toast failures (honest cause), the lifetime |
| #574 | security(backend): populate precedent_verbatim_spans so leakage-scan check 4 is enforced in producti |
| #575 | fix(frontend): layout-audit registers the global .ct-sr-only utility as one component's helper, crea |
| #576 | test(infra): nothing asserts the committed mock .docx fixture still matches its generator — silent d |
| #577 | chore(infra,tests): two stale references to the WATERMARK constant #513 deleted — a documented paylo |
| #578 | test(frontend): version-history row-action reachability is unmeasured, and the widest table was only |
| #580 | feat(llm): wire lookup_clause_evidence as a callable tool — the non-RAG drill-down the digest projec |
| #581 | spike(llm): measure an optional read-only tool with repeats, paired runs, and pre-agreed adoption th |
| #613 | fix(ci): make the source revision an explicit cache input, add the OCI revision annotation, and sepa |
| #615 | fix(redline): rationale footnote body is injected untracked — accept/reject-all leaves it orphaned |
| #617 | fix(leakage): only category 4 enforces a minimum gram length — a short Floor statement could block e |
| #640 | fix(backend): inject an explicit clock into download.py, and settle what "daily" means |
| #649 | Epic: operate the toaster without Coolify — runtime config and secret rotation in the admin UI |
| #664 | chore(loop): tell an infrastructure death apart from a ticket failure, and self-heal the first |
| #681 | feat(preflight): show 'Reviewing as' on the preflight card, before any paid pass |
| #690 | waf/poll: raise polling rate limit to 300/5min and make the review poll interval adaptive |
| #691 | backend: explicit botocore timeouts and retry modes for Bedrock and DynamoDB clients |
| #692 | reviews: add status-index GSI and remove every scan of the reviews table on live paths |
| #693 | upload: fail closed on corrupted S3 puts (ChecksumSHA256) and abort stalled submits in the SPA |
| #694 | review contract: markup_intensity as an explicit enum wire field (hard cutover from prose guidance) |
| #695 | party recognition: legal-form suffix, punctuation, diacritic and d/b/a normalisation plus a prefligh |
| #696 | frontend: code-split admin panels and the Amplify shell; enforce a bundle budget |
| #697 | frontend hosting: add HSTS, Permissions-Policy and COOP headers on Amplify and nginx |
| #698 | review: resume an in-flight review after a page reload via a sessionStorage review id |
| #699 | entity roster: stop calling create_table on the request path; provision at startup and in CDK |
| #700 | epic: 2026-09-05 audit and hardening pass (10 steps, ordered) |
| #701 | deps: replace python-jose with PyJWT, upgrade fastapi/starlette and aws-amplify to clear pip-audit a |
| #702 | runner: recover in-process reviews orphaned by a container restart on the Docker Compose target |
| #703 | python: standardise on 3.13 across CI, the container image, and local dev with a parity test |
| #704 | landing: branch-per-change with scripts/land.sh, a pre-push guard, PR-triggered CI, and self-closing |
| #705 | lint: ruff + mypy and eslint + prettier gates with a committed baseline that can only shrink |
| #706 | authz: one is_admin/require_admin in backend/src/authz.py; AST test forbids local copies |
| #707 | ddb: remove duck-typed hasattr(table, ...) fallbacks; tests use moto tables declared like CDK |
| #708 | dev qol: fail tests on unexpected console.error, check.sh --only, ignore scratch/ and dump/, move re |
| #709 | docs: scaffold docs/INDEX.md and CONTEXT.md, move historical packets to docs/reports/, derive docs-l |
| #710 | review: 'Run again' from the burnt-toast panel and History rows, prefilled from the stored review |
| #711 | review: time-remaining estimate on the progress bar from recent review durations and the preflight s |
| #712 | frontend: shared playbook catalog store so an activation is visible in every tab without reload |
| #713 | refactor: split reviews.py, main.py routes, ReviewSubmission.tsx and AdminPlaybooks.tsx by responsib |
| #714 | tests: parallel per-file runner, check.sh --only, and pytest convergence via importlib import mode |
| #715 | frontend: major-version upgrades (vite, vitest, typescript, react) after the lint gate and code spli |
| #716 | epic: 2026-09-05 stability, maintainability and QoL series (15 steps, ordered) |
| #736 | orbit-diner: automated acceptance — one real review and the eight error routes, driven by browser au |
| #741 | orbit-diner: filename head length and the 561-660px console band (two deferred #739 follow-ups) |

## Private overlay only (6)

Operates on Exos's own production account, domain, legal process, or client corpus. Never moves to the public trunk.

| Issue | Title |
|---|---|
| #96 | Production account bootstrap and cross-account signed-digest promotion |
| #97 | Custom domain and edge hardening for production |
| #98 | Exos AWS production readiness review — verify every claimed control in the real prod account |
| #99 | Exos pilot parallel-run and GC go-live decision (advisory criteria published for adopters) |
| #100 | Go-live cutover checklist and v1 close-out |
| #742 | AWS real-pipeline acceptance gate — deliberately unsatisfied until #243 lands |

## Post-launch refinement (2)

Deliberately not a launch gate, by owner decision.

| Issue | Title |
|---|---|
| #684 | post-launch: stabilise redline shape — material issues consistently identified, output always usable |
| #743 | orbit-diner: device, performance and accessibility matrix — hard gate before the hosted service is p |

## Migration, 2026-09-08

The `pre-pub` and `post-launch` buckets (61 issues) were migrated to
`contract-opf/contract-toaster` and closed here with a pointer. The 6
`overlay-only` issues stayed. The tables above therefore record the state at
triage time, before the move; the numbers in them are the **private** ones.

Two issues were sanitised before publication, caught by scanning every body
against the new counterparty gate and the other leak classes: #58 carried the
corporate email domain, and #225 carried a **real AWS account id**. Both were
redacted in the private tracker first, so the two copies agree.

## Sequencing note

`#341` is the one item that gates the trunk move. The other 59 pre-publication
items are ordinary backlog: the more of them that land first, the better the
repository reads on day one, but none of them is a leak risk, and the two
already-ordered series (#700, ten steps; #716, fifteen steps) carry their own
execution order and owner decisions.
