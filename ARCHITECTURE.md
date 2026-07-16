# Architecture

This document describes the design of `contract-toaster`. For operating procedures, see [RUNBOOK.md](RUNBOOK.md). For the original brief and tradeoff discussion, see [docs/design-notes.md](docs/design-notes.md).

## System overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                              Browser                                │
│                  Reviewer (regular) or Admin                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ HTTPS, Cognito JWT
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Amplify Hosting                               │
│           React SPA  +  Cognito (Google IdP, company.com)           │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ HTTPS, JWT verified by App Runner
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       App Runner service (API)                      │
│   Python FastAPI, Docker, pinned to a SIGNED ECR image DIGEST       │
│   Validates Cognito JWT + company.com on every request. Stateless.  │
│                                                                     │
│   POST /api/reviews     → create idempotent submission record, store │
│                           upload, reserve spend once per review_id,  │
│                           ensure Step Functions execution started    │
│                           DIRECTLY (deterministic execution name),   │
│                           return 202 + review id (async; no inline   │
│                           LLM; no SQS buffer on the entry path).     │
│   GET  /api/reviews/{id}→ poll status/result (owner or admin only). │
│            /api/playbooks   (admin: CRUD + version history)         │
│            /api/audit       (admin: query)                          │
│            /api/users       (admin: manage admin list)              │
│            /api/corpus      (admin: upload → Bedrock KB ingestion)  │
│            /health          (public: liveness only)                 │
│            /version         (allowlisted: version, commit, digest)  │
└────────┬────────────────────────────────────────────────┬─────────┘
         │ StartExecution (direct; no SQS)                  │ read/write
         ▼                                                  │
┌─────────────────────────────┐                             │
│   Step Functions (review)   │                             │
│   extract → normalize →      │                            │
│   std-form diff → retrieve → │                            │
│   primary review →           │                            │
│   adversarial review →       │  Lambda / Fargate tasks     │
│   leakage scan → redline →   │                            │
│   persist → audit            │                            │
└───┬───────────┬─────────────┘                             │
    │           │                                           │
    ▼           ▼                                           ▼
┌──────────────────┐  ┌──────────────┐  ┌────────────┐  ┌────────────┐
│ Bedrock          │  │ Bedrock KB   │  │    S3      │  │  DynamoDB  │
│ Opus 4.8         │  │ (S3 Vectors) │  │ encrypted, │  │   PITR,    │
│ primary reviewer │  │  corpus      │  │ governance │  │   KMS      │
│ + Sonnet 4.6     │  │  retrieval   │  │ lock on    │  │            │
│ adversarial      │  │              │  │ corpus     │  │            │
│ critic           │  │              │  │            │  │            │
└──────────────────┘  └──────┬───────┘  └────────────┘  └────────────┘
                    ▲
                    │ admin upload → draft ingestion snapshot →
                    │ curated/tested active snapshot
                    │
              ┌─────┴──────┐
              │ Corpus     │
              │ uploader   │
              │ (admin UI) │
              └────────────┘

Observability: CloudWatch metrics + dashboards; CloudTrail for all AWS API calls
(incl. Bedrock InvokeModel/Converse as management events); S3 versioning + object
lock on corpus and audit; append-only DynamoDB audit table.

Infra: AWS CDK (TypeScript). All resources defined as code. No click-ops.
Prod runs in a SEPARATE AWS account from dev (legal-facing data isolation).
Deploys are promoted by immutable, signed ECR image digest — never auto-mutated
by a merge to main.
```

## Component-by-component

### Frontend — Amplify Hosting + React

A small React SPA. The user-facing surface area is intentionally narrow:

- **Reviewer flow.** Sign in with Google → upload a `.docx` → wait for review (1–3 minutes typical, 5 minutes p95) → see the decision and download the redlined output, or see the ACCEPT message with a summary of what the counterparty changed and why each change was acceptable.
- **Admin flow.** Everything the reviewer can do, plus: download and upload draft playbook versions, activate release bundles after validation, view playbook version history and diffs, view the audit log, view the cost ledger, manage the admin list, manage the corpus (upload new executed agreements, curate draft corpus snapshots, activate tested snapshots).

**Every output and UI state is watermarked "tool recommendation only — attorney approval required".** This is not cosmetic: it is a misuse-prevention control (see [docs/threat-model.md](docs/threat-model.md)). The watermark appears in the SPA result view, on the ACCEPT summary, and is baked into the generated redline `.docx` (see [Redlining](#redlining--owned-docx-library)).

- **ACCEPT does not mean "approved".** The ACCEPT state never reads "no action needed". It reads **"no requested changes identified by tool"**, with the same attorney-approval watermark. A clean tool pass is a tool result, not legal sign-off.
- **Manual-review is a system status, not a legal category.** When the pipeline routes a review to manual review (low internal confidence with no concrete playbook issue, or a structured-output failure), the UI shows it as a distinct **system status** (`MANUAL_REVIEW_REQUIRED`), visually separate from the `ACCEPT | REQUEST_CHANGE` legal decisions, so a reviewer never mistakes a pipeline outcome for a legal opinion.
- **Per-issue provenance badges — system metadata, not a legal category.** The result view renders a small provenance badge alongside each issue in a `REQUEST_CHANGE` response. The badge reflects the `provenance` field from the model response (`"detector:<rule_id>"` | `"model"` | `"critic-added"`) and is framed as system metadata so an attorney can calibrate scrutiny: a deterministic detector fire is mechanical and near-certain, while an LLM judgment call (`model`) is probabilistic. The badge must never be styled as a legal confidence level or additional legal decision; it is a source-attribution label only. The binary `ACCEPT | REQUEST_CHANGE` decision and the attorney-approval watermark are unchanged.
- **Low-confidence band — visible pre-download.** When the pipeline's `confidence_band` field is non-null (i.e. `confidence_state` is not `OK`), the result view renders a **confidence band** prominently before the download button. The band is a system-status indicator (`LOW_CONFIDENCE`, `MANUAL_REVIEW_REQUIRED`, or `ERROR_MANUAL_REVIEW_REQUIRED`), rendered as a distinct visual element clearly labeled as a pipeline signal, not a legal opinion. This ensures the attorney sees the confidence context before acting on the result — they are not required to open the download to discover the pipeline flagged uncertainty. The band is framed consistently with the `MANUAL_REVIEW_REQUIRED` system-status framing rule above.
- **Critic-delta presentation — impossible to miss before download.** When the pipeline's `critic_delta` field is non-null and contains contested replacements or critic-added issues, the result view surfaces a **critic-delta indicator section** above the download affordance. Contested replacements are shown with a **"critic flagged this replacement" badge** alongside the primary's proposed text; when a critic-suggested alternative is present, the two versions are displayed **side-by-side** (labeled "Primary" / "Critic suggestion") so the disagreement is visible at a glance. Issues with `provenance = "critic-added"` are attributed with a "critic added" badge, using the same visual language as the per-issue provenance badges (one consistent system). The download affordance is not presented until the critic-delta indicator is visible in the document flow — the attorney cannot reach the download without scrolling past the indicator — but there is no blocking modal; the attorney retains full agency. A result with `critic_delta = null` is unaffected. The normative rendering spec lives in [docs/output-contract.md → Critic-delta presentation](docs/output-contract.md#critic-delta-presentation).
- **Disposition nag — reviews awaiting disposition count in list view.** The reviewer's list view displays a **nag count** of completed reviews that have not yet received an attorney disposition (accepted/edited/rejected). The nag surfaces as a badge or banner ("N reviews awaiting disposition") so attorneys see at a glance how many reviews are waiting for outcome capture. This prevents the eval feedback loop from starving silently when attorneys close Word and forget to record the outcome. The nag is informational only — it does not block access and does not change the review's pipeline state. The disposition capture itself is specified in [docs/evaluation.md → Human-review feedback loop](docs/evaluation.md#human-review-feedback-loop).

#### Wrong-format rejection UX — PDF and legacy .doc (v1 scope)

**v1 accepts `.docx` only.** The upload gauntlet checks the file's magic number and `[Content_Types].xml`; only a valid OOXML WordprocessingML document passes. PDF intake is deliberately deferred from v1 scope — see [docs/design-notes.md → v1 .docx-only intake scope](docs/design-notes.md#v1-docx-only-intake-scope-pdf-deferred).

Counterparty turns frequently arrive as PDFs (including scanned PDFs) or as legacy `.doc` files. These are wrong-format uploads, not hostile-file attacks. The rejection UX distinguishes the two cases:

- **Format-specific rejection copy — PDF and .doc get tailored messages, distinct from the generic hostile-file error.** A zip-bomb or macro-laden `.docm` hits the hostile-file gauntlet (see [docs/threat-model.md → Hostile file uploads](docs/threat-model.md#hostile-file-uploads)) and the UI shows a generic security-rejection message with no workflow guidance. A PDF or a legacy `.doc` fails the OOXML magic-number check at the same gauntlet step but for a wholly different reason — wrong format, not a threat — so the UI surfaces a separate, **format-specific rejection message** with actionable guidance:

  | Upload type | UI rejection copy (summary) |
  |---|---|
  | PDF (any, including scanned) | "PDF files cannot be reviewed directly. Please ask the school to send the Word (.docx) original. If only a PDF is available, see RUNBOOK.md for conversion guidance and its tracked-changes caveats." |
  | Legacy `.doc` (binary Word 97–2003) | "Legacy .doc files are not supported. Please convert to .docx (File → Save As → Word Document) and re-upload." |
  | Hostile / unrecognised binary | Generic security-rejection message (no workflow guidance). |

  The format-specific copy is displayed alongside a link to the relevant RUNBOOK section; the hostile-file rejection is a distinct, security-framed message that does not appear for wrong-format uploads and does not appear for hostile-file rejections.

- **No PDF conversion pipeline in v1.** The tool does not automatically convert PDFs or attempt to extract text from a scanned image; converting and reviewing a PDF-derived `.docx` can silently lose the tracked-changes history the counterparty applied. The reviewer guidance in RUNBOOK.md records the workflow: request the `.docx` original when possible; if conversion is unavoidable, understand the tracked-changes caveats before submitting.

Model-generated summaries and any document-derived text are rendered as **escaped text only** — never as HTML. The frontend token, CSP, output-escaping, and stored-XSS posture (the admin UI in particular) is owned by [docs/threat-model.md](docs/threat-model.md).

Hosted on Amplify. Promotion to production is **deliberate**, not a side effect of a merge: CI builds and signs the frontend artifact and prod is advanced to a specific build, consistent with the digest-pinned backend deploy (see [Infrastructure](#infrastructure--aws-cdk)).

### Authentication — Cognito federated to Google

A single Cognito user pool with Google as the only identity provider, restricted to the `company.com` hosted domain. Users do not register; they are provisioned on first sign-in by Cognito's automatic JIT flow.

**Domain membership is authentication, not authorization.** A verified `@company.com` identity proves *who* a user is; it does **not** by itself grant access to a legal-document-review tool. Access requires, in addition to a valid `company.com` token, membership in an **application allowlist** (a Google group `legal-admin@company.com` whose membership is checked at sign-in, or, as a fallback, an explicit allowlist row). A token whose subject is not on the allowlist is rejected with `403` even though it is a valid company identity. The two domain layers below are *prerequisites* to the allowlist check, not a substitute for it.

**Canonical admission path — one path, stated identically here and in RUNBOOK.md.** Admission works as follows:

1. A Google Workspace admin adds the user to the `legal-admin@company.com` group.
2. The user signs in via Google SSO for the first time.
3. The Cognito **pre-token-generation Lambda** checks `legal-admin@company.com` group membership via the Directory API and, on confirmation, **JIT-creates an active users row** (`status=active`) in DynamoDB keyed by the Cognito `sub`. This is the only non-bootstrap admission path.
4. Subsequent sign-ins update `last_auth_at`; the row is never recreated.

The **sync job only deprovisions** — it never auto-admits new members. A user who appears in the group between sync runs is not admitted until they sign in and the Lambda creates their row. There is no path by which a user is admitted without signing in through the Lambda.

**Allowlist source of truth and fail-closed behavior.** The authoritative gate is the **DynamoDB allowlist row** (`users` row in `active` status); the Google group `legal-admin@company.com` is the directory mirror that the sync job reconciles deprovisions into. Checking group membership and reconciling from Workspace require a **Google Directory API service account** (domain-wide delegation), whose credentials live in **Secrets Manager**, are least-privilege (directory read only), and are rotated. Because this is the authorization gate for a legal tool, both the Cognito pre-token-generation Lambda and the backend **fail closed**: if the Directory API is unavailable, the edge check denies, and the backend falls back to the DynamoDB allowlist row as the authoritative decision — it never fails *open*. The deliberate consequence: if Google/Workspace is unreachable, users who are not already allowlisted in DynamoDB cannot get in. "Directory down → deny" is tested at both layers.

**Hosted-domain enforcement is defense-in-depth — enforced in two independent layers:**

1. **At the edge (Cognito).** The Google OAuth request pins the `hd=company.com` parameter, and a Cognito pre-sign-up / pre-token-generation Lambda rejects any identity whose verified email is not `@company.com`. A non-`company.com` account never gets a usable token.
2. **At the backend (every request).** The App Runner JWT validator independently re-verifies the token signature, audience, expiry, **and** that the `email` claim ends in `@company.com` and the Google `hd` claim equals `company.com`. The backend never trusts the edge alone; a token that somehow lacks the domain is rejected. The allowlist check runs **after** the two domain checks pass.

#### Deprovisioning and lifecycle

JIT provisioning alone is a one-way door — it admits users but never removes them. A terminated or transferred employee must not retain access via a lingering refresh token or a stale JIT `users` row. We therefore add:

- **User status.** Each `users` row carries `status` (`active | suspended | deprovisioned`). Authorization requires `status == active` on **every** request, not just at first sign-in.
- **Periodic SSO/Workspace sync — cadence ≤ 1 hour — sync only deprovisions.** A scheduled job reconciles `users` against Google Workspace / the `legal-admin@company.com` group membership (via the least-privilege Directory API service account above) at least every 60 minutes. Identities that have left the group or the directory are flipped to `deprovisioned`; the sync never auto-admits new members (admission is exclusively via the pre-token Lambda path above). A sync run that cannot reach the directory makes **no** changes (it never mass-deprovisions on an API outage, and never auto-admits). A sync failure is alarmed. The 60-minute cadence is the machine-assertable bound referenced in [docs/threat-model.md → Identity and authorization](docs/threat-model.md#identity-and-authorization).
- **Token revocation — access-token TTL ≤ 15–60 minutes.** On deprovisioning we revoke the user's Cognito refresh tokens (global sign-out) so existing sessions cannot be silently extended. The Cognito user pool issues access tokens with a TTL of **15–60 minutes** (configurable, defaulting to 60 minutes); a revoked user loses access within that TTL even between sync runs. The combined worst-case window (sync cadence + max token TTL) is therefore ≤ ~2 hours, documented as an accepted residual risk. This bound is the machine-assertable figure referenced in [docs/threat-model.md → Identity and authorization](docs/threat-model.md#identity-and-authorization).
- **Last-auth check.** `last_auth_at` is recorded; the sync job and admins can spot dormant accounts for review.

Canonical user lifecycle states are:

- `active` — may use the app if also allowlisted.
- `suspended` — temporarily denied even if the Cognito token is valid.
- `deprovisioned` — removed from the app by admin action or directory sync; refresh tokens are revoked.

There is no separate `disabled` state; operator docs and UI labels map urgent removal to `suspended` or `deprovisioned`.

Admin vs reviewer is **not** controlled by Google groups. It is controlled by a flag in the DynamoDB `users` table, settable only by an existing admin. This matches the brief: simple internal list, mutable from the app itself.

**Group-naming misnomer — known, documented.** The allowlist group is named `legal-admin@company.com`, but it serves **all ContractToaster users** (reviewers and admins alike) — the name is a misnomer. Do not treat it as "admins only": a reviewer who is not an admin must still belong to this group for the pre-token Lambda to admit them. The `is_admin` flag in the `users` DynamoDB row (not group membership) is the sole admin-privilege gate. Operators should read "legal-admin" as "ContractToaster allowlist" to avoid accidentally removing reviewers from the group on the assumption that only admins belong there.

The first admin is bootstrapped by the CDK stack on first deploy. Because the `users` table is keyed by Cognito `sub` (which does not exist until first sign-in) we **do not** seed an email-keyed row in the `sub`-keyed `users` table — that would mix two incompatible key shapes in one table. Instead the seed lands in a **separate `admin_bootstrap` table keyed by email** (a stack parameter: the configured GC email). On that user's first sign-in the backend runs a **one-time reconciliation transaction**: it confirms the verified email matches an `admin_bootstrap` row, writes the real `users` row keyed by `sub` with `is_admin=true`, and atomically marks the bootstrap row consumed (conditional write, so the reconciliation cannot run twice or race a concurrent sign-in). The `admin_bootstrap` table is otherwise unused after consumption.

**Break-glass.** If the seed admin is ever lost (wrong email, account disabled, no admin can sign in), recovery does not depend on the app. A dedicated, normally-unused **break-glass IAM role** — assumable only via SSO with MFA and logged in CloudTrail — can write `is_admin=true` directly to the `users` table. Every break-glass use must be recorded in the `audit` table with `reason=emergency-override` and a justification. The procedure lives in [RUNBOOK.md](RUNBOOK.md). There is no other path to admin.

### Backend — App Runner + FastAPI

A Python service in a single Docker container. App Runner is **pinned to an immutable, signed image digest in ECR** — it is **not** wired to auto-deploy from `main`. A merge to `main` must never silently alter production legal behaviour. Instead, CI (CodeBuild or equivalent) runs tests and scans, **builds and signs** a container image, pushes it to ECR, and a **deliberate promotion** advances App Runner to the new digest (see [Infrastructure](#infrastructure--aws-cdk)). Rollback is a re-pin to the prior digest.

The service is stateless. All state lives in S3, DynamoDB, and the Bedrock Knowledge Base.

#### API vs. async worker

App Runner runs the **API only** — fast, request/response work: auth, validation, persistence, starting and polling reviews, and admin CRUD. It never blocks on an LLM call. `POST /api/reviews` stores the upload, reserves estimated spend, creates the `reviews` row as `PENDING`, and **starts a Step Functions execution directly** (no SQS), returning `202` with the review id immediately. The browser polls `GET /api/reviews/{id}`.

**Idempotency (no SQS buffer on the entry path).** SQS has been removed from the review entry path; the API calls `StartExecution` directly. Idempotency is achieved without a queue, but the transaction is explicit so retries cannot double-reserve spend or leave orphan `PENDING` reviews:

1. `POST /api/reviews` uses an **idempotency key**. The preferred source is a **client-supplied key** held stable across a client's own retries (double-click, network timeout, mobile re-send). Absent that, the API derives one from `owner_sub` + uploaded-file SHA-256 + active release-bundle hash + a **coarse timestamp bucket** of a documented, fixed width (default **10 minutes**). Because a derived key would otherwise change at a bucket boundary — letting a boundary-straddling retry start a second pipeline and double-reserve spend — the API checks **both the current and the immediately previous bucket** for an existing submission record before creating a new one. A deliberate re-review of the same file is an **explicit "review again" action** that mints a fresh key on purpose, rather than something a retry can trigger by accident.
2. The API creates a **submission record** with a conditional write on that idempotency key. That record owns the canonical `review_id`, upload pointer, spend-reservation ID, Step Functions execution name, execution ARN, and status.
3. Spend is reserved **once per `review_id`**, not once per HTTP attempt, as a **worst-case upper-bound** estimate (see [Cost shape](#cost-shape)). A retry that finds the submission record reuses the existing reservation.
4. The API stores the upload and creates/updates the `reviews` row only through the submission record.
5. The API then performs an idempotent **ensure execution started** step. If `StartExecution` succeeds, the execution ARN is stored. If a retry sees no ARN, it calls `StartExecution` again with the deterministic name. If Step Functions returns `ExecutionAlreadyExists`, the API records the existing execution and returns the existing review id.

**Worst-case spend reservation.** The reservation happens in the API *before* extraction, so the real token cost is not yet known. To keep the daily ceiling safe against concurrent submissions, the reserved estimate is a **worst-case upper bound** derived from the per-review caps: `passes × (1 + max_retries_per_pass) × (max_input_tokens + max_output_tokens)` at **uncached** pricing. Folding `max_retries_per_pass` into the reservation at reserve-time means the reservation is provably ≥ worst-case settle for any sequence of attempts within the allowed retry budget — bounded structured-output retries and throttling retries are all accounted for, not just the two passes. An optimistic estimate that ignored retries would let N concurrent uploads each reserve low and collectively overshoot before any settles; a retry-inclusive worst-case estimate makes the atomic reservation a true ceiling. Settlement (after the run, from the ledger) corrects the estimate downward. At the default `$20/day` ceiling this admits **~9 worst-case reviews per day** (arithmetic: $20 ÷ ~$2.11 worst-case/review) or roughly 22 typical reviews; the exact per-review caps, the unit-economics table, and the max-reviews/day figure are documented in [Cost shape](#cost-shape) and surfaced on the admin dashboard.

**Recovering an orphaned submission — missing ARN and dead-execution paths.** A scheduled/event-driven **reconciler** handles two distinct stuck-state holes:

1. **Missing ARN** (existing path). For any submission record older than a short threshold that still has no `execution_arn` (e.g. a crash between upload persistence and `StartExecution`), the reconciler re-runs the idempotent "ensure execution started". It only escalates to a human (stale-`PENDING` alarm) if the re-drive still fails.

2. **Dead execution — PENDING-with-dead-ARN** (new path). Step Functions execution names are unique for ~90 days including terminal executions, so if an execution dies before its error-handling states run, a bare "ensure execution started" gets `ExecutionAlreadyExists` and would naïvely record the existing (corpse) ARN — pinning the review to a dead execution while the review row sits `PENDING` with an ARN, invisible to the missing-ARN alarm. The reconciler therefore calls **DescribeExecution** on any non-terminal review that already has an `execution_arn` and whose execution status is **FAILED, TIMED_OUT, or ABORTED** (a terminal execution whose error-handling states did not complete). On detecting a dead execution, the reconciler: (a) transitions the review row to **`ERROR`**, (b) **releases the spend reservation** (settles to zero if no ledgered spend), and (c) **releases the concurrency slot** so subsequent reviews are not blocked. It records the terminal execution status and the reconciler run ID in the audit entry. The stale-`PENDING` alarm is extended to cover this case: a `PENDING` review whose `execution_arn` resolves to a terminal execution is treated the same as a review with no ARN — it is a stuck review, not a healthy one.

There is no SQS queue, consumer, or DLQ buffering submissions; pipeline-level durability comes from Step Functions' own execution history, per-step retries, the submission record's recovery contract, and the orphan reconciler above.

The review itself — extraction, normalization, standard-form diff, retrieval, two-pass LLM review, leakage scan, redline generation — runs **off the request path** in a Step Functions state machine whose steps are Lambda / Fargate tasks. This removes the request-timeout, retry, and partial-failure problems of doing minute-scale LLM work inside an HTTP handler, and lets each stage retry independently. See [Data flow](#data-flow--a-single-review).

#### Why App Runner and not Lambda for the API

The API is a warm, always-on HTTP surface; App Runner gives us a warm Python process with configurable concurrency, predictable cost, and a simple digest-pinned deploy. The long-running review work that *would* have strained Lambda's cold-start/timeout profile is exactly the part we moved into Step Functions, where step-level timeouts and retries are first-class.

#### Routes

| Method | Path                       | Auth          | Purpose                               |
|--------|----------------------------|---------------|---------------------------------------|
| GET    | `/health`                  | none          | Liveness only (`{"status":"ok"}`) — minimal, no build details |
| GET    | `/version`                 | allowlisted   | Version, commit SHA, serving image digest (authenticated) |
| POST   | `/api/reviews`             | allowlisted   | Upload a `.docx` with an optional `playbook_id` selector (defaults to `eiaa`); idempotency-keyed; returns `202` + review id, starts async pipeline directly |
| GET    | `/api/reviews`             | allowlisted   | List my reviews (admin: all reviews)  |
| GET    | `/api/reviews/{id}`        | owner or admin| Get a review's status and result      |
| GET    | `/api/reviews/{id}/output` | owner or admin| Download the redlined `.docx`         |
| GET    | `/api/playbooks`           | admin         | List playbooks                        |
| GET    | `/api/playbooks/{id}`      | admin         | Get current version of a playbook     |
| GET    | `/api/playbooks/{id}/versions` | admin     | Version history                       |
| POST   | `/api/playbooks/{id}/versions` | admin     | Upload a new version (validated)      |
| GET    | `/api/audit`               | admin         | Query audit log                       |
| GET    | `/api/users`               | admin         | List users and admin flags            |
| PATCH  | `/api/users/{sub}`         | admin         | Set admin flag or lifecycle status    |
| POST   | `/api/corpus`              | admin         | Upload an executed agreement to corpus|
| POST   | `/api/corpus/reindex`      | admin         | Ingest into a new staging index → new draft snapshot (never mutates the active store); supersedes the old "re-index after corpus changes" semantics which implied a mutating path incompatible with the draft/staging/activation design |
| POST   | `/api/playbooks/{id}/deactivate` | admin (GC-gated) | Deactivate the currently active bundle for this playbook without promoting a successor — explicitly leaves no bundle active, suspending intake. Audited. Distinct from rollback (which requires a prior bundle). Returns `409` if no bundle is currently active. |

**No-active-bundle system state.** When no release bundle is active for a playbook (either because the first-ever bundle was deactivated before a successor was promoted, or because deactivation was used to suspend intake deliberately), `POST /api/reviews` resolves no active bundle and **refuses the request** with HTTP `503` and the user-visible message **"no active playbook"**. New reviews cannot be submitted until an admin activates a bundle. In-flight reviews (`PENDING` / `RUNNING`) that were started while a bundle was active continue to completion — deactivation does not abort them; their bundle was resolved and recorded at submission (step 3). The no-active-bundle refusal fires at step 3 of the data flow (bundle resolution), before any spend is reserved or a submission record is created.

### LLM — Bedrock + Claude (Opus 4.8 primary, Sonnet 4.6 critic)

We call Bedrock's `InvokeModel` with Anthropic Claude in `us-east-1` using the **single-region native model ID** (e.g. `anthropic.claude-opus-4-8`), invoked against the regional endpoint — **never a cross-region inference profile**. Per-token rates match the direct Anthropic API. Bedrock's data plane keeps prompts and outputs inside the AWS boundary and contractually disclaims any training use. The two passes use **different** models (primary Opus 4.8, critic Sonnet 4.6) so their blind spots are decorrelated and the critic pass is cheaper.

#### Model-selection policy

The model is governed by an explicit **model-policy matrix**, not a vague "best model" default. The policy pins:

- **A specific in-region model ID and region for each role.** The release bundle records `primary_model_id`, `critic_model_id`, **`embedding_model_id`**, optional `fallback_model_id`, `model_region`, request contract, evaluation run, and cost assumptions. v1 pins **Opus 4.8 as the primary reviewer and Sonnet 4.6 as the adversarial critic** (a deliberately *different* critic — see [Two-pass review](#llm--bedrock--claude-opus-48-primary-sonnet-46-critic)) in `us-east-1`, unless a later evaluated policy deliberately changes that matrix.
- **Single-region native inference only — no inference profiles.** Legal-facing data must run in the one region named by the model policy. We use the **native model ID** invoked against the regional endpoint. **Both `global.` global profiles and `us.`/`eu.`/`apac.` geo cross-region inference profiles are forbidden** in configuration — a geo profile can route a request to another region in the geography (e.g. a `us.` profile to us-east-2 or us-west-2), which breaks a strict `us-east-1` residency guarantee. A config check rejects any model ID carrying a `global.`/`us.`/`eu.`/`apac.` prefix unless explicitly approved and recorded. (Research note: as of 2026, Opus is available on-demand via the single-region native ID in `us-east-1` and is Standard-tier only, so this residency posture is achievable **without** Provisioned Throughput — preserving the $0-idle cost model.)
- **The embedding model is governed too.** `embedding_model_id` is pinned, recorded on every review, recertified quarterly, and a change to it (or any re-embedding of the corpus) requires **admin (GC) approval** and produces a new `corpus_snapshot_version` — because changing embeddings changes which precedents are retrieved, and therefore legal output, as surely as a prompt change.
- **A deliberate pin, with rationale.** The matrix is a deliberate pin, not "whatever is newest". A newer model is adopted only after it passes the same gold-set, stochastic-stability, redline-fixture, leakage, and cost gates.
- **Quarterly recertification.** The pinned model matrix (primary, critic, embedding, fallback) is recertified at least quarterly: re-run the gold set, compare false-positive/false-negative rates and redline-patch behaviour, review current AWS model availability and the exact invocable model IDs, and either re-pin (with an audit entry) or document why the incumbent stays. The model IDs, region, request contract, and certifying eval run are recorded with each active release bundle.
- **On-demand quota recorded and throughput ceiling derived.** Forbidding geo/global inference profiles removes cross-region load balancing, so throughput is bounded by the native on-demand TPM/RPM quota for each model in `us-east-1`. The granted quota is recorded in **`model-policy/bedrock-us-east-1.json`** (`models.primary.granted_tpm`, `models.critic.granted_tpm`) alongside the derived `review_throughput_ceiling` and `max_eval_parallelism` fields. Production volume (2–7/day) is safely under any plausible quota; the eval harness (39 cases × 2 passes × multiple stochastic runs at ~60K tokens/call with CI parallelism) is the stress case — it is rate-limited to the recorded quota. Quota figures are re-verified at each quarterly recertification; a stale figure causes the eval harness to under-utilize (too conservative) or throttle (too aggressive).
- **Fallback model policy — automatic failover is prohibited.** If a `fallback_model_id` is recorded in the active release bundle, **automatic failover is prohibited**: the pipeline must never silently substitute a different model for the pinned primary or critic on a `ThrottlingException` or other transient error. An automatic fallback would swap a frontier model for a lighter-weight model on legal output without any record, which is exactly the undocumented drift the model-policy matrix exists to prevent. Fallback use is **manual-only**: an admin (GC) action, audited, and only under a separately activated release bundle that carries a certifying eval-run reference for the fallback model — the fallback model must pass the same gold-set, stochastic-stability, and redline-fixture gates as the primary. Every review run under a fallback bundle records `fallback_used=true` and the fallback model ID. A `fallback_model_id` that has no associated certifying eval run in its release bundle must not appear in the active configuration; the v1 seed playbook omits `fallback_model_id` until Haiku has a qualifying eval run.

The request schema is pinned to the exact contract the pinned models accept: we **omit the `temperature`, `top_p`, and `top_k` sampling parameters** (AWS documents these as no longer supported for the current Opus/Sonnet generation) and rely on the model's default decoding. If extended thinking is used it is **adaptive-only** (the model controls the thinking budget); we do not assume a manually-set thinking-token budget. The exact invocable native model IDs are verified at bootstrap (AWS occasionally renames IDs and version suffixes differ by model).

**Prompt structure** (every review):

1. **System prompt.** Combines (a) review guidance adapted from `claude-for-legal`'s `contract-review` skill (see [Redlining](#redlining--owned-docx-library) and design notes — this is an internal fork we own, not a live dependency), (b) our binary-decision overlay (collapse the upstream GREEN/YELLOW/RED to `ACCEPT | REQUEST_CHANGE`), and (c) the current playbook JSON.
2. **User prompt.** Defined precisely by the per-pass manifest below — see [Per-pass prompt manifest](#per-pass-prompt-manifest). **Both the counterparty document and the retrieved precedent text are untrusted input.** All untrusted content is wrapped in explicit delimiters with an instruction that nothing inside any delimited block is an instruction to the model (see [Security posture](#security-posture) and [docs/threat-model.md](docs/threat-model.md)).
3. **Response format.** A structured JSON output specifying the overall decision and a per-issue list, each issue carrying `section_ref`, `section_title`, `counterparty_change_summary`, `decision`, `external_rationale_for_footnote`, `proposed_replacement_text`, `playbook_topic_id`, and `internal_precedent_citation`. The response is **strictly schema-validated** before it is allowed to produce a redline. Internal precedent citations are audit-only and are stripped from generated `.docx` footnotes.

#### Per-pass prompt manifest

The user-prompt content is defined per-pass. **Manifest changes are prompt changes and are therefore release-bundle gated** — a manifest change requires the same release-bundle activation as any other prompt change. The assembled size of every pass (system prompt + user prompt) is asserted against `max_input_tokens` before any model call; see [Data flow](#data-flow--a-single-review) step 14.

| Block | Primary pass | Critic pass |
|---|---|---|
| Standard-form diff (anchored hunks: standard text, counterparty text, delta, section_anchor) | **Always included** | **Always included** |
| Anchored clause text (standard + counterparty + delta per clause) | **Always included** | **Always included** |
| Retrieved precedent clauses (top-K, hard-labeled positive/negative, from Knowledge Base) | **Always included** | Omitted — critic reasons over the diff and the primary output; re-sending precedents adds tokens without improving missed-issue detection |
| Full counterparty document text | **Included only if doc token count ≤ `full_doc_token_threshold` (default: 15,000 tokens)**; above threshold, replaced by section outline (heading + word count per section) | **Not included** — the critic does not receive the raw counterparty document; the diff + anchored clauses already encode all counterparty changes, and omitting the full doc from the critic removes the primary redundancy identified in the architecture review 2026-06-11 |
| Primary reviewer's output (full structured JSON) | Not applicable (primary produces this) | **Always included** — the critic is tasked with finding missed issues, over-flagging, weak rationales, and replacement-text drift against the primary output |
| Section outline (heading + word count per section) | **Included only if doc exceeds `full_doc_token_threshold`** (replaces full doc above threshold) | Not included |

**Rationale for full-doc threshold (primary).** For a mostly-clean EIAA the full-doc block (~8–15K tokens) is largely redundant with the diff + anchored clauses. Below the threshold it provides useful verbatim context at modest cost; above the threshold the marginal value does not justify the token load, and a section outline preserves navigational context at a fraction of the size. The threshold is set in the active model-policy configuration and recorded per review. See [docs/design-notes.md](docs/design-notes.md) for the decision rationale.

**Rationale for critic input (critic).** The critic must catch what the primary missed. The diff + anchored clauses already encode every counterparty change the primary reasoned over; the primary output shows the primary's conclusions; and sending the full raw document to the critic would send the same 8–15K-token contract body a third time without giving the critic any additional signal about *what changed* — which is the context the critic needs to identify missed issues. Omitting the raw doc from the critic also eliminates the ambiguity in the 2026-06-11 architecture review finding: the critic can no longer reason off the flat document instead of the diff. See [docs/design-notes.md](docs/design-notes.md) for the efficacy argument and the design decision.

**Structured-output failure handling.** Strict validation alone is too brittle — a transient formatting slip should not be indistinguishable from a real pipeline failure, and we never best-effort patch malformed JSON. On a schema-invalid response we perform **exactly one bounded structured-output retry** (re-prompt for valid JSON). If the retry also fails, the review terminates as **`ERROR_MANUAL_REVIEW_REQUIRED`** — a distinct outcome from a pipeline `ERROR` (infrastructure/step failure). The former routes a human to look at a model that won't conform; the latter is an operational incident. Neither produces a redline.

**Replacement text is model-generated, not templated.** Per the [model-selection policy](#model-selection-policy) we run the pinned primary model (Opus 4.8) and rely on its judgment *against the codified playbook guidelines* to draft `proposed_replacement_text` and the footnote rationale. We deliberately do **not** ship 100% scripted/canned clause language — the playbook constrains the position, the model writes the prose to fit the counterparty's specific draft.

**Two-pass (adversarial) review.** Each review runs two model passes as distinct Step Functions steps:

1. **Primary reviewer** — produces the decision and per-issue list as above.
2. **Adversarial critic** — a second pass, using the `critic_model_id` pinned in the active model-policy matrix, is given the playbook, the standard-form diff, the anchored clause text, and the primary reviewer's output (the critic does not receive the raw counterparty document — see [Per-pass prompt manifest](#per-pass-prompt-manifest)), and is tasked with finding what the primary pass got wrong: missed issues, over-flagging, weak rationales, replacement text that drifts from the playbook position.

**Deterministic reconciliation.** The two passes are merged by code, not by a third model call, under fixed rules so the outcome is reproducible and auditable:

- **Hard rejections are monotonic.** Any hard rejection raised by *either* pass forces the overall decision to `REQUEST_CHANGE`. The critic cannot downgrade a hard rejection the primary found, and vice-versa.
- **The critic adds, it does not silently rewrite.** The critic may add issues and may flag the primary's `proposed_replacement_text` as drifting, but it may **not** silently overwrite the primary's replacement text. A contested replacement is surfaced as a critic delta, not swapped in invisibly.
- **Deltas are preserved, but not all in immutable audit.** The final result retains both the primary output and the critic's deltas (added issues, contested replacements, rationale objections). Non-substantive facts about those deltas are written to immutable audit; substantive rationales or clause text live only in retention-governed confidential storage so document retention still means something.

**Critic-pass failure is terminal — never a silent single-pass DONE.** If the critic invocation fails terminally (after its bounded retry: throttle, outage, or persistently schema-invalid critic output), the review must land in `ERROR` (if the failure is an infrastructure or availability problem) or `ERROR_MANUAL_REVIEW_REQUIRED` (if the critic will not produce schema-valid output after one retry). A critic failure must **never** produce a silent single-pass `DONE` — silently losing the decorrelation control would undermine the only reason two passes exist. The Step Functions step for the adversarial pass has its own timeout and retry budget; exhausting that budget is a terminal failure of that step, and the pipeline status transitions accordingly. Every critic invocation attempt — including retries and failures — is ledgered in the spend ledger's finally path.

This catches single-pass misses on the edge cases that are the whole reason the tool exists, at the cost of one extra inference per review (factored into the cost shape and the daily ceiling).

**Prompt caching is a within-model optimization, not a cost guarantee.** Caching is enabled on the playbook block (≈30K tokens, changes rarely) and the static review-guidance content. Cache hits are **per-model**: the Opus 4.8 primary-pass cache can never serve the Sonnet 4.6 critic — each model maintains its own independent cache. Within a single model, a cache hit reduces per-call cost and latency. At v1 production volume (2–7 reviews/day spread across a workday against a ~5-minute TTL), the inter-review steady-state hit rate is **near zero** — the time between reviews is far longer than the cache TTL. Caching therefore delivers savings primarily on **back-to-back retries and eval runs** (where calls are sequential within seconds or minutes), not on typical spaced production usage. The cost model and the daily ceiling are sized to **survive a 0% cache-hit run** — a cache miss makes a review more expensive, never breaks it.

**Bedrock alarm classification — throttle retries vs genuine errors.** The pipeline and the eval harness both perform exponential-backoff retries on `ThrottlingException` (quota pressure); these retries are **not errors** and must not fire the "Bedrock invocation errors > 0" CloudWatch alarm. The alarm split is:

- **`bedrock-invocation-errors` alarm** — fires when a Bedrock call returns a non-throttle error: `AccessDeniedException`, `ValidationException`, `ModelNotReadyException`, or any unclassified exception. These indicate a genuine problem requiring human investigation. **`ValidationException` with "input is too long" specifically indicates cap misconfiguration**: the step-14 cap check (see [Data flow](#data-flow--a-single-review) step 14) is the single authoritative failure point for oversized documents and fires before any model call, so a model-side input-too-long ValidationException is unreachable when the cap is correctly configured — its sole cause is a misconfigured `max_input_tokens` value set above the model's actual context limit.
- **`bedrock-throttle-retries` alarm** — fires when `ThrottlingException` retry count exceeds the configured threshold (default: 5 per 5-minute window), indicating sustained quota pressure. Informational by default; escalated to a page if retries cause SLA misses or require manual quota intervention.

CloudWatch metric filters key on the Bedrock error code (CloudTrail management events): the error alarm filters on `errorCode NOT IN ('ThrottlingException')`; the throttle alarm filters on `errorCode = 'ThrottlingException'`. The granted quota figures, derived `review_throughput_ceiling`, and `max_eval_parallelism` are recorded in **`model-policy/bedrock-us-east-1.json`** (see [Model-selection policy](#model-selection-policy)). The incident-response procedure for quota exhaustion is in [RUNBOOK.md → Bedrock returns errors](RUNBOOK.md).

#### Output leakage scan

**Scan scope — all human-surfaced model prose.** The leakage scan covers every model-generated field
that is surfaced to a human: `verdict_summary` (on **both** the ACCEPT path and the REQUEST_CHANGE
path), `external_rationale_for_footnote` (footnotes), `counterparty_change_summary`, critic deltas
(rationale and contested replacement text), and `proposed_replacement_text` (the field that feeds
the redline `.docx`). The ACCEPT path is **not** a bypass of the scan — a `verdict_summary` that
contains a verbatim playbook fragment or a system-prompt token is held for manual review rather than
rendered in the UI. Critic deltas shown in the admin view are likewise scanned before storage or
display. The full scope table is in [docs/output-contract.md → Leakage scan scope](docs/output-contract.md#leakage-scan-scope--all-human-surfaced-model-prose).

**What the scan detects.** Model output is scanned for system-prompt or playbook leakage,
internal-policy disclosure, excessive verbatim precedent quotation, and external-facing confidential
rationale. Output that leaks internal material is held for manual review rather than rendered into a
deliverable (see [docs/threat-model.md → Model output leakage](docs/threat-model.md#model-output-leakage)
for the full threat treatment and mechanism).

**Why we don't fine-tune.** See [docs/design-notes.md](docs/design-notes.md). In brief: fine-tuning isn't available for Opus, 50 examples is too few to overcome a pre-trained model's prior, and we lose the audit trail that RAG provides. RAG + a codified playbook gives us instant updates and per-decision citations.

### Standard-form comparison

Semantic precedent retrieval is not enough on its own to know *how the counterparty changed our paper*. Issue-spotting starts from a **deterministic diff against your canonical standard form**, not from the uploaded document alone.

- **Canonical standard form per playbook version.** Each playbook version stores the canonical standard-form `.docx` it corresponds to (content-addressed, alongside the playbook snapshot — see [Playbook versioning](#data-flow--a-single-review) and [docs/evaluation.md](docs/evaluation.md)). The standard form is versioned in lockstep with the playbook so a review always diffs against the form that was current when the review ran.
- **Deterministic diff.** Inside the pipeline, the (normalized) uploaded draft is diffed against the canonical standard form for the active playbook version using a deterministic, paragraph/table-cell-anchored diff (not a model call). The diff yields the exact insertions, deletions, and modifications the counterparty made, anchored to specific clauses.
- **The model sees the diff, not just the upload.** We feed the model the **diff plus the anchored clause text** (standard text, counterparty text, and the delta between them), so it reasons about *what changed and whether the change is acceptable against the playbook*, rather than re-deriving the deviations from a raw document. Retrieved precedent (below) supplements this; it does not replace it.

The same anchors produced here drive exact-match redline patching (see [Redlining](#redlining--owned-docx-library)).

**Diff generator implementation (issue #64).** `scripts/diff_standard_form.py` implements the deterministic diff: it loads the canonical standard-form paragraphs (`load_standard_form_paragraphs()` — synthetic mode derives per-anchor text from each covering topic's `our_standard` field until the real `.docx` is committed to `standard-forms/`; real-`.docx` mode extracts paragraph text directly, same optional `python-docx` convention as `scripts/build_anchor_map.py`) and diffs them against the uploaded draft's normalized paragraphs (`diff_draft_against_standard()`), matching by heading text against the anchor map. Every hunk carries `anchor`, `kind` (`unchanged` | `modified_new` | `deleted` | `inserted`), `text`, and — for hunks that touch existing standard-form text — a `source_text_hash` (SHA-256 of the standard-side text) that the redline-patching path (issue #17) validates on an exact-match, fail-closed basis before applying any edit. A draft paragraph whose heading matches no standard-form section is anchored to the reserved pseudo-anchor `sec-_new` (never `deleted`/`unchanged`; see below). `serialize_diff()` / `diff_hash()` give a stable, sorted-key JSON serialization and SHA-256 so the same inputs always produce the same diff — the CI gate for this is `tests/diff/test_deterministic_diff.py` (`.github/workflows/standard-form-diff-gate.yml`), which exercises a verbatim draft (must diff to all-`unchanged`), a sec-8 modification (must produce a `deleted`/`modified_new` hunk with a `source_text_hash`), and a wholly new inserted section (must anchor to `sec-_new`). The model prompt content that consumes this diff is Phase 2 (out of scope for issue #64).

#### Section-anchor map (deterministic detector scoping)

The canonical standard-form `.docx` for a playbook version is parsed once, at bundle-build time, into a **section-anchor map**: each heading / clause / table cell gets a stable `section_anchor` (`sec-8`, `sec-1.2`, `sec-10-precedence`, with **sub-clause anchors** under §10 — `sec-10-notices`, `sec-10-non-exclusive`, `sec-10-merger`, `sec-10-precedence` — so the four §10 topics don't collide). Each topic in the playbook lists its `section_anchors[]` (the machine key; `section_ref` is display-only). The deterministic diff tags **every hunk** with the `section_anchor` of the standard section it falls under, so a hard-rejection rule scoped to a topic reads **only** the diff hunks whose anchor is in that topic's `section_anchors`. This is what makes `applies_to_topics` deterministically enforceable.

**Content-addressed anchor map.** The anchor-map builder (`scripts/build_anchor_map.py`) produces a versioned, hashed artifact — `standard-forms/eiaa-v<version>.anchor-map.json` — that contains each anchor's heading text and `heading_hash` (SHA-256 of the heading text). The artifact carries an `anchor_map_hash` field (SHA-256 of the canonical anchor JSON) that is part of the **release bundle**: `standard_form_hash`, `anchor_map_hash`, `prompt_hash`, `model_policy_hash`, `corpus_snapshot_version`, `eval_run_id`, and signed `legal_approval`. The `anchor_map_hash` field is required in the release bundle — a bundle cannot be activated without it. Every production review records `anchor_map_hash` so anchor-map lineage is auditable.

**Heading-hash drift gate.** When the standard form is revised, heading text changes (e.g., renumbering §8 to §9) would silently produce wrong section scoping without a governance check. CI (`tests/anchor/test_heading_hash_drift.py`) diffs the current anchor map against each known form revision: if any anchor's `heading_hash` has changed and no `anchor_migrations` record in the playbook covers it, the gate **fails with DRIFT WITHOUT MIGRATION**. This is the normative rule: an anchor whose heading hash changes without a covering migration record fails the drift gate. Authors must add an `anchor_migrations` entry in the playbook (GC-approved) before CI can pass. See RUNBOOK.md "Revising the standard form" for the procedure.

**Form-coverage gate.** CI (`tests/anchor/test_form_coverage.py`) verifies every anchor in the map has exactly one covering playbook topic (or an explicit, reviewed `coverage_exempt_anchors` entry). `coverage_exempt_anchors` and its `coverage_exempt_rationales` are **canonical in the anchor map** (`standard-forms/eiaa-v<version>.anchor-map.json`), carried as siblings of `anchors` — not in the playbook. An exemption is a reviewed property of the standard-form section it describes (the section carries no reviewable legal clause, e.g. preamble, signature block, or a structural parent heading), so it is governed alongside the form it describes. The gate reads exemptions from the anchor map only; the playbook never carries this list. This gate prevents silent coverage regressions when the standard form gains a new section.

**Reserved pseudo-anchor `sec-_new` (new inserted sections).** Topics whose position is on a clause **not in the standard form** (indemnification, insurance, governing-law) set `not_in_standard: true` and carry `section_anchors: ["sec-_new"]`. `sec-_new` is a reserved pseudo-anchor: the diff tagger assigns it to any inserted hunk that **does not fall inside any existing standard-form section** — a wholly new article or clause the counterparty has added. This gives `on_insert` rules scoped to `not_in_standard` topics a well-defined, non-empty hunk scope, so they can fire on the highest-risk insertions (standalone indemnification articles, excess insurance sections, counterparty-home arbitration clauses). `sec-_new` is assigned only to inserted/modified-new hunks; it is never assigned to deleted or unmodified hunks. Without this pseudo-anchor, `on_insert` rules for `not_in_standard` topics would have an empty effective scope and could never fire — they would be dead config. `on_remove_or_alter` rules must not reference `sec-_new` (you cannot remove a clause the standard form never contained). CI verifies every `section_anchor` other than `sec-_new` resolves to a real section of the bundled standard form; `sec-_new` is exempt from that resolution check. CI also verifies that every `not_in_standard: true` topic carries exactly `["sec-_new"]` and no other anchors, and that every present standard section maps to exactly one topic, and that every `on_remove_or_alter` rule's `required_tokens` are actually present in its anchored section (a protective rule guarding an absent token is dead config and fails the build).

### Retrieval — Amazon Bedrock Knowledge Bases (S3 Vectors)

Retrieval is an **Amazon Bedrock Knowledge Base** over the executed-agreements corpus, backed by **Amazon S3 Vectors** as the vector store. This is a deliberate change from a standalone OpenSearch Serverless collection: OSS carries a fixed OCU minimum (~$350/mo) that dominated the cost shape even at zero usage, whereas S3 Vectors is pay-per-use with no idle floor — keeping us inside the ≤ $100/mo target and the ~$25/mo idle goal. It is still a *real* managed vector store (not an in-memory shortcut), so the design has headroom to scale to many more documents and future agreement types. See [docs/design-notes.md](docs/design-notes.md) for the full rationale and the Aurora-Serverless-v2 fallback.

#### Semantic retrieval plus a deterministic lexical layer

Semantic-only retrieval is a poor default for legal issue-spotting: Bedrock KB semantic search can miss the exact terms that are non-negotiable. Retrieval therefore has **two layers**:

1. **Semantic (S3 Vectors).** Top-K most analogous clauses for nuance and precedent.
2. **Deterministic rule detectors over the standard-form diff (not raw text).** A rule layer runs over the **deterministic standard-form diff** — the anchored insertions, deletions, and modifications the counterparty made (see [Standard-form comparison](#standard-form-comparison)) — **never over raw full-document text**. Running over raw text is a defect: most legal terms of art (`limitation on liability`, `consequential damages`, `business associate`, `employee`, `non-exclusive`) appear in the canonical standard form itself and in fully-compliant drafts, so a raw-text matcher would force `REQUEST_CHANGE` on clean drafts that must `ACCEPT`. Each `hard_rejections` rule declares a **`kind`**:
   - **`on_insert`** (additive prohibition) — `trigger_terms` (e.g. `indemnify`, `hold harmless`, `duty to defend`, `school official`, `business associate`, `exclusive`) are matched **only against inserted / modified-new diff spans**, scoped to the rule's `applies_to_topics`. Optional `exempt_terms` guard against substring false positives (e.g. `exclusive` must not fire inside the compliant `non-exclusive`). Deleted text is never scanned.
   - **`on_remove_or_alter`** (protective invariant) — fires when a `required_token` that is present in the **standard** side of the anchored section is **deleted or materially altered** on the counterparty side (e.g. the `$150,000` cap or the `consequential damages` waiver disappears from §8). Because the review is a diff against a known standard form, a removal is directly visible — preservation detection stays simple and diff-driven, with numeric-threshold judgments left to the LLM and `reject_if_proposed`.

   A detector hit within scope is a deterministic hard rejection regardless of what the vector search returns, and (per the [reconciliation rules](#llm--bedrock--claude-opus-48-primary-sonnet-46-critic)) forces `REQUEST_CHANGE`. The full rule grammar, the per-kind field requirements, and the CI gates that guarantee **zero fires on a clean draft** live in [playbooks/schema.json](../playbooks/schema.json) and [docs/playbook-governance.md](docs/playbook-governance.md); the gold-set gates are in [docs/evaluation.md](docs/evaluation.md). Rules that lexical matching fits poorly (one-way confidentiality, bare payment terms) are **not** forced into a detector — they are handled by the LLM review against `reject_if_proposed`.

#### Metadata model (fits the S3 Vectors limits)

AWS documents roughly **~1KB of custom metadata and a ~35 metadata-key limit** per vector for **S3 Vectors used as a Bedrock KB vector store** (the [Using S3 Vectors with Amazon Bedrock Knowledge Bases](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html) doc). Note this Bedrock-KB-path limit is **tighter** than the raw S3 Vectors limits (which are higher — on the order of 40KB total / 50 keys per vector, with ~2KB of that *filterable*); we design to the Bedrock-KB figures because that is the path we use. These limits are verified at the bootstrap/spike stage and re-checked if AWS changes them. Storing full clause text as vector metadata does not fit and is **not** done. Instead, vector metadata carries only **compact IDs and filter fields**; the full clause text and rationale live in S3/DynamoDB keyed by an **immutable clause ID**:

- `clause_id` (immutable; the join key to full text in S3/DynamoDB)
- `source_document_id`
- `corpus_snapshot_version` (the snapshot this vector belongs to; the query-time filter — see [activation](#corpus-versioning-and-activation-boundaries))
- `corpus_polarity` (`positive` | `negative`) — see below
- `document_type` (`executed-final`, `accepted-draft`, `rejected-draft`)
- `playbook_id` (the playbook this vector was ingested under; **required retrieval filter** — topic IDs like `confidentiality` or `indemnification` are not globally unique and will collide across agreement types, so every retrieval query must filter on `playbook_id` in addition to `playbook_topic_id`. Defaulting to `eiaa` for v1; a second agreement type will use a different value and must not contaminate EIAA retrieval results.)
- `playbook_topic_id` (mapped during ingestion; scoped within `playbook_id`)
- `counterparty_name`, `date` (filter fields)

Full clause text is fetched by `clause_id` from S3/DynamoDB after retrieval, never stored inline in the vector index.

#### Curation: not every executed clause is good precedent

An executed agreement can contain a one-off concession that should never be reused. The corpus carries **legal-curated** fields so retrieval can respect that:

- `reusable_precedent` (curated boolean — defaults false until a lawyer marks it reusable)
- `negotiation_context` (why this language exists)
- `superseded_by` (clause ID that replaces this one)
- `approved_use_scope` (where this precedent may be applied)

#### Separate positive and negative corpora

Rejected drafts can poison the model if commingled with accepted precedent. Positive precedent and **rejected/negative examples live in separate corpora** (or, equivalently, are partitioned by `corpus_polarity`). Rejected language only ever reaches the model through a **controlled, hard-labeled negative-example channel**; rejected clauses are never placed in the same top-K positive-precedent context. The labels are explicit in the prompt (see [LLM prompt structure](#llm--bedrock--claude-opus-48-primary-sonnet-46-critic)).

#### Corpus versioning and activation boundaries

Every review records the **active corpus snapshot / ingestion-job version** it ran against, so a result is reproducible against a known corpus state. A review **must not run against a corpus while an ingestion job is partially complete** and it also must not query a merely uploaded or draft corpus snapshot.

**Snapshots are an application-layer construct — Bedrock KB has no native snapshot.** A Bedrock Knowledge Base ingests into a **single live vector index that it mutates in place**; it provides no point-in-time, immutable, activatable snapshot primitive and no transactional isolation of in-flight ingestion from concurrent queries. We therefore build "draft → active snapshot" ourselves with four mechanisms:

1. **Active store + staging index.** Reviews query an **active** vector store. A candidate snapshot ingests into a **separate staging index** (a separate KB / S3 Vectors store), so an ingestion in progress never mutates the index reviews are reading. **Activation repoints the active reference** (the application's recorded "active KB id / snapshot version") from the old store to the validated staging store, recorded in audit.
2. **Physical store ID pinned per execution.** At Step Functions execution start (data-flow step 10), the pipeline records the **physical store ID / KB identifier** of the currently active store in the execution input — not only the `corpus_snapshot_version` label. Every subsequent retrieval query within that execution queries that exact pinned store / KB ID directly. This eliminates the repoint-race hazard: an activation repoint that occurs mid-execution cannot redirect retrieval to a new store that lacks the execution's snapshot's vectors, because the execution is bound to the physical store ID it captured at start, not to whatever "active" resolves to at query time.
3. **Frozen content-addressed manifest.** Each `corpus_snapshot_version` freezes a content-addressed **manifest of the `clause_id`s** it contains: candidate pool reproducible; retrieved set recorded in the immutable audit entry (see [Data flow](#data-flow--a-single-review) step 22 and [Audit posture](#audit-posture)). The manifest gives the candidate pool the snapshot contained, but top-K retrieval over an approximate vector index is not reproducible from the manifest alone — the exact retrieved set (clause_ids + polarity/channel) is therefore written to the audit record at review time, so an investigation can determine precisely which clauses the model saw even after the upload purge window expires.
4. **Ingestion interlock (restated).** The pipeline (data-flow step 10) **refuses to run** if the resolved store is not in `active` status or has an in-progress ingestion job. This replaces the earlier formulation ("ingestion targeting the active store is in progress") which was dead under the staging design where ingestion never targets the active store. The meaningful condition is: **the resolved store must be active and must have no in-progress ingestion job targeting it**. The `corpus_snapshot_version` metadata tag on every vector is a **defense-in-depth filter** (retrieval filters to the active version), not the sole isolation mechanism — the active/staging split and the per-execution store pin are.

**In-flight execution semantics during activation.** When an activation repoint occurs while one or more Step Functions executions are in flight, those executions continue querying the **physical store ID they captured at start** — the old store — until they complete. The new active store serves only executions that start after activation. This means: (a) no in-flight execution is silently disrupted by an activation, and (b) both the old store and the new active store may serve queries concurrently during the transition window. The old store remains available until all in-flight executions that hold a pin to it complete; it is not reaped the moment activation occurs.

**Corpus store lifecycle and rollback window.** After a repoint, the previous active store is **retained** — not deleted — so that in-flight executions can complete and so that a rollback can be served. The application retains the **N most recent snapshot stores** (default N = 3: the current active store plus two prior stores) as the rollback window. Stores beyond N are eligible for deletion only after all executions pinned to them have completed. **Re-ingestion is the recovery path beyond the rollback window**: if a rollback target's physical store has been reaped (outside the window), the recovery procedure is to re-ingest the content-addressed clause manifest for that snapshot into a fresh staging index, activate it, and re-run affected reviews — one-click rollback is only executable for snapshots within the retained window. The clause-id manifest preserves the _set_ of clauses but not a queryable index, so a store beyond the window cannot be served without re-indexing. This bound is documented to set expectations: rollback within the window is instant; rollback beyond it requires re-ingestion time.

Corpus changes follow the same governance principle as prompts and playbooks:

1. Admin upload creates a **draft ingestion snapshot**.
2. Legal curates clause-level metadata (`reusable_precedent`, `approved_use_scope`, `superseded_by`, polarity).
3. Retrieval regression and leakage checks run against the candidate snapshot.
4. A deliberate activation marks the snapshot query-eligible and records the activation in audit.

At review time (inside the Step Functions pipeline), the backend segments the uploaded document, runs the lexical layer, and queries the Knowledge Base for the top-K most analogous **positive** clauses from the active snapshot (plus, separately, any hard-labeled negative examples). These are passed to the LLM as precedent context. The model may record specific precedents internally, but external footnotes cite the contract position only.

**Retrieval failure semantics.** If the KB query itself errors after its bounded retry (network failure, Bedrock KB unavailability, throttle exhaustion), the pipeline step terminates to `ERROR` — the review does not proceed without precedent context, because proceeding silently would change legal-output quality without any record. Every retrieval attempt is ledgered.

**Empty-retrieval semantics and degraded mode.** If the KB query succeeds but returns an empty result set (no clauses above the similarity threshold, or an empty active corpus snapshot), the pipeline may proceed only if an explicit **degraded mode** has been documented and the review row records `degraded_mode=true` (flagged in the `reviews` table alongside the corpus snapshot version). A degraded-mode review proceeds on the deterministic diff and playbook alone, without precedent context, and the attorney must be informed via the UI that no precedent was available for this review. An empty result from a corpus that should have content is distinct from an empty corpus (no documents ingested yet); the pipeline records the actual count of retrieved clauses so both cases are distinguishable in audit. If the corpus snapshot is not active or an ingestion is in progress, the pipeline refuses to run (see ingestion interlock above), so an empty result at query time means the active snapshot genuinely contains no matching precedent.

Corpus changes go through a Bedrock KB **ingestion job** (triggered by the admin corpus upload), not a hand-rolled indexer. The Knowledge Base and its S3 Vectors store are reachable only via IAM (and a VPC endpoint where applicable); they are never public. Query-time access (`bedrock:Retrieve` / `bedrock:RetrieveAndGenerate`) is exclusively `pipelineReviewRole`'s.

**Reconciled least-privilege invariant (issue #59 × #60).** `pipelineReviewRole` is the ONLY role in the infra tree granted `bedrock:InvokeModel` scoped to the primary/critic review model ARNs, and the ONLY role granted `bedrock:Retrieve` / `bedrock:RetrieveAndGenerate`. Ingesting into a Bedrock Knowledge Base is not optional plumbing we control — AWS requires the **KB's own service role** to call `bedrock:InvokeModel` on the embedding model to convert corpus documents into vectors. A dedicated `corpusKnowledgeBaseRole` (assumed by `bedrock.amazonaws.com`, defined in `data-stack.ts`) therefore separately holds `bedrock:InvokeModel` scoped **strictly to the embedding-model ARN** (`amazon.titan-embed-text-v2` or equivalent) — never a `foundation-model/*` wildcard, and never the primary/critic model ARNs. The two roles' `InvokeModel` grants are resource-ARN-disjoint by construction, so the original security property is unchanged: no principal other than `pipelineReviewRole` can invoke the primary/critic review models or query the Knowledge Base, and no principal other than `corpusKnowledgeBaseRole` can invoke the embedding model.

### Storage

**S3 buckets:**

| Bucket          | Purpose                              | Lifecycle                              | Object lock |
|-----------------|--------------------------------------|----------------------------------------|-------------|
| `uploads`       | Raw uploaded counterparty docs       | Configurable retention (0–3yr, default 90), purge worker | No          |
| `outputs`       | Generated redlined `.docx` outputs   | Configurable retention (0–3yr, default 90), purge worker | No          |
| `corpus`        | Your executed agreements (reference) | None (versioned)                       | Governance  |
| `audit-archive` | CloudTrail logs + audit DB exports   | Glacier after 1 year                   | Governance  |

All buckets are private and block all public access at the account level. Encryption uses **separate customer-managed KMS keys per data class** — `audit`, `corpus`, `uploads`, `outputs`, and a key for DynamoDB — each with a narrower key policy and a distinct break-glass grant, so that (for example) the role that can read uploads cannot decrypt audit data. See [Security posture](#security-posture).

**Document retention is admin-configurable, but purge is gated by safety rules.** An admin sets a retention window via a slider in the admin UI, from **0 days to 3 years** (default 90). The window is enforced by a **purge worker** rather than a static S3 lifecycle rule, but the worker is constrained so it can never destroy evidence or delete a file mid-pipeline. The deep policy — field-level metadata classification and the legal-hold model — is owned by [docs/data-handling.md](docs/data-handling.md); the behaviour the architecture guarantees is:

- **Purge only terminal reviews.** The worker deletes documents only for reviews in a terminal state (`DONE` / `ERROR` / `MANUAL_REVIEW_REQUIRED` / `ERROR_MANUAL_REVIEW_REQUIRED` / `QUARANTINED` / `SUPERSEDED`). Documents belonging to an **active execution are excluded**, so a low (even `0`-day) window can never delete a file mid-pipeline.
- **Snapshot retention at creation.** Each review snapshots the retention window in effect when it was created; later slider changes do not retroactively shorten a review already in flight.
- **Legal hold overrides purge.** Per-review and corpus **legal-hold flags** gate deletion: a held review or corpus item is never purged regardless of the window. Legal hold must exist before any retroactive purge is honored (see [docs/data-handling.md](docs/data-handling.md)).
- **Not all metadata is retained indefinitely.** Metadata fields are classified; clause text, rationale text, and model summaries are **not** retained indefinitely unless the retention policy explicitly covers them. Only the minimal record-that-the-review-happened fields persist past the document window. See [docs/data-handling.md](docs/data-handling.md) for the field-by-field classification.

Object lock on `corpus` and `audit-archive` uses **Governance** mode (not Compliance): retention is enforced, but a holder of the explicit `s3:BypassGovernanceRetention` permission can override in a genuine emergency, which avoids the irreversible foot-gun of Compliance mode while still preventing routine deletion. Legal hold adds a stronger storage-level interlock: held corpus/review objects carry an S3 Object Lock legal hold or protected hold tag, and bucket policies deny deletion of held objects even to normal application roles. Governance bypass is reserved for MFA break-glass with session reason/ticket tags and alarms.

**DynamoDB tables:**

| Table              | PK            | SK         | Notes                                       |
|--------------------|---------------|------------|---------------------------------------------|
| `users`            | `cognito_sub` | —          | `email`, `is_admin`, `status` (`active`/`suspended`/`deprovisioned`), `last_auth_at`, `created_at` |
| `admin_bootstrap`  | `email`       | —          | First-admin seed only; keyed by email; consumed by a one-time reconciliation transaction on first sign-in (kept out of the `sub`-keyed `users` table) |
| `playbooks`        | `playbook_id` | —          | `current_version`, `agreement_type`, `active_release_bundle_hash` |
| `playbook_versions`| `playbook_id` | `version`  | **`status`** (`draft`/`active`/`retired`) — **SOLE lifecycle authority** (see [Canonicalization and status authority](#canonicalization-and-status-authority-issue-5) below; `playbook.status` in the JSON document is a snapshot label / projection, not the gate). `content_hash` (SHA-256 of the canonical playbook — excludes `playbook.status` and `playbook.release`; computed by `scripts/canonicalize.py`), `standard_form_hash`, `prompt_hash`, `model_policy_hash`, `output_contract_hash`, `corpus_snapshot_version`, `eval_run_id`, `json_blob`, `signed_release_metadata`, `legal_approval` (includes `legal_approval.content_hash` for Gate 7 assertion at activation: `content_hash == legal_approval.content_hash`), `uploaded_by` |
| `reviews`          | `review_id`   | —          | `owner_sub` (the uploader), `access_scope`, `idempotency_key`, `submission_status`, `execution_arn`, `playbook_id` (the playbook this review ran against; defaults to `eiaa`; required for multi-playbook routing, retrieval filtering, and rollback/quarantine scoping), `playbook_version`, `playbook_hash`, `prompt_version`, `prompt_hash`, `standard_form_hash`, `model_policy_hash`, `primary_model_id`, `critic_model_id`, `embedding_model_id`, `model_region`, `corpus_snapshot_version`, `legal_hold`, `legal_hold_reason`, `legal_hold_set_by`, `legal_hold_set_at`, `retention_window_at_creation`, `input_doc_hash`, `output_doc_hash`, `tokens_in`, `tokens_out`, `cost_usd`, `decision`, `confidence_state`, `verdict_summary`, `status`, `created_at`, `degraded_mode` (boolean — true when retrieval returned an empty result and the review proceeded on diff+playbook only; see [Retrieval](#retrieval--amazon-bedrock-knowledge-bases-s3-vectors)), `retrieved_clause_count` (count of precedent clauses actually retrieved; 0 when degraded_mode=true), `fallback_used` (boolean — true only when a manually-activated fallback release bundle was in effect; automatic failover is prohibited). **The canonical field dictionary (name, substance flag, classification, retention) is owned by [docs/data-handling.md](docs/data-handling.md); this list must match it.** |
| `spend_ledger`     | `day` (`YYYY-MM-DD`) | `attempt_id` | Atomic spend reservation/settlement; one row per model attempt (incl. failures/retries) |
| `sync_status`      | `sync_type`   | —          | Single row per sync job (`sync_type = "user_deprovision"`): `last_run_at`, `last_run_outcome` (`ok`/`directory_unavailable`), `users_deprovisioned_count`, `next_run_at`. Written by the scheduled Workspace/SSO sync worker; read (never written) by the admin Users UI's sync-visibility panel (issue #92). |
| `audit`            | `partition` (`YYYY-MM` or `target_type#target_id`) | `timestamp#event_id` | `actor`, `action`, `target`, `before_hash`, `after_hash`; **append-only** |

The `audit` PK is **time-partitioned** (`YYYY-MM`, or `target_type#target_id` for entity-scoped queries) with `timestamp` as the SK, so the SK is actually useful for "what happened, in order, in this window". GSIs index **`actor`** and **`review_id`** for the two common audit queries. (The old `event_id`-as-PK shape made the timestamp SK dead weight.) The append-only enforcement is described under [Audit posture](#audit-posture).

Canonical review statuses are:

- `PENDING` — submission exists, review not yet running.
- `RUNNING` — Step Functions execution is active.
- `DONE` — review completed with a legal decision (`ACCEPT` or `REQUEST_CHANGE`).
- `ERROR` — operational failure.
- `MANUAL_REVIEW_REQUIRED` — system could not reach a confident legal decision.
- `ERROR_MANUAL_REVIEW_REQUIRED` — the model would not produce schema-valid output after one bounded retry, **or** the output failed the leakage scan. Distinct from a pipeline `ERROR` (infrastructure/step failure): it routes a human to a model/output problem, not an ops incident. (There is no retry for a leakage hit — a leak routes here directly.)
- `QUARANTINED` — **post-terminal administrative overlay**: the review reached a pipeline-terminal state (`DONE`, `ERROR`, `MANUAL_REVIEW_REQUIRED`, or `ERROR_MANUAL_REVIEW_REQUIRED`) and was subsequently flagged by an admin rollback sweep or a manual quarantine action because its release bundle, model policy, or corpus snapshot is known-bad or under investigation. `QUARANTINED` is **not** a pipeline-derived status — no `confidence_state` value maps to it. It is written by a separate administrative action (rollback sweep, manual GC action) after the pipeline has already settled.
- `SUPERSEDED` — **post-terminal administrative overlay**: the review was previously `QUARANTINED` and has been replaced by a re-run against a good bundle. Like `QUARANTINED`, it is not pipeline-derived and carries no `confidence_state` counterpart; it is written by the admin re-run workflow after the replacement review is `DONE`.

**`status` vs `confidence_state` (two distinct fields, one derivation).** `confidence_state` is the **internal** signal the pipeline computes (`OK | LOW_CONFIDENCE | MANUAL_REVIEW_REQUIRED | ERROR_MANUAL_REVIEW_REQUIRED`, per [playbooks/schema.json](../playbooks/schema.json) `output_format.system_status`); the review `status` is its **terminal projection**, never an independent value. The mapping is fixed: `confidence_state = OK` with a legal decision → `status = DONE`; `LOW_CONFIDENCE` *with* a concrete playbook issue → `DONE` (decision `REQUEST_CHANGE`); `LOW_CONFIDENCE` with *no* concrete issue → `status = MANUAL_REVIEW_REQUIRED`; `ERROR_MANUAL_REVIEW_REQUIRED` (schema-invalid-after-retry or leakage) → `status = ERROR_MANUAL_REVIEW_REQUIRED`. A `DONE` status with a non-`OK`/non-decision `confidence_state`, or any other contradictory combination, is invalid and must never be written. The external legal decision (`ACCEPT | REQUEST_CHANGE`) lives in `decision`; the manual-review states are **system statuses, never a third legal category**. **Exception — pre-model pipeline-terminal writes:** A pipeline step that terminates *before any model call* may write `status = MANUAL_REVIEW_REQUIRED` directly, without a `confidence_state`, when it detects a condition that makes model invocation impossible or unsafe. The sole defined case is the step-14 oversized-document gate: if the assembled prompt would exceed `max_input_tokens`, the pipeline writes `status = MANUAL_REVIEW_REQUIRED` / `reason = document_too_large` and halts; no `confidence_state` is produced because the model was never invoked. This is a fixed-status pipeline-terminal write that bypasses the `confidence_state` derivation; it is not a contradiction of the projection rule — the rule applies only when a `confidence_state` has been computed. **Exception — post-terminal administrative overlays:** `QUARANTINED` and `SUPERSEDED` are the sole statuses that do not follow this projection rule. They are applied by administrative actions (rollback sweeps, manual GC action) *after* the pipeline has already written a terminal status; they are therefore consistent with the invariant — the invariant governs pipeline writes, and these are post-pipeline administrative writes.

Terminal for purge: `DONE`, `ERROR`, `MANUAL_REVIEW_REQUIRED`, `ERROR_MANUAL_REVIEW_REQUIRED`, `QUARANTINED`, `SUPERSEDED`. Active states (`PENDING`, `RUNNING`) are never purged.

Point-in-time recovery enabled on all tables. KMS-encrypted with the DynamoDB-class key (the `audit` table additionally streams to the object-locked audit key domain — see [Audit posture](#audit-posture)).

### Redlining — owned docx library

Producing `.docx` files with proper tracked changes requires OOXML manipulation. `scripts/redline_docx_writer.py` is a small, dependency-free writer built entirely on the standard library (`zipfile` + `xml.etree.ElementTree`): a `.docx` is a ZIP of XML parts, so the writer assembles `[Content_Types].xml`, `_rels/.rels`, and `word/document.xml` directly and emits `<w:ins>` / `<w:del>` revision elements (each carrying `w:id`, `w:author`, `w:date`) for every applied patch from `scripts/redline_patch.py::apply_patches`, plus (issue #83) a footnoted rationale per issue (`word/footnotes.xml`) and the redundant export marker — cover note + every-page header/footer (`word/header1.xml`, `word/footer1.xml`, wired via `word/_rels/document.xml.rels`). There is **no** vendored `anthropics/skills` `docx` fork and **no** `backend/vendor/` directory — an earlier draft of this document claimed one existed; it did not, and this section is corrected to describe the writer that actually ships. `scripts/redline_generate.py` (issue #83) is the end-to-end orchestrator: it takes a reconciled review result (issue #82) and the standard-form diff, runs the leakage scan gate (issues #26/#73) over the full result before generation, joins and applies patches via `redline_patch.py` (fail-closed per issue #65), builds the docx via `redline_docx_writer.py`, and runs the output-side OOXML scan (see [Export / misuse marker](#export--misuse-marker) below and [docs/threat-model.md](docs/threat-model.md) → "Generated redline output hygiene") before ever returning the document bytes. See [docs/design-notes.md](docs/design-notes.md) and [docs/threat-model.md](docs/threat-model.md) for the supply-chain controls (SBOM, provenance, redline fixture tests) that apply to any future third-party OOXML dependency, `tests/redline/test_docx_tracked_changes_writer.py` for the fixture test covering `<w:ins>`/`<w:del>` correctness, and `tests/redline/test_redline_generation_83.py` for the end-to-end generation gate.

Pure Python. No LibreOffice, no Word, no commercial library. Runs inside the review pipeline.

#### Input normalization (before review)

A counterparty `.docx` can carry pre-existing **tracked changes, comments, hidden text, fields, footnotes, and embedded objects** that would otherwise corrupt both the diff and the redline. Before any review work, the document passes a **normalization pass** that applies a **documented accept/reject rule** to existing revisions and produces a clean canonical body for extraction and diffing. A single, unambiguous **pending** (`w:ins`/`w:del`) counterparty revision — the flagship counterparty-markup scenario, and what a redline *is* — is the proposal under review: it is **accepted-all** into the operative draft, with the disposition recorded in a normalization note (never silent); the downstream standard-form diff then recovers what changed. Fail-closed is reserved for genuinely ambiguous structures — nested/conflicting revisions, multiple interleaved revision authors, or a revision inside a field code — plus malformed revision records and structurally corrupt input. Comments never gate normalization by themselves, even alongside a pending revision that is itself accepted-all. Hidden text and field results are stripped/resolved to their literal text. If a document cannot be normalized to a clean, unambiguous body, the review **fails closed** to an internal analysis report rather than guessing: the pipeline sets `status = MANUAL_REVIEW_REQUIRED` with `reason = unnormalizable_input` and stores the analysis report in the `outputs` bucket (accessible to owner-or-admin only) instead of a redline `.docx`. The report describes the normalization problem and any intended changes so the attorney can apply them by hand. See [docs/output-contract.md → Fail-closed internal analysis report](docs/output-contract.md#fail-closed-internal-analysis-report) for the format, delivery surface, reviewer-facing copy, and retention classification, and `scripts/normalize_input.py` for the full documented accept/reject rule. (The hostile-file model — zip-bomb, XML-entity, external-relationship, macro-template, MIME/magic-number, AV scan — is owned by [docs/threat-model.md](docs/threat-model.md).)

**OOXML part allowlist — what reaches extraction.** A `.docx` is a ZIP containing many XML parts beyond the main document body. Only an explicit **allowlist** of parts whose text reaches extraction and prompt assembly is permitted; everything else is stripped or held for untrusted-display-only. The allowed set is narrow by design:

| Part | Disposition |
|---|---|
| Main document body (`word/document.xml`) | **Allowed** — primary extraction target |
| Tables within the document body | **Allowed** — extracted alongside body text |
| Footnotes and endnotes (`word/footnotes.xml`, `word/endnotes.xml`) | **Allowed only when deliberately surfaced** — processed with explicit normalization rules |
| Core document properties (`docProps/core.xml`) | **Excluded** — stripped; never reach prompt assembly |
| App document properties (`docProps/app.xml`) | **Excluded** — stripped; never reach prompt assembly |
| Custom document properties (`docProps/custom.xml`) | **Excluded** — stripped; never reach prompt assembly |
| Headers and footers (`word/header*.xml`, `word/footer*.xml`) | **Excluded** — stripped outside the extraction allowlist |
| Textbox and shape text (`word/drawings/`, `wp:*`, `mc:*` drawing parts) | **Excluded** — stripped outside the extraction allowlist |
| Image alt text (`a:t` in drawing XML, `w:altChunk`) | **Excluded** — stripped outside the extraction allowlist |
| SmartArt and chart XML (`word/charts/`, `word/diagrams/`) | **Excluded** — stripped outside the extraction allowlist |
| Content-control placeholders (`w:sdt` with display-only content) | **Excluded** — stripped outside the extraction allowlist |

**Filename rule.** The upload filename is **never included in prompts** or passed to the model. It is a user-supplied, attacker-controllable string. Wherever the filename is rendered in the reviewer UI, admin UI, or audit views it is **escaped as plain text** — never interpreted as HTML — to prevent stored-XSS via a crafted filename. See [docs/threat-model.md → Admin UI stored-XSS](docs/threat-model.md#admin-ui-stored-xss).

#### Anchored, hash-validated patching (fail closed)

Redline patches must never land on the wrong clause. Every proposed change carries:

- a **paragraph / table-cell anchor** (the structural location produced by the standard-form diff), and
- a **source-text hash** of the exact target text at that anchor.

At patch time the library re-reads the target text, recomputes the hash, and applies the `<w:ins>`/`<w:del>` edit **only on an exact match**. If the target text no longer matches its hash (document shifted, normalization changed it, anchor stale), the patch is **not** applied approximately — the review **fails closed** and emits an **internal analysis report** describing the intended change and why it could not be safely applied, instead of editing the wrong clause. The pipeline sets `status = MANUAL_REVIEW_REQUIRED` with `reason = hash_mismatch_at_patch` and stores the analysis report in the `outputs` bucket (owner-or-admin access only). The analysis report carries all proposed replacement text and rationale so the attorney can apply the edits by hand. See [docs/output-contract.md → Fail-closed internal analysis report](docs/output-contract.md#fail-closed-internal-analysis-report).

#### Export / misuse marker

Every generated redline carries an **internal-only / export-warning marker** ("tool recommendation only — attorney approval required; do not send externally before attorney approval") baked into the document, matching the UI watermark. To resist accidental removal (e.g. accept-all-changes followed by deleting a single banner paragraph), the marker is placed **redundantly**: a first-page cover note **plus** a running every-page header/footer, not one removable line. This is explicitly **misuse *friction*, not an export control** — a determined user can still strip it, and it does not gate, sign, or record approval; it makes accidental external use conspicuous and deliberate. The framing is in [docs/threat-model.md](docs/threat-model.md).

### Canonicalization and status authority (issue #5)

**Problem.** The original schema said `release.content_hash` is "SHA-256 over the canonicalized
playbook content" without specifying the canonical form. Hashing the whole document is circular:
the `release` block contains `content_hash` itself (writing it changes the bytes), and flipping
`playbook.status` from `draft` to `active` would change the hash that Legal approved.
Additionally, `playbook.status` and `playbook_versions.status` were two status authorities with
no declared winner.

**Canonical form.** The canonical form is the playbook JSON with **`playbook.status` and
`playbook.release` removed**, serialized with sorted keys, no whitespace, UTF-8. This form is
stable under status flips and release-block population/mutation, so the hash Legal approves is
exactly the hash serving production.

Implementation: `scripts/canonicalize.py` (functions `canonicalize()` and `content_hash()`).
Normative specification: `playbooks/schema.json` (`canonicalization` top-level field) and
`docs/playbook-governance.md` (§ Canonicalization and content_hash).
CI golden-hash gate: `tests/gold-fixtures/canonicalize-golden-hash.json` + `tests/test_canonicalize.py`.

**Status authority.** `playbook_versions.status` (the DynamoDB row) is the **sole lifecycle
authority**. `playbook.status` in the JSON is a snapshot label / projection written at upload time
for human readability — never the runtime gate. Code gating production reviews reads the DB row.

**Gate 7 (approved hashes match the artifacts being promoted).** Now implementable step-by-step:

1. **Upload time.** `content_hash(playbook_doc)` (via `scripts/canonicalize.py`) → stored in
   `playbook_versions.content_hash`.
2. **Approval time.** Approver reviews the playbook at that hash → records it in
   `playbook_versions.legal_approval.content_hash`.
3. **Activation time.** Activation gate asserts:
   `playbook_versions.content_hash == playbook_versions.legal_approval.content_hash`.
   Mismatch = bytes changed after approval = bundle cannot be activated.
4. `release.content_hash` in the JSON is written at upload time for audit trail — activation reads
   the DB row, not the document field.

### Infrastructure — AWS CDK

All AWS resources are defined in `infra/` as CDK constructs in TypeScript. No console click-ops. A `cdk deploy` brings the entire stack up from zero. **Prod is a separate AWS account from dev** (see [Environments](#environments)); the same CDK app is deployed per-account.

**Deploy pipeline (no auto-deploy from main).** A merge to `main` does not change production. CI (CodeBuild or equivalent) runs tests and security scans, **builds a container image, signs it, and pushes it to ECR**. Deployment is by **immutable image digest**: App Runner (and the pipeline task images) are pinned to a specific signed digest, and promotion to that digest is a **deliberate** step, not a side effect of a merge. Rollback is re-pinning the prior digest (see [Rollback](#data-flow--a-single-review)).

**CI gates (all must pass before image push).** The CI pipeline (`ContractToasterStack-cicd` / `.github/workflows/ci-pipeline.yml`) runs four gates on every change before building or pushing an image:

1. **Full Python test suite** — every `tests/test_*.py`, `tests/*/test_*.py`, and `tests/lint-*.py` must exit 0.
2. **docs-lint gate** (`scripts/docs-lint.py`) — stale-term denylist, latency consistency, rule-count, field-dictionary, no literal AWS account IDs, and no placeholder phrases (issue #43; _reconciliation: the CI pipeline additionally runs docs-lint_).
3. **Detector-correctness gate** (`tests/detector/`) — empty-scope structural check (D1), planted-violation fires (D2), ReDoS guard (D3) (issues #1, #2; _reconciliation: the CI pipeline additionally runs detector-correctness gates_).
4. **Security/dependency scan** — pip-audit and Trivy container scan.

**Promotion audit and signature verification.** Promotion to a new digest is a deliberate, audited step:

- **Signature verification before promotion.** Every image is signed with cosign (Sigstore) immediately after push. Before the SSM digest parameter is updated, cosign verify must exit 0. An unsigned or unverifiable digest cannot be promoted — the promote job fails, blocking the SSM write.
- **Promotion audit row.** Each promotion writes an immutable audit row (actor, digest, timestamp, environment) to the `audit` DynamoDB table before updating the SSM parameter. The audit row satisfies the "who promoted which digest, when" requirement and is enforced by the append-only IAM policy on the audit table.

**Legal-behavior release bundle.** Code deploy and legal-content activation are separate gates. A playbook/prompt/corpus/model change becomes production behavior only through an active release bundle containing the playbook hash, prompt hash, canonical standard-form hash, model-policy hash, **output-contract hash** (`output_contract_hash` — SHA-256 of `playbooks/output-schema-v1.json`), active corpus snapshot version, evaluation run ID, and legal-approval metadata. Reviews record that bundle at execution start. The output-contract hash binds the response schema to the bundle so that a change to what fields the model must emit — a legal-output-affecting change — is governed by the same approval gate as a prompt or playbook change. See [docs/output-contract.md](docs/output-contract.md) for the coupling rules and schema artifact reference.

**Secrets are not read at synth time.** CDK code and synthesized templates must **never** read OAuth client secrets or other Secrets Manager values into themselves — that would bake a secret into a CloudFormation template. The stack wires **dynamic references / runtime secret resolution** (the running service resolves the secret from Secrets Manager at runtime via its task role), so no secret material appears in `cdk synth` output or version control.

Stack composition:

- `ContractToasterStack-network` — VPC, subnets, security groups, VPC endpoints to Bedrock and the Knowledge Base.
- `ContractToasterStack-data` — S3 buckets, DynamoDB tables, the **per-data-class KMS keys** (audit / corpus / uploads / outputs / DynamoDB), Bedrock Knowledge Base + S3 Vectors store.
- `ContractToasterStack-auth` — Cognito user pool, Google IdP, app client, hosted-domain Lambda, allowlist/group-sync wiring.
- `ContractToasterStack-app` — App Runner API service pinned to a **signed ECR image digest**, the **split** API IAM roles, runtime secret references.
- `ContractToasterStack-pipeline` — Step Functions review state machine (started **directly** by the API; **no SQS** on the entry path), the concurrency semaphore, Lambda/Fargate task definitions, the retention purge worker.
- `ContractToasterStack-frontend` — Amplify Hosting app.
- `ContractToasterStack-observability` — CloudWatch dashboard (tiles include counts of reviews in `MANUAL_REVIEW_REQUIRED` and `ERROR_MANUAL_REVIEW_REQUIRED` states; admin filter view lists all manual-review reviews for daily triage), CloudTrail trail, alarms (including `contract-toaster-manual-review-stale` alarm that fires when a review remains in either manual-review state unacknowledged for more than 24 hours), AWS Budgets. Full alarm list and the manual-review owner/SLA are in [RUNBOOK.md → Observability](RUNBOOK.md#observability).
- `ContractToasterStack-cicd` — CodeBuild project(s), ECR repository with image signing, the digest-promotion mechanism.

## Data flow — a single review

The review runs **asynchronously**. The API never blocks on the LLM; a Step Functions execution drives the stages, and the browser polls for the result.

```
API (App Runner), synchronous:
 1.  User signs in via Google → Cognito issues JWT (company.com enforced).
 2.  User uploads counterparty.docx via POST /api/reviews (multipart, JWT-authed,
     domain re-verified + allowlist/status checked at the backend; request-size
     and per-user concurrency/daily limits enforced).
 3.  Resolve the active release bundle (playbook + prompt + canonical standard
     form + model policy + output contract schema + corpus snapshot + eval run)
     and derive the idempotency key (client-supplied if present; else owner_sub +
     file SHA-256 + release-bundle hash + a fixed-width timestamp bucket, checking
     the current AND previous bucket to avoid a boundary-straddling double-run).
     **The resolved release-bundle hash is stored on the submission record** (see
     step 4) immediately — this is the single resolution point. The execution
     (step 10) reads and verifies this stored bundle hash; it never re-resolves
     the active bundle independently. A bundle activation landing between
     submission and execution start does not change the bundle the review runs
     under, because the bundle was resolved once at submission and is now
     immutably recorded.
 4.  Create or fetch the submission/idempotency record with a conditional write.
     The record owns review_id, execution name, upload pointer, reservation ID,
     execution ARN/status, and the **resolved release-bundle hash** (stored at
     step 3). A retry returns this existing record.
 5.  ATOMIC spend reservation: a conditional DynamoDB counter reserves a WORST-CASE
     upper-bound cost (max input+output tokens x both passes x uncached pricing)
     exactly once per review_id. If reserving would exceed the daily cap, reject
     with a clear "daily limit reached" message. (Settlement corrects it downward.)
 6.  Backend writes the upload to s3://uploads/{owner-sub}/{review-id}/in.docx and
     records the upload hash/pointer on the submission.
 7.  Backend creates or updates the `reviews` row through the submission record
     (status=PENDING, owner_sub, access_scope, release-bundle hashes,
     snapshotted retention window).
 8.  Backend performs idempotent "ensure execution started" directly against Step
     Functions using the deterministic execution name (no SQS). Success stores
     execution_arn; ExecutionAlreadyExists stores the existing execution.
     Returns 202 + review id.

Pipeline (Step Functions), asynchronous — status flips to RUNNING:
 9.  Acquire a concurrency slot (semaphore / reserved concurrency) so parallel
     uploads cannot flood Opus or drain the daily cap at once.
 10. **Verify the submission-time bundle; never re-resolve.** The pipeline reads
     the release-bundle hash that was stored on the submission record at step 3
     (the single resolution point). It does not independently re-resolve the
     active bundle — "single resolution" means the bundle is resolved exactly
     once, at submission. If the bundle recorded at submission is still `active`,
     the pipeline records the bundle hashes, corpus snapshot version, and the
     **physical store ID / KB identifier** of the active store into the execution
     input and proceeds. Refuse to run if the resolved store is not in active
     status or has an in-progress ingestion job (restated interlock — the
     meaningful condition: the resolved store must be active and have no
     in-progress ingestion job).
     **Retired-bundle-before-start behavior.** If the bundle recorded at
     submission has been retired or quarantined before execution starts (a bundle
     activation landed between submission and the execution beginning step 10),
     the pipeline must not silently run under the new active bundle. The review is
     refused: it transitions to `QUARANTINED` with a reason of
     `submission_time_bundle_retired`, releases the concurrency slot and unspent
     reservation, and is surfaced for operator action. The operator procedure is
     in RUNBOOK.md. A review quarantined for this reason may be re-submitted
     explicitly by the user (minting a fresh submission against the now-active
     bundle) if the legal change introduced by the new bundle is acceptable for
     that review.
 11. Extract text (owned docx library); run the input-normalization pass
     (accept/reject pre-existing revisions, strip hidden text/fields/comments).
 12. Deterministic diff of the normalized draft against the canonical standard
     form for this playbook version (anchored to paragraphs/table cells).
 13. Run the lexical hard-rejection detectors; segment by section; query the
     Bedrock Knowledge Base for top-K POSITIVE precedents using the **pinned
     store ID** captured in step 10 (negatives separate). The query targets
     the exact pinned store, not whatever "active" resolves to at query time.
 14. Assemble the prompt: system (review guidance + binary overlay + playbook) +
     user (standard-form diff + anchored clauses + hard-labeled precedents +
     the untrusted, delimited counterparty doc text). Enforce caps: document
     size, extracted tokens, sections, top-K per section, output tokens.
     **Oversized-document single failure point.** The cap check at this step is
     the single authoritative failure point for documents that exceed the
     configured limits.  If the assembled prompt would exceed `max_input_tokens`
     (default 80,000 per pass), the review terminates *before any model call* —
     before step 15 — with `status=MANUAL_REVIEW_REQUIRED` and
     `reason=document_too_large`.  The user sees a clear error message naming the
     `document_too_large` reason; no Bedrock invocation is attempted.  A
     ValidationException "input is too long" from the model layer (step 15 or 16)
     is therefore unreachable in correct operation: its occurrence indicates that
     the step-14 cap is misconfigured (set too high relative to the model's actual
     context limit), not that the user submitted an unusually large document.
     There is no separate "manually segment" procedure — the cap is the single
     gate, and misconfiguration is the only cause of a model-side overflow.
 15. Primary review: Bedrock InvokeModel (primary_model_id from the active model
     policy; no
     temperature/top_p/top_k). Prompt caching is an optimization; cost survives
     a cache miss. LEDGER the attempt in a finally path.
 16. Adversarial review: critic_model_id from the active model policy critiques
     the primary output;
     LEDGER the attempt. Reconcile deterministically (either-pass hard rejection
     forces REQUEST_CHANGE; critic adds, never silently rewrites; keep deltas).
 17. Validate the final JSON. On schema failure, ONE bounded structured-output
     retry; if it still fails, status=ERROR_MANUAL_REVIEW_REQUIRED (distinct
     from a pipeline ERROR). No best-effort redline either way.
 18. Determine the external decision and the internal confidence state. Low
     confidence with a concrete playbook issue → REQUEST_CHANGE; low confidence
     with no concrete issue → MANUAL_REVIEW_REQUIRED (a system status, not a
     legal category).
 19. Leakage scan the output (system prompt / playbook / confidential rationale /
     excessive precedent quotation) before generating any document.
 20. If decision == REQUEST_CHANGE:
        Apply anchored, hash-validated tracked changes + footnoted rationales to
        a copy of the original (fail closed to an internal analysis report at
        MANUAL_REVIEW_REQUIRED with reason=hash_mismatch_at_patch if a
        target hash no longer matches), with the export-warning marker baked in →
        s3://outputs/{review-id}/out.docx.
     If decision == ACCEPT:
        No output document. UI shows "no requested changes identified by tool".
 21. SETTLE actual spend against the reservation; update the `reviews` row with
     the result, token counts, cost, confidence state; status=DONE.
 22. Append a non-substantive entry to the append-only `audit` table (decision,
     topic ids, scanner rule ids, hashes, model ids/region, playbook/prompt/
     standard-form/model-policy hashes, corpus snapshot version, **retrieved
     clause_ids with polarity and channel** — opaque identifiers, not clause
     text; see [docs/data-handling.md](docs/data-handling.md) for classification).
     Store any substantive rationales or critic text in retention-governed
     confidential storage only.

Browser:
 23. UI polls GET /api/reviews/{id} (owner-or-admin) until status is terminal
     (DONE / ERROR / ERROR_MANUAL_REVIEW_REQUIRED / MANUAL_REVIEW_REQUIRED /
     QUARANTINED / SUPERSEDED),
     then renders the result with the attorney-approval watermark.
 24. Human-review outcome capture: the UI records whether the attorney accepted,
     edited, or rejected the tool output (feeds the feedback loop —
     see docs/evaluation.md). Approval itself still happens outside the tool.
```

Each pipeline step has its own timeout and retry policy, and **every model attempt is ledgered in a finally path** — including failed invocations, retries, malformed outputs, and aborted executions — so no spend escapes the ledger. A failed step transitions the execution (and the `reviews` row) to a terminal error state with the failing stage recorded, rather than leaving a review wedged in `PENDING`, and releases the concurrency slot and any unspent reservation. A submission that crashes *before* its execution starts (no `execution_arn`) is recovered by the **orphan reconciler** (see [Idempotency](#api-vs-async-worker)), not left for a human to notice.

**Execution-level timeout (state-machine-level).** Per-step timeouts are necessary but not sufficient: a pathological execution can get stuck in a non-retrying wait or drift into RUNNING indefinitely, leaking a concurrency slot and a spend reservation for days. The Step Functions state machine therefore carries an **overall execution-level timeout** (in addition to per-step timeouts) — set and asserted in the infra CDK definition — that automatically terminates any execution that has not reached a terminal state within the maximum plausible review duration. The execution-level timeout is the backstop that converts a runaway RUNNING execution into a TIMED_OUT terminal state, which the orphan reconciler then detects (via DescribeExecution) and resolves to ERROR with slot and reservation release (see the dead-execution path above).

**Semaphore lease / slot-leak recovery.** The concurrency-semaphore releases a slot on the handled-failure path (the Step Functions Catch/finally states). A hard-killed execution — process kill, Lambda OOM, Fargate SIGKILL, or a Step Functions execution terminated externally — never runs those states, leaking a slot permanently at this system's low concurrency cap. Two mechanisms prevent permanent leaks: (a) **Lease/TTL semantics** on semaphore entries — each slot entry carries an expiry (lease TTL) aligned to the execution-level timeout; a reaper or the next acquire reclaims any expired entry regardless of whether the release state ran; (b) **Slot reaper in the reconciler** — the orphan reconciler also reconciles held semaphore slots against live Step Functions executions: any slot whose associated execution is no longer in RUNNING status is reclaimed. The stale-`PENDING`/`RUNNING` alarm covers both the PENDING-with-dead-ARN case (a `PENDING` review whose ARN resolves to a FAILED/TIMED_OUT/ABORTED execution) and stale RUNNING reviews (a review in RUNNING status whose execution age exceeds the execution-level timeout), so either stuck state pages on-call before manual intervention is needed.

**Release-bundle rollback.** Active playbook/prompt/model/corpus/standard-form bundles are content-addressed and carry `draft`/`active`/`retired` statuses with signed release metadata. A bad bundle is rolled back with **one click** to the prior active bundle (recorded in the audit table), and reviews run under the bad bundle are automatically marked `QUARANTINED` for re-run. Re-runs create new review records; originals become `SUPERSEDED` once replaced. Governance and CI gating (schema validation, prompt regression tests, stochastic stability, retrieval regression, redline fixture tests, legal-approval metadata before a bundle may go `active`) are owned by [docs/evaluation.md](docs/evaluation.md).

**Release-bundle deactivate action.** The **deactivate** action is an explicit admin (GC-gated) operation that takes the currently active bundle out of service **without promoting a successor** — it deliberately leaves no bundle active for that playbook, suspending intake. This is the correct control when the first-ever bundle is found bad (there is no prior bundle to roll back to) or when intake must be suspended during a quarantine without committing to any replacement version. Deactivate is **distinct from rollback**: rollback requires a prior active bundle as the restore target; deactivate requires only that a bundle is currently active. Both actions are **audited** — the deactivate action writes an `audit` entry recording the actor, the deactivated bundle hash, a reason, and the timestamp. The deactivate action is **GC-gated consistently with activation controls**: the same admin approval (General Counsel or designated legal tech owner) required to activate a bundle is required to deactivate it. After deactivation, the deactivated bundle transitions to `retired` status (preserving its content-addressed snapshot for rollback lineage), and `active_release_bundle_hash` on the `playbooks` row is cleared. `POST /api/reviews` returns HTTP `503` with the user-visible message **"no active playbook"** until a new bundle is activated (see Routes above and the no-active-bundle system state note). The deactivate API endpoint is `POST /api/playbooks/{id}/deactivate`; it returns `409` if no bundle is currently active (nothing to deactivate).

**Rollback in-flight race — reviews RUNNING under the bad bundle.** When a rollback fires, some reviews may be `RUNNING` under the bad bundle. These in-flight executions are **not aborted by the rollback sweep** — they are allowed to run to completion. Aborting them mid-execution would lose work already spent (Bedrock calls already billed), corrupt the audit ledger for those attempts, and risk inconsistent state if the pipeline is mid-write. Instead, the rollback sweep applies `QUARANTINED` immediately to all reviews already in a terminal state (`DONE`, `ERROR`, `MANUAL_REVIEW_REQUIRED`, `ERROR_MANUAL_REVIEW_REQUIRED`) under the bad bundle hash. A **second quarantine sweep keyed by bundle hash** runs after the rollback completes (triggered on each pipeline-terminal write and on a short-interval schedule) and quarantines any review whose `playbook_hash` (or release-bundle component hash) matches the bad bundle, regardless of current status — this catches reviews that were `RUNNING` at rollback time and subsequently landed `DONE`. The consequence: a review RUNNING under a bad bundle at rollback time will finish normally, its result will land in `DONE` transiently, and the second sweep will transition it to `QUARANTINED` before any attorney can rely on it. The `QUARANTINED` post-terminal overlay mechanism (see above) is what makes this safe: `QUARANTINED` is an administrative write that overwrites the pipeline-derived terminal status without violating the pipeline projection invariant.

End-to-end latency: 1–3 minutes typical, 5 minutes p95 (including the standard-form diff and the adversarial pass), depending on document length. The primary Opus 4.8 pass emits 4–8K output tokens, which takes 1.5–4 minutes at typical Opus throughput; the adversarial Sonnet 4.6 critic pass adds further time. A "20–90 seconds" figure predates the full two-pass pipeline and is superseded by this baseline.

## Audit posture

Every action that modifies system state, grants document access, or attempts document access writes an immutable row to the `audit` table:

- Document uploads (with content hash).
- Review decisions (with release-bundle hashes, model ids, version, token counts).
- Review views, output downloads, presigned-URL issuance, and failed owner/admin checks.
- Playbook version uploads.
- Release-bundle activations and rollbacks.
- User admin-flag changes.
- Corpus changes.

Audit rows include before/after content hashes where applicable, and (for reviews) model ids/region, playbook hash, prompt hash, standard-form hash, model-policy hash, corpus snapshot version, topic ids, decision, token counts, cost, scanner rule ids, authorization result, and **retrieved clause_ids with polarity and channel** (opaque identifiers — non-substantive; see [docs/data-handling.md](docs/data-handling.md)). They **do not** include raw document text, model-written rationales, clause text, or substantive primary/critic deltas. Those fields are confidential document substance and live only in retention-governed storage.

**Append-only enforcement (not just a name).** Calling a table "audit" does not make it immutable. We enforce it:

- IAM **denies `UpdateItem` and `DeleteItem`** on the `audit` table to **all application roles** (API, pipeline, purge worker). No app role can mutate or remove an audit row.
- Writes are **append-only `PutItem` with a conditional-nonexistence** check on the partition+SK, so a row can be created but never overwritten.
- DynamoDB Streams immediately fan the rows to **object-locked S3** (the audit KMS key domain), so an out-of-band tamper attempt still leaves an immutable copy.
- **Failed/denied mutation attempts are monitored and alarmed** — a denied `UpdateItem`/`DeleteItem` on `audit` is a security signal, not noise.

The key shape (time-partitioned PK + `timestamp#event_id` SK, GSIs on `actor` and `review_id`) is described under [Storage](#storage); it makes ordered, windowed, and entity-scoped audit queries efficient.

**CloudTrail (control-plane signal).** CloudTrail logs every AWS **management** API call. Note that AWS currently documents Bedrock **`InvokeModel`, `InvokeModelWithResponseStream`, `Converse`, and `ConverseStream` as logged as management events** — so we get a control-plane record that an invocation happened, *without* prompt/output content. We treat that as a useful independent audit signal (every model call is attested in CloudTrail) while recognizing it does **not** capture the prompt or the model output. Per-object S3 data events remain off by default (high-volume, costly) and can be switched on for an investigation; the toggle and procedure live in [RUNBOOK.md](RUNBOOK.md).

**Document access audit.** Because S3 data events remain off by default for cost, the application audit trail is the primary record for review views and downloads. If presigned URLs are used, the presign event is audited with the target hash, TTL, actor, IP/user agent, and authorization decision. For investigations where proof of object-level S3 access is required, scoped S3 data events are enabled temporarily for the relevant bucket/prefix.

**Prompt / output logging policy.** Bedrock invocation logging (which *would* capture prompt and output content) is **disabled or tightly controlled** — it is not on by default, and if ever enabled it targets a separately-encrypted, retention-governed destination. Application logs are **redacted**: raw document text, prompts, model outputs, and retrieved clause text are **prohibited from CloudWatch logs**. Anything sensitive that must be persisted goes to the encrypted, retention-governed stores, never to general logs. The deep threat treatment is owned by [docs/threat-model.md](docs/threat-model.md).

## Security posture

- All data at rest is KMS-encrypted with **separate customer-managed keys per data class** — `audit`, `corpus`, `uploads`, `outputs`, and DynamoDB — each with a narrower key policy and a **distinct break-glass grant**. A role scoped to uploads cannot decrypt audit or corpus data. One key per environment was too coarse; this limits blast radius if any single role or key is compromised.
- All data in transit is TLS.
- **Split, least-privilege API roles.** The API's capabilities are split rather than carried by one broad role: distinct **upload**, **review-start**, **read-status**, and **download** capabilities, each scoped to the specific S3 prefixes and KMS keys it needs (with **KMS encryption-context checks** so a key grant only decrypts objects written under the expected context). No role has broad read/write across all document buckets. The pipeline task role queries only the Knowledge Base it owns and invokes only the pinned regional Bedrock model; the retention purge worker can delete only in `uploads`/`outputs` and only for terminal, non-held reviews; the break-glass role is separate and normally unused. Object access is short-lived and scoped.
- No long-lived credentials in any container. App Runner and the pipeline tasks use their task roles; OAuth and other secrets are resolved at runtime (never at CDK synth — see [Infrastructure](#infrastructure--aws-cdk)).
- **Hosted-domain enforcement is defense-in-depth**, in two independent layers (Cognito edge + backend JWT validator), and is a **prerequisite to the application allowlist** — domain membership authenticates, the allowlist authorizes. See [Authentication](#authentication--cognito-federated-to-google). Deprovisioning (status check, token revocation, periodic Workspace sync) is enforced on every request.
- **Authorization / row-level security.** A review is readable and downloadable only by its `owner_sub` or an admin — never by any signed-in user. Every `reviews` row carries `owner_sub` and an `access_scope`, and access is decided by ownership + role on every `GET`.
- **Every non-health endpoint requires authorization.** `/health` is public but **minimal — liveness only** (`{"status":"ok"}`), with **no version, commit SHA, or image digest**, so an unauthenticated caller cannot fingerprint the exact build of a confidential legal tool. Build details move to an **allowlisted `/version`** endpoint. All routes other than `/health` require a valid Cognito token, `company.com` domain checks, allowlist membership, and `users.status == active` before route-specific owner/admin authorization runs.
- **Download authorization.** The redlined `.docx` is served either through an **authenticated streaming endpoint** or via a **very short-lived presigned URL generated only after the owner/admin check passes**, with `Cache-Control: no-store`. Every view, download, presign, and failed access attempt is audited. Review IDs are **high-entropy and non-enumerable** so an output URL cannot be guessed or walked. (Per-user request-size caps, concurrency limits, daily limits, WAF, and rate limits on upload/poll are owned by [docs/threat-model.md](docs/threat-model.md) and enforced in the API.)
- **Prompt-injection resistance.** **Both** the counterparty `.docx` **and the retrieved corpus precedent** are treated as **untrusted data**, not instructions — corpus documents can carry hostile instructions, hidden text, or copied injection language just as an uploaded draft can. All untrusted text is wrapped in explicit delimiters with a system instruction that nothing inside is a directive to the model. Model output is schema-validated and **leakage-scanned** before it can drive a redline. Cost and output-length **outliers are flagged** as a possible injection or runaway signal. The deep threat model is owned by [docs/threat-model.md](docs/threat-model.md).
- **Atomic daily spend ceiling.** A configurable per-day cost cap (**default $20/day**) is enforced by an **atomic reservation** — a conditional DynamoDB counter reserves estimated cost *before* the pipeline starts and settles actual cost afterward — so concurrent submissions cannot collectively bypass a bare pre-check. The cap and today's spend are surfaced in the admin UI alongside the cost ledger.
- **Tool-recommendation framing.** All outputs and UI states are watermarked "tool recommendation only — attorney approval required"; ACCEPT reads "no requested changes identified by tool"; generated redlines carry an internal-only/export-warning marker; and low-confidence outcomes route to `MANUAL_REVIEW_REQUIRED` as a system status, not a legal category. These are misuse-prevention controls (see [docs/threat-model.md](docs/threat-model.md)), distinct from any approval workflow.
- S3 block-public-access at the account level; the Knowledge Base / S3 Vectors store is private (IAM + VPC endpoint), never public.
- Bedrock model access is explicitly enabled in `us-east-1` only, and configuration pins the **single-region native** model ID invoked against the regional endpoint; **both `global.` global profiles and `us.`/`eu.`/`apac.` geo cross-region inference profiles are forbidden** (a geo profile can route to another region in the geography, breaking single-region residency) unless explicitly approved and recorded. A config check rejects any prefixed inference-profile ID.
- **Sensitivity is classified, not assumed.** We no longer lean on a bare "EIAAs are low-sensitivity" assumption: actual document contents (facility, insurance, compliance, student-program, healthcare terms can appear) are classified in [docs/data-handling.md](docs/data-handling.md), and the controls here are built **for the next sensitivity tier now** — the RLS, per-class keys, and split roles drop in for a more-restricted agreement type without re-architecting.
- **All security-bearing work lands in Phase 0** — JWT validation, two-layer domain enforcement, the application allowlist, owner-or-admin authorization, private network access to the retrieval store, the atomic spend ceiling, and append-only audit are foundation items, not later add-ons.

## Cost shape

Design target: **≤ $100/month** for the dev environment, **~$25/month when idle** (no reviews). The single biggest lever was dropping OpenSearch Serverless — its OCU minimum (~$350/mo) alone blew past both targets at zero usage. With that gone, idle cost is essentially just the always-on API instance, and everything else is pay-per-use.

#### Per-review token caps and reservation formula

Each review is bounded by three hard per-review config values:

| Config key             | Default | Notes                                      |
|------------------------|---------|--------------------------------------------|
| `max_input_tokens`     | 80,000  | Per pass (system + user prompt combined)   |
| `max_output_tokens`    | 8,000   | Per pass (structured JSON response)        |
| `max_retries_per_pass` | 1       | One bounded structured-output retry per pass; a second failure → `ERROR_MANUAL_REVIEW_REQUIRED` |

The **worst-case spend reservation** for a review is:

```
reservation = passes × (1 + max_retries_per_pass)
            × (max_input_tokens + max_output_tokens)
            × uncached_price_per_token
```

With the defaults above, worst-case cost is computed per-pass using the pinned model rates from the unit-economics table below — Opus primary (2 attempts × 80K in × $5.50/M + 2 attempts × 8K out × $27.50/M = **$1.32**) plus Sonnet critic (2 attempts × 80K in × $3.30/M + 2 attempts × 8K out × $16.50/M = **$0.79**) totals ≈ **$2.11 worst-case** per review. At the default `$20/day` ceiling this allows **~9 concurrent worst-case reviews per day** before the ceiling blocks further starts. Because the retry budget is folded into the reservation at reserve-time, any sequence of attempts within that budget cannot overshoot it; only the settled actual spend (ledgered after every model attempt, including failures) can be less.

The **leakage scan** is a **deterministic rule-based check** (not a model call). It applies exact and
normalized (case-folded, whitespace-collapsed) n-gram matching against the set of known-confidential
tokens from the system prompt, the active playbook, and the corpus, over **all human-surfaced model
prose** — `verdict_summary` (ACCEPT path), footnote rationales, critic deltas, and
`proposed_replacement_text` (redline). The scan runs over the structured-JSON output fields
independently of which path (ACCEPT or REQUEST_CHANGE) the review is on. A positive detection on any
scanned field routes the review to `ERROR_MANUAL_REVIEW_REQUIRED`. Cost is negligible (CPU only) and
adds no token spend to the reservation.

**Paraphrase residual risk (known limitation).** The deterministic n-gram layer does not catch
paraphrase: a model that rephrases a playbook position rather than quoting it verbatim will not be
blocked by this layer. This is a documented residual risk, not a silent miss. Residual coverage is
provided by the **internal-only watermark** (every output is marked "tool recommendation only —
attorney approval required" and not for external transmission) and the **attorney approval gate**
(approval happens outside this tool; the attorney's review is the final human-in-the-loop check
before any output reaches the counterparty). If a model-based second-layer scan is added in a future
release, it will join the model-policy matrix with its own eval run and cost reservation.

**CI leak fixtures.** CI (`tests/test_leakage_scan_all_prose.py`) includes fixtures that cover:
- An ACCEPT-path `verdict_summary` seeded with a verbatim playbook fragment — expected: held
  (routed to `ERROR_MANUAL_REVIEW_REQUIRED`).
- A `critic_delta` rationale seeded with a system-prompt fragment — expected: held.
- A clean `verdict_summary` with no confidential tokens — expected: pass.
- A paraphrase of a playbook position — expected: pass (documented known limitation).

These fixtures run on every change to scanner logic or prompt/playbook content.

#### Unit economics

Verified current Bedrock rates for the pinned model IDs (us-east-1, single-region native, Standard tier, uncached, 2026 rack rate including the ~10% regional endpoint surcharge over the direct API base rate; see [docs/design-notes.md](docs/design-notes.md) for the reconciled pricing note):

| Model               | Input ($/M tokens) | Output ($/M tokens) |
|---------------------|--------------------|---------------------|
| Opus 4.8 (primary)  | ~$5.50             | ~$27.50             |
| Sonnet 4.6 (critic) | ~$3.30             | ~$16.50             |

Using the caps above (1 primary + 1 critic pass, each up to 80K in / 8K out, no caching, one retry each):

| Metric                               | Value                                                       |
|--------------------------------------|-------------------------------------------------------------|
| **Worst-case $/review**              | ~$2.11 (Opus primary: 2 attempts × 80K in × $5.50/M + 2 attempts × 8K out × $27.50/M = $1.32; Sonnet critic: 2 attempts × 80K in × $3.30/M + 2 attempts × 8K out × $16.50/M = $0.79) |
| **Typical $/review**                 | ~$0.65–$0.90 (average-length EIAA, 1 attempt each, no retries) |
| **Max reviews/day at $20 ceiling**   | ~9 worst-case (arithmetic: $20 ÷ $2.11); ~22 typical       |
| **Steady-state cache hit rate (v1 production)** | **≈ 0** — at 2–7 reviews/day spread across a workday, inter-review gaps far exceed the ~5-min cache TTL; caching saves primarily on back-to-back retries and eval runs, not typical spaced production usage |
| **Dev idle monthly**                 | ~$25–$50: App Runner always-on instance (~$25), VPC endpoints (~$7–10), KMS CMKs (~$3–5), CloudWatch dashboards/alarms (~$2–3), CloudTrail management events (~$1); Bedrock/S3 Vectors/Step Functions are $0 at zero reviews |
| **Prod monthly target (50–200 reviews/mo)** | ~$60–$180: Bedrock alone $30–150 (50–200 × ~$0.65–$0.75 typical), plus ~$50 fixed infrastructure; well inside ≤$100/mo dev target and compatible with the ≤$200/mo prod budget |

At the expected peak of 2–7 reviews/day, a $20/day ceiling provides comfortable headroom: 7 typical-cost reviews = ~$6.30/day, well within budget; and the ~9 worst-case capacity comfortably clears the 7/day peak with room to spare. To prevent legitimate lockouts at sustained high volume, the ceiling can be raised in the admin UI (see [RUNBOOK.md](RUNBOOK.md) → Adjusting the daily spend ceiling). A $50/day ceiling allows ~23 worst-case or ~55 typical reviews per day while still bounding blast radius.

For an expected volume of 50–200 reviews per month:

- **Bedrock (Opus 4.8 primary + Sonnet 4.6 critic).** Dominant *marginal* cost, but $0 when idle (single-region native ID, Standard tier — no provisioned hourly charge). Opus is ~$5.50/M input, ~$27.50/M output (including the ~10% regional endpoint surcharge; direct API base rate is ~$5/M input, ~$25/M output); the Sonnet critic pass is ~$3.30/M input, ~$16.50/M output, so the second pass costs materially *less* than a second Opus pass would — a deliberate decorrelation-and-cost win. Prompt caching on the ≈30K-token playbook is a **within-model optimization only** — caches are per-model (Opus and Sonnet have separate caches), and at v1 production volume the steady-state inter-review hit rate is near zero (see the cost-shape table above). Caching delivers savings on back-to-back retries and eval runs; the cost model and the reservation are sized to survive a 0% cache-hit run. Note: the current Opus tokenizer produces noticeably more tokens than the prior generation for the same text, so real cost runs higher than naïve estimates — bounded by the **atomically-reserved $20/day ceiling** (a worst-case-upper-bound reservation including retry attempts; see [Backend idempotency](#api-vs-async-worker) and the caps table above), and **every model attempt (including failures and retries) is ledgered** so the spend record is complete.
- **App Runner (API).** The main idle cost. A minimal always-on instance is on the order of ~$25/month; we keep it small and let the pipeline (which is pay-per-use Lambda/Fargate) absorb the heavy work.
- **Bedrock Knowledge Base + S3 Vectors.** Pay-per-use storage and query, no idle OCU floor. Near-$0 idle for a ~50-document corpus.
- **Step Functions, Lambda/Fargate pipeline.** Pay-per-execution; ~$0 when no reviews are running. (There is **no SQS** on the review entry path — the API starts Step Functions directly.)
- **S3, DynamoDB (on-demand), Cognito, Amplify, CloudWatch, CloudTrail (management events only).** Single-digit dollars per month combined at this scale.
- **Fixed infrastructure overhead.** VPC endpoints (~$7–10/mo), KMS customer-managed keys (~$3–5/mo), CloudWatch dashboards and alarms (~$2–3/mo), CloudTrail management events (~$1/mo). Combined these are ~$13–19/mo and are included in the dev idle figure above — the total idle cost is roughly double the App Runner instance alone.

**Guardrail.** An **AWS Budgets** monthly budget with alerts is provisioned in CDK so the ≤ $100/mo assurance is enforced by an alarm, not just an estimate. Separately, the in-app **$20/day** ceiling bounds Bedrock spend per day (default; admin-configurable). The UI displays per-review cost to all users and aggregate cost (plus the daily ceiling and today's spend) on the admin dashboard.

## Environments

**Prod runs in a separate AWS account from dev.** Because this tool handles legal-facing data, production is **account-isolated** from development — there is a clean trust and blast-radius boundary, and a dev mistake cannot reach production legal documents. v1 still avoids an elaborate multi-account org structure (no sprawling OU tree), but the prod/dev separation is non-negotiable; this resolves the earlier "no multi-account, but dev + prod" contradiction in favour of isolating prod.

**Account topology.** The two environments live in distinct AWS accounts; CDK context (`--context env=dev` / `--context env=prod`) selects the correct account at deploy time. Account IDs are recorded in `infra/cdk.json` (placeholder values replaced before first deploy) and in the environment table at the top of [RUNBOOK.md](RUNBOOK.md). The same CDK app is deployed to each account — there is no shared account for legal-facing data.

To hold cost down the **dev account** runs a **single shared dev environment** (not per-developer stacks). Day-to-day iteration happens **locally**, but **developer laptops never reach production legal documents**: local dev uses a **synthetic corpus and synthetic documents only**, and retrieval uses a **local stub** (or a tiny local vector index) so iterating on review logic incurs no cloud vector cost and creates no path from a laptop to real legal data (the data-leak controls are owned by [docs/threat-model.md](docs/threat-model.md)). Only changes that genuinely need cloud retrieval touch the shared *dev* Knowledge Base — never prod. This keeps the idle cloud footprint near the ~$25/mo idle goal while still giving a realistic place to test end-to-end. The AWS Budgets alarm guards each account against surprises.

**Dev-to-prod guardrail.** The account boundary is the primary enforcement mechanism: developer SSO does not grant prod credentials, and dev never holds prod keys. There is no cross-account IAM trust between the dev account and the prod account, and no dev-scoped role carries permissions to read or write any prod S3 bucket, DynamoDB table, Knowledge Base, or KMS key. A developer with full `AdministratorAccess` in the dev account cannot reach production legal data. Do not copy, mirror, or point local tooling at prod buckets, the prod corpus, or the prod Knowledge Base; see [RUNBOOK.md → Local development](RUNBOOK.md#local-development) for the operator-facing rule and [docs/threat-model.md](docs/threat-model.md) for the threat framing.

## What we are explicitly not building

- We are not building a Word add-in. Your organization already has one; the redlined `.docx` opens in it directly.
- We are not building approval workflow. Approval happens outside this tool — but we **do** capture the attorney's accept/edit/reject outcome for the feedback loop (see [docs/evaluation.md](docs/evaluation.md)), and outputs are watermarked as recommendations.
- We are not fine-tuning a model. See [docs/design-notes.md](docs/design-notes.md).
- We are not building generic CLM. This tool reviews EIAAs against a single playbook. Other agreement types are out of scope for v1.
- We are not building an elaborate multi-account org structure. v1 keeps the account topology minimal — **but prod is still isolated in its own AWS account** from dev (see [Environments](#environments)); that isolation is in scope, the org sprawl is not.
- We are not auto-deploying from `main`. Production is promoted deliberately by signed-image digest through CI (see [Infrastructure](#infrastructure--aws-cdk)); a merge does not change prod.
