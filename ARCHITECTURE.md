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
│   std-form diff →            │                            │
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
│ + Sonnet 4.6     │  │  DORMANT --  │  │ lock on    │  │            │
│ adversarial      │  │  built,      │  │ corpus     │  │            │
│ critic           │  │  empty, not  │  │            │  │            │
│                  │  │  in the      │  │            │  │            │
│                  │  │  running     │  │            │  │            │
│                  │  │  pipeline    │  │            │  │            │
└──────────────────┘  └──────┬───────┘  └────────────┘  └────────────┘
                    ▲
                    │ admin upload → draft ingestion snapshot →
                    │ curated/tested active snapshot
                    │ (never run in production -- see docs/rag-dormant.md)
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

**Nothing in this product enforces, requires, gates on, or records attorney approval — approval happens outside this tool, in your organization's own review process.** This is not cosmetic: it is a misuse-prevention framing (see [docs/threat-model.md](docs/threat-model.md)). A generated redline `.docx` carries an **internal-notes export marker** ("contains internal notes — not for external transmission") iff that review's notes mode actually put internal-audience content in scope — never unconditional, and carrying no approval semantics (see [Redlining](#redlining--owned-docx-library)); the SPA result view has no UI counterpart to this marker (issue #492).

- **ACCEPT does not mean "approved".** The ACCEPT state never reads "no action needed". It reads **"no requested changes identified by tool"**. A clean tool pass is a tool result, not legal sign-off.
- **Manual-review is a system status, not a legal category.** When the pipeline routes a review to manual review (low internal confidence with no concrete playbook issue, or a structured-output failure), the UI shows it as a distinct **system status** (`MANUAL_REVIEW_REQUIRED`), visually separate from the `ACCEPT | REQUEST_CHANGE` legal decisions, so a reviewer never mistakes a pipeline outcome for a legal opinion.
- **Per-issue provenance badges — system metadata, not a legal category.** The result view renders a small provenance badge alongside each issue in a `REQUEST_CHANGE` response. The badge reflects the `provenance` field from the model response (`"detector:<rule_id>"` | `"model"` | `"critic-added"`) and is framed as system metadata so an attorney can calibrate scrutiny: a deterministic detector fire is mechanical and near-certain, while an LLM judgment call (`model`) is probabilistic. The badge must never be styled as a legal confidence level or additional legal decision; it is a source-attribution label only. The binary `ACCEPT | REQUEST_CHANGE` decision is unchanged.
- **Low-confidence band — visible pre-download.** When the pipeline's `confidence_band` field is non-null (i.e. `confidence_state` is not `OK`), the result view renders a **confidence band** prominently before the download button. The band is a system-status indicator (`LOW_CONFIDENCE`, `MANUAL_REVIEW_REQUIRED`, or `ERROR_MANUAL_REVIEW_REQUIRED`), rendered as a distinct visual element clearly labeled as a pipeline signal, not a legal opinion. This ensures the attorney sees the confidence context before acting on the result — they are not required to open the download to discover the pipeline flagged uncertainty. The band is framed consistently with the `MANUAL_REVIEW_REQUIRED` system-status framing rule above.
- **Critic-delta presentation — impossible to miss before download.** When the pipeline's `critic_delta` field is non-null and contains contested replacements or critic-added issues, the result view surfaces a **critic-delta indicator section** above the download affordance. Contested replacements are shown with a **"critic flagged this replacement" badge** alongside the primary's proposed text; when a critic-suggested alternative is present, the two versions are displayed **side-by-side** (labeled "Primary" / "Critic suggestion") so the disagreement is visible at a glance. Issues with `provenance = "critic-added"` are attributed with a "critic added" badge, using the same visual language as the per-issue provenance badges (one consistent system). The download affordance is not presented until the critic-delta indicator is visible in the document flow — the attorney cannot reach the download without scrolling past the indicator — but there is no blocking modal; the attorney retains full agency. A result with `critic_delta = null` is unaffected. The normative rendering spec lives in [docs/output-contract.md → Critic-delta presentation](docs/output-contract.md#critic-delta-presentation).
- **Disposition capture — optional, never a nag.** After a review finishes, the reviewer can optionally record what happened to the tool's output — accepted as-is, accepted with edits, or rejected — plus a short free-text note. This is metadata for the human-review feedback loop, never an approval gate: it does not block access, does not change the review's pipeline `status`/`decision`, and there is no badge, banner, or reminder pushing a reviewer to record one (an earlier draft of this spec proposed an "N reviews awaiting disposition" nag; the owner corrected that on issue #486 — attorney/legal review is a policy the deploying organization owns entirely outside this product, so this product does not chase anyone for an outcome record either). The control lives on the Review tab's finished-review panel and is settable, or changeable, per row on the History tab. The disposition mechanics are specified in [docs/evaluation.md → Human-review feedback loop](docs/evaluation.md#human-review-feedback-loop).

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

**Design system.** The SPA's UI is built on CTDS, a light-DOM-first Lit component library (`frontend/src/ui/`) consumed through typed React wrappers (`ui/react.ts`) — never raw `<ct-*>` JSX. A dev-only component gallery (`frontend/gallery.html`, excluded from production builds) renders every component in every variant/state, in both themes, alongside the token sheet, and doubles as living documentation; see `frontend/src/ui/README.md` for the contributor guide and [docs/frontend-design-system.md](docs/frontend-design-system.md) for the full doctrine.

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

The service is stateless. All state lives in S3 and DynamoDB. (A Bedrock Knowledge Base
exists in infrastructure but holds nothing — see the retrieval note below — so it carries
no live state today.)

#### API vs. async worker

App Runner runs the **API only** — fast, request/response work: auth, validation, persistence, starting and polling reviews, and admin CRUD. It never blocks on an LLM call. `POST /api/reviews` stores the upload, reserves estimated spend, creates the `reviews` row as `PENDING`, and **starts a Step Functions execution directly** (no SQS), returning `202` with the review id immediately. The browser polls `GET /api/reviews/{id}`.

**Idempotency (no SQS buffer on the entry path).** SQS has been removed from the review entry path; the API calls `StartExecution` directly. Idempotency is achieved without a queue, but the transaction is explicit so retries cannot double-reserve spend or leave orphan `PENDING` reviews:

1. `POST /api/reviews` uses an **idempotency key**. The preferred source is a **client-supplied key** held stable across a client's own retries (double-click, network timeout, mobile re-send). Absent that, the API derives one from `owner_sub` + uploaded-file SHA-256 + active release-bundle hash + a **coarse timestamp bucket** of a documented, fixed width (default **10 minutes**). Because a derived key would otherwise change at a bucket boundary — letting a boundary-straddling retry start a second pipeline and double-reserve spend — the API checks **both the current and the immediately previous bucket** for an existing submission record before creating a new one. A deliberate re-review of the same file is an **explicit "review again" action** that mints a fresh key on purpose, rather than something a retry can trigger by accident.
2. The API creates a **submission record** with a conditional write on that idempotency key. That record owns the canonical `review_id`, upload pointer, spend-reservation ID, Step Functions execution name, execution ARN, and status.
3. Spend is reserved **once per `review_id`**, not once per HTTP attempt, as a **worst-case estimate priced from the per-review caps** (see [Cost shape](#cost-shape), which states the one case where it can settle above the reservation). A retry that finds the submission record reuses the existing reservation.
4. The API stores the upload and creates/updates the `reviews` row only through the submission record.
5. The API then performs an idempotent **ensure execution started** step. If `StartExecution` succeeds, the execution ARN is stored. If a retry sees no ARN, it calls `StartExecution` again with the deterministic name. If Step Functions returns `ExecutionAlreadyExists`, the API records the existing execution and returns the existing review id.

**Worst-case spend reservation.** The reservation happens in the API *before* extraction, so the real token cost is not yet known. To keep the daily ceiling safe against concurrent submissions, the reserved estimate is derived from the per-review caps: `passes × (1 + max_retries_per_pass + max_truncation_retries_per_pass) × (max_input_tokens + max_output_tokens)` at **uncached** pricing, with `max_output_tokens` at the output-sizing function evaluated against the **fail-closed** 32,000-token ceiling (issue #658) — at submission time there is no extracted document to size against. Folding both retry allowances in at reserve-time accounts for bounded structured-output retries, the truncation retry and throttling retries, not just the two passes; an optimistic estimate that ignored retries would let N concurrent uploads each reserve low and collectively overshoot before any settles. The reservation is **atomic** and gates *starts*, and settlement (after the run, from the ledger) reverses it against real ledgered usage. At the default `$20/day` ceiling this admits **~2 reservations held at once** (arithmetic: $20 ÷ ~$6.86 reserved/review) and, because settlement releases each one, roughly 25 typical reviews run one after another; the exact per-review caps, the unit-economics table, and the max-reviews/day figure are documented in [Cost shape](#cost-shape) and surfaced on the admin dashboard.

**What that reservation is not (issue #658).** It is **not** an upper bound on settle, and the arithmetic is the reason: it prices the fail-closed 32,000-token output ceiling, while the sizing function asks for the *selected model's own declared cap* — 128,000 for the Anthropic models the shipped policy pins — on any document over ~44,800 estimated tokens, which is exactly the ~80-page agreement issue #658 exists to enable. Measured at the rates below: ~**$6.86** reserved per review against a worst-case actual of ~**$19.54** (2.85×; 4× on output tokens, up from ~1.5× before #658), always in the direction of reserving *less*, never more. Concretely: two concurrent submissions reserve $13.72, are both admitted under the `$20/day` default, and can settle at $39.08. This is a deliberate trade rather than an oversight — pricing every review at 128,000 would reserve ~$19.54 and refuse the day's second review for a budget almost no review asks for, which is what issue #658's note means by *“the real argument for scaling rather than pinning every review to the maximum.”* What holds in its place: settlement reconciles every reservation from the ledger the moment a review ends, so the day's counter converges on actual spend and an overshoot cannot compound beyond the reviews that were in flight; and the ceiling can be raised where concurrent large-document reviews are expected (see [RUNBOOK.md](RUNBOOK.md) → Adjusting the daily spend ceiling).

**Recovering an orphaned submission — missing ARN and dead-execution paths.** A scheduled/event-driven **reconciler** handles two distinct stuck-state holes:

1. **Missing ARN** (existing path). For any submission record older than a short threshold that still has no `execution_arn` (e.g. a crash between upload persistence and `StartExecution`), the reconciler re-runs the idempotent "ensure execution started". It only escalates to a human (stale-`PENDING` alarm) if the re-drive still fails.

2. **Dead execution — PENDING-with-dead-ARN** (new path). Step Functions execution names are unique for ~90 days including terminal executions, so if an execution dies before its error-handling states run, a bare "ensure execution started" gets `ExecutionAlreadyExists` and would naïvely record the existing (corpse) ARN — pinning the review to a dead execution while the review row sits `PENDING` with an ARN, invisible to the missing-ARN alarm. The reconciler therefore calls **DescribeExecution** on any non-terminal review that already has an `execution_arn` and whose execution status is **FAILED, TIMED_OUT, or ABORTED** (a terminal execution whose error-handling states did not complete). On detecting a dead execution, the reconciler: (a) transitions the review row to **`ERROR`**, (b) **releases the spend reservation** (settles to zero if no ledgered spend), and (c) **releases the concurrency slot** so subsequent reviews are not blocked. It records the terminal execution status and the reconciler run ID in the audit entry. The stale-`PENDING` alarm is extended to cover this case: a `PENDING` review whose `execution_arn` resolves to a terminal execution is treated the same as a review with no ARN — it is a stuck review, not a healthy one.

There is no SQS queue, consumer, or DLQ buffering submissions; pipeline-level durability comes from Step Functions' own execution history, per-step retries, the submission record's recovery contract, and the orphan reconciler above.

The review itself — extraction, normalization, standard-form diff, two-pass LLM review, leakage scan, redline generation — runs **off the request path** in a Step Functions state machine whose steps are Lambda / Fargate tasks. This removes the request-timeout, retry, and partial-failure problems of doing minute-scale LLM work inside an HTTP handler, and lets each stage retry independently. See [Data flow](#data-flow--a-single-review). (Retrieval is **designed but not implemented** — see [Retrieval status](#retrieval-status-dormant-by-decision) — so it is not one of the running stages above.)

#### Why App Runner and not Lambda for the API

The API is a warm, always-on HTTP surface; App Runner gives us a warm Python process with configurable concurrency, predictable cost, and a simple digest-pinned deploy. The long-running review work that *would* have strained Lambda's cold-start/timeout profile is exactly the part we moved into Step Functions, where step-level timeouts and retries are first-class.

#### Routes

| Method | Path                       | Auth          | Purpose                               |
|--------|----------------------------|---------------|---------------------------------------|
| GET    | `/health`                  | none          | Liveness only (`{"status":"ok"}`) — minimal, no build details |
| GET    | `/version`                 | allowlisted   | Version, commit SHA, serving image digest (authenticated) |
| POST   | `/api/reviews`             | allowlisted   | Upload a `.docx` with an optional `playbook_id` selector (defaults to `eiaa`); idempotency-keyed; returns `202` + review id, starts async pipeline directly |
| GET    | `/api/reviews`             | allowlisted   | List my reviews (admin: all reviews). `?scope=mine` narrows it to the caller's own rows for an admin too — what the History tab asks for |
| GET    | `/api/reviews/{id}`        | owner or admin| Get a review's status and result      |
| GET    | `/api/reviews/{id}/output` | owner or admin| Download the redlined `.docx`. `410` once retention has purged the object |
| GET    | `/api/reviews/{id}/input`  | owner or admin| Download the `.docx` that was reviewed. `404` when the review predates the recorded upload pointer; `410` when the stored pointer no longer resolves to an object — i.e. once the retention sweep has purged the input document (see [Storage](#storage)). |
| GET    | `/api/playbooks`           | admin         | List playbooks                        |
| GET    | `/api/playbooks/{id}`      | admin         | Get current version of a playbook     |
| GET    | `/api/admin/playbooks/{id}/versions` | admin | Version-upload trail for a playbook (oldest-first: version, uploader, timestamp, notes) |
| POST   | `/api/admin/playbooks/{id}/versions` | admin | Upload a new playbook version — multipart; content hash computed server-side (lands `draft` until activated) |
| POST   | `/api/admin/playbooks/{id}/versions/{version}/rollback` | admin | Roll back to a previously-active (retired) version; `409` if the target was never active |
| POST   | `/api/admin/playbooks/{id}/pen-rules/validate` | admin | Validate a candidate pen-rules + posture-override document (the shape `scripts/bind_bundle.py`'s CLI accepts, supplied with the `opf` to validate against — there is no server-side OPF keyed by playbook_id) — reuses that module's fail-closed validators and returns one machine-readable error per failure (`unknown_floor_ref`, `stale_parent_section_digest`, `non_monotonic_version`, `colliding_floor_additions`, plus `playbook_id_mismatch`). **Read-only: validation only, no persistence, no audit row.** Even a valid document has **zero runtime effect on any live review** — since issue #479 `pipeline_runner.py`'s `_load_playbook_bundle` does consume an activated **OPF document** (via `_load_opf_bundle_if_active`), but that path hard-codes `"overrides": None`, so the pen-rules/posture *overrides* bundle this route validates is still consumed by nothing. The persist/bind-and-activate counterpart is a deliberate follow-up (it needs a server-side v2-bundle storage/activation model that does not exist yet); see the "Guidance-precedence model" item 4 caveat. |
| GET    | `/api/admin/diagnostics/recent-failures` | admin | Why recent reviews failed (issue #443) — a bounded (`?limit=N`, clamped), newest-first list of recent non-OK terminal reviews carrying **only** `review_id`, `created_at`, `failing_stage`, the issue-#442 `reason` token, and the terminal `status`. **Not a log viewer:** no stack trace, exception message, prompt or document substance, key material, or raw endpoint is reachable through it — the response is an explicit field projection (`reviews._RECENT_FAILURE_FIELDS`), the same allowlist discipline `retention._HOLD_LIST_FIELDS` applies to the legal-hold list view. |
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

- **A specific in-region model ID and region for each role.** The release bundle records `primary_model_id`, `critic_model_id`, optional `fallback_model_id`, `model_region`, request contract, evaluation run, and cost assumptions. `embedding_model_id` is **carried but inert** — the field exists in the bundle schema (`playbooks/schema.json`) and shipped sample bundles still populate it (e.g. `playbooks/samples/synthetic-nda-sample-v1.0.0.json`), but nothing embeds, nothing reads it, and no governance obligation attaches to it today. See the bullet below. v1 pins **Opus 4.8 as the primary reviewer and Sonnet 4.6 as the adversarial critic** (a deliberately *different* critic — see [Two-pass review](#llm--bedrock--claude-opus-48-primary-sonnet-46-critic)) in `us-east-1`, unless a later evaluated policy deliberately changes that matrix.
- **Single-region native inference only — no inference profiles.** Legal-facing data must run in the one region named by the model policy. We use the **native model ID** invoked against the regional endpoint. **Both `global.` global profiles and `us.`/`eu.`/`apac.` geo cross-region inference profiles are forbidden** in configuration — a geo profile can route a request to another region in the geography (e.g. a `us.` profile to us-east-2 or us-west-2), which breaks a strict `us-east-1` residency guarantee. A config check rejects any model ID carrying a `global.`/`us.`/`eu.`/`apac.` prefix unless explicitly approved and recorded. (Research note: as of 2026, Opus is available on-demand via the single-region native ID in `us-east-1` and is Standard-tier only, so this residency posture is achievable **without** Provisioned Throughput — preserving the $0-idle cost model.)
- **The embedding model's governance is designed, not active.** Retrieval is **dormant by decision, not implemented** (see [Retrieval status](#retrieval-status-dormant-by-decision) below) — nothing is embedded. An `embedding_model_id` value still appears in the bundle schema and in shipped sample bundles, but it is inert: no code reads it (`grep -rn embedding_model_id backend/ scripts/` → no hits), it is not recorded on any review, and no quarterly-recertification or admin (GC) approval obligation is currently in force for it. The design intent, if retrieval is revived, is that a pinned embedding model would be recorded on every review, recertified alongside the primary/critic matrix, and a change to it (or any re-embedding of the corpus) would require admin (GC) approval and produce a new `corpus_snapshot_version` — because changing embeddings changes which precedents are retrieved, and therefore legal output, as surely as a prompt change. That obligation re-activates the moment retrieval is turned on; it is not owed while nothing is embedded. See [docs/rag-dormant.md](docs/rag-dormant.md) §5.4.
- **A deliberate pin, with rationale.** The matrix is a deliberate pin, not "whatever is newest". A newer model is adopted only after it passes the same gold-set, stochastic-stability, redline-fixture, leakage, and cost gates.
- **Quarterly recertification.** The pinned model matrix (primary, critic, fallback) is recertified at least quarterly: re-run the gold set, compare false-positive/false-negative rates and redline-patch behaviour, review current AWS model availability and the exact invocable model IDs, and either re-pin (with an audit entry) or document why the incumbent stays. The model IDs, region, request contract, and certifying eval run are recorded with each active release bundle. (Embedding is excluded from this recurring obligation while retrieval is dormant — see the bullet above.)
- **On-demand quota recorded and throughput ceiling derived.** Forbidding geo/global inference profiles removes cross-region load balancing, so throughput is bounded by the native on-demand TPM/RPM quota for each model in `us-east-1`. The granted quota is recorded in **`model-policy/bedrock-us-east-1.json`** (`models.primary.granted_tpm`, `models.critic.granted_tpm`) alongside the derived `review_throughput_ceiling` and `max_eval_parallelism` fields. Production volume (2–7/day) is safely under any plausible quota; the eval harness (39 cases × 2 passes × multiple stochastic runs at ~60K tokens/call with CI parallelism) is the stress case — it is rate-limited to the recorded quota. Quota figures are re-verified at each quarterly recertification; a stale figure causes the eval harness to under-utilize (too conservative) or throttle (too aggressive).
- **Fallback model policy — automatic failover is prohibited.** If a `fallback_model_id` is recorded in the active release bundle, **automatic failover is prohibited**: the pipeline must never silently substitute a different model for the pinned primary or critic on a `ThrottlingException` or other transient error. An automatic fallback would swap a frontier model for a lighter-weight model on legal output without any record, which is exactly the undocumented drift the model-policy matrix exists to prevent. Fallback use is **manual-only**: an admin (GC) action, audited, and only under a separately activated release bundle that carries a certifying eval-run reference for the fallback model — the fallback model must pass the same gold-set, stochastic-stability, and redline-fixture gates as the primary. Every review run under a fallback bundle records `fallback_used=true` and the fallback model ID. A `fallback_model_id` that has no associated certifying eval run in its release bundle must not appear in the active configuration; the v1 seed playbook omits `fallback_model_id` until Haiku has a qualifying eval run.

The request schema is pinned to the exact contract the pinned models accept: we **omit the `temperature`, `top_p`, and `top_k` sampling parameters** (AWS documents these as no longer supported for the current Opus/Sonnet generation) and rely on the model's default decoding. If extended thinking is used it is **adaptive-only** (the model controls the thinking budget); we do not assume a manually-set thinking-token budget. The exact invocable native model IDs are verified at bootstrap (AWS occasionally renames IDs and version suffixes differ by model).

**Prompt structure** (every review):

1. **System prompt.** Combines (a) review guidance adapted from `claude-for-legal`'s `contract-review` skill (see [Redlining](#redlining--owned-docx-library) and design notes — this is an internal fork we own, not a live dependency), (b) our binary-decision overlay (collapse the upstream GREEN/YELLOW/RED to `ACCEPT | REQUEST_CHANGE`), and (c) the current playbook JSON. Issue #398 adds two further blocks, each omitted entirely when there is nothing to say: an optional **toaster-guidance block** between (a) and (b) — the per-review free-text instructions typed into the toaster, `POST /api/reviews`' optional field — which **governs over the playbook's positions on conflict** but never over the Floor block next; and a **judged-NL Floor block** between (b) and (c), present whenever the playbook carries `hard_rejections`, projecting each rule as a non-negotiable "MUST NOT" obligation the model itself must catch and flag with a concrete, document-addressed edit (a block transcript proven against the document's own bytes, issue #627) — the safety companion to retiring the deterministic `hard_rejections` detector (issue #380).
2. **User prompt.** Defined precisely by the per-pass manifest below — see [Per-pass prompt manifest](#per-pass-prompt-manifest). **The counterparty document is untrusted input, and so is retrieved precedent text whenever it is present** (retrieval is currently dormant, so the prompt carries none — see [Retrieval status](#retrieval-status-dormant-by-decision)). All untrusted content is wrapped in explicit delimiters with an instruction that nothing inside any delimited block is an instruction to the model (see [Security posture](#security-posture) and [docs/threat-model.md](docs/threat-model.md)).
3. **Response format.** A structured JSON output specifying the overall decision and a per-issue list, each issue carrying `section_ref`, `section_title`, `counterparty_change_summary`, `decision`, `external_rationale_for_footnote`, `proposed_replacement_text`, `playbook_topic_id`, and `internal_precedent_citation`. The response is **strictly schema-validated** before it is allowed to produce a redline. Internal precedent citations are audit-only and are stripped from generated `.docx` footnotes.

#### Guidance-precedence model (confirmed 2026-07-27/28 for the frontend release audit)

The playbook is negotiating history/precedent; toaster-owned guidance outranks it. In current code this is **five separate mechanisms** at different levels of maturity — a UI or doc that treats them as one layer, or as equally live, would misrepresent what actually happens on a review:

1. **`toaster_guidance` (per-review, ephemeral) — live today.** The precedence above is enforced by prompt instruction, not by code: `scripts/primary_review_pass.py`'s `TOASTER_GUIDANCE_INTRO` tells the model in so many words that this block is "HIGHEST PRECEDENCE AMONG PLAYBOOK POSITIONS" and "does NOT reach the MUST-NOT FLOOR." Nothing mechanically prevents the model from ignoring it; the critic pass is the only check. **Reachable from the UI since issue #431** — the Review tab's submission form carries an optional free-text field, appended to the upload `FormData` as `toaster_guidance` (absent entirely when left empty, so a no-guidance submission is byte-identical to the pre-#431 request), with the precedence rule stated permanently beside the input in the same "governs, never mechanically guaranteed" terms as this item. The value it was submitted with is recorded on the reviews row and projected by `get_review_detail`, so a completed review can show back — read-only — which instructions actually governed it.
2. **The judged-NL Floor, projected from `hard_rejections` — live today, in the same prompt call as (1).** Never waivable by (1) or by anything else (see `FLOOR_BLOCK_INTRO`). This is the mechanism the "playbook is the Floor" language in this document's other sections refers to.
3. **The OPF Floor** (`opf.floor.invariants`, `scripts/floor_judge.py`) — **wired and live for an OPF-governed review as of issue #479.** `scripts/review_spine.py::run_review` judges every invariant exactly once per review as stage 3.5, between the critic pass and reconciliation, and ledgers each attempt with `pass_name="floor"`. If any invariant goes unjudged the review fail-closes to `MANUAL_REVIEW_REQUIRED` / `floor_invariant_unjudged` rather than proceeding on partial coverage; a `violation` verdict becomes a monotonic `detector_fires` entry that `reconcile()` cannot downgrade, so a floor violation cannot be argued away downstream. Which invariants were judged, violated, or left unjudged is persisted on the review row and projected by `get_review_detail`. Explicitly distinct from (2) (`primary_review_pass.py` calls it out as "not composed through this module") — an artifact with an empty `floor` composes no floor block at all rather than an empty header, so a floor-free playbook is a normal state and not a refusal. Do not conflate with (2).
4. **A persistent per-playbook pen-rules / posture-override layer** (`scripts/bind_bundle.py`), authored as a `default`/`per_topic` pen-rules document (`mode`, `max_chars`, `must_not_introduce` phrase bans, optionally tied to a Floor invariant via `floor_ref`) plus a revised `posture` (`system_prompt` prose + a monotonically increasing `version`). This governs **replacement-text generation** (a deterministic post-hoc check via `scripts/replacement_text_enforcement.py::resolve_pen_rules`, bounding length and banning phrases in `proposed_replacement_text`) — a different concern from (1)-(3)/(5), which govern review *judgment*. `bind_bundle.py` fail-closes at build time on an unknown `floor_ref`, a stale `parent_section_digest`, a non-increasing `version`, or a `floor_additions` id colliding with a genesis invariant. **Critical caveat: this layer has zero runtime effect on the currently active catalog.** `resolve_pen_rules` has a `v2` branch (reads the `default`/`per_topic` shape above) and a `v1-passthrough` branch (reads `topic.replacement_text` straight off the loaded playbook); every registry entry today (`playbooks/registry.json`) is v1 (`playbook_path`, never `bundle_path`). Since issue #479 `_load_playbook_bundle` consults `_load_opf_bundle_if_active` first, so **three branches of `resolve_pen_rules` are now reachable**: a registry-v1 review takes the **v1-passthrough** branch as before; an **OPF-governed** review passes `pen_rules_bundle=None` (`scripts/primary_review_pass.py`, `scripts/critic_review_pass.py`) and therefore takes the **`None` → toaster-global-defaults** branch, enforced against `playbooks/pen-rules.defaults.json`; and the **v2** branch remains unreachable, because `_load_opf_bundle_if_active` hard-codes `"overrides": None`. Replacement-text enforcement is therefore **not** inert on the OPF path — it runs against the global defaults. What still affects nothing is a `bind_bundle.py`-authored pen-rules/posture bundle: it has no activation path, so a validated, schema-correct one changes no review until v2 binding lands. `bind_bundle.py`'s **validation** is now also reachable over HTTP — `POST /api/admin/playbooks/{id}/pen-rules/validate` (issue #432, `src/bundle_authoring.py`) — but that route is **validation only, no persistence**, and does not change this caveat: it neither writes a v2 bundle nor makes any live review consume one. **Binding/activating** a v2 bundle is still unbuilt, so until it lands and a v2 bundle is actually activated, this layer continues to affect nothing at review time. **As of issue #484, this validate route is API/CLI-only tooling** — there is no admin-UI authoring surface for it any more (`AdminPenRules.tsx`, the one-off screen issue #435 gave it, was retired and deleted); the standing-instructions pane on the Playbooks admin screen (its own "Playbook instructions" tab until issue #605 folded it in) an operator reaches for today authors mechanism (5) below instead, which is the live, per-playbook judgment-guidance surface this layer was never able to be.
5. **Per-playbook standing instructions** (issue #481/#482/#483, `backend/src/playbook_instructions.py`) — an admin-authored, append-only-versioned, plain-English text box, ONE per playbook, edited from the standing-instructions pane on the Playbooks admin screen (`AdminInstructions.tsx`, issue #484; its own "Playbook instructions" tab until issue #605 merged it into the Playbooks screen). Unlike (4), this governs review **judgment**, the same concern as (1)-(3): `scripts/primary_review_pass.py`'s `STANDING_INSTRUCTIONS_INTRO` tells the model this block "governs over the playbook's positions... wherever the two conflict," in the same enforced-by-instruction-not-by-code terms as (1) — never waivable by (1) or by anything else the Floor (2) covers. Precedence, highest to lowest: (2) Floor → (1) per-review `toaster_guidance` → (5) this playbook-level standing text → the playbook's own positions. **Live today, and live the moment it is saved**: `src/reviews.py`'s `_resolve_instructions_lineage` reads the CURRENT version at review-creation time (issue #482) and stamps `instructions_version`/`instructions_content_hash`/`instructions_text` onto the review's execution input, which `scripts/review_spine.py` threads to both the primary and critic passes (issue #483) — there is no separate "bind/activate" step the way (4) needs one. Saves are compare-and-set (`expected_current_version`, HTTP 409 on a stale page or a losing race) so a concurrent edit is surfaced, never silently dropped, and every version — including an explicit "cleared" (empty-text) save — is retained, newest-first, in the tab's History list.

#### Per-pass prompt manifest

The user-prompt content is defined per-pass. **Manifest changes are prompt changes and are therefore release-bundle gated** — a manifest change requires the same release-bundle activation as any other prompt change. The assembled size of every pass (system prompt + user prompt) is asserted against `max_input_tokens` before any model call; see [Data flow](#data-flow--a-single-review) step 14.

| Block | Primary pass | Critic pass |
|---|---|---|
| Standard-form diff (anchored hunks: standard text, counterparty text, delta, section_anchor) | **Always included** | **Always included** |
| Anchored clause text (standard + counterparty + delta per clause) | **Always included** | **Always included** |
| Retrieved precedent clauses (top-K, hard-labeled positive/negative, from Knowledge Base) | **Designed, not implemented — retrieval is dormant, so this list is always empty and the block is omitted entirely** (see [Retrieval status](#retrieval-status-dormant-by-decision)); if ever non-empty, included | Omitted — critic reasons over the diff and the primary output; re-sending precedents adds tokens without improving missed-issue detection |
| Full counterparty document text | **Always included, in full** (issue #625, 2026-08-25) — the assembled prompt either fits `max_input_tokens` and the whole document is reviewed, or the pass fails closed as `document_too_large` before any model call. There is no reduced-fidelity alternative | **Always included** (issue #618) — the critic receives the identical document string the primary pass read (`scripts/review_spine.py::run_review` forwards the same `doc_text`), in the same untrusted-marked `COUNTERPARTY_DOCUMENT` block the primary uses. **This reverses the 2026-06-11 architecture review's "omitting the full doc from the critic removes the primary redundancy" rationale** — see *Rationale for critic input* below. `assemble_user_prompt_critic` still composes no document block (and emits the no-document variant of its tasking, which says so) for a caller that passes no `doc_text` at all |
| Primary reviewer's output (full structured JSON) | Not applicable (primary produces this) | **Always included** — the critic is tasked with finding missed issues, over-flagging, weak rationales, and replacement-text drift against the primary output |

**Document size policy — no quality tiering (owner decision, 2026-08-25, issue #625).** There is exactly ONE review quality: full-document. Every document whose assembled prompt (system + user) estimates at or under `max_input_tokens` (**100,000**, raised from 80,000 by this decision) gets the full-quality, full-document review; anything above it fails **loudly** as `MANUAL_REVIEW_REQUIRED` / `document_too_large` before any model call. The previous size-gated section-outline fallback (issue #419: above `full_doc_token_threshold` the primary was shown headings and word counts instead of the document) is **deleted**, along with its `input_mode` result field, its one-level confidence degrade, and the fixed notice it appended to the summary. The reason is not cost: a model must never redline text it did not receive, and an outline review degraded exactly that — quietly, on the largest and most complex documents in the corpus, while still returning a confident-looking decision. Failing loudly hands those documents to a human instead. Segmentation/batching for documents over the cap is deliberately **not** in scope.

**Rationale for critic input (critic) — reversed by issue #618.** *The original decision:* the diff + anchored clauses already encoded every counterparty change the primary reasoned over; the primary output showed the primary's conclusions; and sending the full raw document to the critic would have sent the same 8–15K-token contract body a third time without giving the critic any additional signal about *what changed*. Omitting the raw doc was also held to eliminate the ambiguity in the 2026-06-11 architecture review finding — the critic could not reason off the flat document instead of the diff. See [docs/design-notes.md](docs/design-notes.md) for that efficacy argument as it was written.

*Why it no longer holds (issue #618).* Both halves of that rationale rested on the standard-form diff, and **issue #380 retired it**: the `STANDARD_FORM_DIFF` / `ANCHORED_CLAUSES` blocks arrived permanently empty and encoded no counterparty change at all (issue #627 removed the blocks and their plumbing outright). The "primary redundancy" the 2026-06-11 review asked us to remove no longer exists to be redundant with, and neither does the flat-document-versus-diff ambiguity the omission was protecting. What was left was a critic reasoning over the primary's JSON alone — unable to check a single claim against the document it was made about — under the v3 block-transcript contract (issue #627), unable to check the primary's transcript against the blocks it addresses — which is precisely the missed-issue and over-flagging detection the pass exists for. The critic is therefore now given the document the primary read, plus an explicit adversarial tasking (`scripts/primary_review_pass.py::CRITIC_TASKING_BLOCK`) requiring every objection to be grounded in it. **This is a security-boundary change**: untrusted counterparty text now reaches the second model, under the same `UNTRUSTED_BEARING_TAGS` marking and warning as the primary's copy (issue #505), and the two-models-from-two-labs decorrelation argument now covers a critic that has seen the raw text. When a caller composes a critic prompt with no document block at all, `CRITIC_TASKING_BLOCK_NO_DOCUMENT` tells the critic so, rather than demanding evidence from a block that is not there.

**Structured-output failure handling.** Strict validation alone is too brittle — a transient formatting slip should not be indistinguishable from a real pipeline failure, and we never best-effort patch malformed JSON. On a schema-invalid response we perform **exactly one bounded structured-output retry** (re-prompt for valid JSON). If the retry also fails, the review terminates as **`ERROR_MANUAL_REVIEW_REQUIRED`** — a distinct outcome from a pipeline `ERROR` (infrastructure/step failure). The former routes a human to look at a model that won't conform; the latter is an operational incident. Neither produces a redline.

**Replacement text is model-generated, not templated.** Per the [model-selection policy](#model-selection-policy) we run the pinned primary model (Opus 4.8) and rely on its judgment *against the codified playbook guidelines* to draft `proposed_replacement_text` and the footnote rationale. We deliberately do **not** ship 100% scripted/canned clause language — the playbook constrains the position, the model writes the prose to fit the counterparty's specific draft.

#### Retrieval status (dormant by decision)

**Retrieval is designed but not implemented; it does not run.** `scripts/review_spine.py::run_review` always calls the primary/critic prompt assembly with `retrieved_precedent=[]` — there is no retrieval stage in the Step Functions state machine (open: issue #89), and the Bedrock Knowledge Base described elsewhere in this document (S3 Vectors store, corpus DataSource) was built (issue #60) but **never ingested into**, so it holds no state. With an empty precedent list, `assemble_user_prompt_primary` / `assemble_user_content_primary` **omit the `RETRIEVED_PRECEDENT` block from the prompt entirely** rather than composing an empty, untrusted-marked, labelled tag — the same "a block is absent or it has content" doctrine already applied to the toaster-guidance and Floor blocks (see `render_retrieved_precedent_delimited_block`, issue #582). If the precedent list is ever non-empty, the block composes exactly as it always has, so reviving retrieval needs no prompt change.

This is an owner decision (2026-08-11), not an oversight: review is LLM-native (2026-07-22 architecture decision), the playbook digest plus `lookup_clause_evidence` id-based drill-down (issues #579/#580, not yet wired) is the current non-RAG depth path, and retrieval remains parked rather than deleted or built. Reviving it is a real, scoped project with a security-critical prerequisite (re-arming the leakage-scan corpus-name/verbatim-span checks, which are dormant only because no corpus reaches the model today) and a reactivated governance obligation (the embedding-model recertification/GC-approval bullet under [Model-selection policy](#model-selection-policy)). See **[docs/rag-dormant.md](docs/rag-dormant.md)** for the full status, the honest argument for retrieval, and the revival playbook in order — do not treat this section as a substitute for that document.

**Two-pass (adversarial) review.** Each review runs two model passes as distinct Step Functions steps:

1. **Primary reviewer** — produces the decision and per-issue list as above.
2. **Adversarial critic** — a second pass, using the `critic_model_id` pinned in the active model-policy matrix, is given the playbook, the standard-form diff, the anchored clause text, the counterparty document the primary pass read, and the primary reviewer's output (issue #618 reversed the original decision to withhold the raw document from the critic — see [Per-pass prompt manifest](#per-pass-prompt-manifest)), and is tasked with finding what the primary pass got wrong: missed issues, over-flagging, weak rationales, replacement text that drifts from the playbook position.

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

**Why we don't fine-tune.** See [docs/design-notes.md](docs/design-notes.md). In brief: fine-tuning isn't available for Opus, and 50 examples is too few to overcome a pre-trained model's prior. A codified playbook alone already gives us instant updates without retraining. The design intent, if retrieval is ever wired in and activated, is that RAG would additionally preserve the audit trail that fine-tuning would lose and add per-decision precedent citations — retrieval is dormant by decision today, so no retrieval-based audit trail or citation reaches the model or the output yet (see [Retrieval status](#retrieval-status-dormant-by-decision) and [docs/rag-dormant.md](docs/rag-dormant.md)).

### Standard-form comparison

> **Retired 2026-09-02 (issue #631).** The deterministic standard-form diff described in this section no longer runs. The 2026-07-22 LLM-native decision (D3) retired it from issue generation, and issue #631 deleted the code that implemented it (`scripts/diff_standard_form.py`, `scripts/build_anchor_map.py`, `scripts/redline_patch.py`, the anchor CI gates, and the knowledge-vs-precision router). Issue spotting is now LLM-native over the document itself, and an edit is addressed by **block transcript** rather than by an anchor/hash join — see [How an edit is addressed: block transcripts](#how-an-edit-is-addressed-block-transcripts-issues-619628-rewritten-2026-08-31) for what ships. The committed artifacts this section describes (`standard-forms/*.docx`, `standard-forms/*.anchor-map.json`, and the `anchor_map_hash` / `standard_form_hash` release-bundle fields) **remain in place as governed history**: bundle validation still requires them and existing bundles must stay valid. What follows is the record of the retired design, kept because the release-bundle fields and the artifacts are still live.

Semantic precedent retrieval is not enough on its own to know *how the counterparty changed our paper*. Issue-spotting starts from a **deterministic diff against your canonical standard form**, not from the uploaded document alone.

- **Canonical standard form per playbook version.** Each playbook version stores the canonical standard-form `.docx` it corresponds to (content-addressed, alongside the playbook snapshot — see [Playbook versioning](#data-flow--a-single-review) and [docs/evaluation.md](docs/evaluation.md)). The standard form is versioned in lockstep with the playbook so a review always diffs against the form that was current when the review ran.
- **Deterministic diff.** Inside the pipeline, the (normalized) uploaded draft is diffed against the canonical standard form for the active playbook version using a deterministic, paragraph/table-cell-anchored diff (not a model call). The diff yields the exact insertions, deletions, and modifications the counterparty made, anchored to specific clauses.
- **The model sees the diff, not just the upload.** We feed the model the **diff plus the anchored clause text** (standard text, counterparty text, and the delta between them), so it reasons about *what changed and whether the change is acceptable against the playbook*, rather than re-deriving the deviations from a raw document. Retrieved precedent (below), if retrieval is ever wired in and activated, would supplement this diff, not replace it — retrieval is dormant by decision today, so no precedent supplements it — see [Retrieval status](#retrieval-status-dormant-by-decision).

The same anchors produced here drove exact-match redline patching, which issue #380 retired and issue #631 deleted (see [Redlining](#redlining--owned-docx-library)).

**Diff generator implementation (issue #64; deleted 2026-09-02 by issue #631).** `scripts/diff_standard_form.py` implemented the deterministic diff: it loads the canonical standard-form paragraphs (`load_standard_form_paragraphs()` — synthetic mode derives per-anchor text from each covering topic's `our_standard` field until the real `.docx` is committed to `standard-forms/`; real-`.docx` mode extracts paragraph text directly, same optional `python-docx` convention as `scripts/build_anchor_map.py`) and diffs them against the uploaded draft's normalized paragraphs (`diff_draft_against_standard()`), matching by heading text against the anchor map. Every hunk carries `anchor`, `kind` (`unchanged` | `modified_new` | `deleted` | `inserted`), `text`, and — for hunks that touch existing standard-form text — a `source_text_hash` (SHA-256 of the standard-side text) that the redline-patching path (issue #17) validates on an exact-match, fail-closed basis before applying any edit. A draft paragraph whose heading matches no standard-form section is anchored to the reserved pseudo-anchor `sec-_new` (never `deleted`/`unchanged`; see below). `serialize_diff()` / `diff_hash()` give a stable, sorted-key JSON serialization and SHA-256 so the same inputs always produce the same diff — the CI gate for this is `tests/diff/test_deterministic_diff.py` (`.github/workflows/standard-form-diff-gate.yml`), which exercises a verbatim draft (must diff to all-`unchanged`), a sec-8 modification (must produce a `deleted`/`modified_new` hunk with a `source_text_hash`), and a wholly new inserted section (must anchor to `sec-_new`). The model prompt content that consumes this diff is Phase 2 (out of scope for issue #64).

#### Section-anchor map (deterministic detector scoping)

The canonical standard-form `.docx` for a playbook version is parsed once, at bundle-build time, into a **section-anchor map**: each heading / clause / table cell gets a stable `section_anchor` (`sec-8`, `sec-1.2`, `sec-10-precedence`, with **sub-clause anchors** under §10 — `sec-10-notices`, `sec-10-non-exclusive`, `sec-10-merger`, `sec-10-precedence` — so the four §10 topics don't collide). Each topic in the playbook lists its `section_anchors[]` (the machine key; `section_ref` is display-only). The deterministic diff tags **every hunk** with the `section_anchor` of the standard section it falls under, so a hard-rejection rule scoped to a topic reads **only** the diff hunks whose anchor is in that topic's `section_anchors`. This is what makes `applies_to_topics` deterministically enforceable.

**Content-addressed anchor map.** The anchor-map builder (`scripts/build_anchor_map.py`, deleted 2026-09-02 by issue #631 — the artifacts it wrote are committed and still read) produced a versioned, hashed artifact — `standard-forms/eiaa-v<version>.anchor-map.json` — that contains each anchor's heading text and `heading_hash` (SHA-256 of the heading text). The artifact carries an `anchor_map_hash` field (SHA-256 of the canonical anchor JSON) that is part of the **release bundle**: `standard_form_hash`, `anchor_map_hash`, `prompt_hash`, `model_policy_hash`, `corpus_snapshot_version`, `eval_run_id`, and signed `legal_approval`. The `anchor_map_hash` field is required in the release bundle — a bundle cannot be activated without it. Every production review records `anchor_map_hash` so anchor-map lineage is auditable.

**Heading-hash drift gate (retired 2026-09-02, issue #631 — the gate and its test are deleted; the rule below is the record of the retired design).** When the standard form is revised, heading text changes (e.g., renumbering §8 to §9) would silently produce wrong section scoping without a governance check. CI (`tests/anchor/test_heading_hash_drift.py`) diffed the current anchor map against each known form revision: if any anchor's `heading_hash` has changed and no `anchor_migrations` record in the playbook covers it, the gate **fails with DRIFT WITHOUT MIGRATION**. This is the normative rule: an anchor whose heading hash changes without a covering migration record fails the drift gate. Authors must add an `anchor_migrations` entry in the playbook (GC-approved) before CI can pass. See RUNBOOK.md "Revising the standard form" for the procedure.

**Form-coverage gate (retired 2026-09-02, issue #631 — the gate and its test are deleted; the rule below is the record of the retired design).** CI (`tests/anchor/test_form_coverage.py`) verified every anchor in the map has exactly one covering playbook topic (or an explicit, reviewed `coverage_exempt_anchors` entry). `coverage_exempt_anchors` and its `coverage_exempt_rationales` are **canonical in the anchor map** (`standard-forms/eiaa-v<version>.anchor-map.json`), carried as siblings of `anchors` — not in the playbook. An exemption is a reviewed property of the standard-form section it describes (the section carries no reviewable legal clause, e.g. preamble, signature block, or a structural parent heading), so it is governed alongside the form it describes. The gate reads exemptions from the anchor map only; the playbook never carries this list. This gate prevents silent coverage regressions when the standard form gains a new section.

**Reserved pseudo-anchor `sec-_new` (new inserted sections).** Topics whose position is on a clause **not in the standard form** (indemnification, insurance, governing-law) set `not_in_standard: true` and carry `section_anchors: ["sec-_new"]`. `sec-_new` is a reserved pseudo-anchor: the diff tagger assigns it to any inserted hunk that **does not fall inside any existing standard-form section** — a wholly new article or clause the counterparty has added. This gives `on_insert` rules scoped to `not_in_standard` topics a well-defined, non-empty hunk scope, so they can fire on the highest-risk insertions (standalone indemnification articles, excess insurance sections, counterparty-home arbitration clauses). `sec-_new` is assigned only to inserted/modified-new hunks; it is never assigned to deleted or unmodified hunks. Without this pseudo-anchor, `on_insert` rules for `not_in_standard` topics would have an empty effective scope and could never fire — they would be dead config. `on_remove_or_alter` rules must not reference `sec-_new` (you cannot remove a clause the standard form never contained). CI verifies every `section_anchor` other than `sec-_new` resolves to a real section of the bundled standard form; `sec-_new` is exempt from that resolution check. CI also verifies that every `not_in_standard: true` topic carries exactly `["sec-_new"]` and no other anchors, and that every present standard section maps to exactly one topic, and that every `on_remove_or_alter` rule's `required_tokens` are actually present in its anchored section (a protective rule guarding an absent token is dead config and fails the build).

### Retrieval — Amazon Bedrock Knowledge Bases (S3 Vectors)

**Design status: not implemented; dormant by decision — see [Retrieval status](#retrieval-status-dormant-by-decision) and [docs/rag-dormant.md](docs/rag-dormant.md).** Everything in this section, through the end of [Retrieval](#retrieval--amazon-bedrock-knowledge-bases-s3-vectors) (the section ends just above [Storage](#storage)), describes the retrieval subsystem as originally designed and partly built (issue #60): the Bedrock Knowledge Base and S3 Vectors store exist in infrastructure, but nothing has ever been ingested into them, no retrieval stage runs in the Step Functions pipeline, and no precedent text reaches the model. Read every present-tense sentence below (`queries the Knowledge Base`, `the pipeline refuses to run`, `every retrieval attempt is ledgered`, etc.) as **the design's intended behavior if retrieval is wired in and activated**, not as current behavior. [Storage](#storage) below is live, implemented storage — this disclaimer does not extend to it; retrieval-dependent fields within it are annotated individually. The IAM least-privilege statements later in this section — reachability only via IAM, never public, and the reconciled `pipelineReviewRole` / `corpusKnowledgeBaseRole` `bedrock:InvokeModel` scoping (see "Reconciled least-privilege invariant" below) — are likewise exempt from this disclaimer: they are live, currently-enforced invariants over the CDK tree as it exists today (asserted by `tests/test_infra_bedrock_kb.py`, Checks G and H), independent of whether retrieval ever runs. This is kept as design rationale rather than deleted so the [revival playbook](docs/rag-dormant.md) has a specification to build against.

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

**By design** (not today — there is no retrieval stage in the pipeline; see the banner above), at review time the backend would segment the uploaded document, run the lexical layer, and query the Knowledge Base for the top-K most analogous **positive** clauses from the active snapshot (plus, separately, any hard-labeled negative examples), passed to the LLM as precedent context. The model may record specific precedents internally, but external footnotes cite the contract position only.

**Retrieval failure semantics (by design, not implemented).** If the KB query itself errored after its bounded retry (network failure, Bedrock KB unavailability, throttle exhaustion), the pipeline step would terminate to `ERROR` — the review would not proceed without precedent context, because proceeding silently would change legal-output quality without any record. Every retrieval attempt would be ledgered.

**Empty-retrieval semantics and degraded mode (by design, not implemented).** If the KB query succeeded but returned an empty result set (no clauses above the similarity threshold, or an empty active corpus snapshot), the pipeline would proceed only if an explicit **degraded mode** had been documented, with the review row recording `degraded_mode=true` (flagged in the `reviews` table alongside the corpus snapshot version). A degraded-mode review would proceed on the deterministic diff and playbook alone, without precedent context, and the attorney would be informed via the UI that no precedent was available for this review. An empty result from a corpus that should have content is distinct from an empty corpus (no documents ingested yet); the pipeline would record the actual count of retrieved clauses so both cases are distinguishable in audit. If the corpus snapshot were not active or an ingestion were in progress, the pipeline would refuse to run (see ingestion interlock above), so an empty result at query time would mean the active snapshot genuinely contained no matching precedent.

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

**How a sweep targets a review's objects (and the backlog the fix released).** The purge worker derives the objects to delete from the **review row's own recorded pointers** — `upload_s3_key` (the input document, recorded at submission) and `output_s3_key` — falling back to a scan of the review-scoped prefixes (`uploads/{owner_sub}/{review_id}/`, `outputs/{review_id}/`) for rows written before those fields existed. Deriving from the stored pointer rather than reconstructing a prefix is deliberate: a layout change now moves the writer and the purger together.

Until issue #454 it did not. Every sweep listed `uploads/{review_id}/` — a prefix that omits the owner segment the API actually writes under — so `list_objects_v2` matched nothing, **no input document was ever deleted, and the sweep still recorded the review as purged**. Two consequences were fixed together with the prefix:

- **Success is now tied to the outcome.** After issuing its deletes, a sweep re-checks each targeted key; a review with a surviving object is reported in `failed_reviews`, is *not* added to `deleted_reviews`, and keeps its substance fields for the next sweep to retry. Previously the success record was independent of whether anything was deleted, which is why the defect was silent.
- **The legal-hold tagger uses the same targeting**, so a held *input* document now receives the `contract-toaster:legal-hold` tag the bucket-policy DENY backstop keys on — previously that backstop covered outputs only (application-level hold checks were always correct).

**Operator note — the first sweep after #454 deletes a backlog.** Every input document uploaded before that fix survived past its retention window. Deleting them is the correct and legally intended behaviour, but the *volume* is the surprise and it is irreversible. Two things make the scale visible before it becomes history:

1. **Report-only dry run.** Invoke the purge worker with `{"dry_run": true}` (or call `retention.run_purge_sweep_now(..., dry_run=True)` on the Compose deployment). It resolves and itemises exactly what a real sweep would delete — `eligible_reviews`, `objects_by_review`, `object_count` — and deletes nothing, clears no substance field, and returns an empty `deleted_reviews`.
2. **Itemised logging on every sweep.** Each eligible review is logged by id with its object count (WARNING) and per-object key (INFO) *before* any delete, so a real run is auditable in CloudWatch after the fact as well.

No deploy-time, migration-time or automatic mass-delete was added: the worker's existing schedule remains the only thing that deletes, and the dry run is an explicit operator invocation.

Object lock on `corpus` and `audit-archive` uses **Governance** mode (not Compliance): retention is enforced, but a holder of the explicit `s3:BypassGovernanceRetention` permission can override in a genuine emergency, which avoids the irreversible foot-gun of Compliance mode while still preventing routine deletion. Legal hold adds a stronger storage-level interlock: held corpus/review objects carry an S3 Object Lock legal hold or protected hold tag, and bucket policies deny deletion of held objects even to normal application roles. Governance bypass is reserved for MFA break-glass with session reason/ticket tags and alarms.

**DynamoDB tables:**

| Table              | PK            | SK         | Notes                                       |
|--------------------|---------------|------------|---------------------------------------------|
| `users`            | `cognito_sub` | —          | `email`, `is_admin`, `status` (`active`/`suspended`/`deprovisioned`), `last_auth_at`, `created_at` |
| `admin_bootstrap`  | `email`       | —          | First-admin seed only; keyed by email; consumed by a one-time reconciliation transaction on first sign-in (kept out of the `sub`-keyed `users` table) |
| `playbooks`        | `playbook_id` | —          | `current_version`, `agreement_type`, `active_release_bundle_hash` |
| `playbook_versions`| `playbook_id` | `version`  | **`status`** (`draft`/`active`/`retired`) — **SOLE lifecycle authority** (see [Canonicalization and status authority](#canonicalization-and-status-authority-issue-5) below; `playbook.status` in the JSON document is a snapshot label / projection, not the gate). `content_hash` (SHA-256 of the canonical playbook — excludes `playbook.status` and `playbook.release`; computed by `scripts/canonicalize.py`), `standard_form_hash`, `prompt_hash`, `model_policy_hash`, `output_contract_hash`, `corpus_snapshot_version`, `eval_run_id`, `json_blob`, `signed_release_metadata`, `legal_approval` (includes `legal_approval.content_hash` for Gate 7 assertion at activation: `content_hash == legal_approval.content_hash`), `uploaded_by` |
| `reviews`          | `review_id`   | —          | `owner_sub` (the uploader), `access_scope`, `idempotency_key`, `submission_status`, `execution_arn`, `playbook_id` (the playbook this review ran against; defaults to `eiaa`; required for multi-playbook routing, retrieval filtering, and rollback/quarantine scoping), `playbook_version`, `playbook_hash`, `prompt_version`, `prompt_hash`, `standard_form_hash`, `model_policy_hash`, `primary_model_id`, `critic_model_id`, `served_primary_model_id`, `served_critic_model_id` (what the provider reported it actually served, vs the two above which are what was requested — issue #514), `embedding_model_id` (retrieval-dependent; not populated while retrieval is dormant — see [Retrieval status](#retrieval-status-dormant-by-decision)), `model_region`, `corpus_snapshot_version` (retrieval-dependent; not populated while retrieval is dormant — see [Retrieval status](#retrieval-status-dormant-by-decision)), `legal_hold`, `legal_hold_reason`, `legal_hold_set_by`, `legal_hold_set_at`, `retention_window_at_creation`, `input_doc_hash`, `output_doc_hash`, `tokens_in`, `tokens_out`, `cost_usd`, `decision`, `confidence_state`, `verdict_summary`, `status`, `created_at`, `degraded_mode` (boolean, by design — true when retrieval returned an empty result and the review proceeded on diff+playbook only; retrieval-dependent, not populated while retrieval is dormant — see [Retrieval status](#retrieval-status-dormant-by-decision)), `retrieved_clause_count` (by design — count of precedent clauses actually retrieved; 0 when degraded_mode=true; retrieval-dependent, not populated while retrieval is dormant — see [Retrieval status](#retrieval-status-dormant-by-decision)), `fallback_used` (boolean — true only when a manually-activated fallback release bundle was in effect; automatic failover is prohibited). **The canonical field dictionary (name, substance flag, classification, retention) is owned by [docs/data-handling.md](docs/data-handling.md); this list must match it.** GSIs: `owner_sub-index` (`owner_sub` / `created_at`, the History tab's own-reviews page), `playbook_hash-index` (`playbook_hash` / `created_at`, keys only — rollback/quarantine population), `status-index` (`status` / `created_at`, ALL — the admin listing, pipeline health, retention preview/sweep/holds and legal triage read one status partition at a time; **no live path scans this table**, gated by `tests/test_reviews_no_scan.py`). |
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

Producing `.docx` files with proper tracked changes requires OOXML manipulation. `scripts/redline_block_apply.py` is a small, dependency-free writer built entirely on the standard library (`zipfile` + `xml.etree.ElementTree`): a `.docx` is a ZIP of XML parts, so the writer opens the **uploaded** package, rewrites `word/document.xml` in place, and emits `<w:ins>` / `<w:del>` revision elements (each carrying `w:id`, `w:author`, `w:date`) around the edited span of each proven block, plus (issue #83) a footnoted rationale per issue (`word/footnotes.xml`) — which rationale, or whether any, is the review's notes mode (issue #522: `none` emits no footnotes part at all, `internal`/`both` render the internal note behind a leading `[INTERNAL NOTE: …]` marking; the audience rule itself is `scripts/footnote_audience.py`; see [docs/output-contract.md → Which rationale becomes a footnote](docs/output-contract.md#which-rationale-becomes-a-footnote-the-reviews-notes-mode-issue-522-epic-519-item-d)). The non-`document.xml` parts a redline attaches — `word/footnotes.xml`, `word/header1.xml`, `word/footer1.xml`, and the footnote styles injected into `word/styles.xml` — are built by `scripts/docx_parts.py`, and the root-namespace-preservation dance every writer shares lives in `scripts/ooxml_util.py`. There is **no** vendored `anthropics/skills` `docx` fork and **no** `backend/vendor/` directory — an earlier draft of this document claimed one existed; it did not, and this section is corrected to describe the writer that actually ships.

`scripts/redline_generate.py` (issue #83) is the end-to-end orchestrator: it takes a reconciled review result (issue #82), runs the leakage scan gate (issues #26/#73) over the full result before generation, compiles each edit through the block compiler above, injects the export marker and footnotes (`inject_export_marker_and_footnotes`, when the review's notes mode put internal content in scope — see [Export / misuse marker](#export--misuse-marker) below), and runs the output-side OOXML scan (see [docs/threat-model.md](docs/threat-model.md) → "Generated redline output hygiene") before ever returning the document bytes.

> **Retired 2026-09-02 (issue #631).** A second, *standalone* writer (`scripts/redline_docx_writer.py`) used to build a whole synthetic `word/document.xml` from scratch for the retired anchor/hash patch path, and additionally placed the export marker as a first-page cover note. Its last callers were a mock-fixture generator and third-party paper (moved onto the block compiler by issue #629), so issue #631 deleted it along with `scripts/redline_inplace.py`, the in-place patcher it paired with. Its still-needed pieces moved rather than vanished: the OOXML parts to `scripts/docx_parts.py`, the notes-mode audience rule to `scripts/footnote_audience.py`, the export-marker text to `redline_generate.py` next to the seam that decides whether a document carries one.

See [docs/design-notes.md](docs/design-notes.md) and [docs/threat-model.md](docs/threat-model.md) for the supply-chain controls (SBOM, provenance, redline fixture tests) that apply to any future third-party OOXML dependency, `tests/test_redline_block_apply.py` for the writer's own `<w:ins>`/`<w:del>` correctness tests, and `tests/redline/test_redline_generation_83.py` for the end-to-end generation gate.

Pure Python. No LibreOffice, no Word, no commercial library. Runs inside the review pipeline.

#### How an edit is addressed: block transcripts (issues #619–#628, rewritten 2026-08-31)

An edit has to name **where** in the document it applies. This section is the record of what that mechanism is today and what it replaced, because the answer changed twice and the failure mode of each old answer is why the current one is shaped the way it is.

- **Anchors + hashes (retired, issue #380).** Each issue joined to a hunk of the standard-form diff and carried a hash of the text it expected to find. A counterparty draft reformatted from scratch diffed as "no matching anchor" almost everywhere, so those reviews fail-closed with no document at all — and the whole approach died with the deterministic detector engine that fed it.
- **Verbatim quotes (retired, issues #375–#379, deleted by #628).** The model reproduced a document-wide-**unique** `source_quote` per issue and a locator found it. That is a hard thing to ask of a model and a brittle thing to verify: a quote appearing twice was `ambiguous`, a quote whose punctuation drifted was `not_found`, a quote crossing a physical `<w:p>` join was `spans_paragraph_break` — and every one of those silently cost the issue its tracked change. Measured on the real corpus, curly punctuation alone affected 15 of 16 normalizable documents.
- **Block transcripts (Candidate E — current, issues #619/#620/#626/#627).** The extractor stamps a `block_id` on every logical paragraph (`extraction_normalization_stage.build_block_map`), and the rendered document text the model reads carries that id at the head of each paragraph. The model **names the id** and **transcribes that paragraph** as an ordered list of `keep` / `delete` / `insert` segments. `scripts/block_transcript.py` proves the `keep`+`delete` half back onto the block's real text, character for character (tolerating only encoding-level noise, via `scripts/text_fold.py`'s comparison-only fold), and returns true offsets into the document's own characters. `scripts/redline_block_apply.py` compiles those offsets into `<w:ins>`/`<w:del>` markup, fail-closed per edit, with all of one issue's edits rolled back together if any of them is refused. There is no uniqueness burden and no search, so there is no ambiguity to resolve by guessing; a transcript that does not prove is a `source_mismatch` the primary pass retries with the divergence in hand rather than a redline silently going missing.

`scripts/review_spine.py::run_review` routes stage 5 on whether the reconciled result carries a transcript: an edit-bearing review goes to `redline_generate.generate_redline_from_blocks`; a result with no transcript at all (an ACCEPT, or a REQUEST_CHANGE whose issues are every one of them flag-only) goes to `redline_generate.generate_redline`, which produces the labelled analysis report and no document.

#### Input normalization (before review)

A counterparty `.docx` can carry pre-existing **tracked changes, comments, hidden text, fields, footnotes, and embedded objects** that would otherwise corrupt both the diff and the redline. Before any review work, the document passes a **normalization pass** that applies a **documented accept/reject rule** to existing revisions and produces a clean canonical body for extraction and diffing. A **pending** (`w:ins`/`w:del`) counterparty revision — the flagship counterparty-markup scenario, and what a redline *is* — is the proposal under review: it is **accepted-all** into the operative draft, with the disposition recorded in a normalization note (never silent); the downstream standard-form diff then recovers what changed. This holds regardless of how many pending clusters or revision authors a paragraph carries — every cluster's resulting text is the same whole-paragraph accept-all text, so there is nothing to silently compose between them; the note names the cluster/author counts when more than one is present. A pending revision inside a field code (a cross-reference, auto-numbering, or date field under live counterparty edit) accepts-all the same way (issue #530): its resulting text is already the field's own resolved display text, and the note names what the field now resolves to. Fail-closed is reserved for the one structure that remains genuinely ambiguous — a malformed revision record with no resulting text, where there is genuinely no text to read — plus structurally corrupt input. Separately from the revision rule, the same pass **screens the extracted text for zero-width and bidirectional control characters** — invisible or order-reversing Unicode a counterparty can plant so that what the model reads is not what the attorney sees. A hit fails the document closed the same way and is **never stripped or repaired**, since a silently sanitized document is one the model and the attorney no longer agree on; the refusal note is counts-only. See [docs/threat-model.md → Prompt-injection resistance for uploaded documents](docs/threat-model.md#prompt-injection-resistance-for-uploaded-documents). Comments never gate normalization by themselves, even alongside a pending revision that is itself accepted-all. Hidden text and field results are stripped/resolved to their literal text. Accepting a paragraph's text this way is only half the story: the same disposition is also **materialized directly into the uploaded `.docx`'s bytes** (every `w:del` removed, every `w:ins` unwrapped, and — since issue #685 — the counterparty's pending **formatting** revisions accepted the same way: each `*PrChange` record dropped so the properties now in the document stand, and a tracked move applied by removing its `w:moveFrom` and unwrapping its `w:moveTo`, with the kinds and counts recorded in the normalization note and any revision kind the transform has no rule for reported there too rather than silently passed through) before block-addressing, edit compilation, and the delivered redline run, so the block map, the compiler, the delivered redline — and the model's own read of the document — all operate on one canonical, already-accepted document; the redline is delivered *on* that accepted document, and the attorney keeps their own original to diff against. **Known limitation (issue #563 follow-up):** this materialization does not yet apply the semantics of an accepted **deleted paragraph mark** (`<w:pPr><w:rPr><w:del/></w:rPr></w:pPr>`, Word's record of a counterparty proposing to merge a paragraph with the next one) — the revision markers are stripped, but the two `<w:p>` elements are not merged, so the materialized bytes still show the paragraph split the counterparty proposed to remove even though `normalize_paragraphs`'s TEXT-space reading already folds them into one logical paragraph; `tests/test_accept_all_materializer.py` pins this as the current, documented behavior. If a document cannot be normalized to a clean, unambiguous body, the review **fails closed** to an internal analysis report rather than guessing: the pipeline sets `status = MANUAL_REVIEW_REQUIRED` with `reason = unnormalizable_input` and stores the analysis report in the `outputs` bucket (accessible to owner-or-admin only) instead of a redline `.docx`. The report describes the normalization problem and any intended changes so the attorney can apply them by hand. See [docs/output-contract.md → Fail-closed internal analysis report](docs/output-contract.md#fail-closed-internal-analysis-report) for the format, delivery surface, reviewer-facing copy, and retention classification, and `scripts/normalize_input.py` for the full documented accept/reject rule and `scripts/extraction_normalization_stage.py::materialize_accept_all` for the byte-level transform. (The hostile-file model — zip-bomb, XML-entity, external-relationship, macro-template, MIME/magic-number, AV scan — is owned by [docs/threat-model.md](docs/threat-model.md).)

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

A generated redline carries an **internal-notes export marker** ("contains internal notes — not for external transmission") **iff** that review's notes mode actually put internal-audience content in scope (`internal`/`both` — `backend/src/reviews.py::NOTES_MODES`; today unreachable in production while issue #572's `NOTES_MODE_ENABLED` kill switch is off). Issue #513 retired the marker's earlier attorney-approval framing and its unconditional presence — it carries no approval semantics, does not gate, sign, or record anything, and a review with no internal notes produces a document with **no marker in any part**.

Placement is the same on every path since issue #631: `scripts/redline_generate.py::inject_export_marker_and_footnotes` places the marker in a running every-page header/footer, for first-party and third-party paper alike (issue #629 moved third-party onto the same block compiler). The first-page cover-note placement belonged to the standalone writer that issue #631 deleted, and is gone with it. There is deliberately no manual "de-marking" procedure — stripping the marker text would not remove the internal-audience content the notes mode put in the document. The threat framing is in [docs/threat-model.md → External-communication guardrail](docs/threat-model.md#external-communication-guardrail).

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

**Scope of that invariant.** It governs the **AWS production path described above** — the signed, digest-pinned App Runner deployment. The separate self-hosted Docker Compose deployment (`deploy/dts/`, images published to GHCR by `.github/workflows/dts-image-publish.yml`) does **not** share it: since 2026-09-08, by owner decision, that target publishes and rolls forward automatically once the `CI pipeline` workflow succeeds on `main`. The two targets have deliberately different rules, and the automatic one carries a known limit worth restating — the gate cannot see stylesheets, because vitest runs jsdom with `css: false`, so a CSS-only regression can reach that deployment green. Loosening the DTS path is not licence to loosen this one.

**CI gates (all must pass before image push).** The CI pipeline (`ContractToasterStack-cicd` / `.github/workflows/ci-pipeline.yml`) runs five gates on every change before building or pushing an image:

1. **Full Python test suite** — every `tests/test_*.py`, `tests/*/test_*.py`, and `tests/lint-*.py` must exit 0.
2. **docs-lint gate** (`scripts/docs-lint.py`) — stale-term denylist, latency consistency, rule-count, field-dictionary, no literal AWS account IDs, and no placeholder phrases (issue #43; _reconciliation: the CI pipeline additionally runs docs-lint_).
3. **Detector-correctness gate** (`tests/detector/`) — empty-scope structural check (D1), planted-violation fires (D2), ReDoS guard (D3) (issues #1, #2; _reconciliation: the CI pipeline additionally runs detector-correctness gates_).
4. **Security/dependency scan** — pip-audit and Trivy container scan.
5. **Frontend suite gate** (`scripts/check-frontend.sh`) — TypeScript typecheck + production Vite build (`build:ci`), the vitest component suite, and the three CTDS design-system audits (contrast, focus/reduced-motion, layout). Added by issue #634: before it existed no gate ran the frontend suite, so a stale assertion sat red on `main` while CI reported green.

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
     cost priced from the caps (max input tokens + the fail-closed output ceiling,
     x both passes x every allowed attempt, at uncached pricing) exactly once per
     review_id. If reserving would exceed the daily cap, reject with a clear
     "daily limit reached" message. (Settlement reconciles it against the ledger --
     usually downward; see Cost shape for the one case where it settles higher.)
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
     the pipeline records the bundle hashes into the execution input and
     proceeds. (**Not implemented today:** recording a corpus snapshot
     version / physical store ID and the ingestion-in-progress refusal check
     are retrieval-dependent design (see [Retrieval status](#retrieval-status-dormant-by-decision))
     and do not run while retrieval is dormant.)
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
 13. Run the lexical hard-rejection detectors; segment by section. (**Not
     implemented today:** querying the Bedrock Knowledge Base for top-K
     precedents using a pinned store ID is retrieval-dependent design — see
     [Retrieval status](#retrieval-status-dormant-by-decision) — and does
     not run; `retrieved_precedent` is always `[]`.)
 14. Assemble the prompt: system (review guidance + binary overlay + playbook) +
     user (standard-form diff + anchored clauses + the untrusted, delimited
     counterparty doc text; the hard-labeled-precedents block is omitted
     because the precedent list is empty — see
     [Retrieval status](#retrieval-status-dormant-by-decision)). Enforce
     caps: document size, extracted tokens, sections, output tokens.
     **Oversized-document single failure point.** The cap check at this step is
     the single authoritative failure point for documents that exceed the
     configured limits.  If the assembled prompt would exceed `max_input_tokens`
     (default 100,000 per pass), the review terminates *before any model call* —
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
        target hash no longer matches), with the internal-notes export marker included
        iff this review's notes mode put internal-audience content in scope →
        s3://outputs/{review-id}/out.docx.
     If decision == ACCEPT:
        No output document. UI shows "no requested changes identified by tool".
 21. SETTLE actual spend against the reservation; update the `reviews` row with
     the result, token counts, cost, confidence state; status=DONE.
 22. Append a non-substantive entry to the append-only `audit` table (decision,
     topic ids, scanner rule ids, hashes, model ids/region, playbook/prompt/
     standard-form/model-policy hashes; opaque identifiers, not clause
     text; see [docs/data-handling.md](docs/data-handling.md) for classification).
     (**Not implemented today:** corpus snapshot version and **retrieved
     clause_ids with polarity and channel** are retrieval-dependent design
     and are not recorded while retrieval is dormant — see
     [Retrieval status](#retrieval-status-dormant-by-decision).)
     Store any substantive rationales or critic text in retention-governed
     confidential storage only.

Browser:
 23. UI polls GET /api/reviews/{id} (owner-or-admin) until status is terminal
     (DONE / ERROR / ERROR_MANUAL_REVIEW_REQUIRED / MANUAL_REVIEW_REQUIRED /
     QUARANTINED / SUPERSEDED),
     then renders the result (outcome headline, decision copy, and download
     affordance when an output exists — issue #492; the internal-notes
     export marker from step 20, when present, is baked into the `.docx`
     itself, not repeated as UI copy).
 24. Human-review outcome capture: the UI records whether the attorney accepted,
     edited, or rejected the tool output (feeds the feedback loop —
     see docs/evaluation.md). Approval itself still happens outside the tool.
```

Each pipeline step has its own timeout and retry policy, and **every model attempt is ledgered in a finally path** — including failed invocations, retries, malformed outputs, and aborted executions — so no spend escapes the ledger. A failed step transitions the execution (and the `reviews` row) to a terminal error state with the failing stage recorded, rather than leaving a review wedged in `PENDING`, and releases the concurrency slot and any unspent reservation. A submission that crashes *before* its execution starts (no `execution_arn`) is recovered by the **orphan reconciler** (see [Idempotency](#api-vs-async-worker)), not left for a human to notice.

**Execution-level timeout (state-machine-level).** Per-step timeouts are necessary but not sufficient: a pathological execution can get stuck in a non-retrying wait or drift into RUNNING indefinitely, leaking a concurrency slot and a spend reservation for days. The Step Functions state machine therefore carries an **overall execution-level timeout** (in addition to per-step timeouts) — set and asserted in the infra CDK definition — that automatically terminates any execution that has not reached a terminal state within the maximum plausible review duration. The execution-level timeout is the backstop that converts a runaway RUNNING execution into a TIMED_OUT terminal state, which the orphan reconciler then detects (via DescribeExecution) and resolves to ERROR with slot and reservation release (see the dead-execution path above).

**Semaphore lease / slot-leak recovery.** The concurrency-semaphore releases a slot on the handled-failure path (the Step Functions Catch/finally states). A hard-killed execution — process kill, Lambda OOM, Fargate SIGKILL, or a Step Functions execution terminated externally — never runs those states, leaking a slot permanently at this system's low concurrency cap. Two mechanisms prevent permanent leaks: (a) **Lease/TTL semantics** on semaphore entries — each slot entry carries an expiry (lease TTL) aligned to the execution-level timeout; a reaper or the next acquire reclaims any expired entry regardless of whether the release state ran; (b) **Slot reaper in the reconciler** — the orphan reconciler also reconciles held semaphore slots against live Step Functions executions: any slot whose associated execution is no longer in RUNNING status is reclaimed. The stale-`PENDING`/`RUNNING` alarm covers both the PENDING-with-dead-ARN case (a `PENDING` review whose ARN resolves to a FAILED/TIMED_OUT/ABORTED execution) and stale RUNNING reviews (a review in RUNNING status whose execution age exceeds the execution-level timeout), so either stuck state pages on-call before manual intervention is needed.

**Release-bundle rollback.** Active playbook/prompt/model/corpus/standard-form bundles are content-addressed and carry `draft`/`active`/`retired` statuses with signed release metadata. A bad bundle is rolled back with **one click** to the prior active bundle (recorded in the audit table), and reviews run under the bad bundle are automatically marked `QUARANTINED` for re-run. Re-runs create new review records; originals become `SUPERSEDED` once replaced. Governance and CI gating (schema validation, prompt regression tests, stochastic stability, redline fixture tests, legal-approval metadata before a bundle may go `active`; retrieval regression is design-only and does not run while retrieval is dormant — see [Retrieval status](#retrieval-status-dormant-by-decision)) are owned by [docs/evaluation.md](docs/evaluation.md).

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

Audit rows include before/after content hashes where applicable, and (for reviews) model ids/region, playbook hash, prompt hash, standard-form hash, model-policy hash, topic ids, decision, token counts, cost, scanner rule ids, and authorization result. Corpus snapshot version and **retrieved clause_ids with polarity and channel** (opaque identifiers — non-substantive; see [docs/data-handling.md](docs/data-handling.md)) are retrieval-dependent and are not recorded while retrieval is dormant — see [Retrieval status](#retrieval-status-dormant-by-decision). Audit rows **do not** include raw document text, model-written rationales, clause text, or substantive primary/critic deltas. Those fields are confidential document substance and live only in retention-governed storage.

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
- **Split, least-privilege API roles.** The API's capabilities are split rather than carried by one broad role: distinct **upload**, **review-start**, **read-status**, and **download** capabilities, each scoped to the specific S3 prefixes and KMS keys it needs (with **KMS encryption-context checks** so a key grant only decrypts objects written under the expected context). No role has broad read/write across all document buckets. The pipeline task role invokes only the pinned regional Bedrock model, and is scoped to query only the Knowledge Base it owns if retrieval is ever wired in and activated — retrieval is dormant by decision today, so no Knowledge Base query runs (see [Retrieval status](#retrieval-status-dormant-by-decision)); the retention purge worker can delete only in `uploads`/`outputs` and only for terminal, non-held reviews; the break-glass role is separate and normally unused. Object access is short-lived and scoped.
- No long-lived credentials in any container. App Runner and the pipeline tasks use their task roles; OAuth and other secrets are resolved at runtime (never at CDK synth — see [Infrastructure](#infrastructure--aws-cdk)).
- **Hosted-domain enforcement is defense-in-depth**, in two independent layers (Cognito edge + backend JWT validator), and is a **prerequisite to the application allowlist** — domain membership authenticates, the allowlist authorizes. See [Authentication](#authentication--cognito-federated-to-google). Deprovisioning (status check, token revocation, periodic Workspace sync) is enforced on every request.
- **Authorization / row-level security.** A review is readable and downloadable only by its `owner_sub` or an admin — never by any signed-in user. Every `reviews` row carries `owner_sub` and an `access_scope`, and access is decided by ownership + role on every `GET`.
- **Every non-health endpoint requires authorization.** `/health` is public but **minimal — liveness only** (`{"status":"ok"}`), with **no version, commit SHA, or image digest**, so an unauthenticated caller cannot fingerprint the exact build of a confidential legal tool. Build details move to an **allowlisted `/version`** endpoint. All routes other than `/health` require a valid Cognito token, `company.com` domain checks, allowlist membership, and `users.status == active` before route-specific owner/admin authorization runs.
- **Download authorization.** The redlined `.docx` is served either through an **authenticated streaming endpoint** or via a **very short-lived presigned URL generated only after the owner/admin check passes**, with `Cache-Control: no-store`. Every view, download, presign, and failed access attempt is audited. Review IDs are **high-entropy and non-enumerable** so an output URL cannot be guessed or walked. (Per-user request-size caps, concurrency limits, daily limits, WAF, and rate limits on upload/poll are owned by [docs/threat-model.md](docs/threat-model.md) and enforced in the API.)
- **Prompt-injection resistance.** The counterparty `.docx` is treated as **untrusted data**, not instructions, today. Retrieved corpus precedent is untrusted data **whenever it is present** — corpus documents could carry hostile instructions, hidden text, or copied injection language just as an uploaded draft can — but retrieval is dormant by decision, so no corpus precedent reaches the model and none is present (see [Retrieval status](#retrieval-status-dormant-by-decision)). All untrusted text is wrapped in explicit delimiters with a system instruction that nothing inside is a directive to the model. Model output is schema-validated and **leakage-scanned** before it can drive a redline. Cost and output-length **outliers are flagged** as a possible injection or runaway signal. The deep threat model is owned by [docs/threat-model.md](docs/threat-model.md).
- **Atomic daily spend ceiling.** A configurable per-day cost cap (**default $20/day**) is enforced by an **atomic reservation** — a conditional DynamoDB counter reserves estimated cost *before* the pipeline starts and settles actual cost afterward — so concurrent submissions cannot collectively bypass a bare pre-check. The cap and today's spend are surfaced in the admin UI alongside the cost ledger.
- **Tool-recommendation framing.** Nothing in this product enforces, requires, gates on, or records attorney approval — that happens outside this tool (issue #492 removed the UI's approval copy); ACCEPT reads "no requested changes identified by tool"; generated redlines carry an **internal-notes export marker** iff that review's notes mode put internal-audience content in scope (never unconditional, no approval semantics); and low-confidence outcomes route to `MANUAL_REVIEW_REQUIRED` as a system status, not a legal category. These are misuse-prevention framings (see [docs/threat-model.md](docs/threat-model.md)), distinct from any approval workflow.
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
| `max_input_tokens`     | 100,000 | Per pass (system + user prompt combined); raised from 80,000 by issue #625 when outline mode was deleted |
| `max_output_tokens`    | sized per review; 32,000 worst case | Per pass (structured JSON response). Issue #658 replaced the flat 8,000 with `clamp(16,000 + 2.5 × document_tokens, 16,000, the selected model's declared output cap)` — the v3 block-transcript contract makes the response roughly proportional to the reviewed text, and the flat figure killed a real five-page agreement on `model_output_truncated`. The reservation below prices the sizing function against the fail-closed 32,000 ceiling, since the document does not exist yet at submission time — which is also why the reservation is not an upper bound on settle for a model that declares more (see below) |
| `max_retries_per_pass` | 1       | One bounded structured-output retry per pass; a second failure → `ERROR_MANUAL_REVIEW_REQUIRED` |
| `max_truncation_retries_per_pass` | 1 | Issue #658: a retry allowance reserved for a TRUNCATED response and spendable by nothing else, so a schema rejection on attempt 1 cannot leave a truncation immediately terminal |

The **worst-case spend reservation** for a review is:

```
reservation = passes × (1 + max_retries_per_pass + max_truncation_retries_per_pass)
            × (max_input_tokens + max_output_tokens)
            × uncached_price_per_token
```

With the defaults above, worst-case cost is computed per-pass using the pinned model rates from the unit-economics table below — Opus primary (3 attempts × 100K in × $5.50/M + 3 attempts × 32K out × $27.50/M = **$4.29**) plus Sonnet critic (3 attempts × 100K in × $3.30/M + 3 attempts × 32K out × $16.50/M = **$2.57**) totals ≈ **$6.86 worst-case** per review. At the default `$20/day` ceiling that is **~2 worst-case reviews reserved at once** before the ceiling blocks a further start. (Before issue #658 raised the output budget from a flat 8,000 and gave truncation its own attempt, these figures were $1.54 / $0.92 / ≈$2.46 and ~8; before issue #625 raised `max_input_tokens` to 100,000 they were $1.32 / $0.79 / ≈$2.11. The reservation formula scales with the caps automatically.) A reservation is **released at settlement** — `settle_spend` writes back `actual − reserved`, so the day's counter converges on real ledgered spend and the reservation only gates *starts*, not the day's total: at the typical ~$0.79/review actual, a $20/day ceiling absorbs ~25 reviews in a day run one after another ($20 ÷ ~$0.79). Folding the whole retry budget in at reserve-time means no extra *attempt* can push a review past its reservation — but the reservation is **not** an upper bound on settle. It prices the fail-closed 32,000-token output ceiling, while the sizing function asks a model that declares more (128,000, for every Anthropic model in the shipped policy) for up to that cap on any document over ~44,800 estimated tokens: 3 attempts × 100K in × $5.50/M + 3 × 128K out × $27.50/M = **$12.21** for the Opus primary, plus **$7.33** for the Sonnet critic, ≈ **$19.54 actual worst case** against ≈ $6.86 reserved (2.85×, always under-reserving). Two concurrent submissions therefore reserve $13.72, are both admitted, and can settle at $39.08 against a $20/day ceiling before further starts are refused. That residual is deliberate and is stated in full under [Backend idempotency](#api-vs-async-worker) → “What that reservation is not”; the ledger records every attempt, so settlement is exact even when it corrects *upward*.

#### Transport retry budget (Bedrock)

The reservation formula above counts the retries the *pipeline* chooses to make (`max_retries_per_pass`, `max_truncation_retries_per_pass`). Underneath those sits a second, invisible retry layer: botocore's own transport retries, which re-send the same `InvokeModel` when the socket times out or the service throttles. Its defaults — 60 s read timeout, `legacy` mode, five attempts — were wrong for this workload: a single Opus primary pass producing a long redline transcript can take longer than 60 s to answer, botocore would raise `ReadTimeoutError` and silently re-send the identical request up to four more times, and every one of those sends is a billed invocation the ledger records as one attempt (audit finding F3, issue #51). `backend/src/config.py::botocore_config("bedrock-runtime")` therefore pins the Bedrock client to **`read_timeout=300`, `connect_timeout=10`, `retries={"mode": "standard", "max_attempts": 2}`** — a transport retry budget of **2 attempts after the first send** (three sends at most, `total_max_attempts=3` in botocore's own accounting), chosen so a genuinely transient failure still gets a second and third try but a slow generation is given the time to finish rather than being re-bought. The 300 s read window is sized above the longest observed single-response generation so the timeout fires on a stalled connection, not a working one. When that budget is exhausted, `LiveBedrockModelClient.invoke` maps the `ReadTimeoutError` to `ModelTimeoutError` (reason token `model_timeout`, issue #472) — the same token the OpenRouter adapter emits for an `httpx` timeout — so the pipeline's own bounded retry, not botocore's, decides whether to pay again. The DynamoDB client gets `retries={"mode": "adaptive", "max_attempts": 5}` with 15 s / 5 s timeouts (adaptive backoff for throttling bursts at cold start); every other service gets `standard` mode with three attempts. `tests/test_config_botocore_51.py` and `tests/test_bedrock_client_transport_51.py` pin these values.

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
provided by the **attorney's own review** — approval happens outside this tool, and the attorney's
review before sending anything to a counterparty is the final human-in-the-loop check, so a
paraphrased disclosure that evades the scanner is still visible before it leaves your organization.
(Issue #513 retired the always-on internal-only watermark this paragraph used to also cite here as
residual coverage — the export marker is now a notes-mode signpost, present only on a review
configured to include internal notes, not a general-purpose leak control; see
[docs/threat-model.md → External-communication guardrail](docs/threat-model.md#external-communication-guardrail).)
If a model-based second-layer scan is added in a future release, it will join the model-policy matrix
with its own eval run and cost reservation.

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

Using the caps above (1 primary + 1 critic pass, each up to 100K in / 32K out worst case, no caching, one general retry plus one truncation retry each):

| Metric                               | Value                                                       |
|--------------------------------------|-------------------------------------------------------------|
| **Worst-case $/review (reserved)**   | ~$6.86 (Opus primary: 3 attempts × 100K in × $5.50/M + 3 attempts × 32K out × $27.50/M = $4.29; Sonnet critic: 3 attempts × 100K in × $3.30/M + 3 attempts × 32K out × $16.50/M = $2.57) |
| **Worst-case $/review (actual, model declaring a 128K output cap)** | ~$19.54 (the same three attempts at 128K out: Opus $12.21 + Sonnet $7.33). The reservation under-reserves this by 2.85× by design — see the caps note above |
| **Typical $/review**                 | ~$0.65–$0.90 (average-length EIAA, 1 attempt each, no retries) |
| **Max reviews/day at $20 ceiling**   | ~2 reservations held at once (arithmetic: $20 ÷ $6.86), but reservations are released at settlement, so ~25 typical reviews run sequentially ($20 ÷ ~$0.79) |
| **Steady-state cache hit rate (v1 production)** | **≈ 0** — at 2–7 reviews/day spread across a workday, inter-review gaps far exceed the ~5-min cache TTL; caching saves primarily on back-to-back retries and eval runs, not typical spaced production usage |
| **Dev idle monthly**                 | ~$25–$50: App Runner always-on instance (~$25), VPC endpoints (~$7–10), KMS CMKs (~$3–5), CloudWatch dashboards/alarms (~$2–3), CloudTrail management events (~$1); Bedrock/S3 Vectors/Step Functions are $0 at zero reviews |
| **Prod monthly target (50–200 reviews/mo)** | ~$60–$180: Bedrock alone $30–150 (50–200 × ~$0.65–$0.75 typical), plus ~$50 fixed infrastructure; well inside ≤$100/mo dev target and compatible with the ≤$200/mo prod budget |

At the expected peak of 2–7 reviews/day, a $20/day ceiling still covers the day: 7 typical-cost reviews = ~$5.50/day, well within budget, because each reservation is reversed against real ledgered usage the moment it settles. What the larger post-#658 figures bind is *concurrency*: only ~2 reviews can be in flight against a $20 ceiling before a third submission is refused with 429 until one settles — and two concurrent ~80-page reviews can settle at ~$39 on a $20 ceiling, which the counter then reflects until the day rolls over. To prevent legitimate lockouts at sustained or concurrent volume — and before running large documents concurrently — raise the ceiling in the admin UI (see [RUNBOOK.md](RUNBOOK.md) → Adjusting the daily spend ceiling). A $50/day ceiling allows ~7 concurrent reservations, or ~63 typical reviews per day, while still bounding blast radius.

For an expected volume of 50–200 reviews per month:

- **Bedrock (Opus 4.8 primary + Sonnet 4.6 critic).** Dominant *marginal* cost, but $0 when idle (single-region native ID, Standard tier — no provisioned hourly charge). Opus is ~$5.50/M input, ~$27.50/M output (including the ~10% regional endpoint surcharge; direct API base rate is ~$5/M input, ~$25/M output); the Sonnet critic pass is ~$3.30/M input, ~$16.50/M output, so the second pass costs materially *less* than a second Opus pass would — a deliberate decorrelation-and-cost win. Prompt caching on the ≈30K-token playbook is a **within-model optimization only** — caches are per-model (Opus and Sonnet have separate caches), and at v1 production volume the steady-state inter-review hit rate is near zero (see the cost-shape table above). Caching delivers savings on back-to-back retries and eval runs; the cost model and the reservation are sized to survive a 0% cache-hit run. Note: the current Opus tokenizer produces noticeably more tokens than the prior generation for the same text, so real cost runs higher than naïve estimates — bounded by the **atomically-reserved $20/day ceiling** (an atomic reservation priced from the caps, including every retry attempt, and reconciled against the ledger at settlement — not an upper bound on settle; see [Backend idempotency](#api-vs-async-worker) and the caps table above), and **every model attempt (including failures and retries) is ledgered** so the spend record is complete.
- **App Runner (API).** The main idle cost. A minimal always-on instance is on the order of ~$25/month; we keep it small and let the pipeline (which is pay-per-use Lambda/Fargate) absorb the heavy work.
- **Bedrock Knowledge Base + S3 Vectors.** Pay-per-use storage and query, no idle OCU floor — projected near-$0 idle for the designed ~50-document corpus, if retrieval is ever wired in and activated. Today the store is empty (nothing has been ingested) and no queries run, because retrieval is dormant by decision (see [Retrieval status](#retrieval-status-dormant-by-decision)).
- **Step Functions, Lambda/Fargate pipeline.** Pay-per-execution; ~$0 when no reviews are running. (There is **no SQS** on the review entry path — the API starts Step Functions directly.)
- **S3, DynamoDB (on-demand), Cognito, Amplify, CloudWatch, CloudTrail (management events only).** Single-digit dollars per month combined at this scale.
- **Fixed infrastructure overhead.** VPC endpoints (~$7–10/mo), KMS customer-managed keys (~$3–5/mo), CloudWatch dashboards and alarms (~$2–3/mo), CloudTrail management events (~$1/mo). Combined these are ~$13–19/mo and are included in the dev idle figure above — the total idle cost is roughly double the App Runner instance alone.

**Guardrail.** An **AWS Budgets** monthly budget with alerts is provisioned in CDK so the ≤ $100/mo assurance is enforced by an alarm, not just an estimate. Separately, the in-app **$20/day** ceiling bounds Bedrock spend per day (default; admin-configurable). The UI displays per-review cost to all users and aggregate cost (plus the daily ceiling and today's spend) on the admin dashboard.

## Environments

**Prod runs in a separate AWS account from dev.** Because this tool handles legal-facing data, production is **account-isolated** from development — there is a clean trust and blast-radius boundary, and a dev mistake cannot reach production legal documents. v1 still avoids an elaborate multi-account org structure (no sprawling OU tree), but the prod/dev separation is non-negotiable; this resolves the earlier "no multi-account, but dev + prod" contradiction in favour of isolating prod.

**Account topology.** The two environments live in distinct AWS accounts; CDK context (`--context env=dev` / `--context env=prod`) selects the correct account at deploy time. Account IDs are recorded in `infra/cdk.json` (placeholder values replaced before first deploy) and in the environment table at the top of [RUNBOOK.md](RUNBOOK.md). The same CDK app is deployed to each account — there is no shared account for legal-facing data.

To hold cost down the **dev account** runs a **single shared dev environment** (not per-developer stacks). Day-to-day iteration happens **locally**, but **developer laptops never reach production legal documents**: local dev uses a **synthetic corpus and synthetic documents only**, and if retrieval is ever wired in and activated, the design intent is for it to use a **local stub** (or a tiny local vector index) so iterating on review logic incurs no cloud vector cost and creates no path from a laptop to real legal data (the data-leak controls are owned by [docs/threat-model.md](docs/threat-model.md)) — retrieval is dormant by decision today, so no retrieval stub runs and no shared *dev* Knowledge Base is queried (see [Retrieval status](#retrieval-status-dormant-by-decision)). This keeps the idle cloud footprint near the ~$25/mo idle goal while still giving a realistic place to test end-to-end. The AWS Budgets alarm guards each account against surprises.

**Dev-to-prod guardrail.** The account boundary is the primary enforcement mechanism: developer SSO does not grant prod credentials, and dev never holds prod keys. There is no cross-account IAM trust between the dev account and the prod account, and no dev-scoped role carries permissions to read or write any prod S3 bucket, DynamoDB table, Knowledge Base, or KMS key. A developer with full `AdministratorAccess` in the dev account cannot reach production legal data. Do not copy, mirror, or point local tooling at prod buckets, the prod corpus, or the prod Knowledge Base; see [RUNBOOK.md → Local development](RUNBOOK.md#local-development) for the operator-facing rule and [docs/threat-model.md](docs/threat-model.md) for the threat framing.

## What we are explicitly not building

- We are not building a Word add-in. Your organization already has one; the redlined `.docx` opens in it directly.
- We are not building approval workflow. Approval happens outside this tool — but we **do** capture the attorney's accept/edit/reject outcome for the feedback loop (see [docs/evaluation.md](docs/evaluation.md)). Nothing in this product enforces, requires, gates on, or records attorney approval (issue #513).
- We are not fine-tuning a model. See [docs/design-notes.md](docs/design-notes.md).
- We are not building generic CLM. This tool reviews EIAAs against a single playbook. Other agreement types are out of scope for v1.
- We are not building an elaborate multi-account org structure. v1 keeps the account topology minimal — **but prod is still isolated in its own AWS account** from dev (see [Environments](#environments)); that isolation is in scope, the org sprawl is not.
- We are not auto-deploying from `main`. Production is promoted deliberately by signed-image digest through CI (see [Infrastructure](#infrastructure--aws-cdk)); a merge does not change prod.
