import * as cdk from 'aws-cdk-lib';
import * as apprunner from 'aws-cdk-lib/aws-apprunner';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';

export interface AppStackProps extends cdk.NestedStackProps {
  readonly envName: string;
  readonly appRunnerTaskRole: iam.IRole;
  /**
   * VPC from NetworkStack so the App Runner VPC connector can reach the
   * data plane (S3, DynamoDB, Bedrock KB) privately.
   * Required from issue #55 onward.
   */
  readonly vpc: ec2.IVpc;
  /**
   * CMK for the Step Functions state machine (issue #19).
   *
   * Supplying this key enables CMK encryption on the state machine, so that
   * execution-history records (state inputs and outputs) are encrypted at rest
   * with a customer-managed key rather than an AWS-managed default.
   *
   * POINTER-ONLY PAYLOAD RULE (issue #19):
   *   Step Functions records the input and output of every state in execution
   *   history — retained, console-visible, with no classification boundary.
   *   To prevent document substance landing in that unclassified store, state
   *   payloads MUST carry only S3 pointers, content hashes, and non-substantive
   *   metadata.  Document text, prompts, retrieved precedent, and model output
   *   MUST NOT pass inline between states; they travel via the encrypted S3
   *   buckets keyed by review_id.
   *
   * EXECUTION-HISTORY SUBSTANCE SCAN (issue #166; originally deferred as a
   * #59 acceptance criterion):
   *   A real runtime check -- not just a static source-regex -- executes the
   *   pipeline's stage chain (the real infra/lambda/mock_review/handler.py,
   *   plus the other stages' pass-through stub behavior) and asserts, via a
   *   GetExecutionHistory-shaped scan of the actual state input/output JSON,
   *   that no state carries document text, prompt text, retrieved-precedent
   *   text, or model output -- only pointers/hashes/enums/short fixed
   *   strings. See tests/test_pipeline_execution_history_scan.py and
   *   .github/workflows/pipeline-execution-history-gate.yml.
   *
   * Optional until #59 creates the state machine.
   */
  readonly stateMachineKey?: kms.IKey;
  /**
   * ECR image digest — the immutable, signed digest of the container image
   * to deploy.  Format: "sha256:<hex>".
   *
   * IMPORTANT: App Runner is pinned to this digest. Auto-deploy from the
   * GitHub `main` branch is DISABLED (see below). A merge to main must NEVER
   * auto-mutate production legal behavior. Promotion to a new digest is a
   * deliberate, audited step performed via the CI-pipeline issue (#66):
   * `cd infra && cdk deploy --context imageDigest=sha256:<digest> --context
   * env=<env>` (RUNBOOK.md "Deploying a code change" / "Rolling back a code
   * deploy"). The composition root (ContractToasterStack) threads the
   * `imageDigest` CDK context value straight through as this prop.
   *
   * DAY-ONE BOOTSTRAP (issue #224): when this is `undefined` — i.e. no
   * promotion has ever happened yet — the service is sourced from a public
   * "hello world" bootstrap image instead of a private ECR digest (see
   * `usingBootstrapImage` below).  A private-ECR placeholder digest can
   * never resolve to a real image, so pinning to one unconditionally
   * guarantees the first `cdk deploy` fails.
   */
  readonly ecrImageDigest?: string;
  /**
   * ECR repository URI.  Format: "<account>.dkr.ecr.<region>.amazonaws.com/<name>".
   * Real value comes from CicdStack.ecrRepository.repositoryUri (threaded in
   * by the composition root, issue #224).  Defaults to a placeholder for
   * local synth / tests that construct AppStack in isolation.
   */
  readonly ecrRepositoryUri?: string;
  /**
   * Cognito User Pool ID from AuthStack (issue #55 AC: JWT verification env vars).
   * Injected as COGNITO_USER_POOL_ID at runtime so auth.py can derive the JWKS URL.
   * Defaults to a placeholder for local synth.
   */
  readonly cognitoUserPoolId?: string;
  /**
   * Cognito App Client ID from AuthStack (issue #55 AC: JWT audience verification).
   * Injected as COGNITO_APP_CLIENT_ID at runtime.
   * Defaults to a placeholder for local synth.
   */
  readonly cognitoAppClientId?: string;
}

/**
 * AppStack — App Runner service + Step Functions pipeline (issue #55, #59).
 *
 * Issue #55: App Runner + hello-world container.
 *
 * Resources defined here:
 *  1. Security group for the App Runner VPC connector (outbound-only;
 *     App Runner manages inbound via its own load balancer — security Phase 0).
 *  2. App Runner VPC connector so the service container reaches the data
 *     plane (S3, DynamoDB, Bedrock KB) privately without traversing the
 *     public internet.
 *  3. IAM role that App Runner assumes to pull images from ECR
 *     (ecr:GetAuthorizationToken + ecr:BatchGetImage on the repo).
 *  4. App Runner CfnService pinned to an immutable ECR image digest:
 *       - Auto-deploy from main is DISABLED.  Promotion to a new digest is
 *         deliberate (CI-pipeline issue #66 owns the build/sign/push/promote
 *         flow; this stack only consumes a digest).
 *       - VPC connector wired to the private subnets.
 *       - API task role from the parent stack (least-privilege; no bedrock
 *         inference actions — inference runs under the pipeline task role).
 *  5. Least-privilege task role policy additions:
 *       - start-review: sfn:StartExecution on the review state machine.
 *       - read-status:  sfn:DescribeExecution on the review state machine.
 *       - upload:       s3:PutObject on the uploads bucket prefix.
 *       - download:     s3:GetObject on the outputs bucket prefix (scoped;
 *                       full scoped pre-signed download wired in #71).
 *       - dynamodb:     dynamodb:GetItem, PutItem, Query, UpdateItem on
 *                       reviews, review_submissions, users tables.
 *       - dynamodb:     dynamodb:PutItem ONLY on the audit table (issue #191)
 *                       — append-only, matching the documented audit posture
 *                       (ARCHITECTURE.md "Audit posture"). UpdateItem/DeleteItem
 *                       on the audit table are denied to every application role
 *                       by the table's own resource policy
 *                       (DataStack._addAuditImmutabilityPolicy), independent of
 *                       what this role is granted.
 *       - dynamodb:     dynamodb:GetItem on the sync_status table (issue #191)
 *                       — read-only Workspace/SSO sync-status visibility
 *                       (GET /api/users/sync-status).
 *       - dynamodb:     dynamodb:GetItem, UpdateItem on the retention_settings
 *                       table (issue #191) — admin retention-window read/change
 *                       (GET/POST /api/admin/retention).
 *       - bedrock inference actions are EXPLICITLY EXCLUDED — inference runs under
 *         the pipeline task role (see the async-pipeline issue #59).
 *
 * Security invariants:
 *  - Auto-deploy from main is DISABLED on the App Runner service.
 *    A merge to main must NEVER auto-mutate production. Promotion is deliberate.
 *  - The service is sourced from an ECR image pinned to an immutable DIGEST
 *    (not a mutable tag).  The CI pipeline is responsible for building,
 *    signing, pushing, and promoting a new digest (#66).
 *  - The VPC connector restricts outbound traffic to private subnets.
 *    Data resources (S3, DynamoDB, Bedrock KB) are reachable via VPC
 *    endpoints or the NAT gateway, never over direct public internet.
 *  - CloudWatch must never log document content, rationales, or PII.
 *    The App Runner service is configured with --no-access-log in the
 *    Dockerfile CMD to suppress request/response body logging.
 *  - Step Functions payloads carry S3 pointers and hashes ONLY — no document
 *    text, prompts, retrieved precedent, or model output (issue #19).
 *    Substantive content travels via the encrypted S3 buckets, never inline.
 *  - The state machine is CMK-encrypted via stateMachineKey (issue #19).
 *    Wired in #59 when the state machine construct is created.
 */
export class AppStack extends cdk.NestedStack {
  /** App Runner VPC connector ARN (consumed by the CfnService). */
  readonly vpcConnector: apprunner.CfnVpcConnector;
  /** App Runner CfnService. */
  readonly appRunnerService: apprunner.CfnService;
  /** IAM role for App Runner to pull images from ECR. */
  readonly accessRole: iam.Role;

  constructor(scope: Construct, id: string, props: AppStackProps) {
    super(scope, id, props);

    const { envName, appRunnerTaskRole, vpc } = props;

    // -----------------------------------------------------------------------
    // Digest promotion wiring (issue #224).
    //
    // `props.ecrImageDigest` is the `imageDigest` CDK context value, threaded
    // through by the composition root. When it is supplied (deliberate
    // promotion / rollback per RUNBOOK.md), pin App Runner to the real,
    // signed image in the private ECR repo at that digest.
    //
    // When it is NOT supplied — day one, before the CI pipeline (#66) has
    // ever built and promoted a first image — there is no real digest to pin
    // to. The private-ECR placeholder digest can never resolve to a real
    // image, so pinning to it unconditionally guarantees the first
    // `cdk deploy` fails. Instead, source the service from a public,
    // always-available "hello world" bootstrap image so day-one bootstrap
    // succeeds. Promote to the real image afterward via the explicit
    // `--context imageDigest=sha256:<digest>` re-deploy.
    // -----------------------------------------------------------------------
    const promotedDigest = props.ecrImageDigest;
    const usingBootstrapImage = !promotedDigest;
    const ecrRepositoryUri = props.ecrRepositoryUri ?? `123456789012.dkr.ecr.us-east-1.amazonaws.com/contract-toaster-${envName}`;
    // Public, always-existing sample image published by AWS for App Runner
    // quick-starts. Requires no authenticationConfiguration / access role —
    // ECR_PUBLIC pulls are unauthenticated.
    const BOOTSTRAP_IMAGE_IDENTIFIER = 'public.ecr.aws/aws-containers/hello-app-runner:latest';

    // Cognito env vars — real values come from AuthStack outputs (threaded in via props).
    // Defaults are placeholders so `cdk synth` succeeds before AuthStack is wired.
    const cognitoUserPoolId = props.cognitoUserPoolId ?? `us-east-1_PLACEHOLDER`;
    const cognitoAppClientId = props.cognitoAppClientId ?? `PLACEHOLDER_CLIENT_ID`;
    const awsRegion = cdk.Stack.of(this).region;

    // -----------------------------------------------------------------------
    // Security group for the VPC connector.
    //
    // All outbound traffic is allowed so the container can reach:
    //   - S3 / DynamoDB via VPC Gateway endpoints (free, no NAT).
    //   - Cognito JWKS endpoint (public) via NAT gateway for token verification.
    //   - Step Functions via VPC interface endpoint (added in #59).
    //
    // Inbound: App Runner manages inbound via its own managed load balancer;
    // no inbound rules are needed on this security group.
    // -----------------------------------------------------------------------
    const vpcConnectorSg = new ec2.SecurityGroup(this, 'VpcConnectorSg', {
      vpc,
      securityGroupName: `contract-toaster-apprunner-connector-${envName}`,
      description:
        'Security group for the ContractToaster Review App Runner VPC connector. ' +
        'Outbound only — App Runner manages inbound via its managed LB.',
      allowAllOutbound: true,
    });
    cdk.Tags.of(vpcConnectorSg).add('contract-toaster:env', envName);
    cdk.Tags.of(vpcConnectorSg).add('contract-toaster:component', 'app-runner');

    // -----------------------------------------------------------------------
    // VPC connector — uses the private subnets so the container does not
    // traverse the public internet to reach data resources.
    // -----------------------------------------------------------------------
    this.vpcConnector = new apprunner.CfnVpcConnector(this, 'VpcConnector', {
      vpcConnectorName: `contract-toaster-vpc-connector-${envName}`,
      subnets: vpc.privateSubnets.map((s) => s.subnetId),
      securityGroups: [vpcConnectorSg.securityGroupId],
    });
    cdk.Tags.of(this.vpcConnector).add('contract-toaster:env', envName);

    // -----------------------------------------------------------------------
    // ECR access role — allows App Runner to pull images from ECR.
    // Scoped to ECR read-only actions only.
    // -----------------------------------------------------------------------
    this.accessRole = new iam.Role(this, 'AppRunnerAccessRole', {
      roleName: `contract-toaster-apprunner-ecr-access-${envName}`,
      description:
        `ContractToaster Review — App Runner ECR access role (${envName}). ` +
        'Allows App Runner to pull images from ECR. Read-only.',
      assumedBy: new iam.ServicePrincipal('build.apprunner.amazonaws.com'),
    });
    this.accessRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EcrPull',
        effect: iam.Effect.ALLOW,
        actions: [
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage',
          'ecr:DescribeImages',
        ],
        resources: [`arn:aws:ecr:*:*:repository/contract-toaster-${envName}`],
      }),
    );
    this.accessRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EcrAuthToken',
        effect: iam.Effect.ALLOW,
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*'],
      }),
    );
    cdk.Tags.of(this.accessRole).add('contract-toaster:env', envName);

    // -----------------------------------------------------------------------
    // Least-privilege API task role permissions (issue #71 — split API role).
    //
    // Capabilities (split per AC, issue #71):
    //   start-review   — sfn:StartExecution on the review state machine.
    //   read-status    — sfn:DescribeExecution on the review state machine.
    //   upload         — s3:PutObject on the uploads bucket prefix.
    //                    KMS encryption-context: {"contract-toaster:data-class":"uploads","contract-toaster:review-id":"<id>"}
    //                    enforced via kms:EncryptionContext condition key so the
    //                    uploads key only encrypts objects with the correct context.
    //   download       — s3:GetObject on the outputs bucket prefix for presigned URL
    //                    generation (issue #71).  Presigned URLs are generated only
    //                    after owner/admin check; Cache-Control: no-store is set on
    //                    the response.  KMS encryption-context:
    //                    {"contract-toaster:data-class":"outputs","contract-toaster:review-id":"<id>"} is
    //                    checked on every GetObject so the outputs key only decrypts
    //                    objects written under the expected context.
    //                    Additionally: kms:GenerateDataKey on the outputs CMK so the
    //                    presigned URL path can work end-to-end.
    //   dynamodb       — GetItem, PutItem, Query, UpdateItem on review tables.
    //
    // bedrock inference actions are EXPLICITLY EXCLUDED — inference runs under the
    // pipeline task role (issue #59).  This is an intentional design constraint:
    // the API layer must not be able to invoke Bedrock directly.
    //
    // NOTE: ARN placeholders used below.  Real ARNs are wired in #59 and #71
    // when the state machine and bucket resources are created and exported.
    //
    // KMS ENCRYPTION-CONTEXT ENFORCEMENT (issue #71 AC):
    //   The outputs CMK key policy (KmsKeysStack.outputsKey) will be tightened in
    //   the real key policy (via addToResourcePolicy) to require:
    //     kms:EncryptionContextKeys: ["contract-toaster:data-class", "contract-toaster:review-id"]
    //   so that a GetObject with the outputs key only succeeds when the request
    //   carries the expected per-review encryption context.  This prevents a
    //   compromised role from using the outputs key to decrypt objects from a
    //   different data class.
    // -----------------------------------------------------------------------
    const taskRolePolicy = new iam.Policy(this, 'ApiTaskRolePolicy', {
      policyName: `contract-toaster-api-task-policy-${envName}`,
      document: new iam.PolicyDocument({
        statements: [
          // start-review + read-status
          new iam.PolicyStatement({
            sid: 'StartReview',
            effect: iam.Effect.ALLOW,
            actions: ['states:StartExecution'],
            resources: [
              `arn:aws:states:*:*:stateMachine:contract-toaster-${envName}`,
            ],
          }),
          new iam.PolicyStatement({
            sid: 'ReadStatus',
            effect: iam.Effect.ALLOW,
            actions: ['states:DescribeExecution', 'states:GetExecutionHistory'],
            resources: [
              `arn:aws:states:*:*:execution:contract-toaster-${envName}:*`,
            ],
          }),
          // upload — uploads bucket prefix only.
          // KMS encryption-context: {"contract-toaster:data-class":"uploads","contract-toaster:review-id":"<id>"}
          // The kms:EncryptionContextKeys condition below constrains WHICH context
          // keys may appear; it does NOT by itself require that any are present
          // (ForAllValues is subset-semantics — vacuously true for an empty
          // context; see the note on the condition).  The authoritative "both
          // keys must be present and correct" enforcement is the uploads CMK
          // key-policy DENY in KmsKeysStack (StringNotEquals on data-class + Null
          // check on review-id).
          new iam.PolicyStatement({
            sid: 'Upload',
            effect: iam.Effect.ALLOW,
            actions: ['s3:PutObject'],
            resources: [
              `arn:aws:s3:::contract-toaster-uploads-${envName}/uploads/*`,
            ],
            conditions: {
              // Constrain the KMS encryption context for GenerateDataKey to the
              // allowed key set {contract-toaster:data-class, contract-toaster:review-id}.  NOTE this is
              // subset-semantics: ForAllValues:StringEquals is satisfied by any
              // subset of the listed keys (including an EMPTY context), so it
              // does NOT require either key to be present — it only rejects
              // *other* keys.  Presence-and-value enforcement lives in the
              // uploads CMK key policy (the DENY statements in KmsKeysStack).
              // NOTE: the set-qualifier and operator must be a single key
              // "ForAllValues:StringEquals" — two separate keys would be
              // non-existent operators and cause MalformedPolicyDocument on deploy.
              'ForAllValues:StringEquals': {
                'kms:EncryptionContextKeys': [
                  'contract-toaster:data-class',
                  'contract-toaster:review-id',
                ],
              },
            },
          }),
          // download — outputs bucket prefix only (issue #71 presigned URL path).
          // Presigned URLs are generated only after owner/admin check passes;
          // Cache-Control: no-store is set in the API response headers.
          // The kms:EncryptionContextKeys condition below only constrains WHICH
          // context keys may appear (subset-semantics — see the note on the
          // condition); it does NOT guarantee the correct per-review context is
          // present.  Cross-review / cross-data-class decryption is actually
          // prevented by the outputs CMK key-policy DENY in KmsKeysStack.
          new iam.PolicyStatement({
            sid: 'Download',
            effect: iam.Effect.ALLOW,
            actions: ['s3:GetObject'],
            resources: [
              `arn:aws:s3:::contract-toaster-outputs-${envName}/outputs/*`,
            ],
            conditions: {
              // Constrain the KMS encryption context for Decrypt (SSE-KMS
              // GetObject) to the allowed key set {contract-toaster:data-class,
              // contract-toaster:review-id}.  NOTE this is subset-semantics:
              // ForAllValues:StringEquals is satisfied by any subset of the
              // listed keys (including an EMPTY context), so it does NOT require
              // either key to be present — it only rejects *other* keys.  The
              // GetObject-without-correct-context path is denied by the outputs
              // CMK key policy (the DENY statements in KmsKeysStack).
              // NOTE: the set-qualifier and operator must be a single key
              // "ForAllValues:StringEquals" — two separate keys would be
              // non-existent operators and cause MalformedPolicyDocument on deploy.
              'ForAllValues:StringEquals': {
                'kms:EncryptionContextKeys': [
                  'contract-toaster:data-class',
                  'contract-toaster:review-id',
                ],
              },
            },
          }),
          // dynamodb — review tables only, no audit table mutations
          new iam.PolicyStatement({
            sid: 'ReviewDynamoDb',
            effect: iam.Effect.ALLOW,
            actions: [
              'dynamodb:GetItem',
              'dynamodb:PutItem',
              'dynamodb:Query',
              'dynamodb:UpdateItem',
            ],
            resources: [
              `arn:aws:dynamodb:*:*:table/contract-toaster-reviews-${envName}`,
              `arn:aws:dynamodb:*:*:table/contract-toaster-reviews-${envName}/index/*`,
              `arn:aws:dynamodb:*:*:table/contract-toaster-review-submissions-${envName}`,
              `arn:aws:dynamodb:*:*:table/contract-toaster-users-${envName}`,
            ],
          }),
          // audit — append-only PutItem ONLY (issue #191). No GetItem, Query,
          // UpdateItem, or DeleteItem: the audit table is write-only from the
          // API's perspective, and UpdateItem/DeleteItem are additionally
          // denied to every application role by the table's own resource
          // policy (DataStack._addAuditImmutabilityPolicy). Written by
          // src/users.py::_write_audit_entry and src/retention.py::
          // _write_audit_entry (user admin-flag changes, retention changes,
          // legal-hold placements/releases).
          new iam.PolicyStatement({
            sid: 'AuditWrite',
            effect: iam.Effect.ALLOW,
            actions: ['dynamodb:PutItem'],
            resources: [`arn:aws:dynamodb:*:*:table/contract-toaster-audit-${envName}`],
          }),
          // sync_status — read-only (issue #191). GET /api/users/sync-status
          // (src/users.py::get_sync_status) only ever reads this table; no
          // route mutates it from the API.
          new iam.PolicyStatement({
            sid: 'SyncStatusRead',
            effect: iam.Effect.ALLOW,
            actions: ['dynamodb:GetItem'],
            resources: [`arn:aws:dynamodb:*:*:table/contract-toaster-sync-status-${envName}`],
          }),
          // retention_settings — read + update (issue #191). GET/POST
          // /api/admin/retention (src/retention.py::get_retention_settings,
          // request_retention_change) read the single global-settings row and
          // update_item it (update_item upserts, so no PutItem is needed).
          new iam.PolicyStatement({
            sid: 'RetentionSettingsReadWrite',
            effect: iam.Effect.ALLOW,
            actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem'],
            resources: [`arn:aws:dynamodb:*:*:table/contract-toaster-retention-settings-${envName}`],
          }),
          // bedrock inference actions are INTENTIONALLY ABSENT from this role.
          // Inference runs under the pipeline task role (#59).
        ],
      }),
    });
    taskRolePolicy.attachToRole(appRunnerTaskRole);

    // -----------------------------------------------------------------------
    // App Runner CfnService — pinned to an immutable ECR image digest.
    //
    // AUTO-DEPLOY IS DISABLED (see design note below).
    //
    // Design note — auto-deploy from main MUST be disabled (security Phase 0):
    //   A merge to `main` must NOT immediately alter production legal behavior.
    //   This service is pinned to a specific signed image digest.  Promotion
    //   to a new digest is a deliberate, audited step performed via the
    //   CI-pipeline (#66).  The CI pipeline builds, tests, signs, and pushes
    //   a new image, then updates the `ecrImageDigest` CDK context value and
    //   re-deploys this stack.  No auto-mutation on raw push.
    //
    // imageConfiguration — environment variables injected at runtime:
    //   COGNITO_USER_POOL_ID    — from AuthStack userPool.userPoolId (props.cognitoUserPoolId).
    //   COGNITO_APP_CLIENT_ID   — from AuthStack userPoolClient.userPoolClientId (props.cognitoAppClientId).
    //   AWS_REGION              — from cdk.Stack.of(this).region.
    //   VERSION / COMMIT_SHA / IMAGE_DIGEST — baked into the image at build
    //                             time via Dockerfile ARG/ENV; the values
    //                             here are overrides for emergency use only.
    //
    // DynamoDB/S3 env vars (issue #191) — every mounted /api/* route
    // dereferences these via os.environ[...] (backend/src/users.py:71-79,
    // backend/src/retention.py:80-88,357-358; documented at
    // backend/src/main.py:34-43). Prior to #191 only the three Cognito/region
    // vars above were injected, so the first authenticated request to any
    // admin route raised a KeyError -> HTTP 500 in a deployed environment.
    // Table/bucket names follow the `contract-toaster-<resource>-${envName}`
    // convention DataStack uses to create them (data-stack.ts tableName /
    // bucket definitions) — the same convention already relied on for the
    // reviews/review-submissions/users table ARNs and the uploads/outputs
    // bucket ARNs in the task role policy below.
    //   USERS_TABLE               — src/users.py user rows.
    //   AUDIT_TABLE                — src/users.py + src/retention.py append-only audit rows.
    //   SYNC_STATUS_TABLE         — src/users.py Workspace/SSO sync-job status.
    //   REVIEWS_TABLE              — src/retention.py review rows (purge preview/sweep, legal holds).
    //   RETENTION_SETTINGS_TABLE  — src/retention.py global retention-window settings.
    //   UPLOADS_BUCKET             — src/retention.py purge-sweep target bucket.
    //   OUTPUTS_BUCKET             — src/retention.py purge-sweep target bucket.
    // -----------------------------------------------------------------------
    const runtimeEnvironmentVariables = [
      {
        name: 'COGNITO_USER_POOL_ID',
        value: cognitoUserPoolId,
      },
      {
        name: 'COGNITO_APP_CLIENT_ID',
        value: cognitoAppClientId,
      },
      {
        name: 'AWS_REGION',
        value: awsRegion,
      },
      {
        name: 'USERS_TABLE',
        value: `contract-toaster-users-${envName}`,
      },
      {
        name: 'AUDIT_TABLE',
        value: `contract-toaster-audit-${envName}`,
      },
      {
        name: 'SYNC_STATUS_TABLE',
        value: `contract-toaster-sync-status-${envName}`,
      },
      {
        name: 'REVIEWS_TABLE',
        value: `contract-toaster-reviews-${envName}`,
      },
      {
        name: 'RETENTION_SETTINGS_TABLE',
        value: `contract-toaster-retention-settings-${envName}`,
      },
      {
        name: 'UPLOADS_BUCKET',
        value: `contract-toaster-uploads-${envName}`,
      },
      {
        name: 'OUTPUTS_BUCKET',
        value: `contract-toaster-outputs-${envName}`,
      },
    ];

    this.appRunnerService = new apprunner.CfnService(this, 'AppRunnerService', {
      serviceName: `contract-toaster-api-${envName}`,
      sourceConfiguration: usingBootstrapImage
        ? {
            // DAY-ONE BOOTSTRAP (issue #224) — no digest has ever been
            // promoted. ECR_PUBLIC pulls need no accessRoleArn.
            autoDeploymentsEnabled: false, // AUTO-DEPLOY DISABLED — deliberate digest promotion only
            imageRepository: {
              imageIdentifier: BOOTSTRAP_IMAGE_IDENTIFIER,
              imageRepositoryType: 'ECR_PUBLIC',
              imageConfiguration: {
                port: '8080',
                runtimeEnvironmentVariables,
              },
            },
          }
        : {
            authenticationConfiguration: {
              accessRoleArn: this.accessRole.roleArn,
            },
            autoDeploymentsEnabled: false, // AUTO-DEPLOY DISABLED — deliberate digest promotion only
            imageRepository: {
              imageIdentifier: `${ecrRepositoryUri}@${promotedDigest}`,
              imageRepositoryType: 'ECR',
              imageConfiguration: {
                port: '8080',
                runtimeEnvironmentVariables,
              },
            },
          },
      networkConfiguration: {
        egressConfiguration: {
          egressType: 'VPC',
          vpcConnectorArn: this.vpcConnector.attrVpcConnectorArn,
        },
        ingressConfiguration: {
          isPubliclyAccessible: true, // App Runner manages TLS + inbound; private-only would break user access
        },
      },
      instanceConfiguration: {
        instanceRoleArn: appRunnerTaskRole.roleArn,
      },
      healthCheckConfiguration: {
        path: '/health',
        protocol: 'HTTP',
        interval: 10,
        timeout: 5,
        healthyThreshold: 1,
        unhealthyThreshold: 5,
      },
    });

    cdk.Tags.of(this.appRunnerService).add('contract-toaster:env', envName);
    cdk.Tags.of(this.appRunnerService).add('contract-toaster:component', 'api');

    // -----------------------------------------------------------------------
    // Stack outputs
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'AppRunnerServiceArn', {
      value: this.appRunnerService.attrServiceArn,
      description: `App Runner service ARN (${envName})`,
      exportName: `ContractToaster-${envName}-AppRunnerServiceArn`,
    });

    new cdk.CfnOutput(this, 'AppRunnerServiceUrl', {
      value: this.appRunnerService.attrServiceUrl,
      description: `App Runner service URL (${envName})`,
      exportName: `ContractToaster-${envName}-AppRunnerServiceUrl`,
    });
  }
}
