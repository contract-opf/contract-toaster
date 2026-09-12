# Reviewing this repository

A short orientation for someone invited to review this codebase — what it is,
what actually works today, how to see it running, and what the next steps are.
It is deliberately honest about the gap between the design docs (written "docs
as spec", ahead of the code) and what is reachable now.

## What this is

An internal tool that reviews counterparty-modified contract drafts (EIAA —
educational internship affiliation agreements) against a codified playbook and
returns either an **ACCEPT** decision or a redlined `.docx` with tracked changes
and footnoted rationales. Nothing in the tool enforces, requires, gates on, or
records attorney approval; approval happens outside this tool, in your
organization's own review process. It produces drafts and analysis, not legal
advice.

## Current state — read this first

- **The two deployment targets differ, and this is the single most important
  thing to understand before judging the app.**

  | Target | Review pipeline |
  |---|---|
  | Docker Compose | **Real.** `scripts/review_spine.py::run_review` driven by a live model client. |
  | AWS | **Mock.** A Lambda stage returns a pre-baked, clearly-synthetic redline. |

- **Docker Compose runs the real review.** `backend/src/pipeline_runner.py`
  selects `run_real_pipeline` when `MODEL_PROVIDER=openrouter`, running the full
  chain — extract → diff → primary pass → adversarial critic → reconcile →
  redline → leakage-scan — against the activated playbook. With `MODEL_PROVIDER`
  unset it falls back to `run_mock_pipeline`, which is the escape hatch for
  tests and for callers that do not want a live model call.
- **AWS does not.** `infra/lib/nested/pipeline-stack.ts` still wires
  `MockReviewStageFunction` as the review stage. Replacing it is #243, which was
  **deliberately deferred past the open-source launch** by an owner decision on
  2026-07-14 — it is not an oversight, and it is tracked by its own milestone
  ("AWS real-pipeline acceptance — deferred") so that Phase 2 completing on the
  Docker Compose target can never be read as cross-deployment parity.
- **A review completes end-to-end on both.** PENDING → RUNNING → DONE with a
  downloadable `.docx`. The reviewer UI shows the result behind the required
  pre-download trust-calibration gate (confidence band + critic-delta indicator).
- **What ships vs. what's stubbed/planned** for the admin-UI and observability
  surfaces is tracked plainly in
  [docs/implementation-status.md](implementation-status.md) (a lint-enforced
  SHIPPED / STUBBED / PLANNED ledger). Treat any RUNBOOK/ARCHITECTURE procedure
  marked PLANNED there as a design spec, not an operating procedure.
- **Not production-deployable yet.** See #225 (repo not yet deployable from a
  clone), #96 (prod account bootstrap), and the blocking go-live gates #98
  (production readiness review) and #99 (pilot parallel-run + go-live decision).

## Two deployment targets, one codebase

The app runs against two targets, selected by environment variables at process
start (`backend/src/config.py`). The AWS path is byte-identical when the Docker Compose
variables are unset — every AWS-asserting test runs with AWS values.

| Concern | AWS (App Runner) | Docker Compose |
|---|---|---|
| Object store | S3 | MinIO (`S3_ENDPOINT_URL`) |
| Key-value store | DynamoDB | DynamoDB-Local (`DYNAMODB_ENDPOINT_URL`) |
| Auth | Cognito (Google SSO) | username/password (`AUTH_MODE=password`) |
| Pipeline | Step Functions | in-process worker (`PIPELINE_RUNNER=inprocess`) |
| Model | Bedrock | OpenRouter, direct API key (`MODEL_PROVIDER=openrouter`) |

The Docker Compose target (`deploy/dts/`) is the easiest way to see the whole app running
without any AWS account — see below.

## How to see it running (Docker Compose, self-contained)

```bash
cp deploy/dts/.env.example deploy/dts/.env      # set DEMO_TOKEN_SECRET
docker compose -f deploy/dts/docker-compose.yml --env-file deploy/dts/.env up --build
```

- SPA: <http://localhost:8081> — sign in with **admin/admin** or **user/user**
- API: <http://localhost:8080>

Full instructions (including a one-line `/etc/hosts` entry that presigned
downloads need) are in [deploy/dts/README.md](../deploy/dts/README.md).

> **Verification status:** the Docker Compose stack is verified against local emulators
> (config routing, login → token, in-process pipeline PENDING→DONE + redline
> copy + spend settle, presigned download of a valid `.docx`). The literal
> `docker compose up` has not been run in CI (it needs an environment that can
> pull base images), so a first real bring-up is a sensible early check.

## Suggested reading order

1. [README.md](../README.md) — what it does / does not do, and the "docs as
   spec" caveat.
2. This guide, then [docs/implementation-status.md](implementation-status.md)
   — what is actually reachable today.
3. [ARCHITECTURE.md](../ARCHITECTURE.md) — the target design (data flow, model
   policy, least-privilege, retention).
4. [docs/output-contract.md](output-contract.md) and
   [docs/threat-model.md](threat-model.md) — the output/redline contract and
   the security model (both load-bearing for a legal tool).
5. Code entry points: `backend/src/main.py` (API), `backend/src/review_routes.py`
   (the review flow), `backend/src/pipeline_runner.py` (Docker Compose in-process pipeline),
   `scripts/` (the real, offline-tested review chain), `infra/lib/` (the AWS CDK).

## Running the checks

- Backend + infra gate: `bash scripts/check.sh` (Python 3.13 venv, the version
  the root `.python-version` declares; the script prints the interpreter it
  actually activated and warns on a mismatch — see the
  script header). `SKIP_INFRA=1 bash scripts/check.sh` runs the ~20s Python-only
  subset. Each `tests/*.py` is a self-contained script-style runner.
- Frontend: `cd frontend && npm test` (vitest), `npm run typecheck`,
  `npm run build`.

### Reading the gate's answer

**The exit code is the answer, not the last line of output.** `scripts/check.sh`
exits:

| Code | Meaning | Final line |
| --- | --- | --- |
| 0 | every test file passed | `CHECK: ALL GREEN` |
| 1 | a file failed its first run *and* its isolated re-run | `CHECK: FAILURES:<list>` |
| 2 | a file failed and then passed alone, and `ALLOW_FLAKY` was not set | `CHECK: FLAKY-UNRESOLVED:<list>` |
| 3 | another full gate run holds the repo-wide lock; **no tests ran** | `CHECK: LOCK BUSY` |

A *pipeline* reports its last command's status, so `bash scripts/check.sh | tee
gate.log` exits 0 whatever the gate found. Capture the code directly
(`bash scripts/check.sh; rc=$?`) or use `set -o pipefail`. Grepping for
`CHECK: ALL GREEN` is a safe cross-check — that line is printed on exit 0 and
never on 1, 2, or 3.

**A flaky test fails the gate.** The loop re-runs each failing file once, alone.
A file that then passes lands in the FLAKY bucket, and that is red (exit 2), not
green. Pass-on-re-run is not evidence the code is healthy: it is equally
consistent with a genuine intermittent regression. Read the logs (the loop
prints its log directory), re-run, and if you conclude it was noise, say so
explicitly:

```bash
ALLOW_FLAKY=1 bash scripts/check.sh
```

That restores the lenient behaviour and names the files it waved through. CI
never sets it.

**Full runs are serialised across worktrees.** Infra tests shell out to `npx cdk
synth`; git worktrees have no local `infra/node_modules`, so `npx` falls back to
the `~/.npm/_npx` cache — which is shared by every worktree on the machine. Two
concurrent full runs corrupt each other's synth. So a full run takes a lock
anchored at the shared `git rev-parse --git-common-dir`, waits for it
(`CHECK_LOCK_WAIT`, default 1800s), and exits 3 rather than running alongside
another holder. `SKIP_INFRA=1` runs take no lock and stay fully parallel.
`CHECK_NO_LOCK=1` bypasses it if you are certain nothing else is running.

This is the concrete mechanism behind "a green gate is not evidence": on
2026-08-20 two parallel worktree runs each produced `CHECK: ALL GREEN` / exit 0
with real infra failures hidden in the FLAKY bucket.

## Next steps (roadmap for a reviewer)

**To bring the AWS target to parity with Docker Compose:**
1. **Replace the AWS mock Step Functions stage** with the same tested `scripts/`
   chain the Docker Compose target already runs, driven by a real model client.
   The `OpenRouterModelClient` exists; the AWS Bedrock client is still deferred.
   This is #243, and it carries its own milestone.
2. **Add an OpenRouter pricing branch** to the spend model so the daily cost cap
   protects against real-provider spend.

**To make the Docker Compose target production-usable:**
3. Run the live `docker compose up` end-to-end (see the verification note above)
   and, if downloads are proxied rather than host-mapped, finish that path.
4. Add the retention purge scheduler (deferred; see `deploy/dts/README.md`).

**To make AWS production-deployable:**
5. Close #225 (deployability), #96 (prod account bootstrap), and pass the
   blocking gates #98 (production readiness) and #99 (pilot + go-live decision).

**Product completeness:**
6. Finish the reviewer result view (#85): per-issue provenance (#35), the
   disposition capture flow (#74), per-review cost, and every-status copy.
7. Admin surfaces currently STUBBED/PLANNED in
   [docs/implementation-status.md](implementation-status.md) (playbook
   governance UI, audit viewer, corpus upload UI, cost-ledger reconcile).

For phase-level tracking, see the repo
[milestones](https://github.com/contract-opf/contract-toaster/milestones) and the
open issues.
