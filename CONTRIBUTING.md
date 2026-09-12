# Contributing — start here

Written to be read cold. Everything below was run from a clean clone on
2026-09-09; where a command has a trap, the trap is named.

## What this is

A contract-review tool. A lawyer uploads a counterparty's edited draft; the
tool compares it against a **playbook** — an organisation's negotiating
positions, written as machine-readable data — and returns either an ACCEPT
decision or a Word document with tracked changes and footnoted reasons. It
never gives a legal verdict; every output is framed as a recommendation for an
attorney to approve.

This repository is the **development trunk**, and it is public. It became the
trunk on 2026-09-09; before that it was a periodic sanitised copy of a private
repository. A private overlay still exists and holds what cannot be published,
but you do not need it and nothing here should require it.

## Setup

```bash
git clone https://github.com/contract-opf/contract-toaster.git
cd contract-toaster
```

**Use Python 3.13.** This is not a preference: it is what the root
`.python-version` and `pyproject.toml` declare, what every workflow's
`python-version:` pins, and what `deploy/dts/backend.Dockerfile` ships
(`tests/test_python_version_parity_63.py` fails if those four drift apart).
Do not reach for the newest interpreter instead — on 3.14 `pip install` fails
outright building `pydantic-core`.

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt -r backend/requirements.txt
cd frontend && npm ci && cd ..
```

Verified on Node 26.7 / npm 11.19; nothing here pins a Node version yet.

## The gates

```bash
SKIP_INFRA=1 bash scripts/check.sh   # Python suite, docs-lint, detectors
bash scripts/check-frontend.sh       # tsc + build + vitest + design-system audits
```

Both pass from a clean clone. Drop `SKIP_INFRA=1` when you touch `infra/` — it
skips 24 CDK-synth tests that take much longer and need `cd infra && npm ci`.

Individual gates worth knowing:

```bash
python3 tests/lint-brand-free.py          # no private-org reference in a pull path
python3 tests/lint-counterparty-names.py  # no counterparty identity, ever
python3 tools/docs_sync.py audit          # docs/INDEX.md still true
```

## Four things that will bite you

**1 · A push is a publication.** There is no scrub step between your commit and
the world. Never write a real counterparty name into code, a document, a commit
message, or an issue.

**2 · The counterparty gate is fail-open, on purpose.** Its denylist is itself
sensitive and lives in the private overlay, so on your machine
`lint-counterparty-names.py` will report `SKIPPED (no token list)` and exit 0.
That is correct — a contributor without the overlay must still be able to run
the checks. It is a backstop, not a substitute for rule 1. A scheduled job that
does hold the list scans this tree daily.

**3 · Both gates scan `git ls-files`, so an unstaged file is invisible to
them.** Stage first, then run the gate. A new file passing locally and failing
in CI is the single most common surprise here — it happened twice during the
cutover, once to the counterparty scanner's own documentation.

**4 · `main` is not protected.** The plan this organisation is on does not offer
branch protection (the API returns 403), so nothing stops a direct push. Work on
a branch and open a pull request anyway; CI runs on both. Issue #64 tracks the
interim guard.

## Where to start

67 open issues. They are not a flat pile — two of them are ordered series that
carry their own sequencing and their own recorded owner decisions.

**Security first, regardless of order** — #20, #35, #57, #61, #66.

**Then the two ordered series.** Work them in the order their epics list, not by
number:

- **#60** — the 2026-09-05 audit and hardening pass, 10 steps: #50 → #59.
  Rationale, evidence and owner decisions:
  `docs/reports/2026-09-05-audit-hardening-diagnostic.md`.
- **#76** — the 2026-09-05 stability, maintainability and QoL series, 15 steps:
  #61 → #75. Rationale:
  `docs/reports/2026-09-05-stability-maintainability-qol-diagnostic.md`.

Each step names the file, often the line, and the intended change. Several
depend on an earlier step landing — the epics say which.

The remaining ~37 are ordinary feature, bug and chore work with no imposed
order.

## Landing a change

- Branch, then pull request. Conventional commits: `feat:`, `fix:`, `chore:`,
  `docs:`, `refactor:`, `test:`.
- Run both gates before you push. Stage first (see trap 3).
- **Update the docs in the same commit.** `docs/INDEX.md` is a projection: one
  line per document, with its scope, section anchors and the code it covers.
  `python3 tools/docs_sync.py update --staged` prints which documents your
  change affects; `audit` must exit 0 before you are done.
- No co-author trailers.

## Reading the history

Documents under `docs/plans/` and `docs/reports/` include planning history
published from the private repository. Each carries a note saying so. **Issue
numbers in them predate a tracker migration on 2026-09-08** — the backlog moved
here and was renumbered, so an old `#NNN` is history, not a link. Repository
paths and commands in those documents may name the private origin for the same
reason.

`AGENTS.md` is the full working contract, and applies to humans as much as to
coding agents. `docs/INDEX.md` tells you where everything else is.

## What you cannot do here

Deploy the maintainer's AWS environment. That needs the private overlay's real
account values. Everything else — the whole engine, both deployment targets'
code, every test and gate — is here.

Note the two deployment targets differ today: the Docker Compose target runs the
real review pipeline; the AWS target still runs a mock review stage. That gap is
deliberate and tracked by its own milestone. `docs/REVIEW-GUIDE.md` has the
detail.
