# Reviewing this repository

A short orientation for someone invited to review this codebase — what it is,
what actually works today, how to see it running, and what the next steps are.
It is deliberately honest about the gap between the design docs (written "docs
as spec", ahead of the code) and what is reachable now.

## What this is

An internal tool that reviews counterparty-modified contract drafts (EIAA —
educational internship affiliation agreements) against a codified playbook and
returns either an **ACCEPT** decision or a redlined `.docx` with tracked changes
and footnoted rationales. Every output is watermarked **"tool recommendation
only — attorney approval required"**; a human attorney approves before anything
goes out. It produces drafts and analysis, not legal advice.

## Current state — read this first

- **The review "brain" is currently a mock.** The live pipeline returns a
  pre-baked, clearly-synthetic redline for the `eiaa` playbook, not a real
  model review. The real chain (extract → diff → primary pass → adversarial
  critic → reconcile → redline → leakage-scan) exists as **pure, tested
  `scripts/` modules** and is exercised offline by `scripts/eval_harness.py`,
  but it is **not wired into any live request path yet** (neither AWS nor DTS).
  This is the single most important thing to understand before judging the app.
  See issues #187 / #210 and the deferred #80–#83 epic.
- **The mock pipeline now completes end-to-end.** A review moves
  PENDING → RUNNING → DONE and produces a downloadable output `.docx`
  (recent work: #236, #188). The reviewer UI shows the result with the required
  pre-download trust-calibration gate (confidence band + critic-delta indicator;
  partial #85, PR #255).
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
start (`backend/src/config.py`). The AWS path is byte-identical when the DTS
variables are unset — every AWS-asserting test runs with AWS values.

| Concern | AWS (App Runner) | DTS (Docker Compose) |
|---|---|---|
| Object store | S3 | MinIO (`S3_ENDPOINT_URL`) |
| Key-value store | DynamoDB | DynamoDB-Local (`DYNAMODB_ENDPOINT_URL`) |
| Auth | Cognito (Google SSO) | username/password (`AUTH_MODE=password`) |
| Pipeline | Step Functions | in-process worker (`PIPELINE_RUNNER=inprocess`) |
| Model | Bedrock | OpenRouter, direct API key (`MODEL_PROVIDER=openrouter`) |

The DTS target (`deploy/dts/`) is the easiest way to see the whole app running
without any AWS account — see below.

## How to see it running (DTS, self-contained)

```bash
cp deploy/dts/.env.example deploy/dts/.env      # set DEMO_TOKEN_SECRET
docker compose -f deploy/dts/docker-compose.yml --env-file deploy/dts/.env up --build
```

- SPA: <http://localhost:8081> — sign in with **admin/admin** or **user/user**
- API: <http://localhost:8080>

Full instructions (including a one-line `/etc/hosts` entry that presigned
downloads need) are in [deploy/dts/README.md](../deploy/dts/README.md).

> **Verification status:** the DTS stack is verified against local emulators
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
   (the review flow), `backend/src/pipeline_runner.py` (DTS in-process pipeline),
   `scripts/` (the real, offline-tested review chain), `infra/lib/` (the AWS CDK).

## Running the checks

- Backend + infra gate: `bash scripts/check.sh` (Python 3.11 venv; see the
  script header). `SKIP_INFRA=1 bash scripts/check.sh` runs the ~20s Python-only
  subset. Each `tests/*.py` is a self-contained script-style runner.
- Frontend: `cd frontend && npm test` (vitest), `npm run typecheck`,
  `npm run build`.

## Next steps (roadmap for a reviewer)

**To make this a real (not mock) review tool:**
1. **Wire the real pipeline into a live path** — replace the mock body of the
   in-process runner (and the AWS mock Step Functions stage) with the tested
   `scripts/` chain, driven by a real model client. The `OpenRouterModelClient`
   already exists; the AWS Bedrock client is still deferred. This is the #80–#83
   epic and benefits both deployment targets (neither runs the real brain today).
2. **Add an OpenRouter pricing branch** to the spend model so the daily cost cap
   protects against real-provider spend.

**To make DTS production-usable:**
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
