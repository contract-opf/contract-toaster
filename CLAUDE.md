# contract-toaster — start here

## Read this first

**[docs/plans/2026-09-08-orbit-diner-completion-handoff.md](docs/plans/2026-09-08-orbit-diner-completion-handoff.md)** —
current state, what is verified, what is still open, and the two hand steps the
new auto-deploy needs. It is written to be read cold, with no other context.

Architecture is `ARCHITECTURE.md`; operating procedures are `RUNBOOK.md` (note
its published copy is scrubbed — placeholder account IDs and `company.com`).
Reach both through [docs/INDEX.md](docs/INDEX.md), which lists their anchors.

[AGENTS.md](AGENTS.md) is the harness-neutral version of this contract and
carries the docs-maintenance rules; everything below still applies to you.

## Hard rules

- **Never edit `frontend/vendor/orbit-diner/`.** It is the designer's source of
  record and the baseline the next supplier patch verifies its before-hashes
  against. Our copy — compiled, tested, audited — is `frontend/src/orbit-diner/`.
  The patch procedure is in that vendor directory's own `README.md`.
- **No new `localStorage` or `sessionStorage` key.** `security-posture.test.tsx`
  must keep passing untouched.
- **The app owns state.** The Orbit Diner console fetches, polls, persists,
  submits and logs nothing; it calls existing guarded handlers.
- **Escaped text only** — no clause text on the receipt, no raw server exception
  in the status window, no document excerpt in a message.
- **No auto-deploy on the AWS path.** `ci-pipeline.yml`'s security invariant 1
  (deliberate, cosign-verified, audited promotion; `autoDeploymentsEnabled=false`
  pinned in AppStack, #55) stands. The DTS/Coolify path deliberately differs —
  `ARCHITECTURE.md` says which invariant governs which target.
- **No co-author trailers in commit messages.**

## Gates

```
bash scripts/check-frontend.sh          # tsc + vite build + vitest + CTDS audits
bash scripts/check.sh                   # the Python suite, docs-lint, detectors
python3 tests/lint-brand-free.py        # no private-org ref in a pull path
python3 tests/lint-counterparty-names.py # no counterparty identity on the public surface
```

The counterparty gate (#341) is **fail-open**: with no private token list
reachable it SKIPS. That is deliberate, so a contributor without the overlay is
not blocked. `check.sh` picks it up automatically. The backstop that scans
this already-published tree daily lives in the private origin's
`published-tree-scan` workflow, the only place the token list exists — it
cannot run here and is deliberately absent from this repo.

The 14px type floor (#600) and the `minmax(0, …fr)` rule (#457) are enforced by
`npm run audit:layout`, inside `check-frontend.sh`.

## A green gate is necessary, not sufficient

`frontend/vitest.config.ts` runs jsdom with `css: false`, so **no gate here can
see a stylesheet.** Two real defects shipped through green suites this way:
#727 deleted the only declarations of the `.toaster-receipt*` rules while the
component using them stayed live, and #739 orphaned the `.docx` extension onto
its own line past nine `endsWith('.docx')` assertions. Read the diff for
anything touching CSS.

## Docs

Read [docs/INDEX.md](docs/INDEX.md) first — one line per document with a
one-sentence scope, its section anchors, and the code it covers. Jump to the
exact section; do not scan the tree.

Keeping that index true is part of every change. The full contract, including
what to run on a machine that does not have the `docs-sync` skill installed,
is [AGENTS.md](AGENTS.md) — read it alongside this file. The short version:

```bash
python3 tools/docs_sync.py update --staged   # before committing code
python3 tools/docs_sync.py project --write   # after adding or moving a doc
python3 tools/docs_sync.py audit             # must exit 0 before you are done
```
