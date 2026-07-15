# Architecture issue spotting - June 1, 2026

This register captures the supplemental architecture review performed before filing the
next GitHub issues. It supersedes the stale generated review packets and should be read
with `ARCHITECTURE.md`, `RUNBOOK.md`, `docs/threat-model.md`,
`docs/data-handling.md`, `docs/evaluation.md`, and `docs/design-notes.md`.

The prior architecture review response closed 54 first-pass findings. This document
tracks the remaining issue-spotting work needed to keep the design internally
consistent before implementation.

## Top changes before filing issues

1. Make releases a governed bundle: playbook, prompt, canonical standard form, model
   policy, corpus snapshot, evaluation run, and legal approval move together.
2. Treat the Phase 0 issue list as a draft until all stale assumptions are removed:
   no App Runner auto-deploy from `main`, no SQS entry path, separate prod/dev accounts,
   and no legacy hard-rejection playbook format.
3. Define canonical status, audit, and privacy contracts before any backend code:
   review statuses, user statuses, audit field whitelist, read/download audit, and
   retention-governed model substance.

## Findings

### 1. Active playbook activation contract is not yet enforceable

**Severity:** Critical  
**Lens:** correctness, deployment, legal governance

The hardened schema requires release-governance fields, but the seed playbook was still
written as if upload could make it active immediately. A production review must never run
against a playbook that has not passed schema validation, gold-set regression, redline
fixtures, stochastic stability checks, and Legal approval.

**Resolution:** Admin upload creates `draft`. Only the activation path can set `active`,
and activation requires a complete release bundle: playbook hash, prompt hash, standard
form hash, model-policy hash, corpus snapshot version, evaluation run ID, and legal
approval metadata.

### 2. Hard-rejection detector needs machine-readable inputs

**Severity:** Critical  
**Lens:** legal soundness, retrieval, maintainability

The architecture relies on deterministic lexical detectors for terms such as
`indemnify`, `hold harmless`, `uncapped`, `business associate`, `school official`, and
`exclusive`. Legacy free-text hard rejections cannot drive that layer.

**Resolution:** Active playbooks must use structured hard rejections with `id`,
`trigger_terms`, `match`, and `applies_to_topics`. High-risk topics must reference those
IDs through `hard_rejection_refs`, and CI must fail if detector-backed hard rejections
lack gold-set coverage.

### 3. Canonical standard form must be part of the release bundle

**Severity:** Critical  
**Lens:** redline correctness, reproducibility

The review depends on deterministic diffing against the canonical Exos standard form.
If the playbook is approved without binding the exact standard-form `.docx`, reviewers
cannot reproduce the basis for a decision.

**Resolution:** Require `standard_form_hash` in the release bundle and record it on
every review. Rollback and quarantine must operate on the whole bundle, not only the
JSON playbook.

### 4. Review submission idempotency needs an explicit transaction model

**Severity:** High  
**Lens:** reliability, cost control, failure recovery

The prior flow ordered spend reservation, upload, row creation, and `StartExecution` in a
way that could double-reserve spend on retries or leave a review stuck in `PENDING` if
execution start failed.

**Resolution:** Create a submission/idempotency record first. Reserve spend exactly once
per `review_id`, store the upload pointer and execution ARN/status, and make retry logic
perform an idempotent "ensure execution started" operation using the deterministic Step
Functions execution name.

### 5. Corpus ingestion must not silently change legal behavior

**Severity:** High  
**Lens:** retrieval, governance, auditability

Adding corpus documents changes what precedent the model sees. That can alter legal
output as surely as a prompt or playbook change.

**Resolution:** Corpus ingestion produces a draft corpus snapshot. Only a curated and
tested snapshot may become active. Reviews query only the active snapshot, and every
review records the snapshot version it used.

### 6. External precedent citations conflict with confidentiality controls

**Severity:** High  
**Lens:** privacy, legal soundness, prompt leakage

Precedent can guide internal reasoning, but external footnotes must not disclose
counterparty names, deal dates, internal strategy, or verbatim precedent language.

**Resolution:** Split citations into internal audit fields and external footnote text.
External footnotes cite only the contract position and section. Leakage scanning remains
a backstop, not the primary control.

### 7. Immutable audit must not retain confidential substance indefinitely

**Severity:** High  
**Lens:** privacy, retention, audit

Audit rows are intentionally immutable and archived to object-locked S3. They therefore
must not contain model-written rationales, clause text, document summaries, or critic
deltas that reproduce confidential document substance.

**Resolution:** Define an immutable-audit whitelist: event IDs, actor, action, target,
decision, hashes, topic IDs, model/playbook/prompt/corpus identifiers, token counts,
cost, scanner rule IDs, and authorization result. Store substantive rationales and
critic deltas only in retention-governed confidential storage.

### 8. Document reads and downloads need first-class audit

**Severity:** High  
**Lens:** audit, authorization, privacy

The design audited state-changing actions, but legal-document reads and downloads are
also material access events.

**Resolution:** Audit every review view, output download, presigned URL issuance, and
failed access attempt with actor, review ID, document hash, IP/user agent, and auth
decision. Prefer authenticated streaming where proof of actual transfer matters.

### 9. Legal hold needs storage-level enforcement

**Severity:** High  
**Lens:** legal hold, IAM, evidence preservation

Governance-mode object lock can be bypassed by principals with
`s3:BypassGovernanceRetention`. A purely application-level hold flag is not enough.

**Resolution:** Use S3 Object Lock legal holds or object tags plus bucket-policy denies
to block deletion of held material. Restrict governance bypass to MFA break-glass,
require session reason/ticket tags, and alarm on every bypass attempt.

### 10. Status enums are inconsistent across documents

**Severity:** High  
**Lens:** reliability, operations, maintainability

Review terminal states and user lifecycle states were named differently in different
documents (`disabled` vs. `suspended`/`deprovisioned`, and inconsistent terminal review
sets).

**Resolution:** Define canonical enums once:
`ReviewStatus = PENDING | RUNNING | DONE | ERROR | MANUAL_REVIEW_REQUIRED |
ERROR_MANUAL_REVIEW_REQUIRED | QUARANTINED | SUPERSEDED` and
`UserStatus = active | suspended | deprovisioned`. Use those names everywhere.

### 11. Rollback and quarantine need queryable data

**Severity:** High  
**Lens:** rollback, audit, operations

The runbook described quarantining reviews under a bad prompt/playbook version, but the
data model did not record all affected-version fields or the query path.

**Resolution:** Reviews must record playbook hash/version, prompt hash/version, model
policy hash, primary and critic model IDs, standard-form hash, and corpus snapshot
version. Add indexes or standard queries for "all reviews under bundle X" and use
`QUARANTINED` and `SUPERSEDED` statuses explicitly.

### 12. Phase 0 issue list was not ready to file

**Severity:** High  
**Lens:** project execution, deployment safety

Several issue bodies still contradicted the architecture: App Runner redeploy on push to
`main`, single-account scoping, and bundled issues too large to review safely.

**Resolution:** Update Phase 0 so `main` produces signed artifacts only; explicit
promotion changes runtime. Keep prod/dev account context from the start. Split oversized
issues by independently testable controls and add dependencies between them.

### 13. Model policy needs one source of truth

**Severity:** Medium  
**Lens:** cost control, evaluation, deployment

The docs mixed "same Opus critic", "Sonnet critic", and "Sonnet fallback" language. The
playbook also used a model shorthand rather than a full policy.

**Resolution:** Maintain one model matrix with `primary_model_id`, `critic_model_id`,
`embedding_model_id`, optional `fallback_model_id`, region, request contract, evaluation
run, and cost assumptions. Reject global **and geo** inference profiles unless explicitly
approved and recorded.

> **Superseded in part by the 2026-06-01 red-team remediation (see Addendum):** the critic
> is now a deliberately *different* model from the primary (Sonnet 4.6 critic against an
> Opus 4.8 primary) for decorrelated errors and lower cost — so a "Sonnet critic" is now
> intended, not a defect. The embedding model is added to the governed matrix.

### 14. Remove unverified model-version examples

**Severity:** Medium  
**Lens:** documentation accuracy

The docs used a specific newer-model example that was not verified against the current
official AWS model-card sources. That made an anti-drift warning look like a factual
model-availability claim.

**Resolution:** Describe the policy generically: any newer model is adopted only after
recertification. Avoid naming unverified future/current models in source docs.

### 15. Evaluation needs stochastic stability gates

**Severity:** Medium  
**Lens:** model quality, CI reliability

The pinned model generation no longer accepts sampling parameters such as `temperature`,
so output determinism cannot be assumed.

**Resolution:** Run each candidate bundle over the gold set multiple times. Require zero
hard-rejection misses, stable redline anchors, and false-positive variance within budget.

### 16. Async observability must cover real workflow failures

**Severity:** Medium  
**Lens:** operations, reliability

High-level API and Bedrock metrics are not enough to run the pipeline.

**Resolution:** Add metrics and alarms for stale `PENDING`/`RUNNING` reviews, per-stage
failures and timeouts, semaphore saturation, abandoned spend reservations, purge
deleted/skipped counts, and audit archive stream lag.

### 17. Endpoint authorization labels must match the allowlist invariant

**Severity:** Medium  
**Lens:** authorization, documentation

Every endpoint except `/health` requires an active, allowlisted user before applying
owner/admin checks. Route tables that say only "signed-in" are too loose.

**Resolution:** Update route labels and tests to enforce:
active + allowlisted, then owner-or-admin or admin-only as applicable.

### 18. Break-glass audit should be automatic

**Severity:** Medium  
**Lens:** audit, IAM, incident response

The break-glass procedure depended on the human operator manually recording the audit
entry after changing admin status.

**Resolution:** Require session tags and reason, constrain the role to the narrow update
path, emit EventBridge/CloudTrail alerts, and automatically append an audit record for
every break-glass assumption or attempted write.

### 19. Extensibility needs a dormant contract now

**Severity:** Medium  
**Lens:** maintainability, future agreement types

"Only the playbook changes" is not true until review requests, corpus retrieval,
standard-form storage, evaluation suites, and cost caps are all namespaced by playbook.

**Resolution:** Add `playbook_id` and agreement type to review creation and storage.
Store standard forms per playbook version, filter corpus by agreement type/scope, and
namespace evaluation suites and cost controls by playbook.

### 20. Generated packets must not be maintained by hand

**Severity:** Medium  
**Lens:** documentation integrity

Self-contained review packets became stale and reintroduced old assumptions such as SQS,
auto-deploy, and single-account scope.

**Resolution:** Either generate packets from source docs on demand or replace committed
packet files with a short non-authoritative notice.

## Verification checklist

- Search active docs for stale terms: `auto-deploy`, `SQS + Step Functions`,
  `disabled`, `both Opus` (the critic is now a *different* model — Sonnet 4.6), `Opus 4.7`,
  raw-text hard-rejection matching, geo/`us.` inference-profile permissiveness, vague
  best-model language, `single account`, and `prompt_version TBD`.
- Validate `contract-toaster/playbooks/eiaa-v1.0.0.json` against
  `contract-toaster/playbooks/schema.json`, including the per-key uniqueness/coverage and
  detector-`kind` gates (no duplicate `section_ref`; each `on_remove_or_alter.required_tokens`
  present in its anchored standard section; `not_in_standard` topics referenced only by
  `on_insert` rules).
- Confirm no active doc contradicts digest promotion, direct Step Functions, separate
  prod/dev accounts, **diff-driven** structured hard rejections, a cross-model critic,
  single-region native inference, internal-only precedent citations, or retention-governed
  model substance.

## Addendum — 2026-06-01 red-team remediation

A second principal-level red-team produced a 28-finding register (2 Critical, 7 High, 8
Medium, 3 Low) addressed in full. Where it updates the findings above, this addendum is
authoritative:

- **C1 / detector redesign.** Hard-rejection detectors now run over the **standard-form
  diff**, not raw text, and split into `on_insert` (additive prohibition, matches inserted
  spans, with `exempt_terms`) and `on_remove_or_alter` (protective invariant, fires on
  deletion/alteration of a standard-form token). This fixes the false-positive storm that
  made clean-`ACCEPT` gold cases impossible. A CI gate asserts **zero** hard-rejection fires
  on the clean standard form and clean-ACCEPT cases. Poorly-fitting rules (one-way
  confidentiality, bare payment terms) are demoted to the LLM + `reject_if_proposed`.
- **C2 / seed playbook.** Duplicate `section_ref` fixed; absent-section topics carry
  `not_in_standard: true` and empty `section_anchors`.
- **Model matrix.** Primary **Opus 4.8**, critic **Sonnet 4.6** (different model),
  embedding model governed; single-region native inference only (geo + global profiles
  forbidden).
- **Corpus snapshots.** Built at the application layer (active store + staging index +
  clause-id manifest + ingestion interlock); Bedrock KB has no native snapshot.
- **Other.** Worst-case spend reservation; CI-eval spend budget; idempotency bucket-boundary
  fix; PENDING reconciler; canonical `reviews` field dictionary; `status`↔`confidence_state`
  mapping; CODEOWNERS review enforced in branch protection; allowlist fails closed;
  PITR 35-day substance tail accepted and documented; minimal public `/health`; deliberate
  prod frontend promotion; group renamed to `legal-admin@example.com`. New docs:
  `docs/playbook-governance.md`, `docs/output-contract.md`, `docs/audit-queries.md`.
