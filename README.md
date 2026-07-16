# Contract Toaster

OPF-native contract review: upload a counterparty-modified draft, and get back
either an ACCEPT decision or a redlined `.docx` with in-place tracked changes
and footnoted rationales, reviewed against your codified negotiating position
(a "playbook").

> **Reviewing this repo?** Start with **[docs/REVIEW-GUIDE.md](docs/REVIEW-GUIDE.md)** — an honest orientation to what actually works today (the review pipeline is currently a mock), the two deployment targets (AWS and a self-contained Docker/DTS stack), how to see it running, and the roadmap.

## Status

In active development. Not yet deployed. See [milestones](https://github.com/contract-opf/contract-toaster/milestones) for phase progress.

RUNBOOK.md and ARCHITECTURE.md describe the target design, some of it ahead of
the code — that's a deliberate "docs as spec" choice for a solo team, but it
means the docs alone don't tell you what's reachable today. See
[docs/implementation-status.md](docs/implementation-status.md) for the
SHIPPED / STUBBED / PLANNED ledger of every admin-UI capability and
observability surface RUNBOOK.md describes.

## What this does

1. Reviewer signs in (Google SSO on AWS, restricted to your organization's
   Workspace domain; username/password in the DTS quickstart below).
2. Reviewer uploads a counterparty-modified `.docx`.
3. The service reviews it against the active playbook — the **Sample
   Agreement** playbook ships as the default, fully-working worked example
   (see "How review works" below).
4. The service returns either:
   - **ACCEPT** — the counterparty's changes are within your acceptable range; no redline required.
   - **REQUEST_CHANGE** — a redlined `.docx` with in-place tracked changes and a footnoted rationale for each material edit.
5. Every review is logged with the playbook version, model, token counts, and cost.

A human attorney reviews and approves before anything goes back to the
counterparty. This tool produces drafts and analysis; it does not give legal
advice and does not constitute approval. Every output is a **tool
recommendation only; attorney approval required** — an ACCEPT means "no
requested changes identified by the tool", not "no action needed".

## What this is not

- Not a replacement for attorney judgment.
- Not connected to any counterparty system.
- Not training a model on your documents. We use Anthropic Claude via Amazon Bedrock; per Bedrock terms, prompts and outputs are not used to train models.

## How review works: knowledge and precision

Contract Toaster is **type-blind**: adding a new agreement type is data, not
code — author a playbook (an [OPF](https://contract-opf.github.io/)-native
JSON document), add one entry to `playbooks/registry.json`, and it's
reviewable. No call site in `backend/src/` or `scripts/` hard-codes a
playbook name.

Every playbook resolves to one of two review modes:

- **Knowledge** (the universal default). The uploaded draft is compared
  against the playbook's codified negotiating positions directly — no
  canonical standard form is required, so this mode works for any
  counterparty-authored paper from day one.
- **Precision** (opt-in, per playbook). When a playbook carries a canonical
  standard form and a matching anchor map (`standard-forms/`), a
  deterministic router (`scripts/form_match_router.py`) detects when an
  upload is close enough to that form to run a section-anchored diff
  instead of the knowledge comparison — catching smaller, clause-level
  edits a pure-knowledge pass could miss.

The default `sample-agreement` playbook ships with a full precision profile
(`playbooks/sample-agreement-v1.0.0.json` +
`standard-forms/sample-agreement-v1.0.0.anchor-map.json`), so it's a genuine,
working end-to-end example, not just a knowledge-only stub.

## Playbooks

Playbooks are plain data: JSON documents following the [Open Playbook Format
(OPF)](https://contract-opf.github.io/) schema (`playbooks/schema.json`,
`playbooks/opf/`). `playbooks/registry.json` is the single source of truth
mapping a `playbook_id` to its playbook, anchor-map, section-config, and
fixtures paths — adding a contract type never requires a code change.

- **Default:** `sample-agreement` — a clearly fictional "Sample Agreement"
  used throughout this README and in the DTS quickstart below, so no real
  negotiating position is ever exposed by accident.
- **Community playbooks:** [contract-opf/playbooks](https://github.com/contract-opf/playbooks)
  *(coming soon)* — a shared library of OPF playbooks for common agreement
  types, licensed CC BY 4.0.
- **Bring your own:** author a playbook + anchor map (optional, for a
  precision profile) + section config + fixtures directory, then add one
  `playbooks/registry.json` entry.

## Architecture, in brief

- **Frontend.** React SPA on AWS Amplify Hosting. Cognito user pool federated to Google as the only IdP, hosted-domain restricted.
- **Backend.** Python (FastAPI) container on AWS App Runner serving the API only. Builds go through CI (CodeBuild or equivalent): tests and scans run, a signed container image is pushed to ECR, and App Runner is pinned to an immutable image **digest** that is promoted deliberately. A merge to `main` never auto-mutates production legal behavior.
- **Review pipeline.** Reviews run asynchronously: the API starts an AWS Step Functions execution **directly** (extract → retrieve → primary review → adversarial review → leakage scan → redline → persist), and the UI polls for the result. There is no SQS buffer on the entry path. Idempotency comes from a submission record, one spend reservation per `review_id`, deterministic Step Functions execution names, and retry-safe "ensure execution started" semantics.
- **LLM.** Anthropic Claude via Amazon Bedrock (single-region, `us-east-1`, native model ID — no cross-region inference profile), in a two-pass (reviewer + adversarial critic) design. The model is governed by an explicit model-policy matrix: primary model, critic model, embedding model, optional fallback, region, request contract, evaluation run, and cost assumptions. v1 pins **Opus 4.8 as the primary reviewer and a *different* model (Sonnet 4.6) as the adversarial critic** — a different critic decorrelates the two passes' blind spots and costs less — unless a future evaluated policy says otherwise.
- **Retrieval.** Hybrid lexical + semantic retrieval over the executed-agreements corpus: an Amazon Bedrock Knowledge Base (backed by S3 Vectors) supplies semantic recall, paired with deterministic keyword/rule detectors for hard-rejection terms that semantic search can miss.
- **Corpus governance.** Corpus ingestion creates a draft snapshot. Only a curated, regression-tested snapshot can become active, and every review records the corpus snapshot it used.
- **Storage.** S3 (uploads, redlines, corpus, audit; governance object lock on corpus and audit; admin-configurable document retention plus storage-level legal hold). DynamoDB (users, playbooks, playbook versions, reviews, audit log, cost ledger). Immutable audit rows contain only non-substantive audit facts; model rationales and critic deltas live only in retention-governed confidential storage.
- **Infrastructure as code.** AWS CDK (TypeScript). Everything is `cdk deploy`.
- **Redlining.** `scripts/redline_docx_writer.py` is a small, dependency-free OOXML tracked-changes writer we own outright, built entirely on the Python standard library (`zipfile` + `xml.etree.ElementTree`). There is no `backend/vendor/` directory in this repo; see [ARCHITECTURE.md → Redlining](ARCHITECTURE.md#redlining--owned-docx-library).
- **Review prompt and playbook structure.** Prompts are assembled in code by `scripts/primary_review_pass.py` (system prompt = review guidance + binary-decision overlay + playbook JSON, per a fixed manifest), not stored as a `prompts/` directory. The review guidance was informed by [`anthropics/claude-for-legal`](https://github.com/anthropics/claude-for-legal)'s `contract-review` skill as reference material, with our own overlay for the binary-decision output format. Active releases are a governed bundle: playbook hash, prompt hash, standard-form hash, model-policy hash, corpus snapshot, evaluation run, and legal approval. Precedent citations are internal-only; generated external footnotes cite the contract position, not prior counterparties.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full picture.

## Repository layout

```
contract-toaster/
├── README.md
├── ARCHITECTURE.md
├── RUNBOOK.md
├── LICENSE
├── NOTICE
├── playbooks/                    # OPF-native playbooks: schema, registry, and playbook data
│   ├── schema.json               # generalized playbook schema
│   ├── registry.json             # playbook_id -> artifact paths (add one entry per new agreement type)
│   └── sample-agreement-v1.0.0.json  # default playbook -- the worked example in this README
├── standard-forms/               # canonical standard-form .docx files and derived anchor maps (precision profile)
│   ├── README.md                 # directory guide and build instructions
│   └── sample-agreement-v1.0.0.anchor-map.json  # anchor map for the default precision profile
├── scripts/                      # the review pipeline: pure, tested modules (no separate prompts/ or vendor/ tree)
│   ├── docs-lint.py              # documentation lint
│   ├── build_anchor_map.py       # anchor-map builder: docx -> anchor map artifact
│   ├── form_match_router.py      # deterministic knowledge-vs-precision routing
│   ├── primary_review_pass.py    # prompt assembly + primary review pass (code-assembled, not a prompts/ directory)
│   └── redline_docx_writer.py    # dependency-free OOXML tracked-changes writer we own outright
├── infra/                        # AWS CDK (TypeScript)
├── backend/                      # Python service for App Runner + pipeline tasks
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
├── frontend/                     # React SPA for Amplify
├── deploy/                       # DTS (Docker Compose) deployment target
└── docs/
```

## Documentation source of truth

The source docs are this README, [ARCHITECTURE.md](ARCHITECTURE.md), [RUNBOOK.md](RUNBOOK.md), and the files under [docs/](docs/). Generated review packets are not authoritative; regenerate them from these files if needed.

## Local development

The fastest way to see the app running is the self-contained DTS (Docker
Compose) stack — no AWS account required:

```bash
gh repo clone contract-opf/contract-toaster
cd contract-toaster
cp deploy/dts/.env.example deploy/dts/.env      # set DEMO_TOKEN_SECRET
docker compose -f deploy/dts/docker-compose.yml --env-file deploy/dts/.env up --build
```

- SPA: <http://localhost:8081> — sign in with **admin/admin** or **user/user**
- API: <http://localhost:8080>

Full instructions, what's mocked, and what to expect are in
[docs/REVIEW-GUIDE.md](docs/REVIEW-GUIDE.md) and
[deploy/dts/README.md](deploy/dts/README.md).

## Deploying to AWS

This repo is a work in progress; the AWS deployment (App Runner, Amplify
Hosting, Cognito, Step Functions) is not yet reachable end-to-end from a
fresh clone — see [docs/REVIEW-GUIDE.md](docs/REVIEW-GUIDE.md) for the
current status and blocking issues.

```bash
# Prerequisites
brew install node awscli gh
npm install -g aws-cdk

# Infrastructure (CDK)
cd infra
npm install
cdk synth                        # validate
cdk diff                         # preview changes
cdk deploy                       # deploy

# Backend (run locally against deployed AWS resources)
cd ../backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn src.main:app --reload

# Frontend (run locally against deployed Cognito)
cd ../frontend
npm install
npm run dev
```

Full setup details live in [RUNBOOK.md](RUNBOOK.md).

## Contributing

- All work is tracked in GitHub issues, grouped by phase milestone.
- Every change goes through a pull request. `main` is protected.
- Anything that modifies `playbooks/` or `prompts/` requires legal review (enforced by [.github/CODEOWNERS](.github/CODEOWNERS)).
- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`.
- Branch naming: `phase-N/short-description` (e.g., `phase-0/cognito-google-idp`).

## License

- **Code** (this repository, except where noted below) is licensed under the [Apache License 2.0](LICENSE).
- **Playbook content** — negotiation positions and related content under `playbooks/` — is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) where marked in the file or an accompanying README.
- "Exos" and "Contract Toaster" are trademarks of Athletes' Performance, Inc.; see [NOTICE](NOTICE) for the full trademark notice and attribution requirements.
