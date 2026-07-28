# Contract Toaster Review — Infrastructure (CDK)

This directory contains all AWS infrastructure for the Contract Toaster contract review tool,
defined as code using [AWS CDK v2](https://docs.aws.amazon.com/cdk/v2/guide/).

## Stack layout

The entry point is `bin/contract-toaster.ts`, which instantiates a single top-level stack:

```
ContractToasterStack  (contract-toaster-dev | contract-toaster-prod)
├── NetworkStack        — VPC, subnets, security groups (#55+)
├── DataStack           — S3 buckets, DynamoDB tables (#51, #52)
├── AuthStack           — Cognito user pool + Google IdP (#53)
├── AppStack            — App Runner service + Step Functions pipeline (#55, #59)
├── FrontendStack       — Amplify Hosting + React SPA (#54)
└── ObservabilityStack  — CloudWatch dashboards + CloudTrail (#57)
```

The top-level stack also owns:

- **Environment-scoped customer-managed KMS key** — one CMK per environment;
  per-data-class keys (uploads / redlines / corpus / audit) are added in #70.
- **Base IAM roles** — `deployRole` (CI deploys) and `appRunnerTaskRole` (runtime
  container); both start with empty permission sets filled in by downstream issues.

## Environments

| Environment | AWS account     | Region     | Stack name         |
|-------------|-----------------|------------|--------------------|
| `dev`       | `111111111111`  | us-east-1  | `contract-toaster-dev`  |
| `prod`      | `222222222222`  | us-east-1  | `contract-toaster-prod` |

Account IDs in `cdk.json` are placeholders — replace them with real AWS account IDs
before deploying. Dev and prod are **always separate AWS accounts**; deploying both
into the same account is not supported.

## Deploy identity context (issues #316, #349, #409)

This engine ships with no tenant identity baked in — no corporate domain, IdP
hosted-domain, or alarm inbox. A `cdk synth`/`cdk deploy` with none of these
supplied fails closed, naming every missing key, rather than silently wiring
a stranger's stack to this project's own GitHub source, alarm inbox, domain,
admin identity, or sign-in enforcement domain.

| Context key    | Required? | Default                          | Used for |
|-----------------|-----------|-----------------------------------|----------|
| `githubOwner`   | **Yes** — no default | —                       | GitHub org/user CodeBuild watches for the CI webhook trigger |
| `alarmsEmail`   | **Yes** — no default | —                       | SNS email subscription for CloudWatch alarms |
| `appDomain`     | **Yes** — no default | —                       | Cognito hosted-UI callback/logout URL domain (SPA hosting domain) |
| `adminEmail`    | **Yes** — no default | —                       | Initial admin (GC) email seeded into `admin_bootstrap` |
| `hostedDomain`  | **Yes** — no default | —                       | Google Workspace hosted domain enforced for sign-in (two independent layers — see `auth-stack.ts`) |
| `appName`       | No        | `contract-toaster`                 | Resource-name prefix |
| `githubRepo`    | No        | `contract-toaster`                 | GitHub repo name CodeBuild watches |
| `profile`       | No        | `hardened`                         | Deploy profile — see "Deploy profiles" below |

Supply these either as repeated `--context key=value` flags, or — the
recommended path for a real deploy — collected in `infra/cdk.context.json`:

```bash
cp infra/cdk.context.example.json infra/cdk.context.json
# edit infra/cdk.context.json with your real values
npx cdk synth --context env=dev
```

`infra/cdk.context.json` is **git-ignored** — it is where the *real* values
for a real deployment live, never committed. `infra/cdk.context.example.json`
is the committed, brand-free reference for its shape; copy it as the
starting point above. This keeps the public engine's repo history free of
any tenant literal while still letting a real deployer avoid retyping
`--context` flags on every command.

This is unrelated to the CDK context-provider cache (e.g. availability-zone
lookups) that `cdk synth` also needs — that cache is structural, brand-free
(dummy account IDs only), and lives in the committed `cdk.json` instead, so
an offline `cdk synth`/test run never depends on `cdk.context.json` existing.

Two more context flags exist but are deliberately **not** part of the
standing `cdk.context.json`/example file above — they are one-off,
per-invocation promotion/signing overrides, not standing identity, and are
documented where they're used: `imageDigest` (see
[RUNBOOK.md](../RUNBOOK.md) "Deploying a code change") and
`cosignCertId`/`cosignOidcIssuer` (see `cicd-stack.ts` — only needed for a
signing workflow other than the default GitHub Actions OIDC one).

## Usage

```bash
# Install dependencies
npm install

# Synthesize the dev stack (dry run) — see "Deploy identity context" above;
# githubOwner/alarmsEmail/appDomain/adminEmail/hostedDomain must be supplied
# via --context or infra/cdk.context.json, omitted here for brevity.
npx cdk synth --context env=dev

# Deploy to dev (requires AWS credentials for dev account)
npx cdk deploy --context env=dev

# Deploy to prod (requires AWS credentials for prod account)
npx cdk deploy --context env=prod
```

## Deploy profiles (issue #231)

By default (and for the production legal-data story) every deploy is
**`hardened`**: 6 CMKs with encryption-context DENY policies, a 2-AZ VPC with
NAT + Bedrock interface endpoints, a WAF WebACL, CloudTrail + an Object-Lock
audit archive, and cosign image signing. That posture is a non-starter for a
quick demo or an open-source adopter evaluating the tool — a legal-ops team
will not stand up a Google Workspace service account and a cosign pipeline
just to try it, and the NAT gateway + interface endpoints alone add ~$47/mo
of idle cost before any traffic.

`--context profile=minimal` trims the stack to a demo/onboarding footprint:

| Control                    | `hardened` (default)                          | `minimal`                                   |
|-----------------------------|-----------------------------------------------|------------------------------------------------|
| WAF WebACL                  | Created, fronts App Runner                    | Not created                                   |
| VPC NAT gateway              | 1 (dev) / 2 (prod)                            | 0 — App Runner egresses publicly              |
| Bedrock VPC interface endpoints | Created (BEDROCK_RUNTIME, BEDROCK_AGENT_RUNTIME) | Not created                             |
| CloudTrail trail             | Created, logs to audit-archive                | Not created                                   |
| S3 Object Lock (corpus, audit-archive) | Enabled (GOVERNANCE, 7yr)          | Disabled                                      |
| S3 bucket encryption         | Customer-managed CMK per data class           | AWS-managed (`S3_MANAGED`)                    |
| DynamoDB table encryption    | Customer-managed CMK (shared `dynamodbKey`, dedicated `auditKey` for the audit table) | AWS-managed (`AWS_MANAGED`) |
| Step Functions state-machine CMK | Created (not gated by profile)          | Created (not gated by profile)                |

```bash
# Demo / onboarding footprint
npx cdk synth --context env=dev --context profile=minimal
npx cdk deploy --context env=dev --context profile=minimal

# Production posture (default — identical to omitting --context profile entirely)
npx cdk synth --context env=dev --context profile=hardened
```

`profile` is resolved independently in each nested stack via
`node.tryGetContext('profile')` (CDK context walks up the construct tree), so
it does not need to be threaded through every stack's props — see
`kms-keys-stack.ts`, `data-stack.ts`, `network-stack.ts`, and
`observability-stack.ts` for the resolution pattern, and
`contract-toaster-stack.ts` for the conditional `WafStack` instantiation.
Structural coverage: `tests/test_infra_minimal_profile_231.py` asserts (via
`cdk synth` + JSON template inspection, entirely offline) that `minimal`
produces the reduced footprint above and that `hardened` remains unchanged.

Not yet covered by a `minimal` profile (left for a follow-on issue): Cognito
auth is still Google Workspace DWD-only (no plain email/password user path),
and the CI/CD pipeline still requires cosign image signing — an OSS adopter
without Google Workspace or a signing pipeline cannot yet complete an
end-to-end minimal deploy on those two fronts.

## Security invariants

- **Fail-closed auth**: Cognito pre-token Lambda rejects on error — never grants on failure (#53).
- **Per-data-class KMS**: One CMK per env at skeleton; per-class keys in #70.
- **Split API role + scoped download**: Separate deploy vs. task roles here; scoped
  pre-signed download URLs in #71.
- **XSS/CSP**: Amplify CSP header in #72; no `innerHTML` with user content (#54).
- **Hostile-file AV**: Upload scanning in #63.
- **Pointer-only payloads**: Step Functions inputs carry only S3/DDB pointers (#59).
- **No doc substance in logs**: CloudWatch must never log document content, rationales,
  or PII; enforced in Lambda/task configurations (#55+).
- **Watermark**: Every reviewer output carries an attorney-approval watermark (#59).

## Nested stack notes

Each nested stack is a logical domain boundary. They are intentionally empty
placeholders in this skeleton (issue #50) — resources land in the issues listed
next to each stack above.
