# Phase 0 issues

The issues below are ready to file as the initial backlog. Each is sized to a single PR.

Phase 0 deliberately carries **all security-bearing work** (JWT validation, two-layer
hosted-domain enforcement, owner-or-admin authorization, private network access to the
retrieval store, the daily spend ceiling) and the **async review pipeline skeleton**
(Step Functions, started **directly** by the API — there is no SQS buffer on the review
entry path) — these are foundation, not later add-ons.

Two architecture decisions taken after the original draft shape every issue below and are
called out where they bite:

- **The API starts Step Functions directly; SQS is removed from the review entry path.**
  Idempotency comes from deterministic execution names plus conditional DynamoDB writes,
  not from an SQS dedup window. There is no entry-path queue, consumer, or DLQ.
- **Production runs in a separate AWS account from dev.** v1 still avoids an elaborate
  multi-account org structure, but the legal-facing prod data is isolated from the dev
  account. Every AWS issue below should be read as environment-scoped: prove it in dev,
  then promote the same CDK shape into the separate prod account. See the dev-data-isolation
  issue below and ARCHITECTURE.md.

Suggested workflow:

1. Apply labels from `.github/labels.yml` via `gh label create` (or a labeler action) before filing issues.
2. Create the `phase:0-foundation` milestone in GitHub Issues.
3. File every issue below, each with:
   - Title: copied from the heading below
   - Body: copied from the body below (everything between `---begin---` and `---end---`)
   - Labels: as listed
   - Milestone: `phase:0-foundation`

Once we switch to Claude Code, this can be done in one batch with `gh issue create` calls.

---

## 1. Repo bootstrap

**Labels:** `phase:0-foundation`, `area:docs`, `type:chore`

---begin---

## Context

We need the basic repository scaffolding in place before any code is written. This issue covers the non-code files: documentation, GitHub templates, labels, ownership, and licensing. The artifacts drafted during the initial design phase are committed under this issue.

## Acceptance criteria

- [ ] `README.md`, `ARCHITECTURE.md`, `RUNBOOK.md`, and `LICENSE` are committed at the repo root.
- [ ] `.github/` contains issue templates, pull-request template, `CODEOWNERS`, and `labels.yml`.
- [ ] `.gitignore` covers Python, Node, CDK, IDE, AWS, and OS files.
- [ ] `playbooks/schema.json` and `playbooks/eiaa-v1.0.0.json` are committed.
- [ ] All labels in `.github/labels.yml` exist on the repo.
- [ ] All six milestones (`phase:0-foundation` through `phase:5-cutover`) exist with target dates.
- [ ] Branch protection on `main` requires PR + one approval + conversation resolution **and "Require review from Code Owners"** — so GC sign-off on `playbooks/`, `prompts/`, `model-policy/`, `standard-forms/`, and the governed `docs/` is *enforced*, not merely conventional. (A plain one-approval rule lets an engineer approve a playbook change without GC; "Require review from Code Owners" closes that.)
- [ ] The `legal-review-required` label is required to merge any PR touching legal-content paths (CODEOWNERS path or label-gate), pairing the machine gate with the PR-template checkbox.
- [ ] **Dependency noted:** CODEOWNERS must point at real team handles for the above to actually gate (it currently uses placeholders) — track replacing `@exos-legal/gc`/`@exos-legal/engineering` with real handles as a prerequisite to the control being live.

## Out of scope

- Any code (CDK, backend, frontend).
- Branch protection that requires status checks (we don't have CI yet).
- Custom labels beyond what's in `labels.yml`.
- The one-time replacement of placeholder CODEOWNERS slugs with real handles (tracked as the dependency above; do it once the org/teams exist).

## Notes

Files were drafted during the initial design phase. CODEOWNERS uses placeholder team slugs (`@exos-legal/gc`, `@exos-legal/engineering`); replace these with real handles once teams are created in the org.

---end---

---

## 2. CDK skeleton

**Labels:** `phase:0-foundation`, `area:infra`, `type:chore`

---begin---

## Context

Stand up the AWS CDK project that will hold all infrastructure-as-code. Same stack shape per environment, deployed separately into dev and prod accounts, split into composable nested stacks for clarity. No resources beyond the bare minimum needed to `cdk synth` cleanly.

## Acceptance criteria

- [ ] `infra/` directory created with `cdk init app --language typescript`.
- [ ] `cdk.json`, `package.json`, `tsconfig.json` committed.
- [ ] Stack structure: top-level `ContractToasterStack` with nested stacks for `network`, `data`, `auth`, `app`, `frontend`, `observability` (some will be empty in this issue).
- [ ] Account and region are environment-scoped context values (`dev` account, separate `prod` account, `us-east-1`). The implementation must not hard-code one account as both dev and prod.
- [ ] Customer-managed KMS key defined for the environment.
- [ ] Base IAM roles defined (deploy role, app runner task role — empty permission sets for now).
- [ ] `cdk synth` runs cleanly with no errors.
- [ ] README in `infra/` explaining the stack layout.

## Out of scope

- Any actual resources (S3, DynamoDB, Cognito, App Runner — all separate issues).
- Multi-region setup or an elaborate multi-account organization. The minimal dev/prod account separation is in scope and non-negotiable for v1.
- Custom domain wiring (Phase 5).

## Notes

- Use CDK v2 (`aws-cdk-lib`).
- Use `App.fromContext` for environment-specific values.
- Stack names should include the environment name (`contract-toaster-dev`, `contract-toaster-prod`).

---end---

---

## 3. S3 buckets

**Labels:** `phase:0-foundation`, `area:infra`, `type:chore`

---begin---

## Context

Create the four S3 buckets defined in ARCHITECTURE.md. Note that `uploads`/`outputs`
retention is **admin-configurable** (0–3yr, default 90), enforced by a purge worker (see
the "Spend ceiling + retention purge worker" issue), **not** by a fixed bucket lifecycle
rule. This issue creates the buckets; it does not build the purge worker.

## Acceptance criteria

- [ ] `contract-toaster-uploads-{env}` bucket: private, encrypted with the **uploads** CMK, block-all-public-access. **No** fixed lifecycle-delete rule (retention is handled by the purge worker).
- [ ] `contract-toaster-outputs-{env}` bucket: private, encrypted with the **outputs** CMK, block-all-public-access. **No** fixed lifecycle-delete rule.
- [ ] `contract-toaster-corpus-{env}` bucket: private, encrypted with the **corpus** CMK, block-all-public-access, versioning on, object lock in **governance** mode with a 7-year retention default.
- [ ] `contract-toaster-audit-archive-{env}` bucket: private, encrypted with the **audit** CMK, block-all-public-access, versioning on, object lock in **governance** mode, lifecycle transition to Glacier after 1 year.
- [ ] Each bucket uses the data-class CMK provisioned in the per-data-class KMS issue, **not** a single shared environment key. Key policies are scoped so the upload path cannot decrypt corpus or audit objects, and vice versa.
- [ ] Legal-hold enforcement exists at the storage layer: held uploads/outputs/corpus objects use S3 Object Lock legal holds where enabled, or protected hold tags plus bucket-policy deny rules where Object Lock is not the selected primitive. Normal app and purge roles cannot delete or overwrite a held object.
- [ ] Any governance-bypass path for held objects requires an MFA break-glass role, a reason/ticket tag, CloudTrail visibility, an application audit row, and an alarm.
- [ ] All buckets defined in `infra/lib/data-stack.ts`.
- [ ] `cdk diff` shows clean creation; `cdk deploy` succeeds in a dev environment.

## Out of scope

- The retention purge worker and the admin retention slider (separate issues).
- Replication to another region (revisit at Phase 5).
- Bucket inventory / S3 Storage Lens setup.

## Notes

Object lock can only be enabled at bucket-creation time. If we ever need to recreate a corpus or audit bucket, this is a non-trivial migration. We use **governance** (not compliance) mode so a tightly-controlled MFA break-glass holder of `s3:BypassGovernanceRetention` can override in a genuine emergency. Upload/output buckets may use Object Lock or protected legal-hold tags; either way, hold is enforced by bucket policy, not only by application code.

---end---

---

## 4. DynamoDB tables

**Labels:** `phase:0-foundation`, `area:infra`, `type:chore`

---begin---

## Context

Create the DynamoDB tables defined in ARCHITECTURE.md. This issue also bakes in the
audit-table immutability posture, the audit key shape suited to audit queries, and the
admin-bootstrap key design — all foundation, not later hardening. (Closes findings 14, 24,
25.)

## Acceptance criteria

- [ ] `users` table: PK `cognito_sub`. Attributes include `status` (`active`/`suspended`/`deprovisioned`) and `last_auth_at`. PITR enabled. Encrypted with the **DynamoDB** customer-managed KMS key (see the per-data-class KMS issue).
- [ ] `admin_bootstrap` table: PK `email`, used **only** for the first-admin seed. We do **not** seed an email-keyed row into the `cognito_sub`-keyed `users` table. The backend reconciles the bootstrap email to the real Cognito `sub` on first sign-in in a one-time transaction. (Alternative documented below: an email GSI on `users` plus the same reconciliation transaction — pick one and record the choice in ARCHITECTURE.md.)
- [ ] `playbooks` table: PK `playbook_id`, including the currently active `active_release_bundle_hash`. PITR enabled. KMS-encrypted.
- [ ] `playbook_versions` table: PK `playbook_id`, SK `version`. PITR enabled. KMS-encrypted. Content-addressed snapshots and release-bundle fields include playbook hash, prompt hash, canonical standard-form hash, model-policy hash, active corpus snapshot version, eval run ID, and legal approval.
- [ ] `reviews` table: PK `review_id`. Attributes include `owner_sub`, `access_scope`, canonical `ReviewStatus`, submission id/status, execution ARN/status, playbook hash, prompt hash, standard-form hash, model-policy hash, primary model id, critic model id, and active corpus snapshot version. GSI on `owner_sub` for "my reviews" queries and queryable indexes for rollback/quarantine by release-bundle/component hash. (The `owner_sub`/`access_scope` fields are the row-level-security foundation — owner-or-admin reads — and must exist from the start, see ARCHITECTURE.md.) PITR enabled. KMS-encrypted.
- [ ] `review_submissions` table or equivalent unique-index item: one row per idempotency key, recording `review_id`, upload pointer, active release-bundle hash, spend-reservation id, execution ARN/status, and timestamps so retries can safely "ensure execution started" without double-spending or double-running.
- [ ] `audit` table: **time-partitioned** PK (`YYYY-MM`, or `target_type#target_id` where queries are target-scoped) with a `timestamp` SK; GSIs for `actor` and for `review_id`. (Do **not** use `event_id` as the PK — it makes the timestamp SK useless for range queries.) PITR enabled. Encrypted with the dedicated **audit** customer-managed KMS key.
- [ ] **Audit immutability** is enforced at the IAM-policy level: every application role is **denied** `dynamodb:UpdateItem` and `dynamodb:DeleteItem` on the `audit` table; writes are append-only `PutItem` with a `attribute_not_exists` condition on the key. The table streams to the object-locked `audit-archive` S3 bucket. Denied/failed mutation attempts on the table raise a CloudWatch alarm. (The "audit rows are immutable" claim must be backed by a policy, not by convention.)
- [ ] **Audit substance whitelist:** audit rows contain non-substantive proof facts only — actor/action/target/time/outcome/status/hash/cost/reason codes. They must not store raw clause text, model rationales, summaries, critic deltas, prompt bodies, retrieved precedent text, or downloaded document contents.
- [ ] Tables defined in `infra/lib/data-stack.ts`.
- [ ] `cdk deploy` succeeds in dev.

## Out of scope

- Backup beyond PITR (DynamoDB on-demand backups are PITR-included for our retention window).
- Provisioned capacity. Use on-demand throughout v1.
- The deprovisioning sync job and token-revocation behavior themselves (built in the Cognito issue); this issue only adds the `status`/`last_auth_at` columns they read/write.

## Notes

- Set `removalPolicy: RETAIN` on production tables. Dev tables can be `DESTROY` to allow tear-down.
- The audit table now uses DynamoDB Streams from day one (it feeds the object-locked archive), so the Phase 4 "wire audit-to-S3" note is folded in here rather than deferred.

---end---

---

## 5. Cognito + Google IdP

**Labels:** `phase:0-foundation`, `area:infra`, `area:backend`, `type:feature`

---begin---

## Context

Stand up the Cognito user pool federated to Google as the only identity provider,
restricted to the `teamexos.com` hosted domain. Domain membership alone is **not**
authorization for a legal-document tool, so this issue also builds the application
allowlist/group check, the deprovisioning + Workspace-sync + token-revocation behavior,
and the email-keyed admin bootstrap. (Closes findings 14, 15, 16, 36.)

## Acceptance criteria

- [ ] Cognito user pool created with username = email.
- [ ] Google OAuth client credentials live in AWS Secrets Manager at `contract-toaster/cognito/google-oauth`. CDK **does not read the secret value at synth time** — it wires a **dynamic reference / runtime secret resolution** (e.g. `SecretValue.secretsManager` resolved by CloudFormation, or the IdP configured to fetch at runtime) so the plaintext never lands in synthesized templates, `cdk.out`, or CI logs. (Reading the secret into CDK code at synth is explicitly prohibited.)
- [ ] Google IdP attribute mapping: email, name, sub.
- [ ] User pool client configured with appropriate callback and logout URLs (Amplify URL + localhost for dev).
- [ ] Hosted UI enabled with a domain prefix.
- [ ] App client does not allow user-password sign-in. Google IdP only.
- [ ] Hosted-domain restriction is **defense-in-depth, enforced in BOTH layers** (not "pick one"): (a) the Google OAuth request pins `hd=teamexos.com` **and** a Cognito pre-sign-up / pre-token-generation Lambda rejects any non-`@teamexos.com` verified email; (b) the backend JWT validator independently re-verifies the `email` domain and the `hd` claim on every request. Backend enforcement is built in this issue's companion (App Runner) issue but the contract is documented here.
- [ ] **Authorization beyond the domain:** a verified `@teamexos.com` email is necessary but **not sufficient**. Access additionally requires membership in the application allowlist — the authoritative gate is the **DynamoDB `users` allowlist row** (`active` status). The pre-token-generation Lambda denies sign-in (and the backend independently denies requests) for an in-domain user who is not allowlisted.
- [ ] **Canonical admission path — one path, machine-assertable:** (a) a Google Workspace admin adds the user to `legal-admin@example.com`; (b) the user signs in; (c) the pre-token Lambda checks `legal-admin@example.com` group membership via the Directory API and **JIT-creates an active users row** in DynamoDB on first sign-in. This is the only non-bootstrap admission path. The **sync job only deprovisions** — it never auto-admits new members; a user in the group who has not yet signed in does not have a `users` row. A user in neither the group nor the table is denied at sign-in. An integration test asserts: (i) group-member first sign-in produces an `active` row; (ii) a user in neither group nor table is denied; (iii) a deprovisioned user (removed from group, sync has run) cannot sign in. The `users` row lifecycle (JIT-create by Lambda, deprovision by sync) must be stated identically in ARCHITECTURE.md and RUNBOOK.md.
- [ ] **Directory integration + fail-closed:** group/Workspace checks use a **Google Directory API service account** (domain-wide delegation, directory-read only) whose credentials live in Secrets Manager and are rotated. Both the edge Lambda and the backend **fail closed** when the Directory API is unreachable (deny; backend falls back to the DynamoDB allowlist row as authoritative) — never fail open. A "directory unavailable → deny" test exists at both layers, and the sync makes no changes on an API outage (no mass-deprovision, no auto-admit).
- [ ] **Deprovisioning — pinned numbers (machine-checkable):** the `users` table carries a `status` (`active`/`suspended`/`deprovisioned`) and `last_auth_at`; a scheduled sync from Google Workspace / SSO flips terminated or transferred employees to `deprovisioned` and the backend rejects their requests on the next call. Token revocation is wired (Cognito global sign-out + short access-token lifetime). The following bounds are **pinned and must be asserted in CI** (see [docs/threat-model.md → Identity and authorization](threat-model.md#identity-and-authorization)): **sync cadence ≤ 1 hour** (the sync job runs at least every 60 minutes; a failed sync is alarmed and makes no changes); **access-token TTL ≤ 15–60 minutes** (the Cognito pool is configured with a maximum 60-minute access-token lifetime). The combined deprovisioning window (sync cadence + max token TTL) is therefore ≤ ~2 hours worst-case and must be verified by an integration test that reads the Cognito pool configuration and the sync-job schedule and asserts both numbers match these bounds. A deprovisioned or suspended user is denied even with a structurally valid token.
- [ ] **First-admin seed (email-keyed):** CDK writes a single row into the `admin_bootstrap` table keyed by the configured GC **email** (not into the `cognito_sub`-keyed `users` table). On first sign-in the backend runs a one-time reconciliation transaction that creates the real `users` row keyed by Cognito `sub`, copies the admin grant, and marks the bootstrap row consumed. (See the DynamoDB-tables issue for the table; if the email-GSI variant is chosen there, the reconciliation transaction is identical.)
- [ ] Break-glass IAM role defined (normally unused, SSO+MFA-assumable, CloudTrail-logged) that can set `is_admin=true` directly on `users` for recovery. Procedure documented in RUNBOOK.md.
- [ ] CDK deploy succeeds in dev; manual smoke test confirms a `@teamexos.com` allowlisted Google account can sign in, a `@teamexos.com` account **not** on the allowlist cannot, and a non-`@teamexos.com` account cannot.

## Out of scope

- Custom email templates.
- MFA (Google already provides this).
- SAML or any other IdP.
- The admin UI for editing the allowlist (admin phase); this issue establishes the allowlist store and the enforcement.

## Notes

The Google OAuth client must be created manually in Google Cloud Console before this CDK can deploy. The setup steps are documented in RUNBOOK.md → "Day-one bootstrap" → "Confirm Google OAuth client".

---end---

---

## 6. Amplify Hosting + empty React app

**Labels:** `phase:0-foundation`, `area:infra`, `area:frontend`, `type:feature`

---begin---

## Context

Scaffold the React SPA and wire it to Amplify Hosting. The app at this stage shows a sign-in button and, after sign-in, "Signed in as you@teamexos.com" plus the version number from the backend's authenticated `/version` endpoint. Nothing else.

## Acceptance criteria

- [ ] `frontend/` scaffolded with Vite + React + TypeScript.
- [ ] AWS Amplify libraries integrated (`aws-amplify`, `@aws-amplify/ui-react`).
- [ ] Cognito hosted-UI sign-in works end-to-end.
- [ ] Header shows the signed-in user's email.
- [ ] Footer shows the version from the authenticated `/version` endpoint (the backend will return a stub for now). `/health` stays public/liveness-only.
- [ ] Amplify Hosting app defined in `infra/lib/frontend-stack.ts`. **Branch auto-build/auto-publish on push to `main` is allowed in the DEV account only.** The **prod** Amplify app does **not** auto-publish on merge — prod is advanced by a **deliberate promotion of a specific built frontend artifact**, consistent with the digest-pinned backend (see ARCHITECTURE.md → Frontend / Infrastructure). The frontend is legal-facing (it renders the attorney-approval watermark and the ACCEPT framing), so a merge to `main` must not silently change prod presentation.
- [ ] CI build produces the frontend artifact; the dev URL is reachable on push, and prod requires an explicit promotion step.

## Out of scope

- Any feature beyond sign-in and the version display.
- Custom domain (Phase 5).
- Styling beyond the default Amplify UI.
- Error pages or unauthenticated-user routing.

## Notes

Use `@aws-amplify/ui-react`'s `Authenticator` component for the sign-in flow. Configure Amplify Auth via the `aws-exports.js` output from `cdk deploy` (we'll write a small script to generate this from CDK outputs).

---end---

---

## 7. App Runner + hello-world container

**Labels:** `phase:0-foundation`, `area:infra`, `area:backend`, `type:feature`

---begin---

## Context

Stand up the App Runner service that serves the **API only** (no LLM work — that runs in the Step Functions pipeline). The service exposes a public, minimal `/health` (liveness only) and an allowlisted `/version` (version, commit SHA, image digest), and ships the security middleware that every later endpoint relies on.

## Acceptance criteria

- [ ] `backend/Dockerfile` builds a Python 3.12 container running FastAPI via uvicorn.
- [ ] `backend/src/main.py` exposes a public `GET /health` returning **liveness only** (`{"status": "ok"}`, no build details) and an **allowlisted** `GET /version` returning `{"version": "...", "commit": "...", "image_digest": "...", "uptime_seconds": N}`.
- [ ] **JWT verification middleware is in scope here (security ⇒ Phase 0):** verifies the Cognito token signature, audience, and expiry, **and** independently re-verifies the `email` domain and Google `hd` claim are `teamexos.com` (the backend half of the two-layer hosted-domain enforcement). A `/whoami` (or equivalent) authenticated echo endpoint proves it end-to-end.
- [ ] Version and commit SHA are read from environment variables set at container build time.
- [ ] App Runner service defined in `infra/lib/app-stack.ts`, sourced from an **ECR image pinned to an immutable digest** (not from the GitHub `main` branch, and not auto-mutated by a merge). The build/sign/push/promote flow lives in the CI-pipeline issue; this issue consumes a digest.
- [ ] **Auto-deploy from `main` is disabled.** A merge to `main` must not immediately alter production legal behavior. Promotion to a new digest is a deliberate step (see the CI-pipeline issue).
- [ ] **VPC connector configured (security ⇒ Phase 0)** so the service reaches data/retrieval resources privately.
- [ ] API task role is least-privilege. It is split by capability where practical (start-review, read-status, upload, download) rather than one broad role with read/write to all document buckets; it uses short-lived scoped object access with strict KMS encryption-context checks. It may start Step Functions executions and touch only its own S3 prefixes + DynamoDB tables. It does **not** get `bedrock:InvokeModel` — inference runs under the pipeline task role (see the async-pipeline issue). The full split is detailed in the API-role / download-auth / WAF issue.
- [ ] Promoting a new signed digest updates the running service; the authenticated `/version` shows the new commit SHA after a deliberate promotion (not on raw push).

## Out of scope

- Business endpoints beyond `/health` and the auth echo.
- The review pipeline itself (separate issue).
- The CI build/sign/push pipeline itself (separate issue) — this issue only pins App Runner to a digest it produces.
- Custom domain.

## Notes

The ECR repository and the App Runner ECR access role require one-time setup; RUNBOOK.md → "Day-one bootstrap" covers this alongside the CI-pipeline bootstrap.

---end---

---

## 8. Bedrock access

**Labels:** `phase:0-foundation`, `area:llm`, `type:chore`

---begin---

## Context

Enable the Bedrock models named by the current model-policy matrix in each environment account, region `us-east-1`. Today that means the pinned single-region Anthropic Claude Opus 4.8 (primary reviewer), Claude Sonnet 4.6 (adversarial critic — a deliberately different model from the primary), the pinned embedding model used by the Knowledge Base, plus an optional fallback only if explicitly retained and approved. This is a one-time per-account console action.

## Acceptance criteria

- [ ] Bedrock model access page shows "Access granted" for the **pinned single-region** Anthropic Claude Opus 4.8 (primary) and Claude Sonnet 4.6 (critic) model IDs, and the pinned embedding model, in every environment account that will run reviews. The matrix is a deliberate pin per the model-policy matrix, not a vague "best model" default; the critic is a deliberately *different* model from the primary.
- [ ] If the model-policy matrix includes a fallback, Bedrock model access page shows "Access granted" for the pinned single-region fallback model. The fallback is optional, separately approved, audited when used, and not the critic unless the matrix explicitly says so.
- [ ] A one-off CLI call invokes the pinned **single-region native** Opus 4.8 model ID against the regional endpoint with a trivial prompt and returns a response (verify the exact invocable ID — version suffixes differ by model). The request **omits sampling params AWS no longer supports** (`temperature`, `top_p`, `top_k`); extended thinking, if used, is treated as adaptive-only.
- [ ] The config uses **single-region native** model IDs only. **Both `global.` global profiles and `us.`/`eu.`/`apac.` geo cross-region inference profiles are prohibited** in configuration (data residency — a geo profile can route to another region), and a check rejects any prefixed inference-profile ID in config.
- [ ] A model-policy config artifact exists with `primary_model_id`, `critic_model_id`, **`embedding_model_id`**, optional `fallback_model_id`, region, request contract, eval gate, and cost assumptions. It is hashable so release bundles can record `model_policy_hash`. A change to the embedding model is a model-policy change requiring admin (GC) approval and a new corpus snapshot version.
- [ ] Bedrock invocation quota for the pinned Opus model reviewed; quota increase requested if the default is too low for our expected volume.

## Out of scope

- Any production code.
- Wiring Bedrock into the App Runner service (Phase 2).

## Notes

The authoritative model-policy matrix (pinned regional ID, quarterly recertification, request-schema constraints, eval gate, and cost assumptions) lives in ARCHITECTURE.md / docs/design-notes.md — not as a "best model" claim. AWS occasionally renames Bedrock model IDs; pin the version explicitly rather than using a `:latest` alias.

---end---

---

## 9. CloudWatch dashboard

**Labels:** `phase:0-foundation`, `area:infra`, `area:audit`, `type:feature`

---begin---

## Context

Build the production observability dashboard. Tiles for the metrics that matter at this scale.

## Acceptance criteria

- [ ] CloudWatch dashboard `contract-toaster-{env}` defined in CDK.
- [ ] Tiles:
  - Deployed version (text widget, sourced from App Runner deployment metadata).
  - App Runner request rate (per minute).
  - App Runner error rate (4xx and 5xx separately).
  - App Runner p99 latency.
  - Bedrock invocations per day.
  - Cost-to-date for the Bedrock model (via Cost Explorer integration).
  - Step Functions stage failures and stale `PENDING`/`RUNNING` reviews.
  - Abandoned spend reservations and release-bundle activation/rollback audit events.
  - Audit-archive stream lag.
- [ ] CloudTrail trail created, logging to `audit-archive` bucket.
- [ ] Two CloudWatch alarms: 5xx > 5% for 5 minutes; any Bedrock invocation error.
- [ ] Alarms route to an SNS topic. Topic subscription is a placeholder email for now.

## Out of scope

- Custom metrics from the backend (we don't have any yet).
- PagerDuty or other paging integration.
- Athena queries over CloudTrail (Phase 4).

## Notes

Use the `cdk-monitoring-constructs` library if it simplifies things, otherwise raw CloudWatch CDK constructs.

---end---

---

## 10. End-to-end smoke test

**Labels:** `phase:0-foundation`, `type:chore`

---begin---

## Context

The Phase 0 acceptance gate. This issue stays open until every other Phase 0 issue is done and all of the below pass on a fresh `cdk deploy --all` against a clean account.

## Acceptance criteria

- [ ] `cdk deploy --all` from a workstation succeeds with no manual steps beyond the documented bootstrap.
- [ ] Pushing a no-op commit to `main` triggers CI to build, test, scan, sign, and publish an immutable image digest, but does **not** change the running App Runner service.
- [ ] The serving digest (via authenticated `/version` or App Runner metadata) remains the currently promoted digest after the raw push.
- [ ] Explicitly promoting the signed digest updates App Runner; the authenticated `/version` then reports the promoted digest and commit SHA.
- [ ] A no-op push to `main` does **not** mutate the **prod** frontend (prod Amplify does not auto-publish on merge); the prod frontend changes only on a deliberate artifact promotion. (Dev frontend may auto-build.)
- [ ] The Amplify URL loads.
- [ ] A `@teamexos.com` Google account can sign in via Cognito.
- [ ] A non-`@teamexos.com` Google account is rejected at sign-in with a clear message.
- [ ] A request to an authenticated endpoint with a token that fails the domain check is rejected by the **backend** (proves two-layer enforcement, not just the Cognito edge).
- [ ] After sign-in, the React app shows the user's email and the version from the authenticated `/version` endpoint.
- [ ] A `users` row is created in DynamoDB on first sign-in.
- [ ] The Step Functions review state machine exists and a trivial test execution completes end-to-end (stubbed stages OK at this phase).
- [ ] The AWS Budgets monthly budget and alarm exist; idle-cost sanity check is consistent with the ~$25/mo idle target.
- [ ] CloudTrail records the sign-in event (management events; data events confirmed **off**).
- [ ] CloudWatch dashboard shows non-zero traffic.

## Out of scope

- Any business functionality. Phase 0 only proves the foundation works.

## Notes

When all boxes check, close this issue and the `phase:0-foundation` milestone, and open the Phase 1 milestone.

---end---

---

## 11. Async review pipeline (Step Functions skeleton, started directly by the API)

**Labels:** `phase:0-foundation`, `area:infra`, `area:backend`, `type:feature`

---begin---

## Context

Reviews must not run synchronously inside `POST /api/reviews`. Stand up the async skeleton
now so the foundation proves out: **the API starts a Step Functions execution directly**
and returns `202`; the state machine drives the stages; the UI polls. **There is no SQS on
the entry path** — no queue, no consumer, no DLQ buffering review submissions. Idempotency
comes from a durable submission record, one spend reservation per `review_id`, and a
retry-safe "ensure execution started" step with deterministic Step Functions execution
names, not from an SQS dedup window. Stages are stubbed in Phase 0 — this issue is about the orchestration
plumbing, not the LLM logic. (Closes findings 17, 18, 19, 20, 46, 47.)

## Acceptance criteria

- [ ] `infra/lib/pipeline-stack.ts` defines a Step Functions state machine `contract-toaster-{env}`. **No SQS queue or DLQ on the review entry path** (the API → Step Functions call is synchronous from the caller's perspective and returns the execution/review id).
- [ ] State machine has the stage skeleton: extract → retrieve → primary review → adversarial review → redline → persist → audit, each a stubbed Lambda/Fargate task that just passes through for now.
- [ ] Each stage has its own timeout and retry policy; a failed stage transitions the execution and the `reviews` row to `ERROR` with the failing stage recorded (no review left wedged in `PENDING`).
- [ ] **Idempotency transaction:** `POST /api/reviews` requires an idempotency key — a **client-supplied key** preferred (stable across the client's own retries), else derived from uploader `sub` + file hash + active release-bundle hash + a **fixed-width timestamp bucket** (document the width, default 10 min). To avoid a boundary-straddling retry double-running, the derive path **checks the current AND previous bucket** for an existing submission before creating one; a deliberate re-review is an explicit "review again" action that mints a fresh key. The API first creates or reads a `review_submissions` record with a conditional write; that record owns the `review_id`, upload pointer, spend-reservation id, execution name, execution ARN/status, and submission status. A retry returns the existing review id and resumes from the recorded state, not a second review.
- [ ] **Atomic, worst-case spend reservation:** before starting the execution, the API **reserves** a **worst-case upper-bound** estimate (max input + max output tokens × both passes × uncached pricing) exactly once per `review_id` on a conditional DynamoDB counter (a single atomic conditional update that fails closed if the day's cap would be exceeded), then **settles** actual spend after the run. An optimistic estimate would let concurrent submissions collectively overshoot before settlement; a bare pre-check is also not acceptable. Document the resulting max-reviews/day at the default `$20`. (Enforcement half of the daily-ceiling issue.)
- [ ] **Retry-safe execution start + orphan reconciler:** after upload persistence, the API runs `ensure execution started`: if no execution ARN is recorded it starts Step Functions with the deterministic name and records ARN/status; if `ExecutionAlreadyExists` or an ARN is already present, it records/returns the existing execution. A **scheduled/event-driven reconciler** re-runs this step for any submission older than a short threshold with no `execution_arn` (crash between upload and execution start), escalating to the stale-`PENDING` alarm only if the re-drive still fails — so no review is silently stuck in `PENDING`.
- [ ] **Cost ledger in a `finally` path:** every model attempt is ledgered — successful invocations, retries, malformed outputs, aborted/failed executions — so failed or retried reviews cannot escape the ledger. Settlement reconciles the reservation against ledgered actuals even on the error path.
- [ ] **Concurrency control:** a Step Functions semaphore (or reserved/maximum concurrency) caps simultaneous pipeline executions so a burst of uploads cannot flood Bedrock/Opus invocations or drain the daily cap in parallel.
- [ ] **RAG / context caps wired as state-machine and config limits** (even though stages are stubbed): max input document size, max extracted tokens, max sections, top-K per section, max output tokens, and max retries. A 1M-token window does not justify million-token reviews.
- [ ] Pipeline task role is least-privilege and is the **only** role with `bedrock:InvokeModel` and Knowledge Base query permissions.
- [ ] `POST /api/reviews` (stub is fine) creates a `PENDING` review through the submission record, reserves spend once, stores the upload pointer, ensures execution is started, and returns `202` + review id; `GET /api/reviews/{id}` reflects status transitions `PENDING → RUNNING → DONE/ERROR`.
- [ ] A trivial test execution completes end-to-end through the stubbed stages, and a duplicate-submission test proves the retry collides rather than double-running.

## Out of scope

- Real extraction, retrieval, LLM calls, or redline generation (Phase 2).
- The two-agent prompt content (Phase 2 / `area:llm`).
- The adversarial-pass reconciliation semantics and structured-output retry policy (Phase 2 / `area:llm`).

## Notes

**Use Lambda for the LLM stages** (primary review, adversarial critic) — these are thin Bedrock API callers; Lambda's 15-minute limit is ample for a single InvokeModel with retries, and Lambda avoids the 30–60 s Fargate provision penalty per stage. Reserve Fargate only for stages with genuine large-memory needs (extraction, redline generation). See docs/design-notes.md "Why Lambda (not Fargate) for the LLM stages" for the full rationale. Keep the state machine definition readable; this is the backbone for Phase 2. Because there is no SQS buffer, the submission record plus deterministic execution name **is** the dedup mechanism — keep its derivation documented and stable.

---end---

---

## 12. Bedrock Knowledge Base + S3 Vectors (retrieval store)

**Labels:** `phase:0-foundation`, `area:infra`, `area:rag`, `type:feature`

---begin---

## Context

Provision the retrieval store as an Amazon Bedrock Knowledge Base backed by Amazon S3
Vectors (replacing the previously-planned OpenSearch Serverless collection, whose OCU
minimum broke the cost target). Empty corpus is fine at this phase — this issue creates the
store and proves private access and draft-ingestion wiring. It also fixes the metadata model
(which cannot hold full clause text), separates positive from negative precedent, records
the active corpus snapshot used by every review, and establishes the lexical-detector layer
that semantic search alone cannot replace. (Closes findings 5, 6, 7, 8, 9.)

## Acceptance criteria

- [ ] Bedrock Knowledge Base + S3 Vectors store defined in `infra/lib/data-stack.ts`.
- [ ] Ingestion is wired so an admin corpus upload triggers a Bedrock KB ingestion job into a **draft** corpus snapshot (the upload endpoint itself can be stubbed in Phase 0). Draft snapshots are not review-queryable.
- [ ] **Corrected metadata model:** vector chunk metadata holds only **compact IDs and filter fields** — immutable clause ID, source document ID, document type, **`playbook_id`** (required; the playbook this vector was ingested under — `eiaa` in v1; every retrieval query must filter on `playbook_id` to prevent cross-agreement-type contamination when a second playbook is introduced), `playbook_topic_id` (scoped within `playbook_id`), counterparty, date, and curation flags. It does **not** store original clause text, rationale, or model summaries (AWS documents ~1KB custom metadata and a 35 metadata-key limit for S3 Vectors with Bedrock KBs). Full clause text lives in S3/DynamoDB keyed by the immutable clause ID and is fetched after retrieval.
- [ ] **Positive/negative separation:** accepted precedent and rejected drafts are **not commingled in the same top-K context**. Either separate corpora/indexes, or a hard-labeled negative-example channel — rejected language never appears as positive precedent. `document_type` distinguishes `executed-final`/`accepted-draft`/`rejected-draft`, and the retrieval contract keeps rejected clauses out of the positive context.
- [ ] **Curation fields:** the clause record carries a legal-curated `reusable_precedent` flag, `negotiation_context`, `superseded_by`, and `approved_use_scope`. Not every executed agreement is positive precedent (a one-off concession must not be treated as authoritative).
- [ ] **Corpus versioning / activation (app-layer snapshots; Bedrock KB has no native snapshot):** because a Bedrock KB mutates one live index in place, snapshots are built at the application layer — reviews query an **active** store; a candidate snapshot ingests into a **separate staging index**; activation **repoints the active reference**; each `corpus_snapshot_version` freezes a content-addressed **clause-id manifest** for reproducibility. Every review records the active snapshot version. An **ingestion interlock** makes a review refuse to run against a store mid-ingestion or against a draft/failed/partial/superseded snapshot. The `corpus_snapshot_version` metadata tag is a defense-in-depth query filter, not the sole isolation mechanism.
- [ ] **Deterministic detector layer over the DIFF (companion to semantic search):** detectors run over the standard-form **diff**, never raw full text. `on_insert` rules match trigger terms in counterparty *insertions* (with `exempt_terms` guards); `on_remove_or_alter` rules fire when a protected standard-form token is *deleted/altered*. Semantic search can miss exact legal terms; the deterministic layer catches them without firing on the standard form's own compliant language. (The detector implementation lands with the review logic in Phase 2, but the retrieval/detector contract — schema `kind`, `section_anchors`, the zero-false-positive CI gate — is reserved here; see ARCHITECTURE.md and docs/playbook-governance.md.)
- [ ] The KB and its S3 Vectors store are **private** — reachable only via the pipeline task role (IAM) and a VPC endpoint where applicable. Never public. (security ⇒ Phase 0)
- [ ] A trivial query from the pipeline task role returns results (or an empty set against an empty corpus) without error.
- [ ] Idle cost sanity check: no provisioned/OCU minimum is being charged.

## Out of scope

- Real corpus ingestion and the document-to-clause extraction logic (Phase 3).
- Retrieval tuning / top-K selection (Phase 2/3).
- Treating retrieved corpus text as untrusted (prompt-injection handling) — that contract is owned by the threat model and the review-logic phase; see docs/threat-model.md.

## Notes

If S3 Vectors proves a poor fit for a required Bedrock KB feature, the documented fallback is Aurora Serverless v2 (pgvector) with scale-to-zero — not standalone OpenSearch Serverless. See ARCHITECTURE.md / docs/design-notes.md.

---end---

---

## 13. Spend ceiling, retention purge worker, and budget guardrail

**Labels:** `phase:0-foundation`, `area:backend`, `area:infra`, `type:security`

---begin---

## Context

Three cost/abuse guardrails that are foundation, not later add-ons. The admin **UI surfaces** for these arrive in the admin phase; this issue builds the **enforcement**.

## Acceptance criteria

- [ ] **Daily spend ceiling** (default `$20/day`, configurable) enforced server-side via an **atomic conditional reservation** of a **worst-case upper-bound** estimate (not an optimistic estimate, not a bare pre-check): `POST /api/reviews` reserves on a conditional DynamoDB counter and fails closed with a clear "daily limit reached" message if the cap would be exceeded, then settles actuals afterward. Because the estimate is a worst-case bound, concurrent submissions cannot collectively overshoot the cap. The reservation/settlement mechanism is shared with — and detailed in — the async-pipeline issue (findings 17/18). The limit value and today's spend are stored where the admin dashboard can later read them.
- [ ] **CI/eval Bedrock spend is budgeted too:** the evaluation harness invokes Bedrock outside the per-review path, so its spend is routed through the same reservation/ledger (or a separate explicitly-budgeted CI ceiling), surfaced on the dashboard, and capped by gold-set-size × stochastic-run-count against a documented budget — it must not silently bypass the daily ceiling. (See the evaluation-harness issue.)
- [ ] **Retention purge worker:** a scheduled + on-demand job that deletes objects in `uploads`/`outputs` older than the configured retention window (0 days–3 years, default 90). On a settings save it sweeps **retroactively** (e.g. `0` = immediate purge-all). It never deletes non-substantive `reviews` metadata, which is retained indefinitely, but retention-governed confidential substance (summaries, model rationales, critic deltas, and generated outputs) expires with the document class. The retention value is stored where the admin slider will later read/write it.
- [ ] **Legal hold respected at storage layer:** purge skips held reviews and held corpus material, and tests prove held S3 objects cannot be deleted by the purge role because Object Lock legal hold or protected hold-tag bucket policy blocks it.
- [ ] **AWS Budgets** monthly budget + alarm defined in CDK (target ≤ $100/mo dev), routing to the alarms SNS topic.
- [ ] Unit/integration coverage for the ceiling check and the retroactive purge sweep.

## Out of scope

- The admin-UI controls themselves (the daily-spend display and the retention slider) — built in the admin phase, reading the values this issue establishes.
- Per-user (vs. global) spend limits.

## Notes

The retention worker exists because S3 lifecycle rules are static and can't express an admin-tunable, retroactive window. Keep the stored settings in a small config table/item so both the worker and the future admin UI share one source of truth.

---end---

---

## 14. Evaluation harness + gold test set 🚧 BLOCKING GATE

**Labels:** `phase:0-foundation`, `area:llm`, `type:chore`

---begin---

## Context

There is no way to know whether a model/prompt/playbook change made review **better or
worse** without a deterministic evaluation harness, and that harness has to exist **before
any LLM code is written**. This issue builds the gold test set and the harness that gates
every later prompt/playbook change. (Closes finding 29.)

**This is a BLOCKING GATE:** no work on the LLM review path (primary/adversarial passes,
prompt content, redline generation) may merge until this harness exists and is green. The
async-pipeline stages may be stubbed before it; the *real* LLM logic may not.

## Acceptance criteria

- [ ] A **synthetic** gold test set (≈33–39 cases seeded from the playbook + standard form; structure in [docs/evaluation.md](evaluation.md)), each annotated with: expected issue list, expected ACCEPT / REQUEST_CHANGE decision, expected hard-rejection hits, `must_not_flag`, `fp_tolerance`, and expected redline-patch targets.
- [ ] A documented false-positive tolerance and the pass/fail thresholds the harness enforces.
- [ ] Regression gates that run on any playbook, prompt, standard form, model-policy (incl. embedding model), or corpus-snapshot activation and **block** the release bundle if the gold set regresses beyond tolerance.
- [ ] **Detector-correctness gate (deterministic, no model):** D1 zero-fire on the clean standard form + clean-ACCEPT + near-miss probes (target 0, hard gate); D2 each planted violation fires the right rule and only it (target 100%); D3 injection probes do not fire. This is the deterministic backstop for the C1 false-positive class.
- [ ] **CI eval spend is budgeted** (routed through the reservation/ledger or a separate CI ceiling; gold-set size × stochastic runs capped against a documented budget; fails loudly rather than truncating coverage) — the harness must not bypass the daily spend ceiling.
- [ ] Stochastic stability gate: repeated runs over a representative subset stay within documented variance bounds for decision, hard-rejection hits, issue IDs, and redline targets. (The deterministic detector layer is exempt and held to the hard D1/D2 gates instead.)
- [ ] Corpus/retrieval regression gate: known queries return expected positive precedent, keep rejected examples in the negative channel, and do not leak counterparty-specific precedent into external citation fields.
- [ ] Redline-patch fixture checks (anchor + exact-match) wired so a patch that would touch the wrong clause fails the harness.
- [ ] The harness runs in CI and locally against **synthetic** documents only (no production legal data — see the dev-data-isolation issue).
- [ ] Authoritative spec and rubric documented in [docs/evaluation.md](evaluation.md); release-bundle validation records the playbook hash, prompt hash, standard-form hash, model-policy hash, corpus snapshot, eval run ID, and legal approval.

## Out of scope

- The LLM review logic itself (Phase 2) — this issue must precede it.
- Live-traffic quality dashboards (later); this is an offline gold-set harness.

## Notes

Treat the gold set as a versioned asset. When the playbook changes, the expected outputs may legitimately change — the harness exists to make that change **deliberate and reviewed**, not silent.

---end---

---

## 15. Hostile-file upload validation + AV scan + hardened OOXML parsing

**Labels:** `phase:0-foundation`, `area:backend`, `area:security`, `type:security`

---begin---

## Context

The upload path currently has no hostile-file model. A `.docx` is a zip of XML and can
carry zip bombs, XML-entity expansion, external relationships, embedded objects, and macro
templates. We validate **before** we ever extract or parse. (Closes finding 4.)

## Acceptance criteria

- [ ] Pre-extraction validation enforces: maximum file size, MIME/magic-number match (the bytes are actually OOXML, not a renamed payload), zip-bomb limits (uncompressed-size and entry-count caps), XML-entity-expansion protection, rejection of external relationships, rejection of embedded objects, and rejection of macro-enabled templates.
- [ ] The uploaded object is **AV-scanned before extraction**; a positive scan fails the upload closed and is audited.
- [ ] OOXML is parsed only with **hardened libraries** configured against entity expansion / external entity resolution; no parser is invoked before validation passes.
- [ ] A failed validation returns a clear client error and writes an audit row; the file is not handed to the pipeline.
- [ ] Unit tests cover each hostile-file class (oversized, zip bomb, entity bomb, external-relationship, embedded-object, macro template, MIME mismatch).

## Out of scope

- Document content normalization (tracked changes / comments) — owned by the input-normalization issue below.
- Clause extraction (Phase 3).

## Notes

This is the trust boundary for everything downstream. Deeper attacker modeling, including untrusted corpus text, is owned by [docs/threat-model.md](threat-model.md); this issue implements the upload-path controls it specifies.

---end---

---

## 16. Standard-form storage + deterministic diff 🚧 BLOCKING GATE

**Labels:** `phase:0-foundation`, `area:rag`, `area:llm`, `type:feature`

---begin---

## Context

The review cannot soundly judge a draft without comparing it to the canonical Exos standard
form. Feeding the model only the uploaded document (no anchored standard) invites
hallucinated "issues" and missed deviations. This issue stores the standard form per
playbook version and produces a deterministic diff that the model consumes. (Closes
finding 1.)

**This is a BLOCKING GATE** for the soundness of the review path: the LLM passes must
receive the deterministic diff plus anchored clause text, not a bare upload.

## Acceptance criteria

- [ ] The canonical standard `.docx` is stored **per playbook version** (content-addressed, alongside the playbook snapshot).
- [ ] A **deterministic** diff is generated between the stored standard and the uploaded draft (same inputs → same diff).
- [ ] The review contract feeds the model the **diff plus anchored clause text**, not just the uploaded document.
- [ ] The diff output carries paragraph/table-cell anchors that the redline-patching path can rely on.
- [ ] **Anchor-map builder (dependency of #14 and #19):** the standard-form `.docx` is parsed at bundle-build time to produce the **section-anchor map** — a deterministic, content-addressed mapping of every paragraph/table-cell to its `sec-<slug>` anchor ID. The §10 Miscellaneous heading is split into its four sub-clause anchors (`sec-10-notices`, `sec-10-non-exclusive`, `sec-10-merger`, `sec-10-precedence`) so the four §10 playbook topics each resolve to a distinct anchor. This map is stored alongside the standard-form snapshot and is the authority against which issue #19's anchor-resolution gate checks every `section_anchors[]` entry. A change to the standard form that alters headings or sub-clause structure must regenerate the anchor map. **This deliverable is a prerequisite for the evaluation harness (#14) and for the playbook CI anchor-resolution gate (#19).**
- [ ] Gold-set fixtures (see the evaluation-harness issue) exercise the diff on known draft/standard pairs.

## Out of scope

- The model prompt content that consumes the diff (Phase 2).
- Redline patch application (next issue).

## Notes

The standard form is versioned with the playbook so a review always diffs against the standard that was active at execution time.

---end---

---

## 17. Redline anchoring + fail-closed patching + input normalization 🚧 BLOCKING GATE

**Labels:** `phase:0-foundation`, `area:backend`, `area:security`, `type:security`

---begin---

## Context

Redline patching is a security-bearing operation: an approximate edit can silently modify
the **wrong** clause of a legal document. And an uploaded `.docx` may already contain
tracked changes, comments, hidden text, fields, or footnotes that corrupt review and
patching. This issue makes patching exact-match-or-fail and normalizes input revisions
before review. (Closes findings 2, 3.)

**This is a BLOCKING GATE:** no redline is applied unless the target text matches exactly.

## Acceptance criteria

- [ ] Every patch carries a **paragraph/table-cell anchor** and a **hash of the source text** it intends to replace.
- [ ] Patch application **validates exact match** of the target text against the hash/anchor; if the target no longer matches, the pipeline **FAILS CLOSED** — it emits an internal analysis report and applies **no** approximate edit.
- [ ] Input normalization: the pipeline either requires a **clean** input document (no tracked changes / comments / hidden text / fields / footnotes affecting clause text) **or** runs a normalization pass that accepts/rejects existing revisions per a **documented rule** before review begins.
- [ ] Redline fixture tests cover tracked-change semantics, anchor drift, and the fail-closed path (mismatched target → no edit, report emitted).
- [ ] The fail-closed outcome is surfaced as a SYSTEM status (manual review required), not as a legal decision.

## Out of scope

- The diff generation (previous issue).
- Provenance controls for vendored redline code — owned by the supply-chain / playbook-governance issue.

## Notes

"Apply the closest match" is explicitly prohibited. For a legal document, a wrong-clause edit is worse than no edit.

---end---

---

## 18. CI pipeline: CodeBuild → tests/scans → signed image → ECR → digest deploy

**Labels:** `phase:0-foundation`, `area:infra`, `area:security`, `type:security`

---begin---

## Context

A merge to `main` must **not** immediately alter production legal behavior. Replace
auto-deploy-from-main with a CI pipeline that builds, tests, scans, **signs**, and publishes
an immutable container image, which is then deployed by **digest** through a deliberate
promotion. (Closes finding 35.)

## Acceptance criteria

- [ ] CodeBuild (or equivalent CI) runs the test suite, the evaluation-harness gates, and security/dependency scans on every change.
- [ ] On success it builds a container image, **signs** it, and pushes it to **ECR** with an immutable tag/digest.
- [ ] App Runner (the service) is **pinned to a specific image digest** and is **never auto-mutated by a merge to `main`** — promotion to a new digest is a deliberate, audited step.
- [ ] Image signatures are verified at/ before deploy; an unsigned or unverifiable digest cannot be promoted.
- [ ] The promotion writes an audit row (who promoted which digest, when).
- [ ] Auto-deploy from the GitHub `main` source is disabled on the App Runner service (see the App Runner issue).

## Out of scope

- Multi-region image replication (revisit Phase 5).
- The playbook/prompt release governance (separate issue) — code deploy and content release are distinct gates.

## Notes

Two independent promotion gates exist: **code** (this issue, by signed digest) and **playbook/prompt content** (the governance issue). Neither should be able to change production legal behavior implicitly.

---end---

---

## 19. Playbook schema hardening + release-bundle governance

**Labels:** `phase:0-foundation`, `area:llm`, `area:security`, `type:feature`

---begin---

## Context

The playbook is a production control surface, but it is not the only one. Prompt text,
the canonical standard form, the model-policy matrix, and the active corpus snapshot can
all change legal output. This issue hardens the playbook schema and creates the governed
release-bundle lifecycle that activates those inputs together. A CODEOWNERS rule alone does
not make legal behavior safe to activate. (Closes findings 32, 33, 34.)

## Acceptance criteria

- [ ] **Schema hardening (validated in CI) — post-redesign contract (supersedes the pre-redesign checklist):**
  - **Unique keys.** Topic `id` values are unique; topic `section_ref` values are unique **including placeholder/absent-section topics** (e.g., absent-section topics carry distinct refs like `[absent] Insurance`).
  - **Kind-conditional validation.** Every `hard_rejections[]` rule carries a valid `kind` and satisfies the per-kind field contract: `on_insert` rules require `trigger_terms` and forbid `protects`; `on_remove_or_alter` rules require `protects` (with `section_anchor` and `required_tokens`) and forbid `trigger_terms`. CI fails the build on any violation.
  - **`not_in_standard` rules.** Topics with `not_in_standard: true` may be referenced **only** by `on_insert` rules (you cannot remove a clause the standard form never contained). Every `not_in_standard: true` topic **must** carry `section_anchors: ["sec-_new"]` (the reserved pseudo-anchor); a `not_in_standard` topic with empty `section_anchors` makes any `on_insert` rule referencing it dead config. **CI fails the build** on violation.
  - **Anchor resolution.** Every `section_anchors[]` entry on a topic (other than the `sec-_new` pseudo-anchor) must resolve to a real section in the bundle's standard-form section-anchor map (produced by issue #16's anchor-map builder). The §10 Miscellaneous heading resolves to sub-clause anchors (`sec-10-notices`, `sec-10-non-exclusive`, `sec-10-merger`, `sec-10-precedence`). **CI fails the build** if any anchor is unresolved.
  - **Required-token-presence check.** Every `on_remove_or_alter.required_tokens` entry must be **present** in its anchored section of the canonical standard form. A protective rule guarding an absent token is dead config. **CI fails the build** on violation.
  - **`exempt_terms` liveness check.** Each `exempt_terms` phrase must contain at least one of the rule's `trigger_terms`; a dead exemption warns/fails CI.
  - **Empty-scope gate.** For every `hard_rejections[]` rule that has `applies_to_topics` defined, the union of `section_anchors` over all referenced topics must be non-empty (where `sec-_new` counts as non-empty for `on_insert` rules referencing `not_in_standard` topics). A rule with provably empty effective hunk-scope can never fire and **CI fails the build**.
  - **`hard_rejection_refs` resolve.** Every topic `hard_rejection_refs` id exists in `hard_rejections[]`. Configured high-risk topics (indemnity, liability) must reference at least one rule.
  - **Coverage.** Every section present in the canonical standard form maps to exactly one topic.
  - **Acceptable-variations lint (zero detector fires).** Every `acceptable_variations[].if/to` text is rendered through the full `on_insert` detector pass as a simulated inserted hunk. Required result: zero hard-rejection fires (a documented acceptable variation must never be blocked by a monotonic hard-rejection rule).
  - **Rule-count note (corrected from pre-redesign plan).** The shipped v1 playbook has **15 hard-rejection rules** (9 `on_insert` + 6 `on_remove_or_alter`). The original backlog plan cited 16 rules ("9+5"); the sixth `on_remove_or_alter` rule (`preserve-exos-precedence`) was added during the detector redesign. Issue-filing math and evaluation fixtures are keyed to 15 rules (one planted D2 case per rule).
  - Schema validation fails the build on any of the above violations.
- [ ] **Release lifecycle:** admin upload creates `draft` inputs; only gated activation creates the single `active` release bundle; superseded bundles become `retired`.
- [ ] **Release bundle metadata:** activation requires playbook hash, prompt hash, canonical standard-form hash, model-policy hash, active corpus snapshot version, eval run ID, and explicit legal-approval metadata. An incomplete or unapproved bundle cannot become active.
- [ ] **Content-addressed snapshots:** activating a bundle stores immutable, content-addressed snapshots. Every review records the active release-bundle hash and component hashes at execution start.
- [ ] **CI gating beyond CODEOWNERS:** automated schema validation (all rules above), acceptable-variations lint, prompt/model/corpus regression tests, leakage tests, stochastic stability checks, and redline fixture tests must pass before a bundle can become active.

## Out of scope

- One-click rollback / quarantine of affected reviews — owned by the rollback issue below (closely related; keep them separately buildable).
- The admin UI for editing playbooks (admin phase).

## Notes

The release-bundle gate here is the **legal-behavior** counterpart to the signed-image deploy gate in the CI-pipeline issue. The full rules for each CI check are the authoritative contract in [docs/playbook-governance.md](playbook-governance.md); this issue's AC enumerates them so the backlog correctly reflects the post-redesign scope.

---end---

---

## 20. One-click release-bundle rollback + quarantine of affected reviews

**Labels:** `phase:0-foundation`, `area:backend`, `area:llm`, `type:feature`

---begin---

## Context

If a bad prompt, playbook, model policy, standard form, or corpus snapshot reaches `active`,
we need to revert it instantly and contain the damage. This issue adds one-click release-bundle
rollback and automatic quarantine of reviews produced under the bad bundle. (Closes finding 50.)

## Acceptance criteria

- [ ] One-click rollback of the active release bundle to a prior content-addressed bundle.
- [ ] The rollback writes an audit row (who, when, from-bundle → to-bundle, component hashes, reason).
- [ ] Reviews run under the rolled-back bad bundle are queryable by release-bundle hash/component hash and are **automatically marked `QUARANTINED`** — flagged and excluded from normal surfaces — so a known-bad output is not mistaken for a good one.
- [ ] Rerunning a quarantined review under the replacement bundle creates a new review/output and marks the original `SUPERSEDED`, preserving traceability.
- [ ] Rollback is testable end-to-end against stubbed stages (activate → detect → rollback → quarantine → rerun → supersede).

## Out of scope

- The rollback admin UI button (admin phase) — this issue establishes the action and its audit/quarantine behavior.

## Notes

Rollback depends on the content-addressed snapshots, release-bundle records, and rollback indexes from the release-governance and DynamoDB issues.

---end---

---

## 21. Dev synthetic-data isolation + separate production AWS account

**Labels:** `phase:0-foundation`, `area:infra`, `area:security`, `type:security`

---begin---

## Context

Two environment decisions, resolved together. Production legal documents must never be
reachable from a developer laptop, and the original "no multi-account" stance is
inconsistent with running dev and prod side by side against legal-facing data. (Closes
findings 38, 39.)

## Acceptance criteria

- [ ] **Production runs in a SEPARATE AWS account from dev.** v1 still avoids an elaborate multi-account org structure, but prod is isolated; cross-account access to prod data is not granted to dev principals or developer laptops.
- [ ] Account IDs and the dev/prod boundary are documented in ARCHITECTURE.md, and CDK context selects the correct account per environment (no shared account for legal-facing data).
- [ ] **Dev uses synthetic corpus and synthetic documents only.** Production legal documents must not be reachable from developer machines or the dev account.
- [ ] Local development and the evaluation harness run against the synthetic fixtures; there is no path from a laptop to prod uploads/outputs/corpus.
- [ ] A documented check (or guardrail) prevents pointing dev tooling at prod data stores.

## Out of scope

- An elaborate AWS Organizations / Control Tower setup (deliberately deferred past v1).
- Cross-account CI promotion mechanics beyond what the CI-pipeline issue covers.

## Notes

The CDK already stamps environment names into stack names; this issue makes the account boundary real rather than nominal.

---end---

---

## 22. Per-data-class KMS keys

**Labels:** `phase:0-foundation`, `area:infra`, `area:security`, `type:security`

---begin---

## Context

One KMS key per environment is too coarse for legal-facing data — a single key means a
single blast radius and one break-glass policy for audit, corpus, uploads, outputs, and
DynamoDB alike. This issue splits keys per data class. (Closes finding 40.)

## Acceptance criteria

- [ ] Separate customer-managed keys for **audit**, **corpus**, **uploads**, **outputs**, and **DynamoDB**, each defined in CDK.
- [ ] Each key has a **narrow key policy** scoped to the roles that legitimately use that data class; the upload path cannot decrypt corpus or audit data, and vice versa.
- [ ] Break-glass permissions differ per key (e.g. audit-key break-glass is tighter than uploads).
- [ ] The S3 buckets and DynamoDB tables reference their data-class key (see the S3-buckets and DynamoDB-tables issues, which consume these keys).
- [ ] `cdk deploy` succeeds in dev with the per-class keys in place.

## Out of scope

- Cross-region key replication (Phase 5).
- Automatic key rotation tuning beyond AWS defaults.

## Notes

This issue is a dependency of the S3-buckets and DynamoDB-tables issues; land the keys first or co-deploy.

---end---

---

## 23. Split API role + scoped download auth + WAF & abuse limits

**Labels:** `phase:0-foundation`, `area:backend`, `area:security`, `type:security`

---begin---

## Context

The API role has too much blast radius, document download lacks tight authorization, and
there is no WAF / throttling / per-user abuse limit. These are security-bearing and belong
in Phase 0. (Closes findings 41, 42, 43.)

## Acceptance criteria

- [ ] **Split API role:** upload, review-start, read-status, and download capabilities are separated where practical, rather than one role with broad read/write to all document buckets. Object access is short-lived and scoped, with strict **KMS encryption-context** checks.
- [ ] **Download authorization:** files are streamed through an authenticated endpoint **or** served via **very short-lived presigned URLs generated only after owner/admin checks**. Responses set `Cache-Control: no-store`. Review IDs are high-entropy and **non-enumerable**.
- [ ] **Document access audit:** review-detail reads, download attempts, presigned URL issuance, successful stream/download completion where observable, and access denials write non-substantive audit rows (actor, target, route/action, outcome, request id, reason code; no document/model substance).
- [ ] **WAF + abuse limits:** request-size caps, WAF rules, rate limits on upload and polling endpoints, **per-user review-concurrency limits**, and **per-user daily limits**.
- [ ] Tests cover: a non-owner cannot download another user's review; an expired presigned URL fails; per-user concurrency and daily limits reject excess requests.

## Out of scope

- The global daily **spend** ceiling (owned by the spend-ceiling and async-pipeline issues) — this issue covers per-user request abuse, not cost.
- Custom domain / edge config (Phase 5).

## Notes

Per-user request limits (this issue) and the global spend reservation (pipeline issue) are complementary controls — neither replaces the other.

---end---

---

## 24. Frontend + admin-UI token storage and XSS posture

**Labels:** `phase:0-foundation`, `area:frontend`, `area:security`, `type:security`

---begin---

## Context

The frontend token and XSS posture is currently undefined, and the admin UI is a
particularly attractive stored-XSS target because it renders playbook content, corpus
metadata, audit fields, document section titles, and model outputs. This issue establishes
the client-side security posture. (Closes findings 44, 45.)

## Acceptance criteria

- [ ] **Token posture:** documented token storage strategy, a Content-Security-Policy, Trusted Types where feasible, and dependency scanning in the frontend build.
- [ ] **No unsafe HTML rendering:** model-generated summaries and any document-derived text are output-escaped; no `dangerouslySetInnerHTML` on untrusted content.
- [ ] **Admin UI treats as untrusted and renders as escaped text only:** playbook content, corpus metadata, audit fields, document section titles, and model outputs.
- [ ] XSS regression tests cover a hostile string flowing through model output, corpus metadata, and an audit field into both the user UI and the admin UI.

## Out of scope

- Feature UI beyond what each phase introduces; this issue sets the posture the later UI work must follow.
- Server-side output-leakage scanning (separate issue) — distinct from client-side escaping.

## Notes

Escaping is the last line of defense; the upload-validation and output-leakage-scanning issues reduce what reaches the client in the first place.

---end---

---

## 25. Model-output leakage scanning

**Labels:** `phase:0-foundation`, `area:llm`, `area:security`, `type:security`

---begin---

## Context

Model output can disclose the system prompt, the playbook, internal policy, or excessive
verbatim precedent — none of which belongs in an external-facing redline `.docx`. This issue
scans output before the document is generated. (Closes finding 49.)

## Acceptance criteria

- [ ] Before the `.docx` is generated, model output is scanned for: system-prompt leakage, internal-policy/playbook leakage, excessive precedent quotation, external-facing confidential rationale, and citation leakage (counterparty names, precedent document dates, internal precedent IDs, or verbatim precedent text in external footnotes).
- [ ] A positive detection blocks document generation and routes the review to manual review as a SYSTEM status (not a legal decision), with an audit row.
- [ ] Tests cover a planted leakage string (prompt fragment, playbook text, over-quoted precedent) being caught before generation.

## Out of scope

- The internal-only export marker on generated redlines (compliance guardrail) — owned by the compliance/output-watermarking work; see ARCHITECTURE.md.
- Client-side escaping (frontend XSS issue).

## Notes

This is a confidentiality control on what leaves the system, complementary to the client-side escaping in the frontend/admin-UI issue.

---end---

---

## 26. Human-review-outcome capture

**Labels:** `phase:0-foundation`, `area:backend`, `type:feature`

---begin---

## Context

Even though attorney approval stays **outside** the tool, we get no feedback loop unless we
record what the attorney did with the tool's output. This issue captures that outcome so
quality can improve over time. (Closes finding 51.)

## Acceptance criteria

- [ ] A lightweight capture records, per review, whether the attorney **accepted**, **edited**, or **rejected** the tool output, plus structured reason codes/topic IDs where applicable and an optional free-text note.
- [ ] The outcome is stored against the review (and is feedable into the evaluation harness / gold-set curation later). Edited/rejected outcomes enter a legal triage queue before becoming candidate gold-set changes.
- [ ] Capturing the outcome does **not** turn the tool into an approval workflow — it is a feedback signal, not a legal gate.

## Out of scope

- Building an in-tool approval workflow (explicitly not what this is).
- Automated retraining or gold-set updates from the captured signal (later).

## Notes

This closes the quality loop the evaluation harness depends on: real attorney outcomes become candidate gold-set cases.

---end---
