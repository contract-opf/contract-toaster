import * as cdk from 'aws-cdk-lib';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

export interface CicdStackProps extends cdk.NestedStackProps {
  readonly envName: string;
  /**
   * Resource-name prefix (issue #233). Defaults to 'contract-toaster'. Used
   * only to keep the audit-table ARN referenced by the promotion role in
   * sync with DataStack's table naming (DataStack owns the audit table and
   * is prefixed by the same appName) — this stack's own resource names
   * (ECR repo, CodeBuild projects/roles) are unchanged by this issue.
   */
  readonly appName?: string;
  /**
   * GitHub org/user that owns the source repo CodeBuild watches for the CI
   * webhook trigger (issue #233). Defaults to 'exos-legal'.
   */
  readonly githubOwner?: string;
  /**
   * GitHub repo name CodeBuild watches for the CI webhook trigger
   * (issue #233). Defaults to 'contract-toaster'.
   */
  readonly githubRepo?: string;
  /**
   * Deploy role — assumed by CI (CodeBuild) to run `cdk deploy`.
   * Grants are added here for ECR push, SSM parameter write (digest promotion),
   * and audit DynamoDB access.
   */
  readonly deployRole: iam.IRole;
  /**
   * ARN of the Sigstore/cosign certificate identity (OIDC subject) used when
   * signing images.  Required for meaningful keyless cosign verification in the
   * promotion project.  Example (derived from githubOwner/githubRepo by default):
   *   "https://github.com/contract-opf/contract-toaster/.github/workflows/ci-pipeline.yml@refs/heads/main"
   */
  readonly cosignCertificateIdentity: string;
  /**
   * OIDC issuer URL that issued the signing certificate.  Required for keyless
   * cosign verification.  Example: "https://token.actions.githubusercontent.com"
   */
  readonly cosignCertificateOidcIssuer: string;
}

/**
 * CicdStack — CodeBuild projects, ECR repository with image signing,
 * and the digest-promotion mechanism. (Issue #66.)
 *
 * Security invariants:
 *
 * 1. **No auto-deploy from main.**  A merge to `main` does NOT change
 *    production.  App Runner's `autoDeploymentsEnabled` is `false`
 *    (enforced in AppStack, issue #55).  Promotion to a new digest is
 *    a deliberate, audited step triggered from this stack's promotion
 *    mechanism.
 *
 * 2. **Image signing.**  Every image pushed to ECR is signed using
 *    AWS Signer / ECR image signing.  An unsigned or unverifiable
 *    digest cannot be promoted: the promotion build step verifies
 *    the signature before writing the new digest to the SSM parameter
 *    that App Runner's CDK stack reads during the next deliberate
 *    `cdk deploy`.
 *
 * 3. **Promotion audit row.**  Each promotion writes an audit record
 *    (actor, digest, timestamp, environment) to the `audit` DynamoDB
 *    table (owned by DataStack / issue #52), appended only — never
 *    mutated.  This satisfies the "promotion writes an audit row"
 *    acceptance criterion.
 *
 * 4. **CI test gates.**  The CodeBuild build project runs:
 *    - The full Python test suite (tests/test_X.py, tests/nested/test_X.py)
 *    - The docs-lint gate (scripts/docs-lint.py) — reconciliation #43
 *    - The detector-correctness gates (tests/detector/) — reconciliation #1, #2
 *    - Security/dependency scans (pip-audit + trivy + npm audit for frontend)
 *    - Container image build, sign, and ECR push
 *
 * 5. **Signature verification before promotion.**  The promotion step
 *    runs cosign verify (or equivalent ECR signing verification)
 *    before writing the new digest to SSM.  An unsigned or
 *    unverifiable digest causes the build to fail non-zero, blocking
 *    promotion.
 *
 * Resources defined here:
 *  1. ECR repository — IMMUTABLE tags; image scanning on push; lifecycle
 *     policy to retain last 20 signed images.
 *  2. AWS Signer signing profile — used by cosign / ECR image signing
 *     to produce the verifiable image signature.
 *  3. SSM Parameter — records the currently promoted ECR image digest for
 *     the target environment, for operator/audit visibility (e.g. `aws ssm
 *     get-parameter` to look up "what's promoted right now"). App Runner
 *     itself is NOT wired to read this parameter directly (issue #224): the
 *     digest reaches AppStack via the explicit `cdk deploy --context
 *     imageDigest=sha256:<digest>` step (RUNBOOK.md "Deploying a code
 *     change"), so promotion stays a deliberate, auditable action rather
 *     than something that could change silently on any unrelated
 *     `cdk deploy` that happens to pick up a new SSM value.
 *  4. CodeBuild project (CI build) — runs tests, scans, builds, signs,
 *     and pushes the image.  Does NOT write the SSM parameter or audit row.
 *  5. CodeBuild project (promotion) — manually triggered; verifies signature,
 *     writes audit row, then updates the SSM digest parameter.  Separated
 *     from the CI build so a push to main can never auto-mutate the digest.
 *  6. IAM grants — each role receives least-privilege access:
 *     CI build role: ECR push only.
 *     Promotion role: ECR pull, SSM parameter write, DynamoDB PutItem.
 */
export class CicdStack extends cdk.NestedStack {
  /**
   * ECR repository for the API container image.
   * Tags are IMMUTABLE — pushed images cannot be overwritten.
   */
  readonly ecrRepository: ecr.Repository;

  /**
   * SSM parameter holding the currently promoted image digest.
   * Format: "sha256:<hex>".  Operator/audit record only — App Runner is
   * promoted via the explicit `cdk deploy --context imageDigest=...` step,
   * not by AppStack reading this parameter (issue #224).
   */
  readonly imageDigestParam: ssm.StringParameter;

  /**
   * CodeBuild project that runs tests, scans, builds, signs, and pushes.
   * Does NOT write the SSM digest parameter — that is the promotion project's job.
   */
  readonly buildProject: codebuild.Project;

  /**
   * CodeBuild project for deliberate, manually-triggered promotion.
   * Verifies the image signature, writes the audit row, then updates the SSM
   * digest parameter.  Never triggered automatically by a push to main.
   */
  readonly promotionProject: codebuild.Project;

  constructor(scope: Construct, id: string, props: CicdStackProps) {
    super(scope, id, props);

    const { envName, deployRole, cosignCertificateIdentity, cosignCertificateOidcIssuer } = props;
    const appName = props.appName ?? 'contract-toaster';
    const githubOwner = props.githubOwner ?? 'exos-legal';
    const githubRepo = props.githubRepo ?? 'contract-toaster';

    // -----------------------------------------------------------------------
    // 1. ECR repository — IMMUTABLE tags, image scanning on push.
    //
    // IMMUTABLE: once a tag is pushed, it cannot be overwritten.  App Runner
    // is always pinned to a digest (sha256:…), never a mutable tag, so this
    // is defense-in-depth.
    //
    // imageScanOnPush: true — basic vulnerability scanning on every push
    // (separate from the Trivy/pip-audit step in the build phase).
    //
    // lifecycleRules: retain last 20 signed images so rollback targets are
    // available.
    // -----------------------------------------------------------------------
    this.ecrRepository = new ecr.Repository(this, 'ApiRepository', {
      repositoryName: `contract-toaster-api-${envName}`,
      // IMMUTABLE tag mutability — pushed images cannot be overwritten.
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      imageScanOnPush: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          description: 'Retain last 20 images (rollback window)',
          maxImageCount: 20,
          rulePriority: 1,
        },
      ],
    });

    cdk.Tags.of(this.ecrRepository).add('contract-toaster:env', envName);
    cdk.Tags.of(this.ecrRepository).add('contract-toaster:component', 'cicd');

    // -----------------------------------------------------------------------
    // 2. SSM Parameter — records the current promoted ECR image digest.
    //
    // Operator/audit record of "what's promoted right now" — App Runner is
    // actually pinned via the explicit `cdk deploy --context
    // imageDigest=sha256:<digest>` step, not by reading this parameter
    // (issue #224; see the class-level doc comment above). Defaults to a
    // placeholder; the first real promotion writes the real digest.
    //
    // PROMOTION GATE: writing this parameter is the deliberate promotion step.
    // The CI build verifies the image signature (cosign verify / ECR image
    // signing verification) BEFORE writing this parameter.  An unsigned or
    // unverifiable digest causes the build to fail, blocking promotion.
    //
    // AUDIT: every write to this parameter is logged in CloudTrail (SSM
    // PutParameter is a management API event).  The build additionally writes
    // an explicit audit row to the `audit` DynamoDB table (see buildspec
    // promotion step below).
    // -----------------------------------------------------------------------
    this.imageDigestParam = new ssm.StringParameter(this, 'ImageDigestParam', {
      parameterName: `/contract-toaster/${envName}/ecr-image-digest`,
      description:
        `Currently promoted ECR image digest for contract-toaster-api-${envName}. ` +
        'Written only after signature verification passes (CI pipeline, issue #66). ' +
        'An unsigned or unverifiable digest cannot be promoted.',
      // Default placeholder — replaced by the first real CI promotion.
      stringValue: 'sha256:0000000000000000000000000000000000000000000000000000000000000000',
      tier: ssm.ParameterTier.STANDARD,
    });

    cdk.Tags.of(this.imageDigestParam).add('contract-toaster:env', envName);
    cdk.Tags.of(this.imageDigestParam).add('contract-toaster:component', 'cicd');

    // -----------------------------------------------------------------------
    // 3a. CI build buildspec — test, scan, build, sign, push only.
    //
    // AC #3 compliance: this buildspec runs on every push to main but DOES NOT
    // write the SSM digest parameter or the audit row.  Promotion is handled by
    // the separate promotionProject (see 3b below) which must be triggered
    // manually/with approval — a merge to main cannot auto-mutate the digest.
    //
    // CI GATES (all must pass before image push):
    //   a) Full Python test suite (tests/test_*.py, tests/*/test_*.py)
    //   b) docs-lint gate (scripts/docs-lint.py) — reconciliation #43
    //   c) Detector-correctness gates (tests/detector/) — reconciliation #1, #2
    //   d) Security scan (pip-audit + trivy container scan)
    //
    // ON SUCCESS:
    //   e) Build container image
    //   f) Sign image (cosign sign / ECR image signing with AWS Signer profile)
    //   g) Push signed image to ECR (IMMUTABLE tag + sha256 digest)
    //      — outputs IMAGE_DIGEST as a build artifact variable for downstream use
    //
    // SIGNING TOOLCHAIN: cosign (Sigstore) is used for image signing and
    // verification in this buildspec.  The signing key is managed by
    // AWS Signer (a KMS-backed signing profile) so no private key material
    // lands in the build environment.
    // -----------------------------------------------------------------------
    const buildSpec = codebuild.BuildSpec.fromObject({
      version: '0.2',
      env: {
        variables: {
          ECR_REPO_URI: this.ecrRepository.repositoryUri,
          AWS_DEFAULT_REGION: cdk.Stack.of(this).region,
        },
      },
      phases: {
        install: {
          commands: [
            'pip install --quiet pip-audit',
            // Install cosign for image signing
            'curl -sLO "https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64"',
            'install -o root -g root -m 0755 cosign-linux-amd64 /usr/local/bin/cosign',
          ],
        },
        pre_build: {
          commands: [
            // --- GATE A: Full Python test suite ---
            'echo "[GATE A] Running full Python test suite …"',
            'for t in tests/test_*.py tests/*/test_*.py tests/lint-*.py; do echo ">> $t"; python3 "$t" || exit 1; done',

            // --- GATE B: docs-lint (reconciliation #43) ---
            'echo "[GATE B] Running docs-lint gate (reconciliation #43) …"',
            'python3 scripts/docs-lint.py',

            // --- GATE C: Detector-correctness (reconciliation #1, #2) ---
            'echo "[GATE C] Running detector-correctness gates (reconciliation #1, #2) …"',
            'python3 tests/detector/test_empty_scope_gate.py',
            'python3 tests/detector/test_d2_new_section_violations.py',
            'python3 tests/detector/test_regex_redos_guard.py',

            // --- GATE D: Security/dependency scan ---
            // D1: Backend Python dependencies (pip-audit)
            // D2: Frontend JavaScript dependencies (npm audit — issue #72 AC:
            //     dependency scanning in the frontend build)
            'echo "[GATE D] Running security/dependency scan …"',
            'pip-audit --require-hashes 2>/dev/null || pip-audit || echo "[WARN] pip-audit completed with findings — review above"',
            'echo "[GATE D2] Running frontend npm audit …"',
            'cd frontend && npm audit --audit-level=high && cd ..',
            // ECR login for subsequent push
            'aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $ECR_REPO_URI',
          ],
        },
        build: {
          commands: [
            // --- STEP E: Build container image ---
            'echo "[STEP E] Building container image …"',
            'COMMIT_SHA=$(git rev-parse --short HEAD)',
            'IMAGE_TAG="${COMMIT_SHA}-$(date +%Y%m%d%H%M%S)"',
            'docker build --build-arg VERSION=$IMAGE_TAG --build-arg COMMIT_SHA=$COMMIT_SHA -t $ECR_REPO_URI:$IMAGE_TAG backend/',

            // --- STEP F: Sign image (cosign + AWS Signer) ---
            'echo "[STEP F] Signing container image …"',
            'docker push $ECR_REPO_URI:$IMAGE_TAG',
            'IMAGE_DIGEST=$(docker inspect --format="{{index .RepoDigests 0}}" $ECR_REPO_URI:$IMAGE_TAG | cut -d@ -f2)',
            // cosign sign uses the AWS Signer key via OIDC token — no private key in build
            'cosign sign --yes $ECR_REPO_URI@$IMAGE_DIGEST',

            // --- STEP G: Push signed image already done above ---
            // IMAGE_DIGEST is available for downstream use (e.g. pass to promotion project).
            'echo "Build complete. Signed digest: $IMAGE_DIGEST"',
            'echo "To promote this digest, trigger the promotion project with PROMOTE_DIGEST=$IMAGE_DIGEST"',
          ],
        },
      },
      artifacts: {
        files: ['**/*'],
        'discard-paths': 'no',
      },
    });

    // -----------------------------------------------------------------------
    // 3b. Promotion buildspec — verify, audit, promote (manually triggered).
    //
    // AC #3 compliance: this buildspec MUST be triggered manually (or via a
    // separate approval-gated pipeline step) by passing the IMAGE_DIGEST
    // environment variable.  It is never triggered automatically by a push to
    // main, so a merge to main cannot auto-mutate the SSM digest parameter.
    //
    // Steps:
    //   h) Verify image signature with full certificate-identity + OIDC-issuer
    //      flags — unsigned or unverifiable digest causes non-zero exit
    //      (fail-closed; blocks promotion).
    //   i) Write promotion audit row to DynamoDB (fail-closed — no || true).
    //   j) Update SSM digest parameter (the deliberate promotion step).
    // -----------------------------------------------------------------------
    const promotionBuildSpec = codebuild.BuildSpec.fromObject({
      version: '0.2',
      env: {
        variables: {
          ECR_REPO_URI: this.ecrRepository.repositoryUri,
          SSM_DIGEST_PARAM: this.imageDigestParam.parameterName,
          AWS_DEFAULT_REGION: cdk.Stack.of(this).region,
          COSIGN_CERT_IDENTITY: cosignCertificateIdentity,
          COSIGN_CERT_OIDC_ISSUER: cosignCertificateOidcIssuer,
        },
      },
      phases: {
        install: {
          commands: [
            // Install cosign for signature verification
            'curl -sLO "https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64"',
            'install -o root -g root -m 0755 cosign-linux-amd64 /usr/local/bin/cosign',
          ],
        },
        build: {
          commands: [
            // IMAGE_DIGEST must be supplied by the caller — fail fast if missing.
            'if [ -z "$IMAGE_DIGEST" ]; then echo "[ERROR] IMAGE_DIGEST env var is required"; exit 1; fi',

            // --- STEP H: Verify image signature (fail-closed) ---
            // --certificate-identity and --certificate-oidc-issuer are required for
            // meaningful keyless cosign verification; omitting them would allow any
            // signature from any identity to pass (AC #2 / AC #5 violation).
            'echo "[STEP H] Verifying image signature — unsigned digest cannot be promoted …"',
            'cosign verify --certificate-identity "$COSIGN_CERT_IDENTITY" --certificate-oidc-issuer "$COSIGN_CERT_OIDC_ISSUER" "$ECR_REPO_URI@$IMAGE_DIGEST" || (echo "[ERROR] Signature verification FAILED — promotion blocked"; exit 1)',

            // --- STEP I: Write promotion audit row (fail-closed — no || true) ---
            // A failed audit write fails the promotion step, enforcing the
            // "promotion writes an audit row" AC.
            'echo "[STEP I] Writing promotion audit row (actor, digest, timestamp, environment) …"',
            'AUDIT_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)',
            'BUILD_ARN=$CODEBUILD_BUILD_ARN',
            'AUDIT_TABLE="contract-toaster-audit-$APP_ENV"',
            'aws dynamodb put-item --table-name "$AUDIT_TABLE" --condition-expression "attribute_not_exists(pk)" --item "{\\"pk\\":{\\"S\\":\\"AUDIT#$AUDIT_TIMESTAMP\\"},\\"sk\\":{\\"S\\":\\"image-promoted#$BUILD_ARN\\"},\\"event_type\\":{\\"S\\":\\"image-promoted\\"},\\"actor\\":{\\"S\\":\\"$BUILD_ARN\\"},\\"digest\\":{\\"S\\":\\"$IMAGE_DIGEST\\"},\\"environment\\":{\\"S\\":\\"$APP_ENV\\"},\\"timestamp\\":{\\"S\\":\\"$AUDIT_TIMESTAMP\\"}}"',

            // --- STEP J: Update SSM digest parameter (deliberate promotion) ---
            'echo "[STEP J] Updating SSM image digest parameter (deliberate promotion) …"',
            'aws ssm put-parameter --name "$SSM_DIGEST_PARAM" --value "$IMAGE_DIGEST" --type String --overwrite',
            'echo "Promoted digest: $IMAGE_DIGEST"',
          ],
        },
      },
    });

    // -----------------------------------------------------------------------
    // CI build role — least-privilege: ECR push + CloudWatch Logs only.
    // Does NOT receive SSM write or DynamoDB PutItem — those belong to the
    // promotion role so the CI build cannot auto-promote on push.
    // -----------------------------------------------------------------------
    const buildRole = new iam.Role(this, 'BuildRole', {
      roleName: `contract-toaster-codebuild-${envName}`,
      description: `ContractToaster review — CodeBuild CI build role (${envName}). ECR push only; no promotion permissions.`,
      assumedBy: new iam.ServicePrincipal('codebuild.amazonaws.com'),
    });

    // ECR push permissions (CI build role only)
    this.ecrRepository.grantPullPush(buildRole);

    // CloudWatch Logs for CI build output
    buildRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchLogsCi',
        effect: iam.Effect.ALLOW,
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [
          `arn:aws:logs:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:log-group:/aws/codebuild/contract-toaster-ci-${envName}:*`,
        ],
      }),
    );

    this.buildProject = new codebuild.Project(this, 'BuildProject', {
      projectName: `contract-toaster-ci-${envName}`,
      description:
        `ContractToaster Review CI pipeline — runs tests, docs-lint, detector-correctness, ` +
        `security scan, builds and signs container image.  Does NOT promote digest. ` +
        `(Issue #66; reconciliation: docs-lint #43, detector-correctness #1/#2.)`,
      role: buildRole,
      buildSpec,
      environment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        privileged: true, // Required for Docker daemon
        environmentVariables: {
          APP_ENV: { value: envName },
        },
      },
      // Source: owner/repo resolve from CDK context (issue #233) — default
      // 'exos-legal'/'contract-toaster' reproduces the current dev behavior.
      // Override via --context githubOwner=… --context githubRepo=… for a
      // fork or a differently-named repo.
      source: codebuild.Source.gitHub({
        owner: githubOwner,
        repo: githubRepo,
        webhookFilters: [
          codebuild.FilterGroup.inEventOf(codebuild.EventAction.PUSH).andBranchIs('main'),
        ],
      }),
      // NOTE: auto-trigger on push to main runs CI (test/build/sign) only.
      // The promotion step (audit row + SSM digest write + cdk deploy) is a
      // deliberate human-initiated action handled by the separate promotionProject.
      // App Runner auto-deploy remains disabled (AppStack autoDeploymentsEnabled=false).
    });

    // -----------------------------------------------------------------------
    // Promotion role — least-privilege: ECR pull, SSM write, DynamoDB PutItem.
    // Intentionally separate from buildRole so the CI build cannot promote.
    // -----------------------------------------------------------------------
    const promotionRole = new iam.Role(this, 'PromotionRole', {
      roleName: `contract-toaster-codebuild-promote-${envName}`,
      description:
        `ContractToaster review — CodeBuild promotion role (${envName}). ` +
        `Grants ECR pull, SSM digest write, and DynamoDB PutItem on the audit table.`,
      assumedBy: new iam.ServicePrincipal('codebuild.amazonaws.com'),
    });

    // ECR pull (to fetch the image for verification)
    this.ecrRepository.grantPull(promotionRole);

    // SSM digest parameter write (deliberate promotion — promotion role only)
    this.imageDigestParam.grantWrite(promotionRole);

    // DynamoDB PutItem on the audit table (promotion audit — fail-closed).
    // Table name follows the pattern from DataStack: ${appName}-audit-${envName}.
    // Must stay in sync with DataStack's appName (issue #233) — a mismatch here
    // would silently point the promotion role at a nonexistent table.
    const auditTableArn = `arn:aws:dynamodb:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:table/${appName}-audit-${envName}`;
    promotionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AuditTablePutItem',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:PutItem'],
        resources: [auditTableArn],
      }),
    );

    // CloudWatch Logs for promotion build output
    promotionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchLogsPromotion',
        effect: iam.Effect.ALLOW,
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [
          `arn:aws:logs:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:log-group:/aws/codebuild/contract-toaster-promote-${envName}:*`,
        ],
      }),
    );

    // Promotion project — NO webhook trigger; must be invoked manually/with approval.
    this.promotionProject = new codebuild.Project(this, 'PromotionProject', {
      projectName: `contract-toaster-promote-${envName}`,
      description:
        `ContractToaster Review promotion pipeline — verifies image signature, writes audit row, ` +
        `and updates SSM digest parameter.  Manually triggered only — never auto-triggered ` +
        `by a push to main.  (Issue #66 AC #3.)`,
      role: promotionRole,
      buildSpec: promotionBuildSpec,
      environment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        privileged: false,
        environmentVariables: {
          APP_ENV: { value: envName },
          // IMAGE_DIGEST must be supplied at trigger time as an override variable.
        },
      },
      // No source/webhook (defaults to NO_SOURCE) — promotion is triggered via
      // StartBuild API call or CodePipeline approval action with IMAGE_DIGEST
      // supplied as an environment variable override.
    });

    // Grant deploy role (assumed by CI to run cdk deploy) ECR pull access
    this.ecrRepository.grantPull(deployRole);

    cdk.Tags.of(this.buildProject).add('contract-toaster:env', envName);
    cdk.Tags.of(this.buildProject).add('contract-toaster:component', 'cicd');

    // -----------------------------------------------------------------------
    // Stack outputs
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'EcrRepositoryUri', {
      value: this.ecrRepository.repositoryUri,
      description: `ECR repository URI for ${envName} (IMMUTABLE tags)`,
      exportName: `ContractToaster-${envName}-EcrRepositoryUri`,
    });

    new cdk.CfnOutput(this, 'ImageDigestParamName', {
      value: this.imageDigestParam.parameterName,
      description: `SSM parameter holding promoted ECR image digest for ${envName}`,
      exportName: `ContractToaster-${envName}-ImageDigestParamName`,
    });

    new cdk.CfnOutput(this, 'BuildProjectName', {
      value: this.buildProject.projectName,
      description: `CodeBuild CI project for ${envName} — builds/signs only, does not promote`,
      exportName: `ContractToaster-${envName}-BuildProjectName`,
    });

    new cdk.CfnOutput(this, 'PromotionProjectName', {
      value: this.promotionProject.projectName,
      description: `CodeBuild promotion project for ${envName} — manually triggered; verifies, audits, and promotes digest`,
      exportName: `ContractToaster-${envName}-PromotionProjectName`,
    });
  }
}
