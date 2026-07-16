# Runbook

Operating procedures for `contract-toaster`. Architecture lives in [ARCHITECTURE.md](ARCHITECTURE.md).

## Conventions

- AWS accounts: **prod and dev are separate accounts** (legal-facing data — see [docs/data-handling.md](docs/data-handling.md) and [ARCHITECTURE.md](ARCHITECTURE.md)). v1 deliberately avoids an elaborate multi-account org structure, but production is isolated in its own account. Every command below is account-scoped; confirm which account you are in with `aws sts get-caller-identity` before acting.
  - Dev account: `123456789012`
  - Prod account: `<prod-account-id>` (separate; never holds dev credentials and never reachable from a developer laptop)
- Region: `us-east-1`
- Google Workspace domain: `company.com`
- Production URL: `https://contract-toaster.company.com` (planned; defaults to the service-assigned URL until DNS is wired)
- Repo: `github.com/contract-opf/contract-toaster`
- Container registry: ECR, per account. Production is pinned to an **immutable image digest** — never a `latest` tag and never an auto-mutating branch hook.

## Prerequisites for any operator

```bash
# Required tools
brew install awscli gh node
npm install -g aws-cdk

# AWS credentials
aws configure                    # or use AWS SSO
aws sts get-caller-identity      # confirm <account-id> matches the environment table above

# GitHub
gh auth login
```

You will need IAM permissions at minimum: CloudFormation, S3, DynamoDB, Cognito, App Runner, Amplify, Bedrock (incl. Knowledge Bases + S3 Vectors), Step Functions, ECR, CodeBuild, CloudWatch, CloudTrail, Budgets, IAM (for CDK role creation), KMS, Lambda. There is no SQS in the review entry path — the API starts Step Functions directly (see [ARCHITECTURE.md](ARCHITECTURE.md)). During development, an `AdministratorAccess` permission set assumed via SSO (short-lived credentials, no static keys) is acceptable **in the dev account only**. Production access is a separate SSO permission set in the prod account; tighten before promoting to production.

### Local development

Local development runs against a **synthetic corpus and synthetic documents only**. Production legal documents are never reachable from a developer laptop: prod is a separate AWS account, developer SSO does not grant prod credentials, and dev never holds prod keys. Do not copy, mirror, or point local tooling at prod buckets, the prod corpus, or the prod KB. If you need a realistic document for testing, generate or redact a synthetic one; see [docs/data-handling.md](docs/data-handling.md) for the synthetic-data rules and the rationale.

## Day-one bootstrap (one-time per environment)

The first deploy of an environment requires steps that won't be needed again.

1. **Enable Bedrock model access and record granted quotas.** AWS Console → Bedrock → Model access → Manage model access → enable every model named by the current **model-policy matrix** ([ARCHITECTURE.md](ARCHITECTURE.md) → Model selection): today the pinned single-region **Anthropic Claude Opus 4.8** (primary reviewer), **Claude Sonnet 4.6** (adversarial critic — a deliberately *different* model), the pinned **embedding model** used by the Knowledge Base, plus any explicitly approved fallback model. Wait for status `Access granted`. One-time per-account action; do it in each account (dev and prod). This matrix is a **deliberate pin**, not an automatic "newest/best" selection; any newer model is adopted only after it clears recertification (see "Model recertification" below). Use the **single-region native model ID invoked against the regional endpoint** — verify the exact invocable ID at this step (AWS occasionally renames IDs / version suffixes differ by model). Do **not** use a cross-region inference profile: both `global.` and the geo `us.`/`eu.`/`apac.` profiles are forbidden for residency (a geo profile can route to another region). The request schema omits sampling params (`temperature`, `top_p`, `top_k`) that AWS no longer supports for this model generation.

   **Record the granted on-demand quota** for each pinned model: AWS Console → Service Quotas → Amazon Bedrock → search for "Tokens per minute" and "Requests per minute" for `claude-opus-4-8` and `claude-sonnet-4-6`. Record the granted values in `model-policy/bedrock-us-east-1.json` under `models.primary.granted_tpm`, `models.primary.granted_rpm`, `models.critic.granted_tpm`, `models.critic.granted_rpm`. Then re-derive the `review_throughput_ceiling` and `max_eval_parallelism` fields (formulas are inline in the artifact). Commit the updated artifact in a PR (CODEOWNERS requires admin/GC approval). The eval harness reads `max_eval_parallelism` to set its token-bucket rate limit — an unrecorded or stale quota value causes the harness to throttle or under-utilize capacity. If the default quota is too low for the eval harness, request an increase via AWS Support before running CI evaluations.

2. **Confirm Google OAuth client.** Cognito needs Google OAuth client credentials (client ID + secret). These come from Google Cloud Console → APIs & Services → Credentials → an OAuth 2.0 client of type "Web application", with authorized redirect URI matching the Cognito hosted-UI domain. The client ID and secret are stored in AWS Secrets Manager under `contract-toaster/cognito/google-oauth`; CDK reads from there.

3. **Reserve the DNS name.** If using `contract-toaster.company.com`, ensure the parent zone exists in Route 53 (or wherever your organization manages DNS) and that a delegation or CNAME can be added later. Not required for first deploy.

4. **Bootstrap CDK.** From `infra/`:
    ```bash
    cdk bootstrap aws://<account-id>/us-east-1
    ```

5. **Set stack parameters.** Edit `infra/cdk.context.json` with the initial admin email (the GC's `@company.com` address), then commit.

6. **First deploy.** Provision infrastructure first, then promote an image (the two are separate concerns now — infra is CDK, the running image is a pinned digest):
    ```bash
    cdk synth
    cdk deploy --all
    ```
    This stands up ECR, the CodeBuild pipeline, and the App Runner service with **no** image pinned. Trigger the first CI build (push to `main` runs CodeBuild → tests/scans → signs the image → pushes to ECR), then promote the resulting digest as described in "Deploying a code change". Note: CDK must **not** read OAuth secrets at synth time — they are resolved at runtime via dynamic references, so synthesized templates never contain secret material.

7. **Verify health.** Visit the App Runner URL `/health` (public) and confirm liveness. For the deployed version, commit SHA, and serving **image digest**, call the **allowlisted `/version`** endpoint with a valid token (build details are not exposed unauthenticated). App Runner deployment metadata also shows the serving digest.

8. **Verify Cognito.** Visit the Amplify URL, sign in with a `@company.com` Google account, confirm successful sign-in and that a row exists in DynamoDB `users`.

9. **Seed the first playbook.** From the admin UI, upload `playbooks/eiaa-v1.0.0.json` as a **draft** version 1.0.0 of the `eiaa` playbook. It is not active until the release bundle is activated: playbook hash, prompt hash, canonical standard-form hash, anchor-map hash, model-policy hash, corpus snapshot, evaluation run ID, and Legal approval. (Once we have a corpus, seed it as a draft corpus snapshot and activate only after curation and retrieval regression checks pass.)

10. **Verify alarm delivery (bootstrap acceptance — required before declaring prod operable).** An unconfirmed SNS email subscription receives no alarm mail — silent monitoring failure is the exact defect this step closes. Two sub-steps, both required:

    a. **Confirm the SNS email subscription.** After CDK deploys the SNS topic and its email subscription targeting `legal-eng@company.com`, AWS sends a confirmation email to that address. Open the email and click **Confirm subscription**. Verify in the AWS Console (SNS → Topics → `contract-toaster-alarms` → Subscriptions) that the subscription status shows **Confirmed**, not **PendingConfirmation**. Do not proceed to sub-step (b) until status is **Confirmed** — an unconfirmed subscription will silently drop every alarm notification.

    b. **Send a test alarm and confirm receipt.** Force one alarm into ALARM state to prove end-to-end delivery:
       ```bash
       # Substitute the real alarm name as deployed (visible in CloudWatch → Alarms)
       aws cloudwatch set-alarm-state \
         --alarm-name contract-toaster-app-runner-5xx-<env> \
         --state-value ALARM \
         --state-reason "Bootstrap acceptance test — verifying SNS→email delivery" \
         --region us-east-1
       ```
       Wait up to 5 minutes. Confirm that an alarm notification email arrives at `legal-eng@company.com`. After confirming receipt, restore the alarm:
       ```bash
       aws cloudwatch set-alarm-state \
         --alarm-name contract-toaster-app-runner-5xx-<env> \
         --state-value OK \
         --state-reason "Bootstrap acceptance test complete — restoring to OK" \
         --region us-east-1
       ```
       Record the date, the alarm name used, and the name of the person who confirmed receipt in the environment's bootstrap log (or in the PR/issue that tracks this deployment). This record is the documented proof that monitoring is live and reachable. The environment is not considered prod-operable until this receipt is confirmed and recorded.

## Routine operations

### Deploying a code change

**A merge to `main` does not change production.** Merging only triggers CI; promotion to prod is a deliberate, separate step. This is intentional — production renders legal output, so no merge may silently alter its behavior.

The flow:

```bash
# 1. Make the change
git checkout -b phase-N/short-description
# ... make changes, commit ...
gh pr create
# ... review, merge to main ...
```

```text
# 2. CI runs automatically on main (CodeBuild or equivalent):
#    - runs tests and security scans
#    - builds the container image
#    - SIGNS the image
#    - pushes it to ECR
#    The build records an immutable image DIGEST (sha256:...). Note it from the
#    CodeBuild logs or `aws ecr describe-images`.
```

```bash
# 3. Promote that digest to an environment (dev first, then prod after sign-off).
#    Promotion pins the App Runner service to the exact, signed digest — never a
#    tag, never "latest". Use the promotion helper / CDK context, e.g.:
cd infra
cdk deploy --context imageDigest=sha256:<digest> --context env=dev
# verify in dev, then:
cdk deploy --context imageDigest=sha256:<digest> --context env=prod
```

Promotion verifies the image signature before App Runner is repointed; an unsigned or tampered digest is refused. Confirm the authenticated `/version` endpoint (or App Runner deployment metadata) reports the digest you promoted. Production is only ever mutated by an explicit promotion of a named digest — nothing auto-mutates it.

For infrastructure changes (`infra/`):

```bash
cd infra
cdk diff                         # preview
cdk deploy --context env=<dev|prod>   # apply (account-scoped)
```

Run `cdk diff` after any merge that touches `infra/` to see what a deploy would change. Infra changes do not flow automatically either — they are applied per account with an explicit `cdk deploy`.

### Rolling back a code deploy

Rollback is **re-promotion of the last-known-good digest**, not a git revert. The previous image is still in ECR.

```bash
# Find the digest that was serving before the bad promotion:
#   - the previous /version value you recorded, or
#   - `aws ecr describe-images --repository-name contract-toaster` history, or
#   - the promotion audit entry.
cd infra
cdk deploy --context imageDigest=sha256:<previous-good-digest> --context env=prod
```

App Runner repoints to the prior signed digest immediately; the bad image stays in ECR for forensics. Only after the service is stable do you fix forward: revert the offending commit on `main`, let CI produce a new signed digest, and promote that. Do not delete ECR images during an active rollback.

For infrastructure: identify the previous stable revision of `infra/`, check it out, and `cdk deploy --context env=prod`. CloudFormation handles the rollback; do not delete and recreate resources by hand.

### Revising the standard form

The canonical standard form (in `standard-forms/`) is versioned in lockstep with
the playbook.  A revision to the form must go through this procedure — an informal
edit breaks the content-address guarantee and will fail the heading-hash drift and
form-coverage CI gates.

**When to use this procedure:** any time your canonical EIAA standard form
`.docx` changes — including wording edits, renumbering, adding or removing sections,
or restructuring §10 sub-clauses.

**Version-bump semantics** (what "versioned in lockstep" means for semver):

| Type of standard-form change | Playbook semver bump required |
|------------------------------|-------------------------------|
| Wording edit to a section that changes the `heading_hash` of any anchor | **minor** (new section headings require a new anchor_map_hash) |
| Adding a new section that needs a new topic | **minor** |
| Removing a section that had a topic | **minor** |
| Renumbering sections (changes `heading_hash` or anchor assignments) | **minor** |
| Structural restructuring (e.g., splitting or merging §10 sub-clauses) | **major** |
| Typo / formatting corrections that do NOT change any `heading_hash` | **patch** |

A **patch** bump is the only case that does not require a new anchor migration
record; confirm by running `python3 tests/anchor/test_heading_hash_drift.py` — it
must pass before committing.

**Procedure:**

1. **Edit the `.docx`.** Make the intended change in Word or LibreOffice.  Do not
   commit until the steps below are complete.

2. **Rebuild the anchor map.**
   ```bash
   pip install python-docx          # one-time
   python3 scripts/build_anchor_map.py --docx standard-forms/contract-toaster-vX.Y.Z.docx
   ```
   Note the printed `anchor_map_hash` and `standard_form_hash`.

3. **Identify heading-hash drift.** Compare the new anchor map with the previous
   one (diff the `anchors` block).  Any anchor whose `heading_hash` changed is a
   **drift candidate** and requires a migration record.

4. **Author migration records** for each drifted anchor.  Add an entry to
   `anchor_migrations` in `playbooks/contract-toaster-vX.Y.Z.json`:
   ```json
   {
     "anchor": "sec-8",
     "from_heading_hash": "sha256:<old-hash>",
     "to_heading_hash":   "sha256:<new-hash>",
     "from_standard_form_hash": "sha256:<old-form-hash>",
     "to_standard_form_hash":   "sha256:<new-form-hash>",
     "reason": "Section renumbered from §8 to §9 in v1.1 to accommodate new Insurance section.",
     "approved_by": "Marc Mandel, General Counsel",
     "approved_at": "2026-XX-XXTXX:XX:XXZ"
   }
   ```
   **GC sign-off is required** on each migration record — adding a record is a
   legal-content change and requires GC approval (enforced by CODEOWNERS on
   `playbooks/`).

5. **Update `coverage_exempt_anchors`** if the revision adds a section that has
   no playbook topic and should remain exempt (e.g. a preamble, signature block,
   or a structural parent heading). `coverage_exempt_anchors` and
   `coverage_exempt_rationales` are **canonical in the anchor map**
   (`standard-forms/contract-toaster-vX.Y.Z.anchor-map.json`), as siblings of `anchors` — not
   in the playbook. Add the anchor and a reviewed rationale there. An exemption is
   a property of the standard-form section, so it is governed alongside the form.

6. **Update the release bundle fields** in the playbook:
   - `playbook.release.standard_form_hash` ← the new `standard_form_hash`
   - `playbook.release.anchor_map_hash`     ← the new `anchor_map_hash`

7. **Run CI gates locally:**
   ```bash
   python3 tests/anchor/test_heading_hash_drift.py
   python3 tests/anchor/test_form_coverage.py
   python3 tests/detector/test_empty_scope_gate.py
   python3 tests/detector/test_d2_new_section_violations.py
   ```
   All must pass before opening a PR.

8. **Open a PR** with the `.docx`, the new anchor map, and the updated playbook.
   CODEOWNERS requires GC + engineering review.  The heading-hash drift gate and
   form-coverage gate run in CI on every change to `standard-forms/` or `playbooks/`.

9. **Activate the new release bundle** after all gates pass and legal approval is
   recorded.  Past reviews remain pegged to the bundle they ran against; reviews
   that ran under a bundle whose standard form is now revised are **not** retroactively
   quarantined unless the GC determines the revision changed the legal position
   in a material way (in which case follow the "Rolling back a bad playbook or
   prompt" procedure).

### Adding a new playbook version

This is the canonical Contract Toaster review-improvement loop.

1. From the admin UI, click **Download current** to get the current `contract-toaster-vX.Y.Z.json`.
2. Edit locally.
3. Bump the version field: patch for clarifications, minor for new topics or footnote templates, major for structural changes.
4. From the admin UI, **Upload new version**. The server validates against `playbooks/schema.json` before accepting and stores it as `draft`.
5. Run the release gates: schema validation, detector coverage, prompt/gold-set regression, stochastic stability, redline fixtures, retrieval/corpus checks where applicable, and Legal approval over the full release bundle.
6. Activate the release bundle deliberately. Only after activation do future reviews use it. Past reviews remain pegged to the bundle they were run against.

To revert to an earlier version: from the version history view, click **Revert** on the desired release bundle. This creates a new active bundle that points back to the earlier content-addressed artifacts, preserving the audit trail.

### Rolling back a bad playbook or prompt

When an active playbook or prompt version is producing wrong output (a hard rejection that should not fire, a missed rejection, a leaked-policy summary), roll it back rather than scrambling to author a fix under pressure.

1. Admin UI → Release bundles → version history → **Roll back** on the last-known-good bundle. This is one click: it sets that bundle `active` again and demotes the bad bundle to `retired`. The content-addressed snapshot of the bad bundle is preserved, not deleted.
2. The rollback writes an `audit` entry recording actor, the bad version hash, the restored version hash, and a free-text reason.
3. Every review run under the bad bundle is **automatically quarantined** — status `QUARANTINED`, flagged so its decision is not relied upon. Query `reviews` by the release-bundle hash or by any component hash (`playbook_hash`, `prompt_hash`, `standard_form_hash`, `model_policy_hash`, `corpus_snapshot_version`) to find the affected population.
4. **In-flight reviews under the bad bundle (RUNNING at rollback time) are not aborted.** They run to completion so the audit trail and spend ledger stay consistent. The initial rollback sweep quarantines all already-terminal reviews; a **second quarantine sweep keyed by bundle hash** runs continuously (triggered on each pipeline-terminal write and on a short-interval schedule) and quarantines any review whose release-bundle component hash matches the bad bundle, regardless of current status — this catches reviews that were `RUNNING` at rollback time and subsequently land `DONE`. You do not need to manually find and quarantine these late completions; the sweep handles them. If you need to confirm the sweep has run, check the admin UI → Release bundles → bad bundle → "quarantined reviews" count, which should include any late completions within minutes of their landing `DONE`.
5. **Re-run** quarantined reviews against the restored bundle once it is active. Re-running creates new review records pegged to the good bundle; the quarantined originals are marked `SUPERSEDED` and retained for audit. Notify any attorney who already received a quarantined output.

Rollback is the immediate control; authoring a corrected new version comes after the bad one is out of service.

### Suspending intake (deactivate without a successor)

Use this procedure when you need to take the tool out of service entirely — for example, when the **first-ever bundle is found bad and there is no prior bundle to roll back to**, or when you must suspend review intake during a quarantine investigation and you do not yet have a replacement bundle to activate.

**When to use deactivate vs. rollback.**

| Situation | Action |
|-----------|--------|
| A bad bundle exists and a known-good prior bundle is available | **Rollback** (see "Rolling back a bad playbook or prompt" above) |
| The first-ever bundle is bad (no prior bundle to revert to) | **Deactivate** (this section) |
| Intake must be suspended during an investigation, no replacement is ready yet | **Deactivate** (this section) |

**Procedure:**

1. **Deactivate the active bundle.** Admin UI → Release bundles → the currently active bundle → **Deactivate**. This action requires GC approval (the same admin-approval level required to activate). Confirm the action with a free-text reason (e.g., "first-ever bundle found bad — v1.0.0 misses clause X; suspending intake pending v1.0.1").
2. The deactivate action writes an `audit` entry recording the actor, the deactivated bundle hash, the reason, and the timestamp. The deactivated bundle transitions to `retired` status (its content-addressed snapshot is preserved).
3. **Intake is now suspended.** Any attempt to submit a new review via `POST /api/reviews` will immediately receive HTTP `503` with the message **"no active playbook"**. Communicate to reviewers that the tool is temporarily unavailable.
4. **In-flight reviews (PENDING or RUNNING at the time of deactivation) are not aborted.** They continue to completion — their bundle was resolved and recorded at submission, so they run against the bundle that was active when they were submitted. Wait for any in-flight reviews to complete before declaring the environment fully quiesced, if that matters for your investigation.
5. **Quarantine reviews if necessary.** If the deactivated bundle was producing bad output, quarantine affected reviews via Admin UI → Release bundles → the retired bundle → "Quarantine associated reviews". This marks them `QUARANTINED` and prevents attorneys from relying on those outputs. Notify any attorney who already received output from the bad bundle.
6. **Author and activate a replacement bundle.** Once the underlying problem is resolved, upload a new playbook version, run the release gates (schema validation, detector coverage, prompt/gold-set regression, legal approval), and activate the new release bundle. Activation clears the no-active-bundle state immediately — new reviews can be submitted from that point on.
7. **Re-enable intake announcement.** After activation, notify reviewers that the tool is back in service.

**Re-enabling intake without a new bundle version** (if deactivation was precautionary and the existing bundle is found good on closer inspection):

1. Admin UI → Release bundles → the retired bundle → **Re-activate**. This re-promotes it to `active` status and clears the no-active-bundle state.
2. Confirm in the audit log that the re-activation entry records the actor and reason.
3. Run the bootstrap-acceptance smoke test (`GET /health` → `{"status":"ok"}`; submit a synthetic test review) to confirm the service is healthy before notifying reviewers.

### Onboarding a reviewer

This is the canonical admission path (see [ARCHITECTURE.md](ARCHITECTURE.md) → Authentication). A reviewer is anyone with access to submit documents for review; they are not admins. The pre-token Lambda checks `legal-admin@company.com` group membership and JIT-creates the active `users` row on first sign-in — there is no other non-bootstrap admission path.

> **Group-naming note:** The group `legal-admin@company.com` is a **misnomer** — it covers all ContractToaster users, not only admins. Non-admin reviewers must be in this group. The `is_admin` flag in the DynamoDB `users` row is the sole admin-privilege gate; group membership is only the access allowlist. See [ARCHITECTURE.md → Authentication](ARCHITECTURE.md#authentication--cognito-federated-to-google) for the authoritative note.

1. **Add to the Google group.** In Google Workspace admin console, add the user's `@company.com` account to the `legal-admin@company.com` group. The user must be in the group **before** they sign in; the pre-token Lambda will deny sign-in for a user not yet in the group.
2. **User signs in.** The user visits the Amplify URL and signs in via Google SSO. On successful sign-in, the pre-token Lambda confirms group membership and creates an `active` row in DynamoDB `users`.
3. **Verify the row.** Admin UI → Users → confirm the new user appears with `status=active`. The user can now submit documents for review.

If sign-in is denied with a `403` after the group add, confirm the group membership is visible in the Directory API (propagation can take a few minutes). Do not attempt to create the `users` row manually — let the Lambda create it on sign-in.

To revoke reviewer access, see "Deprovisioning a user" below or wait for the next sync run (≤ 1 hour) after removing them from the group.

### Adding a new admin

The admission path for a new admin is identical to onboarding a reviewer (above): the user must be in the `legal-admin@company.com` group before signing in. After sign-in creates their `active` row, an existing admin elevates them.

1. **Add to the Google group.** In Google Workspace admin console, add the user's `@company.com` account to the `legal-admin@company.com` group. This step must happen before sign-in — the pre-token Lambda checks group membership and will deny sign-in for a user not yet in the group.
2. **User signs in once via Google SSO.** The pre-token Lambda confirms group membership and creates their `users` row with `is_admin=false`.
3. **Existing admin elevates them.** Admin UI → Users → find the new user → toggle **Admin**.
4. The change is logged in `audit`.

There is no other path to admin. Do not edit the DynamoDB `users` table directly except in the documented emergency procedure below.

### Deprovisioning a user

Domain membership is not authorization (see [ARCHITECTURE.md](ARCHITECTURE.md) → Authentication), so removing access is an explicit action — a terminated or transferred employee must not retain access through a lingering token or a JIT-created row.

1. Admin UI → Users → find the user → set **status** to `suspended` for temporary removal or `deprovisioned` for a terminated/transferred user. This is the authoritative gate: the API rejects every request from a non-`active` user regardless of token state, and the next poll/upload fails closed.
2. **Revoke tokens.** Suspension/deprovisioning triggers Cognito global sign-out / token revocation for that user, so an outstanding access or refresh token cannot be replayed. Confirm the user can no longer reach an authenticated endpoint.
3. **Source of truth is the directory.** A periodic sync from Google Workspace/SSO marks users who are no longer in the directory (or no longer in the required allowlist/group) as `deprovisioned` automatically; this catches departures that never came through the admin UI. The sync also re-checks last-auth so dormant accounts surface for review. Do not rely on the sync alone for an urgent removal — suspend or deprovision in the UI immediately, then let the sync confirm.
4. **In-flight reviews.** Suspending or deprovisioning a user does not abort their running Step Functions executions — those finish and write their results so the audit trail stays complete. The non-active user simply cannot start new reviews, poll, or download outputs. If a security incident requires stopping in-flight work, stop the specific executions in the Step Functions console and mark the reviews `ERROR`.
5. The status change and any token revocation are logged to `audit`.

To re-enable a returning employee, set status back to `active`; they sign in normally and the directory sync stops flagging them.

### Adding a document to the corpus

1. Admin UI → Corpus → Upload.
2. Select the `.docx`, choose document type (`executed-final`, `accepted-draft`, `rejected-draft`), counterparty name, and date.
3. The upload triggers a **Bedrock Knowledge Base ingestion job** (chunk → embed → index into the S3 Vectors store). Wait for the job to report complete.
4. The result is a **draft corpus snapshot**, not yet authoritative. Legal curates clause metadata (`reusable_precedent`, `approved_use_scope`, polarity, supersession), then retrieval regression and leakage tests run against the candidate snapshot.
5. Activate the snapshot deliberately. Future reviews retrieve only from the active corpus snapshot and record that snapshot version.

No "retrain" step. The model itself is unchanged; only the retrieved precedent set changes.

### Viewing the audit log

Admin UI → Audit. Filter by user, by action type, by playbook version, by date range. Export as CSV.

For deeper queries (e.g., "every review that used playbook 1.0.0 and was REQUEST_CHANGE"), the table is also queryable via DynamoDB queries; see [docs/audit-queries.md](docs/audit-queries.md) for the standard query catalogue.

### Changing document retention

Admin UI → Settings → **Document retention** slider (0 days–3 years, default 90). On save:

- The retention purge worker deletes documents in `uploads`/`outputs` older than the new window, but only for **terminal** reviews and only for files **not under legal hold**. Setting `0` is a purge of all eligible stored documents — it will **not** touch documents whose review is still running or that are on hold.
- Each review snapshots the retention setting in force when it was created; changing the slider does not retroactively re-govern a review's documents against a stricter window mid-pipeline. Active executions are excluded from any purge.
- This affects documents and matched confidential substance fields. Per-review non-substantive facts in `reviews` (decision, cost, hashes, timestamps) are retained indefinitely and are unaffected. Confidential *substance* — clause text, rationale, model summaries, substantive critic deltas — is cleared or deleted with the document unless legal hold applies; see [docs/data-handling.md](docs/data-handling.md).
- The action is logged to `audit`. Lowering the window is destructive and cannot be undone — confirm before saving.

**Dual-control requirement for retroactive reductions.** A retroactive reduction — lowering the retention window below its current value — is a single-admin, immediately destructive action on unversioned S3 objects (deletes are real and irreversible). To prevent accidental or compromised-session destruction, retroactive reductions require one of the following before the sweep runs:

- **Second admin confirmation.** A different admin must confirm the reduction in the UI within the current session. The confirmation is logged to `audit` with both actors' identities, the old and new window values, and a free-text reason.
- **72-hour delay with GC alarm.** If a second admin is unavailable, the reduction enters a *pending* state for 72 hours. A CloudWatch alarm immediately notifies the GC (General Counsel) — the `contract-toaster-retention-reduction` alarm fires, sending an SNS notification to the GC's email — so Legal is aware before the sweep runs. The sweep executes automatically at the end of the delay unless cancelled by any admin. Cancellation is logged to `audit`.

Forward-looking changes — raising the window, or setting a future-effective date — are not retroactive reductions and continue to apply single-admin with immediate effect. Moving **to** `forever` is a forward-looking change (single-admin, immediate); moving **away from** `forever` to any finite window is always a retroactive reduction and requires dual control, same as any other reduction.

The dual-control / delay requirement is enforced server-side; the API rejects a retroactive-reduction sweep that lacks either a second-admin confirmation token or the mandatory delay. The authoritative policy and purge invariant are in [docs/data-handling.md](docs/data-handling.md) (purge invariant 5). The threat context (admin-UI XSS reaching this path) is in [docs/threat-model.md](docs/threat-model.md) → Malicious admin or compromised session.

**`forever` / indefinite preservation (issue #34).** In addition to the bounded 0-day–3-year window, `POST /api/admin/retention` accepts the literal setting `"forever"` — records snapshotted at it are never purge-eligible, at any age. Use it for a class of review that must stay answerable indefinitely (e.g. executed agreements a GC wants recoverable years later) rather than relying on the 90-day default. `GET /api/admin/retention` returns the full set of selectable options (`window_options`, including `forever`) alongside the current and default windows. The admin-UI slider surfacing this option as a selectable choice (rather than the API alone) is tracked separately.

### Placing and releasing a legal hold

A legal hold **overrides document retention**: a held review or held corpus document is never purged, regardless of the retention slider or a `0`-day setting. This exists to prevent evidence destruction; the authoritative policy lives in [docs/data-handling.md](docs/data-handling.md) → Legal hold.

**To place a hold:**

1. Admin UI → find the **review** (or the **corpus** document/collection) → **Place legal hold**, with a matter reference and reason.
2. This sets the per-review (or per-corpus) `legal_hold` flag. The purge worker skips anything with the flag set, even documents far past the retention window and even at retention `0`.
3. The storage layer is marked too: held S3 objects receive an Object Lock legal hold or a protected hold tag covered by bucket-policy denies. Application roles cannot delete held objects by bypassing the app.
4. The action is logged to `audit` with actor, target, matter reference, and reason.

**To release a hold:**

1. Only release on explicit instruction from counsel managing the matter. Admin UI → the held item → **Release legal hold**, recording who authorized release.
2. On release, the item returns to normal retention governance — it is **not** purged immediately; it becomes eligible for purge under the current window at the next worker run.
3. The release is logged to `audit`.

If a retention change and a hold ever appear to conflict, the hold wins. Never work around a hold by hand-deleting objects or bypassing object lock.

**Governance-bypass rule.** `s3:BypassGovernanceRetention` is reserved for MFA break-glass only. Any bypass session must carry a reason/ticket tag, emits CloudTrail/EventBridge alerts, and requires post-incident review. Do not use governance bypass to remove an object under legal hold; release the hold through the documented workflow first.

### Adjusting the daily spend ceiling

Admin UI → Settings → **Daily spend ceiling** (default `$20/day`). The dashboard shows today's spend against the ceiling. Once the cap is reached, new reviews are refused with a clear message until the next day; in-flight reviews finish.

The cap is enforced by an **atomic reservation**, not a pre-check: before a pipeline starts, the API conditionally increments a daily DynamoDB spend counter by a **worst-case upper-bound** estimate (max input + output tokens × both passes × uncached pricing — the real token cost is not known until extraction, which happens later in the pipeline); if that would exceed the ceiling the increment fails and the review is refused. Actual cost is **settled** against the counter after the run (every model attempt is ledgered, including failures and retries), correcting the estimate downward. Reserving the *worst case* is what makes concurrent submissions safe — several uploads at once cannot collectively overshoot the cap before any settles. Note this also means the cap admits ~9 worst-case reviews per day at the default `$20` (comfortably above the expected 2–7/day peak); the documented max-reviews/day figure lives with the per-review caps. The same reservation/ledger also governs **CI evaluation** Bedrock spend so the harness cannot bypass the ceiling.

**Users hitting the daily cap mid-day.** When a user receives a "daily limit reached" error on a legitimate review (not just a retry), the ceiling is full. Diagnose and resolve in this order:

1. **Check for phantom reservations first.** Admin UI → Cost ledger → today's reservations. Compare the reserved total to the sum of settled, ledgered attempts for the day. A persistent gap with no active execution behind it is an abandoned (phantom) reservation inflating the counter. Resolve it via the admin reconcile action, which settles that review's reservation to its actual cost. Do not edit the counter by hand — a blind edit races live reservations.
2. **If no phantoms, the cap is legitimately full.** At the default `$20/day` ceiling, ~9 worst-case reviews exhaust the budget (see [ARCHITECTURE.md](ARCHITECTURE.md) → Cost shape for the arithmetic). If the daily volume is regularly hitting this, raise the ceiling:
   - Admin UI → Settings → **Daily spend ceiling** → increase (e.g. to `$50/day`, which allows ~23 worst-case or ~55 typical reviews per day while still bounding blast radius).
   - The new ceiling applies immediately; in-flight reviews unaffected.
3. **After a ceiling raise, reconcile.** If phantom reservations also contributed, reconcile them after the raise so tomorrow starts clean.
4. Log the ceiling change to the audit record (the change writes an audit entry automatically).

If the cap fills during an abnormal burst (suspected runaway or injection attempt), do **not** raise the ceiling — leave it in place and investigate the cost outlier in the audit log.

**Clearing a stuck reservation.** If a pipeline dies between reserving and settling (e.g., the execution is aborted before the settle step), its estimate can sit on the counter as "phantom" spend, making today look more expensive than it is. To diagnose: compare the counter's reserved total against the sum of settled, ledgered attempts for the day. A persistent gap with no active execution behind it is an abandoned reservation. Resolve it by settling that review's reservation to its actual (often zero) cost via the admin reconcile action — do not edit the counter by hand, because a blind edit races live reservations. The daily counter resets at the day boundary, so a stuck reservation self-clears the next day; reconcile sooner only if it is blocking legitimate reviews under the cap.

### Duplicate submissions and idempotent retries

`POST /api/reviews` is **idempotent** by design (see [ARCHITECTURE.md](ARCHITECTURE.md)). A retry — client double-click, network timeout, mobile re-send — carries the same idempotency key: a **client-supplied key** if the client sends one (the robust path), otherwise one derived from uploader + file hash + active release-bundle hash + a fixed-width timestamp bucket. Because a derived key would change at a bucket boundary, the API checks **both the current and previous bucket** for an existing submission before creating one, so a retry that straddles a boundary still collides instead of double-running. The API first creates or fetches a submission record. Spend is reserved once per `review_id`, the upload pointer and execution ARN/status are stored on that record, and retries perform an idempotent "ensure execution started" against the deterministic Step Functions execution name. A duplicate does not start a second pipeline or reserve spend twice: it returns the **existing** review. A deliberate **re-review of the same file is an explicit "review again" action** that mints a fresh key on purpose — it is not something a retry can trigger. There is no SQS buffer or DLQ on the entry path to inspect — the API talks to Step Functions directly.

Operationally this means: if a user reports "I submitted twice," expect to find **one** submission, **one** review, and one execution in the Step Functions console. If a submission has no execution ARN, rerun the admin "ensure execution started" action; do not create a second review. If you genuinely see two executions for the same submission, that is a bug (the key was not stable) — capture the two execution names and the request log for that review ID rather than just deleting one.

### Model recertification (quarterly)

The model is governed by an explicit **model-policy matrix** ([ARCHITECTURE.md](ARCHITECTURE.md) → Model selection), not an automatic "newest/best" choice. The pinned matrix (today Opus 4.8 primary, Sonnet 4.6 critic, the pinned embedding model, in `us-east-1`, with any fallback separately approved) is re-examined **every quarter**, and unconditionally whenever AWS announces a model change affecting our pin. Recertification covers the **embedding model** too — a change to it (or a re-embedding) requires admin (GC) approval and a new corpus snapshot version, because it changes retrieval and therefore legal output.

1. Review what AWS now lists in Bedrock for the region. New entries do **not** auto-adopt — the current pin stays in force until a candidate clears this process.
2. If considering a change, run the candidate through the evaluation harness / gold test set (see [ARCHITECTURE.md](ARCHITECTURE.md) and the eval docs) and compare decisions, false-positive rate, and redline-patch behavior against the incumbent. Recertification is a gated regression test, not a console toggle.
3. Re-confirm the **request contract** for the chosen model: exact pinned **single-region native** model ID (never a `global.`/`us.`/`eu.`/`apac.` inference profile), and the request schema with unsupported sampling params omitted (`temperature`, `top_p`, `top_k`; verify the equivalent for any candidate). Account for adaptive-only extended thinking if used.
4. **Re-verify and record the granted on-demand quota** for each pinned model (AWS Console → Service Quotas → Amazon Bedrock). Update `model-policy/bedrock-us-east-1.json` with the current `granted_tpm` and `granted_rpm` values and re-derive `review_throughput_ceiling` and `max_eval_parallelism` (derivation formulas are inline in the artifact). If quota has changed — either increased by a prior request or adjusted by AWS — update the eval harness rate-limit settings before running CI. An outdated quota figure causes the harness to throttle (too aggressive) or waste CI time on unnecessary serialization (too conservative). This step is **required** each quarter even when the model pin is unchanged.
5. Record the recertification outcome — primary model ID, critic model ID, optional fallback, granted quota figures, derived throughput ceiling and eval parallelism, date, who certified, eval results, and cost assumptions — in `audit`, even when the outcome is "no change; pin reaffirmed".
6. Adopting a new model is a config change that ships through normal CI and promotion (build → sign → ECR → promote a digest), not a hot edit in the console.

## Reviewer workflow guidance

### Counterparty sent a PDF — what to do

v1 accepts `.docx` only. If a school or counterparty sends a PDF, the tool rejects it with a format-specific message (not the generic hostile-file error — see [ARCHITECTURE.md → Wrong-format rejection UX](ARCHITECTURE.md#wrong-format-rejection-ux--pdf-and-legacy-doc-v1-scope)). The reviewer should take one of the following paths:

**Preferred path — request the .docx original from the school.**
Contact the school or counterparty and ask them to send the Word (.docx) document directly. Most schools author the agreement in Word and have the `.docx` available; the PDF is often an export for transmittal. Email template:

> "Thank you for sending the agreement. To process it through our review tool, we need the original Word (.docx) file rather than the PDF export. Could you please send the .docx version?"

This is the recommended path. A `.docx` sourced directly from the school retains any tracked changes the school applied, which is important for the review.

**Fallback — PDF-to-.docx conversion (use with caution).**
If the school cannot or will not provide the `.docx` original, a PDF can be converted to `.docx` using Word (File → Open → select the PDF) or Adobe Acrobat (Export → Microsoft Word). **Important caveats before submitting a converted file:**

- **Tracked changes are not preserved through PDF conversion.** A PDF is a flat rendered representation; it does not carry the `.docx` revision history. A converted `.docx` will show the school's changes as plain text, not as tracked-change marks. The review tool will still diff against your standard form and flag deviations, but the attorney should be aware that the revision history visible in a native `.docx` is absent.
- **Conversion fidelity varies.** Complex tables, special characters, footnotes, and non-standard fonts can convert imperfectly. Review the converted document for obvious formatting errors before submitting.
- **Scanned PDFs (image-only) require OCR.** A scanned PDF contains no machine-readable text; Word's PDF import will attempt OCR, but accuracy varies. Prefer the `.docx` original if the school can provide it.

After conversion, open the `.docx` in Word, do a quick visual pass, and then upload it. Note in your review record that the file was obtained via PDF conversion (not from the school's `.docx` original) so the context is preserved.

**Counterparty sent a legacy `.doc` file (Word 97–2003).**
The tool also rejects legacy `.doc` binary files with a tailored message. The fix is straightforward: open the `.doc` in Word, save it as `.docx` (File → Save As → Word Document), and re-upload. Tracked changes and most content should survive this conversion intact; verify visually before submitting.

### Removing the export marker from an approved redline

Every generated redline `.docx` carries an **internal-only / export-warning marker** ("tool recommendation only — attorney approval required; do not send externally before attorney approval") placed redundantly in two locations:

1. A **first-page cover note** — a dedicated cover page at the front of the document.
2. A **running every-page header/footer** — text in the header and footer of every page.

The redundant placement is intentional misuse friction: a routine accept-all-changes clears inline tracked-change paragraphs but does not strip the header/footer. An attorney cannot accidentally forward an unapproved document without the marker being visible.

**When to use this procedure.** Only after an attorney has approved the redline — reviewed it, made any necessary edits, and is ready to send the final version to the counterparty. Do not remove the marker from a document that has not received explicit attorney approval. The marker is the default on every generated redline; removal is the deliberate approval exit, not the routine download step.

**Procedure:**

1. **Confirm attorney approval.** Verify that the reviewing attorney has approved this specific redline (reviewed it and decided it is ready for the counterparty). If approval is unclear, obtain explicit sign-off before proceeding.

2. **Open the `.docx` in Word.** Open the document that was downloaded from the Contract Toaster review tool.

3. **Remove the cover page.** Delete the entire first-page cover note. The cover page is a standalone page at the front of the document containing the internal-only/export-warning notice. Select all content on that page (including any section break that follows it) and delete it.

4. **Remove the header/footer marker.** Open the header and footer (double-click the header area, or Insert → Header & Footer). Delete the export-warning text from both the header and the footer. Check all header/footer sections — if the document has different first-page or odd/even page headers/footers, clear the marker from each distinct section. Close the header/footer editor.

5. **Verify no marker fragments remain.** Use Word's Find (Ctrl+F / Cmd+F) to search for key phrases such as "tool recommendation only", "attorney approval required", and "do not send externally". Confirm zero matches. A missed fragment in a header/footer or a section break boundary is the most common error; scroll through the document to confirm.

6. **Save the clean copy with a distinct filename.** Save the de-marked document as a new file (e.g., `<original-name>-approved-clean.docx`) so the original marked version is preserved alongside it. Do not overwrite the marked original — keep it so the review record remains intact.

7. **Record the action.** Note in your review record (review disposition, email thread, matter management system, or equivalent) that you removed the export marker from review `<review-id>` on `<date>`, with the name of the approving attorney. This is the audit trail for the de-marking step: the tool does not automatically record marker removal, so the attorney's record is the authoritative log of the approval and clean-export action.

**What not to do.**

- Do not use accept-all-changes as the de-marking step: accept-all clears tracked-change markup but does not remove header/footer content. The every-page header/footer marker survives accept-all — this is intentional.
- Do not edit the `.docx` while in tracked-changes mode if you can avoid it: stray tracked changes in the header/footer can leave the marker visible in some views even after deletion. Accept all changes first, then remove the marker.
- Do not rely on the counterparty to not notice the marker and ignore it: the marker is prominent by design. If the document reaches the counterparty with the marker still present, the attorney must clarify that it was sent in error and resend a clean copy.

**If you cannot locate the header/footer marker.** The document may have been generated by a version of the tool that placed the marker differently (e.g., only as a cover page with no header/footer). In that case, remove the cover page per step 3, run the Find check per step 5, and confirm visually that no marker text appears in any page header or footer. If you are uncertain whether the document has been fully de-marked, contact `legal-eng@company.com` before sending.

See [docs/output-contract.md → Attorney-approval framing and export marker](docs/output-contract.md#attorney-approval-framing-a-misuse-prevention-control-not-cosmetic) for the policy rationale and [docs/threat-model.md → External-communication guardrail](docs/threat-model.md#external-communication-guardrail) for the threat framing.

## Incident response

### Bedrock returns errors

Check the App Runner logs (CloudWatch log group `/aws/apprunner/contract-toaster/*`). Common causes:

- **AccessDeniedException** — Bedrock model access not enabled. See bootstrap step 1.
- **ThrottlingException** — Hit Bedrock per-account quota. The pipeline and the eval harness both perform exponential-backoff retries on `ThrottlingException`; these retries are **classified separately from genuine errors** and do **not** fire the `bedrock-invocation-errors` alarm. A sustained quota-pressure signal fires the dedicated `bedrock-throttle-retries` alarm instead (threshold: 5 `ThrottlingException` retries per 5-minute window). Check the `bedrock-throttle-retries` CloudWatch alarm and the `model-policy/bedrock-us-east-1.json` artifact: if the granted quota has changed or the eval harness is running without the rate-limiter, that explains the pressure. As a short-term stopgap, the backend retries with exponential backoff until the quota window refreshes. For a sustained fix, request a quota increase via AWS Support (AWS Console → Service Quotas → Amazon Bedrock → "Tokens per minute for claude-opus-4-8") and update `model-policy/bedrock-us-east-1.json` once the increase is granted.
- **ValidationException with "input is too long"** — This exception from the model layer indicates **cap misconfiguration** in step 14 of the pipeline, not a document that is simply too large. The step-14 cap check (see [ARCHITECTURE.md → Data flow](ARCHITECTURE.md) step 14) is the single authoritative failure point for oversized documents: when a document's assembled prompt would exceed `max_input_tokens`, the review terminates *before any model call* with `status=MANUAL_REVIEW_REQUIRED` and `reason=document_too_large`, and the user sees that outcome directly. A model-side "input is too long" ValidationException is therefore unreachable in correct operation. **If this exception fires in production, the `max_input_tokens` cap is misconfigured** (set too high relative to the model's actual context limit) — correct the cap value in the active model-policy configuration and redeploy. There is no document-splitting or segmentation procedure; that is not the remediation path. Affected reviews will have landed `MANUAL_REVIEW_REQUIRED` with `reason=document_too_large` if the oversized-document check worked; if the cap was misconfigured and the exception escaped to the model layer, treat it as an operational incident (cap repair + re-run of affected reviews).

### Review quarantined because its submission-time bundle was retired before execution

When a release bundle is rolled back or retired between the moment a user submits a review (step 3) and the moment the Step Functions execution begins (step 10), the pipeline detects that the bundle recorded on the submission record is no longer `active`. Rather than silently running under the newly active bundle, the review transitions to `QUARANTINED` with reason `submission_time_bundle_retired`. This is the defined behavior — see [ARCHITECTURE.md](ARCHITECTURE.md) data-flow step 10.

**Who sees this.** The review lands in `QUARANTINED` status before any LLM work runs. The concurrency slot and unspent spend reservation are released. The user is shown a `QUARANTINED` result with a message that the bundle in effect when they submitted is no longer active.

**Operator steps:**

1. **Confirm the rollback was intentional.** Open Admin UI → Release bundles → version history and confirm that the bundle retirement was deliberate. If the bundle was retired by accident, re-activate it: one click restores it to `active`, and the user can re-submit.
2. **Notify the user.** The user must re-submit their document explicitly against the now-active bundle. There is no automatic re-run: a re-run would use a different bundle than the one in the original idempotency key, which is a deliberate re-review, not a retry.
3. **Log the incident.** The `audit` table records the `QUARANTINED` transition with reason `submission_time_bundle_retired`, the review ID, the retired bundle hash, and the active bundle hash at the time execution started.
4. **Mark the original `SUPERSEDED` after replacement.** Once the user re-submits and the new review is `DONE`, mark the quarantined original `SUPERSEDED` so the audit trail is linked.

If you are seeing a large number of reviews quarantined for this reason, check whether a bundle rollback happened without prior notice to active reviewers. Consider temporarily holding further bundle activations until in-flight reviews drain (see "Rolling back a bad playbook or prompt" above for the rollback procedure and reviewer-notification steps).

### Reviews are stuck in PENDING / RUNNING

Reviews run in a Step Functions execution, so most failures self-classify: a failed stage transitions the review to `ERROR` with the failing stage recorded. Find the review's execution in the Step Functions console to see exactly which stage failed and why. Three distinct stuck observations, each with a different remediation:

**Observation 1 — PENDING with no `execution_arn`.** The submission record exists but no Step Functions execution was ever started (e.g. a crash between upload persistence and `StartExecution`). The orphan reconciler automatically re-runs "ensure execution started" for stale ARN-less submissions. The stale-`PENDING` alarm fires only if that automatic re-drive keeps failing — at which point inspect the submission record and run the admin "ensure execution started" action manually (it calls `StartExecution` with the deterministic name and records the resulting ARN or existing execution). As a last resort, an admin can mark the review `ERROR` and the user re-uploads.

**Observation 2 — PENDING with a dead ARN (PENDING-with-dead-ARN).** The review row is `PENDING` and has an `execution_arn`, but the Step Functions execution is in a terminal status (`FAILED`, `TIMED_OUT`, or `ABORTED`) — the execution died before its error-handling states ran, so the review row was never transitioned. This case is invisible to the missing-ARN alarm (there is an ARN) but is caught by the reconciler's DescribeExecution check: the reconciler calls DescribeExecution on non-terminal reviews with an ARN and, on finding a terminal execution status, transitions the review to `ERROR`, releases the spend reservation, and releases the concurrency slot. The stale-`PENDING` alarm covers this case — a `PENDING` review whose ARN resolves to a terminal execution is treated as a stuck review. If you see this, no manual action should be needed once the reconciler runs; if it persists, inspect the execution history in the Step Functions console for the root cause.

**Observation 3 — Stale RUNNING (execution timeout fired; slot recovery).** The review row is `RUNNING` and the Step Functions execution age exceeds the state-machine-level execution timeout. The execution-level timeout automatically terminates the execution to `TIMED_OUT`, which the reconciler then detects (same dead-execution path as Observation 2) and resolves to `ERROR` with slot and reservation release. The stale-`RUNNING` alarm fires when a `RUNNING` review's execution age exceeds the execution-level timeout; this pages on-call for investigation. The slot-reaper / semaphore lease TTL reclaims any leaked concurrency slot independently of the reconciler so subsequent reviews are not permanently blocked. If the alarm fires: confirm the execution timed out in the Step Functions console, check for upstream causes (Lambda OOM, Fargate SIGKILL, Bedrock throttle loop), and verify the reconciler has run and transitioned the review. If the concurrency cap shows saturation after a timeout event, force a slot reconcile from the admin UI.

### A wrong decision was rendered

This is the case the audit log is designed for — but **what's actually available depends on the
review's retention window** (`retention_window_at_creation`; admin-configurable, including a
`forever` / indefinite-preservation setting — see [docs/data-handling.md](docs/data-handling.md)
→ "Document retention and purge safety"; the answerable/not-answerable boundary is spelled out in
[docs/audit-queries.md](docs/audit-queries.md) → "Substance retention boundary"). Check which tier
you're in before promising a requester the reasoning is recoverable:

**Still inside the review's retention window (or its window is `forever`).** Full substance is
available. From the admin UI:

1. Pull up the review.
2. Click **View audit detail** — see the release-bundle hash, playbook hash/version, prompt hash/version, standard-form hash, model-policy hash, corpus snapshot, scanner rules, token counts, `verdict_summary`, and `issue_rationale_text`.
3. Determine whether the issue is:
   - **Playbook gap** — add a topic, an acceptable variation, or a structured hard rejection. Cut a new draft playbook and activate only through the release-bundle gates.
   - **Retrieval gap** — relevant precedent wasn't in the active corpus snapshot. Add it to a draft corpus snapshot, curate, test, and activate.
   - **Model error** — log it, raise with Anthropic if pattern emerges, consider re-prompting.
4. If the review is being relied on, mark it `QUARANTINED`, activate the corrected bundle, re-run it, and mark the original `SUPERSEDED` after replacement.

**Past the review's retention window.** The purge worker has already cleared `verdict_summary` and
`issue_rationale_text` from the row and deleted the uploaded document, redline, and
`analysis_report` from S3 (purge invariant 4). **View audit detail** still shows the hashes,
`decision`, and `attorney_disposition` — enough to prove *that* the same playbook/prompt/corpus
inputs would recur under a re-run, and to still act on steps 3–4 above at the bundle level (a
playbook or corpus gap doesn't require the specific review's own text to fix going forward). What
it cannot do is recover *why* the attorney accepted or rejected that specific draft, or reproduce
the draft's actual text — that reasoning is gone, not merely hidden. Don't promise a requester it
can be pulled up; say so and point them at the boundary explanation in
[docs/audit-queries.md](docs/audit-queries.md).

**If this keeps happening for a class of review that matters** — e.g. executed agreements a GC
wants answerable years later — the fix is to set that environment's default retention window (or
the window for reviews meeting a criterion your process applies before submission) to `forever`,
not to try to recover reasoning that's already been purged. See "Changing document retention"
below.

### Output flagged for prompt/policy leakage (output scanning alert)

Before any `.docx` is generated, model output is scanned for prompt leakage, internal-policy/playbook leakage, and excessive precedent quotation (retrieved precedent is treated as untrusted, too — see [docs/threat-model.md](docs/threat-model.md) → Prompt injection). When the scanner flags a review:

1. **The flagged output is not released.** The review does not produce a downloadable redline; it terminates as `ERROR_MANUAL_REVIEW_REQUIRED` — a SYSTEM STATUS, not a legal decision (there is no retry for a leak; see [ARCHITECTURE.md](ARCHITECTURE.md) → review statuses). Confirm no attorney has already pulled the output.
2. Open the review's audit detail: which scanner rule fired, the active release bundle, and the precedents retrieved. Do **not** paste the raw flagged output into CloudWatch, Slack, or a ticket — it may contain the system prompt or confidential clause text. Inspect it in the controlled admin view only.
3. Triage the cause:
   - **Injection via the uploaded document or a corpus precedent** — hostile text instructed the model to reveal its prompt. Treat retrieved precedent as untrusted too: if a corpus document is the source, quarantine the corpus snapshot and re-run affected reviews after a clean snapshot is active (see "Rolling back a bad playbook or prompt" for the quarantine-and-re-run pattern).
   - **Prompt/playbook regression** — a recent release bundle is over-quoting policy. Roll it back (see "Rolling back a bad playbook or prompt").
4. Log the incident in `audit` with the review ID, the rule that fired, and the disposition. If a leak was actually delivered to a user before the scanner caught a sibling case, escalate via your organization's standard security path.

### Audit-table denied-mutation alarm

The `audit` table is append-only: `UpdateItem` and `DeleteItem` are denied to all application roles, and entries stream to the object-locked `audit-archive` bucket (see [docs/threat-model.md](docs/threat-model.md) → Audit integrity). A CloudWatch alarm fires on any **denied** mutation attempt against the table.

1. A denied `UpdateItem`/`DeleteItem` is **expected to never happen** in normal operation — every legitimate write is a conditional `PutItem`. Treat a fired alarm as a potential integrity event, not noise.
2. From CloudTrail (management events), identify the principal that attempted the mutation, the time, and the source. Cross-check against the object-locked `audit-archive` copy: the archived stream is the tamper-evident source of truth and must still match.
3. If the principal is an app role, this indicates a code path attempting a forbidden write (bug or compromise) — capture it and treat as a security incident; do not "fix" by widening the table's IAM policy.
4. If the principal is a human/break-glass role, confirm it was an authorized investigation and record the justification in `audit`.

Never grant `UpdateItem`/`DeleteItem` on the `audit` table to resolve an alarm. The denial is the control working.

### CI build or promotion failed

CI (CodeBuild or equivalent) shows the failure in its console with build/test/scan logs. A failed CI run **does not** change production — prod keeps serving its currently pinned digest. Common causes:

- Test or security-scan failure → fix on a branch, PR, merge; CI re-runs.
- Image signing failure → no signed digest is published, so there is nothing to promote; investigate the signing step before retrying.
- Dockerfile build error → fix on a branch, PR, merge.

If a **promotion** fails (the `cdk deploy --context imageDigest=...` step), the digest may be unsigned/tampered or App Runner could not start the new image. App Runner keeps the previous digest running. Re-promote the last-known-good digest (see "Rolling back a code deploy") to be certain, then diagnose the bad image. Missing env vars or IAM gaps are still fixed in CDK and applied with `cdk deploy --context env=<dev|prod>`.

### Total outage / emergency procedure

If the service is down and no admin is available:

1. Anyone with AWS console access can pause the App Runner service to prevent further damage.
2. Reach the on-call engineer via `legal-eng@company.com` (owned by Marc Mandel, General Counsel; backup: your IT operations contact on-call). For after-hours P0 outages, send a direct message to the GC and to your IT on-call lead.
3. If no admin can sign in (lost seed admin, non-active account, Cognito problem), use the **break-glass procedure** below. Do not hand-edit the `users` table with ad-hoc credentials.

### Break-glass: restoring admin access

There is **no dedicated CDK-managed break-glass IAM role for v1** (issue #229). An earlier `AuthStack` revision created one whose trust policy used a managed-policy ARN (`arn:aws:iam::aws:policy/AdministratorAccess`) as its `FederatedPrincipal` — an invalid IAM principal that would have failed `CreateRole` at deploy time, so that path had never actually been exercisable. There is also no real IAM Identity Center permission-set ARN or SAML-provider ARN available yet to wire a proper dedicated role, so break-glass instead uses the **`AdministratorAccess` SSO permission set already granted for this account** (see `aws-access-request.md` → Fulfillment record) — MFA-enforced by the SSO identity provider on every sign-in, used narrowly for this procedure only.

1. Sign in via the SSO portal using the `AdministratorAccess` permission set (MFA is enforced by the SSO identity provider). Note the reason/ticket you are acting under.
2. Set `is_admin=true` on the relevant `users` row (by `cognito_sub`, or reconcile by `email` if the user has signed in at least once) via the DynamoDB console or CLI.
3. **Manually** record an `audit` entry with `reason=emergency-override`, actor, target, session reason/ticket, and timestamp — there is **no** automated CloudTrail → `audit` mirror for this path (every AWS API call under the session is still logged as a CloudTrail management event automatically, unconditionally, but that is not the same as an application `audit` row). Treat a missing `audit` row as an incident; do not continue broad break-glass work silently.
4. Sign back in through the app as that admin and confirm normal admin access; then sign out of the SSO session.

This is the only sanctioned path to admin outside the in-app Users screen. `AdministratorAccess` is dev-account convenience, not least-privilege — before promoting to prod, replace it with a dedicated least-privilege Identity Center permission set (or SAML-federated role) scoped to break-glass only, and run one live drill of the path (recorded per this runbook's bootstrap-acceptance philosophy).

## Backups and recovery

- **DynamoDB.** Point-in-time recovery enabled on all tables; restore any table to any second within the last 35 days via console or CLI.
- **S3.** Versioning enabled on `corpus` and `audit-archive`. Object lock in **governance** mode on `corpus` and `audit-archive` (a holder of `s3:BypassGovernanceRetention` can override only through MFA break-glass). Held objects also carry an S3 Object Lock legal hold or protected hold tag. `uploads` and `outputs` are intentionally not versioned; their lifetime is the admin-configured document-retention window (0–3yr, default 90), enforced by the retention purge worker — not a fixed bucket lifecycle rule. Per-review **non-substantive metadata** in `reviews` is retained indefinitely regardless of document retention; confidential substance is retention-governed.
- **Playbooks.** Every version is preserved indefinitely in `playbook_versions`. Reverting is a UI action, not a backup-restore.
- **Container images.** Every signed image CI publishes is retained in ECR per the repository's lifecycle policy (set in CDK). Rollback is re-promotion of a prior **digest** (see "Rolling back a code deploy"), so keep enough history that the last several known-good digests are always present; do not prune aggressively.

There is no separate "backup" job; the configured retention is the backup.

## Observability

- **CloudWatch dashboard.** `contract-toaster-prod` and `contract-toaster-dev`. Tiles: deployed version, request rate, error rate (4xx and 5xx separately), p99 latency, Bedrock invocations per day, cost-to-date, Step Functions stage failures, stale `PENDING`/`RUNNING` reviews, semaphore saturation, abandoned spend reservations, purge deleted/skipped counts, audit-archive stream lag, **count of reviews currently in `MANUAL_REVIEW_REQUIRED` state**, and **count of reviews currently in `ERROR_MANUAL_REVIEW_REQUIRED` state**. The admin UI exposes a **manual-review filter** view that lists all reviews in either manual-review state so the legal admin can triage them without querying DynamoDB directly.
- **Alarms.** Configured to email `legal-eng@company.com` (owner: Marc Mandel, General Counsel; subscribers: legal-eng team; cadence: immediate SNS → email on alarm state change) on: App Runner 5xx > 5% for 5 min; Bedrock errors > 0; Cognito sign-in failures spike; cost-to-date exceeds budget threshold; stale review status (PENDING/RUNNING beyond threshold); **any review entering `MANUAL_REVIEW_REQUIRED` or `ERROR_MANUAL_REVIEW_REQUIRED` that remains unacknowledged for more than 24 hours** (the `contract-toaster-manual-review-stale` alarm — fires if the manual-review filter count is > 0 and the oldest unacknowledged entry exceeds 24 hours, so a 4:55 PM failure does not silently wait overnight); failed/timed-out pipeline stage; abandoned reservation; purge worker error; audit stream lag; denied audit-table mutation; break-glass role assumption; governance retention bypass attempt.

### Bootstrap alarm acceptance test

Bootstrap step 10 (see "Day-one bootstrap" above) is the **required** one-time proof that alarm delivery is live. It must be completed — and its outcome recorded — before the environment is declared prod-operable. The test covers two failure modes that configuration alone cannot rule out:

1. **Unconfirmed SNS subscription** — AWS does not deliver alarm mail to a `PendingConfirmation` subscription; the alarm fires silently. Confirming the subscription in the AWS Console (SNS → Topics → `contract-toaster-alarms` → Subscriptions) closes this gap.
2. **End-to-end delivery failure** — SMTP relay, spam filter, or alias misconfiguration can silently drop messages after the subscription is confirmed. Forcing a test alarm state via `aws cloudwatch set-alarm-state` and waiting for the notification email at `legal-eng@company.com` proves the full delivery path.

The test is documented here so operators who stand up a new environment know it is mandatory, not optional hygiene. The bootstrap log (or deployment PR/issue) must record: the date, the alarm name tested, and the name of the person who confirmed receipt at `legal-eng@company.com`. Absence of that record means the environment has not satisfied acceptance criterion 2 ("Alarm alias real, tested, with named owner").

### Manual-review filter: owner and SLA

**Owner.** The legal admin (General Counsel or their delegate) is responsible for checking the manual-review filter in the admin UI **daily** — ideally first thing each morning. The filter lists all reviews in `MANUAL_REVIEW_REQUIRED` and `ERROR_MANUAL_REVIEW_REQUIRED` states. For each entry the legal admin determines whether the review needs to be re-run (after the underlying cause is fixed), escalated to engineering, or simply acknowledged and the uploader notified.

**SLA.** The target is acknowledgement within one business day of the review entering a manual-review state. The `contract-toaster-manual-review-stale` alarm provides the backstop: it fires if any review remains in a manual-review state without acknowledgement for more than 24 hours, alerting `legal-eng@company.com` (engineering can then ping the legal admin if the filter has not been checked). The 24-hour window is intentionally longer than a single working session to accommodate end-of-day submissions.
- **CloudTrail.** All **management** API calls logged to `audit-archive` bucket. Console queryable via Athena. Note that AWS currently logs Bedrock `InvokeModel`, `InvokeModelWithResponseStream`, `Converse`, and `ConverseStream` as **management** events — so these give us a control-plane audit signal (who invoked the model, when) without enabling data events. That signal does **not** capture prompt or output content, by design; the per-review facts come from the application `audit` table, and raw prompts/outputs are never written to CloudWatch (see [docs/threat-model.md](docs/threat-model.md) → Logging). Per-object **S3 data events** remain off by default for cost; to enable them for a specific bucket during an investigation, update the trail's event selectors in CDK and `cdk deploy`, then disable again when the investigation closes.
- **Step Functions console inspection — what you will and will not see (issue #19).** When you open an execution in the Step Functions console to diagnose a stuck or failed review, execution-history event inputs and outputs will contain **S3 object references, content hashes, review identifiers, status values, and token/cost facts — not document text, prompts, or model output**. This is intentional and enforced by the pointer-only payload rule (see [docs/threat-model.md](docs/threat-model.md) → Step Functions execution history and [docs/data-handling.md](docs/data-handling.md) → Step Functions execution-history classification). Substantive content travels via the encrypted `uploads`/`outputs` S3 buckets; you must retrieve the relevant S3 object directly (with appropriate authorization) if you need to inspect the document or model output for a specific review. Do not expect to recover document text from execution history — if a payload looks sparse, that is the correct behavior, not a missing-field bug.
