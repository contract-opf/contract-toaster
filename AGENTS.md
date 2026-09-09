# AGENTS.md — the contract for any agent working in this repo

Harness-neutral. Claude Code, Codex, Cursor, Gemini CLI, or a human: this file
applies to all of them, on any machine and any account. Nothing here depends on
a skill, plugin, or dotfile being installed on your machine — everything it
tells you to run is checked into this repo.

## Read order

1. `docs/INDEX.md` — the projection. One line per document: path, a
   one-sentence scope, its section anchors, and the code globs it covers.
   **Read it first and jump to the exact section.** Do not scan the doc tree.
2. `CLAUDE.md` — the hard rules (what you must never edit, never add, never
   auto-deploy) and the gate commands. They are not repeated here; read them
   before you touch code, whatever harness you are running.
3. `docs/CONTEXT.md` — domain vocabulary. Extend it when a term gets pinned
   down in conversation.

`ARCHITECTURE.md` is the system description; `RUNBOOK.md` is the operating
procedures. Both are large — reach them through their `docs/INDEX.md` anchors.

## Your obligation to docs/INDEX.md

The index only works if it is true. Keeping it true is part of every change,
not a follow-up task.

**Before you commit a change that touches code:**

```bash
python3 tools/docs_sync.py update --staged
```

It prints the docs whose `covers:` globs match what you staged. For each one:
open it at the anchors it lists, read the change, and edit the doc. Then update
that doc's `docs/INDEX.md` line if the change altered its scope, its headings,
or the code it covers. Stage the doc edit, the INDEX line, and the code
together — **one commit, not a follow-up.**

**When you add, move, rename, or delete a `*.md` file under a configured root:**

```bash
python3 tools/docs_sync.py project --write
```

New files arrive with an auto-derived scope ending ` (auto)`. That suffix means
"a script wrote this, no human has checked it". Read the file and replace the
scope with a real one-sentence description; the suffix disappears when you do.
`project` never re-derives a scope you have written, and never invents a
`covers:` line — you add those by hand for docs that describe specific code.

One gotcha when editing `docs/INDEX.md` by hand: any line in the preamble that
starts `` - `something` — `` is parsed as an index entry, not as prose, and the
whole config block stops applying. Lead such bullets with a word instead.

**Before you call the work done:**

```bash
python3 tools/docs_sync.py audit
```

Exit 0 and no findings, or the change is not finished. Every finding names its
file and what is wrong with it; these are the ones you will actually hit:

| Code | What to do |
|---|---|
| `F-AUTO-SCOPE` | Read the doc, write a real scope, drop the ` (auto)` |
| `F-DOC-MISSING` / `F-DEAD-LINE` / `F-STALE-ANCHOR` | `project --write` |
| `F-LINK-BROKEN` | Fix the link — usually re-basing `../` after a move |
| `F-COVERS-EMPTY` | The glob matches no tracked file: fix it or drop it |
| `F-CODE-UNCOVERED` | Add a `covers:` glob to the doc describing that code |
| `F-LAYOUT` | `python3 tools/docs_sync.py scaffold` |

## Where new documents go

`docs/adr/` decisions (one resolved choice with its rationale, numbered,
`0000-template.md` is the template) · `docs/plans/` approved plans ·
`docs/reports/` reviews, audits, findings, sweeps.

Plans and reports are permanent. Never write them to `/tmp` or
`/private/tmp` — macOS purges those after a few days idle — and never delete
one because it is finished. Superseded is a note at the top, not a `rm`.

## Two repo-specific traps

**A push here is a publication.** This repository is the development trunk and
it is public. There is no scrub step between your commit and the world — that
model retired on 2026-09-09, along with the cut script that implemented it.
Two consequences:

- **Never write a counterparty name** into code, a document, a commit message,
  or an issue. `tests/lint-counterparty-names.py` guards this, but it is
  **fail-open**: the denylist is itself sensitive and lives in a private
  overlay, so with no token list reachable the gate SKIPS and passes. On your
  machine it will almost certainly skip. It is a backstop, not a substitute for
  not writing the name. A scheduled job with the list scans this tree daily.
- `tests/lint-brand-free.py` hard-fails on a pull path, repo URL, or team
  reference pointing at the private origin. Naming that org in prose is fine.

Both gates scan `git ls-files` minus `public-cut-exclude.txt`, so **a file you
have not staged is invisible to them.** Stage first, then run the gate — an
untracked file passing locally and failing in CI is the most common way to be
surprised here.

**`frontend/vendor/orbit-diner/` is read-only and deliberately unindexed.** It
is the designer's source of record and the baseline the next supplier patch
verifies its before-hashes against. Its docs (`INTEGRATION.md`,
`ACCEPTANCE.md`, `CONTROL-COVERAGE.md`, `MOTION-AND-SOUND.md`,
`ART-DIRECTION.md`) are worth reading and must not be edited or re-scoped by
us — that is why they are in `exclude:` rather than in the index. The patch
procedure is in that directory's own `README.md`.

## Where the tooling lives

`tools/docs_sync.py` is a **vendored copy** of the `docs-sync` skill's script,
committed so this contract works with no skill installed, on any machine or
account. It is stdlib-only Python 3 — nothing to install — and takes `--repo` /
`--index` *after* the subcommand, never before it.

`tools/docs_guard.py` is the matching pre-commit guard, wired up in
`.pre-commit-config.yaml`. It is a convenience, not the contract: it warns when
staged code touches a documented area. `pre-commit` is deliberately **not** in
`requirements-dev.txt` (that file is the pinned, CI-reproducible gate set), so
the guard is opt-in per machine:

```bash
pre-commit install   # optional, once per clone, needs pre-commit on PATH
```

The three `docs_sync.py` commands above are the obligation whether or not you
run the guard.

If you are on a machine that has the skill (`~/Dev/Skillz/docs-sync`), the
upstream copy is the source of truth and `/docs-sync` drives the same
workflows. Refresh both vendored copies from it:

```bash
cp ~/Dev/Skillz/docs-sync/scripts/docs_sync.py tools/docs_sync.py
python3 ~/Dev/Skillz/docs-sync/scripts/docs_sync.py install-hook
```

Two things follow from `docs_sync.py` resolving the canonical guard as its own
sibling, and they bite silently if you do not know them:

- **`install-hook` only works from the skill copy.** Run from
  `tools/docs_sync.py` it looks for a canonical guard at `tools/docs_guard.py`
  — itself — and either crashes or copies the file over itself.
- **`F-GUARD-STALE` is inert from the vendored copy**, for the same reason: it
  byte-compares `tools/docs_guard.py` against its own sibling and always passes.
  Only an `audit` run from the skill copy can actually detect guard drift. If
  you do not have the skill, you cannot check it — that is fine, `audit` is
  still authoritative for every other finding.

If you do not have the skill, use the vendored copy for `update`, `project`,
`audit` and `scaffold` and change nothing else.

## Reading an issue number

The backlog moved from the private tracker to `contract-opf/contract-toaster`
on 2026-09-08, and everything that moved was renumbered — private #58–#741
became public #19–#79. Each migrated issue carries a footer naming its original
number, and references between migrated issues were rewritten.

**A `#NNN` you find in code comments, in `docs/`, or in a closed issue almost
certainly predates the migration** and refers to the old private numbering. It
is history, not a pointer: read it as a breadcrumb, and do not assume the
same-numbered public issue is related. Only the 6 Exos overlay issues still
live in the private repo.

## Commits

No co-author trailers, and no attribution lines of any kind.
