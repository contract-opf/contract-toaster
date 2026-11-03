# Contract Toaster Review — Infrastructure (CDK)

This directory contains all AWS infrastructure for the Contract Toaster contract review tool,
defined as code using [AWS CDK v2](https://docs.aws.amazon.com/cdk/v2/guide/).

## Stack layout

The entry point is `bin/contract-toaster.ts`, which instantiates a single top-level stack
and these nested stacks (`lib/contract-toaster-stack.ts`, in construction order):

```
ContractToasterStack  (contract-toaster-dev | contract-toaster-prod)
├── NetworkStack        — VPC, subnets, security groups; NAT + Bedrock interface endpoints (hardened only)
├── KmsKeysStack        — per-data-class CMKs: uploads, outputs, corpus, audit, DynamoDB (hardened only), Step Functions (always)
├── DataStack           — S3 buckets and DynamoDB tables
├── AuthStack           — Cognito user pool + Google IdP, pre-token Lambda
├── PipelineStack       — Step Functions review pipeline and its Lambdas (the review stage is still a MOCK on this target)
├── CicdStack           — CodeBuild CI, cosign signing, ECR
├── AppStack            — App Runner API service, pinned to an image digest
├── WafStack            — WAF WebACL in front of App Runner (hardened only; skipped under profile=minimal)
├── FrontendStack       — Amplify Hosting + React SPA, CSP and security headers
└── ObservabilityStack  — SNS alarm topic, CloudWatch dashboard and alarms, CloudTrail (hardened only)
```

The top-level stack also owns:

- **An environment-baseline CMK** (`alias/<appName>-<env>`). The data itself
  is encrypted with the per-data-class keys in `KmsKeysStack`, not this one;
  under `profile=minimal` the S3 and DynamoDB keys are not created and those
  stores use AWS-managed encryption instead (see "Deploy profiles").
- **Base IAM roles** — `deployRole` (CI deploys) and `appRunnerTaskRole`
  (runtime container). `AppStack` attaches the task role's scoped policy
  (`app-stack.ts`).

The App Runner image runs **Python 3.12** (`backend/Dockerfile`), while the
toolchain, CI and the Docker Compose image use 3.13 (`.python-version`,
`deploy/dts/backend.Dockerfile`). `tests/test_python_version_parity_63.py`
pins the 3.13 declarations; the AWS image is the known exception.

That image is built from the **repo root** (`docker build -f backend/Dockerfile .`,
in both `ci-pipeline.yml` and the CodeBuild buildspec), not from `backend/`:
the backend imports pipeline modules from `scripts/` at runtime, and the
Dockerfile COPYs exactly that import closure.
`tests/test_aws_image_runtime_scripts.py` fails if the list drifts from the
import graph.

## Environments

| Environment | AWS account     | Region     | Stack name         |
|-------------|-----------------|------------|--------------------|
| `dev`       | `111111111111`  | us-east-1  | `contract-toaster-dev`  |
| `prod`      | `222222222222`  | us-east-1  | `contract-toaster-prod` |

Account IDs in `cdk.json` are dummy values (`111111111111`, `222222222222`) so
an offline `cdk synth` works anywhere — replace them with real AWS account IDs
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
| `allowedEmailDomains` | **Yes** — no default | —                 | Comma-separated email domains a Cognito sign-in may come from; injected into App Runner as `ALLOWED_EMAIL_DOMAINS`. Synth refuses a blank entry, `*` or anything that is not a bare domain |
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

## Runtime variables the stack does not set

`AppStack` injects the table and bucket names the API needs, and
`ALLOWED_EMAIL_DOMAINS` from the required `allowedEmailDomains` context above.
Two runtime settings are not injected; set them on the App Runner service when
the default does not fit (their meaning is documented once, in
[deploy/dts/README.md](../deploy/dts/README.md#spend-concurrency-and-sign-in-variables)):

- `DAILY_SPEND_CAP_USD_CENTS` — default `2000` ($20/day). This target has no
  admin settings store (`MODEL_SETTINGS_TABLE` is not provisioned), so the
  Settings tab cannot override it here; change the variable and redeploy.
- `PIPELINE_MAX_CONCURRENCY` — default `5`; read for the occupancy figure in
  `GET /api/admin/health`.

## Usage

```bash
# Install dependencies
npm install

# Synthesize the dev stack (dry run) — see "Deploy identity context" above;
# githubOwner/alarmsEmail/appDomain/adminEmail/hostedDomain/allowedEmailDomains must be supplied
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
- **Per-data-class KMS**: separate CMKs for uploads, outputs, corpus, audit and DynamoDB under the default `hardened` profile (`kms-keys-stack.ts`); `minimal` falls back to AWS-managed keys.
- **Split API role + scoped download**: Separate deploy vs. task roles here; scoped
  pre-signed download URLs in #71.
- **XSS/CSP**: Amplify CSP header in #72; no `innerHTML` with user content (#54). The
  same custom-headers block also carries `Strict-Transport-Security`,
  `Permissions-Policy` and `Cross-Origin-Opener-Policy` (#57) — see
  `docs/threat-model.md` → Frontend security posture.
- **Hostile-file AV**: Upload scanning in #63.
- **Pointer-only payloads**: Step Functions inputs carry only S3/DDB pointers (#59).
- **No doc substance in logs**: CloudWatch must never log document content, rationales,
  or PII; enforced in Lambda/task configurations (#55+).
- **Internal-notes marker**: A generated redline `.docx` carries an internal-notes export marker iff that review's notes mode put internal-audience content in scope — never unconditional, no approval semantics (#513).

## Nested stack notes

Each nested stack is a logical domain boundary; see the stack list above for
what each one creates. Two are deliberately incomplete on this target: the
`PipelineStack` review stage is a mock Lambda (the Docker Compose target runs
the real review chain), and the `ObservabilityStack` dashboard renders several
tiles as placeholder text until the custom metrics behind them exist — see
[docs/implementation-status.md](../docs/implementation-status.md).
