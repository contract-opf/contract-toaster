# Stability, maintainability and quality-of-life diagnostic — 2026-09-05

> *Published from the project's planning history on 2026-09-09. This is a
> record of what was decided at the time, not current instructions. Issue
> numbers predate the tracker migration of 2026-09-08 and refer to the old
> private numbering, and any repository path or command shown may name the
> private origin this work was done in. Read both as history, not as links
> to follow.*

> **Status note, 2026-09-11.** G2 / B2 **landed** as public issue
> `contract-opf/contract-toaster#61` (private #701 in the table below):
> `python-jose[cryptography] 3.3.0` is replaced by `PyJWT[crypto] 2.14.0`
> across `backend/src/auth.py` (Cognito RS256) and
> `backend/src/demo_auth.py` (demo HS256 session tokens), both pinning
> `algorithms=` explicitly; `fastapi` 0.115.6 -> 0.141.1 (starlette 0.41.3 ->
> 1.6.0), with `uvicorn`, `pydantic` and `boto3` moved to their current pins;
> `aws-amplify` 6.18.0 -> 6.20.0 and `@aws-amplify/ui-react` 6.15.4 -> 6.15.6
> clear the `fast-xml-parser` HIGH. `pip-audit -r backend/requirements.txt -r
> requirements-dev.txt` and `npm audit --omit=dev --audit-level=high` both
> report clean, and `ecdsa` is gone with `python-jose`. Pinned by
> `tests/test_auth_jwt_pyjwt_61.py` (happy path plus `alg: none`,
> HS256-signed-with-the-RSA-public-key, wrong `aud`, expired and unknown
> `kid`, all asserted as HTTP 401 from `get_current_user`) and by the new
> weekly `.github/workflows/dependency-audit.yml`. The finding text below is
> left as written; it describes the state before that change.

> **Status note, 2026-09-12.** G1 / B1 **landed** as public issue
> `contract-opf/contract-toaster#62` (private #702 in the table below).
> `backend/src/runner_recovery.py::recover_orphaned_reviews` runs from
> `main.py::_lifespan` on the Docker Compose target, relabels each orphaned
> review `ERROR` / `reason=runner_restarted`, settles its reservation through
> `reviews.settle_reservation_for_cancel` and writes an audit row with
> `action=review_runner_restarted`; the shutdown half drops the pool
> (`wait=False, cancel_futures=True`) and records a cancel intent for every
> id in `pipeline_runner.in_flight_review_ids()`.
>
> **One correction to B1 as written below.** B1 says to select rows "whose
> `execution_arn` is absent". On the shipped code that selects nothing:
> `reviews.ensure_execution_started` records the ARN its client returns onto
> the reviews row for BOTH targets, and the in-process client returns the
> pseudo-ARN `inprocess:<execution-name>` — so a started in-process review
> DOES carry an `execution_arn`. The implemented selector is the predicate
> `reviews.stop_running_execution` already applies to the same field
> (`reviews.is_step_functions_execution`): recover a non-terminal row whose
> execution is not a Step Functions one, which covers both the absent-ARN
> case B1 named and the `inprocess:` case it missed. Also: B1's boot-time
> read no longer needs the "bounded scan" concession, because the
> `status-index` GSI it made conditional on private #692 has since landed as
> public `#52`. Pinned by `tests/test_runner_recovery.py`. The finding text
> below is left as written; it describes the state before that change.

> **Status note, 2026-09-12.** G3 / B3 **landed** as public issue
> `contract-opf/contract-toaster#63` (private #703 in the table below), on
> **3.13**, not the 3.12 B3 proposes below: owner decision Q5 chose the
> current stable rather than the tag the image happened to ship. A root
> `.python-version` (`3.13`) and `pyproject.toml`
> (`requires-python = ">=3.13,<3.14"`) declare it,
> `deploy/dts/backend.Dockerfile` builds `FROM python:3.13-slim`, and the 28
> `python-version:` pins across the 21 workflows that carry one were moved to
> `3.13` in one commit — five of those pins written as
> `${{ env.PYTHON_VERSION }}`, defined in `ci-pipeline.yml` and
> `dependency-audit.yml`. The agreement is pinned by a new
> `tests/test_python_version_parity_63.py` rather than by additions to
> `tests/test_ci_env_parity_639.py` as B3 suggests: that file's scanner walks
> `run:` step bodies, which is the wrong shape for a `with:` key, so the new
> file reuses its `WORKFLOWS_DIR` and reporting helpers and adds its own pin
> resolver. `scripts/check.sh` now prints the interpreter it actually
> activates and warns — does not fail — when it disagrees with
> `.python-version`, because the local venv here is still 3.11. The AWS
> `backend/Dockerfile` is out of scope and still says `python:3.12-slim`.
> The finding text below is left as written; it describes the state before
> that change.

> **Status note, 2026-09-12.** G8 / B8 **landed** as public issue
> `contract-opf/contract-toaster#64` (private #704 in the table below), as the
> local interim control owner decision Q6 chose: branch protection and rulesets
> still answer 403 on this repository's plan.
> `scripts/land.sh` refuses to run on `main`, runs `npm test`,
> `scripts/check.sh` (`SKIP_INFRA=1` unless the branch touches `infra/`, and
> its exit code read directly, so 2/FLAKY and 3/lock-busy both stop the
> landing) and `tests/lint-brand-free.py`, then pushes and runs
> `gh pr create --fill`. `.githooks/pre-push` refuses a push to
> `refs/heads/main` unless `LAND_TO_MAIN`, `CI` or `CONTRACT_TOASTER_LOOP` is
> set, and `scripts/setup-hooks.sh` installs it — **opt-in**, never run by a
> gate or an npm lifecycle script, because the autonomous loop lands by direct
> push to `main` by design. A `close-main-red` job in `ci-pipeline.yml` closes
> any open `ci-main-red` issue on a green push to `main`, commenting with
> `GITHUB_SHA`. Repository issue #1 (the `#689` named below, renumbered by the
> 2026-09-08 migration) is **deliberately left open**: B8 and this issue both
> assumed it was stale, but `main` is red again as of 2026-09-12 — the
> `CI pipeline` runs at `692b951`, `b752ebe` and `2e7762c` all failed the
> frontend gate at `npm ci` with `Missing: @esbuild/<platform>@0.28.2 from lock
> file` — `frontend/package-lock.json` still resolves `esbuild` to `0.21.5` and
> carries only that version's 23 `@esbuild/*` entries, so it is out of sync
> with `frontend/package.json`. That is a separate defect, out of scope here. Closing an issue that reports a live breakage would be wrong; the new
> `close-main-red` job closes it on the next green push to `main`, which is the
> mechanism B8 asked for. B8's remaining half — requiring the `CI pipeline` and
> `brand-free-gate` checks — waits on the plan, with the reminder in
> RUNBOOK.md's "Deploying a code change". Item 3 of the issue needed no change:
> every gate workflow already ran on `pull_request`; the two that do not
> (`dts-image-publish.yml`, chained off a completed run, and
> `dependency-audit.yml`, a weekly cron) are not gates. Pinned by
> `tests/test_landing_flow_64.py`, which drives `land.sh` and the hook as real
> subprocesses in a throwaway `git init` repository. The finding text below is
> left as written; it describes the state before that change.

> **Status note, 2026-09-12.** G5 / B5 **landed** as public issue
> `contract-opf/contract-toaster#66` (private #706 in the table below).
> `backend/src/authz.py` now holds the only `is_admin(row)` /
> `require_admin(row, detail)` in the backend, and the 17 local definitions
> the finding counted are gone from the twelve modules that carried them.
> Two departures from B5 as written. First, the predicate is
> `row.get("is_admin", False) is True`, not the `bool(...)` every copy used:
> a hand-edited or break-glass row carrying `"false"` or `1` used to count as
> an admin and no longer does. Every writer of the attribute persists a real
> boolean — `users.update_user` rejects a non-bool with 400,
> `demo_auth.add_user` coerces with `bool(...)`, `demo_auth.seed_demo_users`
> writes the `SEED_USERS` literals — so nothing legitimate changed status.
> Second, `download.py` keeps a module-level `from src.authz import is_admin
> as _is_admin` alias, because `tests/test_download_auth_attack.py` binds and
> asserts on that private name; an import binding is not a `def`, so the AST
> rule still sees one definition. Behaviour is otherwise neutral: the 403
> detail strings stayed per call site and byte-identical, including the two
> variants that built or hard-coded their own (`admin_dashboard._require_admin`
> took a `what` fragment, `audit_queries._require_admin` took no argument at
> all). Pinned by `tests/test_authz_single_source.py`, which AST-walks every
> module under `backend/src`, pins the truth table, and drives the three real
> users-table writers against moto to prove the persisted flag is a real
> `bool`. The finding text below is left as written; it describes the state
> before that change.


Second sweep of the day, following `2026-09-05-audit-hardening-diagnostic.md`
(F1–F14, issues #690–#700). This one covers what makes the project hard to
keep running, hard to change, and tedious to use. Codes continue the series:
findings `G`, actions `B`, questions `Q`.

Evidence gathered at `01a58e9`: CI runs, open issues, dependency audits,
grep counts, and the local gates (frontend 962 tests green, Python gate ALL
GREEN, brand-free PASS).

## Priority summary

| Code | Area | Severity | One line |
|---|---|---|---|
| G1 | Stability | **P1** | Docker Compose target has no recovery for in-process reviews killed by a container restart: RUNNING rows stay RUNNING forever, the spend reservation is never settled, and the concurrency slot is gone. The AWS target has an orphan reconciler; the DTS target has nothing. |
| G2 | Stability | **P1** | Pinned backend deps carry known vulnerabilities: `python-jose 3.3.0` (PYSEC-2024-232/233, PYSEC-2025-185, algorithm-confusion and DoS in JWT parsing, the SSO auth path), `starlette 0.41.3` via `fastapi 0.115.6` (nine advisories incl. multipart DoS, the upload path), `ecdsa 0.19.2`. Frontend: `fast-xml-parser` high via `aws-amplify`. |
| G3 | Maintainability | P2 | Runtime version drift: CI runs Python 3.11, the container image ships 3.12, local dev is 3.14, and nothing declares `requires-python`. Same class of bug as issue #654 (Node flag) waiting to recur. |
| G4 | Maintainability | P2 | No linter, formatter, or type checker in CI for Python or TypeScript. `tsc` runs in `build:ci` only; there is no `ruff`, `mypy`, `eslint`, or `prettier` config anywhere. Every style and typing rule is enforced by prose review. |
| G5 | Maintainability | P2 | Admin authorization is implemented 17 times: twelve `_require_admin`/`_is_admin` definitions across twelve modules, all reading the same `is_admin` row flag. One drift and a route opens. |
| G6 | Maintainability | P2 | Production code branches on test doubles: seven `hasattr(table, "query")` / `hasattr(table, "scan")` / `hasattr(table, "query_by_owner")` sites choose the DynamoDB access path by duck-typing the fake. A real table always has both, so the scan fallback is dead in production but live in tests, which means tests exercise a code path production never runs. |
| G7 | Maintainability | P2 | Module size: `reviews.py` 3649 lines / 96 top-level defs, `main.py` 2471 lines / 51 routes, `ReviewSubmission.tsx` 3366 lines / 41 `useState`, `AdminPlaybooks.tsx` 2235, `Toaster.tsx` 2182. Every audit step above touches `reviews.py` and `ReviewSubmission.tsx`. |
| G8 | Stability | P2 | Work lands directly on `main` (0 PR merges in the last 40 commits; branch protection unavailable on the current GitHub plan). Three red-main runs on 2026-09-05 alone, each fixed by a follow-up commit. The `main is red` auto-issue loop is the safety net, and #689 is still open although main is green. |
| G9 | Maintainability | P3 | Test harness is bespoke: 290 test files, each a `__main__` script run as its own process by `scripts/collect_test_failures.sh`; `pytest` cannot collect the tree. No parallelism, no `-k` selection, no fixture reuse; the full gate is the only gate. |
| G10 | Maintainability | P3 | Docs sprawl: 21 top-level files in `docs/`, 14 in `docs/planning/`, historical review packets alongside living docs, no `docs/INDEX.md` projection (the operating contract's §7 layout), and `docs-lint.py` hand-lists which files are "living". |
| G11 | QoL (reviewer) | P3 | No "run this again" affordance. After a failure or a playbook change the reviewer must re-select the file, playbook, dial, and notes mode by hand. History rows have download and cover-note actions but no re-run. |
| G12 | QoL (reviewer) | P3 | A finished review is announced by sound/notification only if opted in; the tab title theater exists. Missing: an estimated time remaining on the progress bar (the preflight already produces a page estimate and the model latency budget is known). |
| G13 | QoL (admin) | P3 | Admin panels refetch independently on tab switch and share no cache; `AdminPlaybooks` and `ReviewSubmission` each fetch `/api/playbooks`. There is no "something changed, refresh" signal, so an activation in one tab does not update the dial in another until reload. |
| G14 | QoL (developer) | P3 | `npm test` prints hundreds of lines of expected `console.error` noise (mocked 404/500 fetches) that hide real failures; `scripts/check.sh` has no watch or single-file mode documented; `scratch/` holds 15 MB of untracked material including a real-corpus candidate `.docx` beside the repo. |
| G15 | Stability | P3 | Frontend dependency drift: `vite 5` (latest 8), `vitest 4` (5), `typescript 5.9` (7), React 18 (19). None are urgent; all widen the next upgrade. |

## Details and remediation

### G1 — In-process reviews orphaned by a restart  (P1)

`backend/src/pipeline_runner.py` runs a review on a `ThreadPoolExecutor`
(line ~1742 `self._pool.submit`). `main.py::_lifespan` verifies env and starts
the purge scheduler; it neither drains the pool on shutdown nor scans for
RUNNING rows on boot. `infra/lambda/orphan_reconciler` exists only on AWS.
A Coolify redeploy (every image publish) kills every in-flight review; the
row stays RUNNING, the History tab shows a review that never ends, the daily
spend ledger keeps the worst-case reservation, and `pipeline_health` counts
it as in-flight forever.

**B1.** (a) On boot, in `_lifespan`, when the deployment is the Docker target,
query PENDING/RUNNING rows (after #692 via `status-index`; before it, a
bounded scan is acceptable at boot only) whose `execution_arn` is absent and
whose `updated_at` predates process start, mark them `ERROR` with reason
`runner_restarted`, settle the reservation with `settle_reservation_for_cancel`
semantics, and write an audit entry. (b) On shutdown, `pool.shutdown(wait=False,
cancel_futures=True)` and record cancel intent on each in-flight review so the
model client's cancel checkpoints stop spend. (c) Frontend: `runner_restarted`
gets its own `explainFailure` entry: "The service restarted while your review
was running. Nothing was charged beyond the passes that completed — please
start it again." (d) Tests: an e2e that submits, simulates process death by
constructing a fresh runner over the same table, and asserts the relabel and
settlement.

### G2 — Vulnerable pinned dependencies  (P1)

`pip-audit -r backend/requirements.txt` (run 2026-09-05):

| Package | Pinned | Advisories | Fixed in |
|---|---|---|---|
| python-jose | 3.3.0 | PYSEC-2024-232, PYSEC-2024-233, PYSEC-2025-185 | 3.4.0 / unfixed for the last |
| starlette (via fastapi 0.115.6) | 0.41.3 | PYSEC-2026-161, -248, -249, -1941, -1942, -2280, -2281 | 1.3.1 (fastapi ≥ 0.135) |
| ecdsa (via python-jose) | 0.19.2 | PYSEC-2026-1325 | unfixed upstream |

`npm audit --omit=dev`: `fast-xml-parser` high (entity expansion) via `aws-amplify`; fixed by `aws-amplify 6.20.0`.

**B2.** Replace `python-jose` with `PyJWT[crypto]` (actively maintained,
no `ecdsa` dependency): `auth.py` and `demo_auth.py` are the only importers.
Bump `fastapi` to the current 0.14x line and `uvicorn`, `pydantic`, `boto3`
to current; run the full gate. Bump `aws-amplify` to 6.20. Add
`pip-audit` and `npm audit --omit=dev --audit-level=high` as a weekly
scheduled workflow that opens an issue, not a blocking gate on every push.

### G3 — Python version drift  (P2)

CI: `python-version: '3.11'` in 16 workflows; `deploy/dts/backend.Dockerfile`:
`python:3.12-slim`; local: 3.14. No `pyproject.toml`.

**B3.** Pick 3.12 (the shipped image). Add a root `pyproject.toml` with
`requires-python = ">=3.12,<3.13"`, a `.python-version` file, and change the
16 workflows to 3.12 in one commit. Add `tests/test_ci_env_parity_639.py`
assertions that the Dockerfile, workflows, and `.python-version` agree (that
test already enumerates workflows for the Node case).

### G4 — No mechanical lint or type gates  (P2)

**B4.** Python: `ruff` (lint + format) with a config that starts at the
current baseline (`ruff check --add-noqa` once, then forbid new violations)
and `mypy --strict` on `backend/src` with a per-module ignore list that
shrinks over time. TypeScript: `eslint` with `typescript-eslint` recommended
and `react-hooks`, `prettier` for format. One workflow `lint.yml` running all
four; pre-commit via the existing `setup-pre-commit` skill pattern. Keep the
14 px floor audit scripts as they are.

### G5 — Seventeen admin checks  (P2)

**B5.** One `backend/src/authz.py` with `is_admin(row)` and
`require_admin(row, what)`; every module imports it. A test asserts by AST
that no module under `backend/src` defines its own `_is_admin`/`_require_admin`.
Behaviour-neutral; the 403 detail strings stay per call site.

### G6 — Duck-typed test doubles in production code  (P2)

**B6.** Replace the seven `hasattr` branches with a single
`backend/src/ddb.py` port (`query_index`, `get`, `put`, `update`) and give
tests a real moto table (already a dev dependency) instead of hand-rolled
fakes. Delete the scan fallbacks; #692 already removes the live ones.

### G7 — Module size  (P2)

**B7.** Split by responsibility, no behaviour change, one PR each:
`reviews.py` → `reviews/{submission, listing, detail, cancel, spend, projections}.py`
with `reviews/__init__.py` re-exporting the public names so imports are
untouched; `main.py` → move the playbook-admin routes (11 handlers, lines
1391–2425) into `playbook_routes.py` with an `APIRouter` like `review_routes.py`;
`ReviewSubmission.tsx` → extract `useReviewPolling`, `usePreflight`,
`useReviewPreferences` hooks and the `explainFailure` table into
`reviewFailures.ts`. Do G7 AFTER #690–#699 land to avoid rebasing the audit
work across a file split.

### G8 — Direct pushes to main  (P2)

**B8.** Adopt a branch-per-change flow with the existing CI as the PR gate,
using a required status check once the plan allows it; until then, a
`scripts/land.sh` that runs the gate locally and refuses to push red
(the `git-guardrails-claude-code` skill can block a raw `git push` to main).
Close #689 (stale: main has been green since 4bbfea6). Make the
`main is red` auto-issue close itself when the next run is green.

### G9 — Bespoke test harness  (P3)

**B9.** Keep the per-file process isolation (it is the correct boundary for
the `sys.modules` pollution the header describes) but run files in parallel
with `xargs -P` and add `scripts/check.sh --only <glob>`. Longer term, convert
new tests to pytest style and let `pytest -p no:cacheprovider tests/<file>`
be the per-file runner.

### G10 — Docs sprawl  (P3)

**B10.** Scaffold `docs/INDEX.md` with `/docs-sync scaffold`, move the four
`architecture-review-*` packets and `architecture-issue-spotting-*` into
`docs/reports/`, `handoff_*` into `docs/reports/`, and let `docs-lint.py`
derive its "living docs" list from INDEX.md instead of the hand list.

### G11–G13 — Reviewer and admin quality of life  (P3)

**B11.** "Run again" on History rows and on the burnt-toast panel: pre-fills
playbook, dial, notes mode, and guidance from the stored row; the file is
re-fetched from `GET /api/reviews/{id}/input` when it is still retained,
otherwise the user is asked to choose it. Uses the existing submit path.
**B12.** Time-remaining estimate on the progress bar from the preflight page
estimate and a per-deployment rolling median of recent DONE reviews'
`created_at → completed_at` (the `pipeline_health` data already exists).
**B13.** A tiny in-memory catalog store (`playbooksStore.ts`) shared by
`ReviewSubmission`, `AdminPlaybooks`, and `ReviewHistory`, invalidated by
the admin mutations and by the existing `adminRefresh.ts` seam.

### G14 — Developer quality of life  (P3)

**B14.** In `setupTests.ts`, replace the global `console.error` with a
filter that suppresses the known mocked-fetch messages and fails the test on
any other error (a `react-testing-library` idiom). Document `check.sh`
single-file use. Add `scratch/` and `dump/` to `.gitignore` (they are
untracked but unignored) and move the real-corpus candidate out of the repo
tree to the location `docs/planning/Internship-Agreement-Library` already uses
for excluded material, or delete it.

### G15 — Frontend major-version drift  (P3)

**B15.** Deferred until after B4 (lint) and #696 (code split). Then Vite 6
and Vitest 5 first, React 19 last.

## Owner decisions (2026-09-05)

Pre-launch: prefer the robust, secure, maintainable path even where it is a
breaking change today.

- Q4: `python-jose` is replaced by `PyJWT[crypto]`; algorithms pinned explicitly.
- Q5: Python 3.13 everywhere (CI, image, local), declared in `pyproject.toml`.
- Q6: branch-per-change with `scripts/land.sh` and a pre-push guard; CI runs on
  pull requests; branch protection when the plan allows it.

## GitHub issues (execution order)

| Step | Issue | Action |
|---|---|---|
| 1 | #701 | B2 dependency vulnerabilities |
| 2 | #702 | B1 Docker-target orphan recovery |
| 3 | #703 | B3 Python 3.13 parity |
| 4 | #704 | B8 landing flow |
| 5 | #705 | B4 lint and type gates |
| 6 | #706 | B5 single authz module |
| 7 | #707 | B6 remove duck-typed fallbacks |
| 8 | #708 | B14 developer QoL |
| 9 | #709 | B10 docs index |
| 10 | #710 | B11 Run again |
| 11 | #711 | B12 time-remaining estimate |
| 12 | #712 | B13 shared catalog store |
| 13 | #713 | B7 module splits |
| 14 | #714 | B9 test runner |
| 15 | #715 | B15 frontend majors |

## Proposed order

1. B2 dependency vulnerabilities (small, urgent, isolated).
2. B1 Docker-target orphan recovery (correctness; depends on nothing).
3. B3 Python version parity.
4. B8 landing flow + close #689.
5. B4 lint and type gates (baseline-then-ratchet, no code churn).
6. B5 single authz module. 7. B6 remove duck-typed fallbacks (after #692).
8. B14 developer QoL. 9. B10 docs index.
10. B11, B12, B13 reviewer/admin QoL.
11. B7 module splits (after the audit series lands). 12. B9, B15.

## Questions

- Q4. B2 swaps `python-jose` for `PyJWT`. Approve the library change on the
  SSO verification path? The alternative is `python-jose 3.4.0`, which still
  drags the unfixed `ecdsa` advisory.
- Q5. B3 standardises on Python 3.12. Approve, or move the image to 3.13?
- Q6. B8: until the plan supports branch protection, is a local push guard
  that refuses a red gate acceptable as the interim control?
