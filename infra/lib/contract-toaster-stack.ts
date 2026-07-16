import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';
import { NetworkStack } from './nested/network-stack';
import { KmsKeysStack } from './nested/kms-keys-stack';
import { DataStack } from './nested/data-stack';
import { AuthStack } from './nested/auth-stack';
import { AppStack } from './nested/app-stack';
import { PipelineStack } from './nested/pipeline-stack';
import { WafStack } from './nested/waf-stack';
import { FrontendStack } from './nested/frontend-stack';
import { ObservabilityStack } from './nested/observability-stack';
import { CicdStack } from './nested/cicd-stack';

export interface ContractToasterStackProps extends cdk.StackProps {
  /** Environment name: 'dev' or 'prod' */
  readonly envName: string;
}

/**
 * ContractToasterStack — top-level CDK stack for the ContractToaster review tool.
 *
 * This stack is the composition root.  It owns:
 *  1. The environment-scoped customer-managed KMS key (CMK) — retained as a
 *     baseline / fallback key for resources not yet assigned a data-class key.
 *  2. Per-data-class CMKs (#70) — owned by KmsKeysStack (nested):
 *     - uploadsKey:   incoming contract .docx files
 *     - outputsKey:   generated redlines and result packets
 *     - corpusKey:    standard-form corpus, S3 Vectors, clause-text store (#32)
 *     - auditKey:     audit log objects + CloudTrail (tighter break-glass)
 *     - dynamodbKey:  all DynamoDB tables
 *  3. Base IAM roles:
 *     - deployRole:         used by CI to deploy CDK stacks (empty permission set
 *                           until specific resources are added in downstream issues).
 *     - appRunnerTaskRole:  assumed by the App Runner service container (empty
 *                           permission set until #55 wires App Runner).
 *  4. Eight nested stacks, each owning a logical domain:
 *     - NetworkStack        VPC, subnets, security groups (#55+)
 *     - KmsKeysStack        Per-data-class CMKs with narrow key policies (#70)
 *     - DataStack           S3 buckets, DynamoDB tables (#51, #52)
 *     - AuthStack           Cognito + Google IdP (#53)
 *     - AppStack            App Runner + Step Functions (#55, #59)
 *     - FrontendStack       Amplify Hosting + React SPA (#54)
 *     - ObservabilityStack  CloudWatch, CloudTrail (#57)
 *     - CicdStack           CodeBuild, ECR (IMMUTABLE + image signing),
 *                           digest-promotion mechanism, promotion audit (#66)
 *
 * Account and region are resolved from CDK context so dev and prod are
 * always deployed into separate AWS accounts (never the same account).
 * Stack names follow the convention 'contract-toaster-{envName}'.
 *
 * Security invariants enforced at skeleton level:
 *  - Fail-closed auth: Cognito pre-token Lambda rejects on error (see #53).
 *  - Per-data-class KMS: five CMKs with narrow key policies (#70).
 *  - Split API role + scoped download: separate roles (deploy vs. task) here;
 *    scoped pre-signed download URLs in #71.
 *  - XSS/CSP: Amplify CSP header (#72); no innerHTML with user content (#54).
 *  - Hostile-file AV: upload scanning in #63.
 *  - Pointer-only payloads: Step Functions inputs carry S3/DDB refs only (#59).
 *  - No doc substance in logs: enforced in Lambda/task configurations (#55+).
 *  - Watermark: every reviewer output carries attorney-approval watermark (#59).
 */
export class ContractToasterStack extends cdk.Stack {
  readonly envKmsKey: kms.Key;
  readonly deployRole: iam.Role;
  readonly appRunnerTaskRole: iam.Role;

  readonly networkStack: NetworkStack;
  readonly kmsKeysStack: KmsKeysStack;
  readonly dataStack: DataStack;
  readonly authStack: AuthStack;
  readonly appStack: AppStack;
  readonly pipelineStack: PipelineStack;
  /**
   * WAF WebACL fronting App Runner. Undefined under `--context
   * profile=minimal` (issue #231) -- the minimal deploy profile omits WAF
   * entirely so a demo/OSS adopter can stand up ContractToaster without it.
   */
  readonly wafStack?: WafStack;
  readonly frontendStack: FrontendStack;
  readonly observabilityStack: ObservabilityStack;
  readonly cicdStack: CicdStack;

  constructor(scope: Construct, id: string, props: ContractToasterStackProps) {
    super(scope, id, props);

    const { envName } = props;

    // -----------------------------------------------------------------------
    // Resource-name / identity context (issue #233, #316, #349).
    //
    // These read from CDK context so a NEW deployment (open-source adopter,
    // the eventual prod account) can pick its own app name, GitHub source,
    // alarm destination, callback domain, admin identity, and Cognito
    // hosted-domain enforcement without touching source.
    //
    // `githubOwner`, `alarmsEmail`, `appDomain`, `adminEmail`, and
    // `hostedDomain` have NO internal default (issues #316, #349) — a
    // context-less `cdk synth`/`cdk deploy` must not wire a stranger's stack
    // to this project's own GitHub source, alarm inbox, domain, admin
    // identity, or sign-in enforcement domain. Synth fails closed, naming
    // every missing key, until the caller supplies all five explicitly.
    // `appName` and `githubRepo` keep defaulting to 'contract-toaster' —
    // they're just a resource-name prefix and repo name, not another
    // tenant's identity.
    //   --context appName=acmecorp        (default: 'contract-toaster')
    //   --context githubOwner=…           (REQUIRED — no default)
    //   --context githubRepo=…            (default: 'contract-toaster')
    //   --context alarmsEmail=…           (REQUIRED — no default)
    //   --context appDomain=…             (REQUIRED — no default)
    //   --context adminEmail=…            (REQUIRED — no default)
    //   --context hostedDomain=…          (REQUIRED — no default)
    // -----------------------------------------------------------------------
    const appName = (this.node.tryGetContext('appName') as string | undefined) ?? 'contract-toaster';
    const githubRepo = (this.node.tryGetContext('githubRepo') as string | undefined) ?? 'contract-toaster';
    const githubOwner = this.node.tryGetContext('githubOwner') as string | undefined;
    const alarmsEmail = this.node.tryGetContext('alarmsEmail') as string | undefined;
    const appDomain = this.node.tryGetContext('appDomain') as string | undefined;
    const adminEmail = this.node.tryGetContext('adminEmail') as string | undefined;
    const hostedDomain = this.node.tryGetContext('hostedDomain') as string | undefined;

    const missingIdentityContext = (
      [
        ['githubOwner', githubOwner],
        ['alarmsEmail', alarmsEmail],
        ['appDomain', appDomain],
        ['adminEmail', adminEmail],
        ['hostedDomain', hostedDomain],
      ] as const
    )
      .filter(([, value]) => !value)
      .map(([key]) => key);
    if (missingIdentityContext.length > 0) {
      throw new Error(
        `Missing required CDK context: ${missingIdentityContext.join(', ')}. ` +
          "Supply them explicitly, e.g. --context githubOwner=<owner> " +
          '--context alarmsEmail=<email> --context appDomain=<domain> ' +
          '--context adminEmail=<email> --context hostedDomain=<domain> ' +
          '(see infra/lib/contract-toaster-stack.ts, issues #316, #349).',
      );
    }

    // -----------------------------------------------------------------------
    // Deploy profile (issue #231): 'minimal' | 'hardened' (default).
    //   --context profile=minimal    no WAF, no NAT/interface endpoints, no
    //                                CloudTrail/Object Lock, AWS-managed (not
    //                                customer-managed) S3/DynamoDB encryption.
    //   --context profile=hardened   unchanged pre-#231 behavior (default).
    // Each nested stack re-resolves `profile` independently via
    // node.tryGetContext('profile') (see kms-keys-stack.ts, data-stack.ts,
    // network-stack.ts, observability-stack.ts); this stack only needs it to
    // decide whether to instantiate WafStack at all.
    // -----------------------------------------------------------------------
    const profile = (this.node.tryGetContext('profile') as string | undefined) ?? 'hardened';
    const isMinimalProfile = profile === 'minimal';

    // -----------------------------------------------------------------------
    // Environment-scoped customer-managed KMS key
    //
    // One CMK per environment.  Per-data-class keys (uploads, redlines, corpus,
    // audit) will be derived from this in issue #70.  Key rotation is enabled;
    // keys are retained on stack deletion to prevent accidental data loss.
    // -----------------------------------------------------------------------
    this.envKmsKey = new kms.Key(this, 'EnvKmsKey', {
      description: `ContractToaster review — environment KMS key (${envName})`,
      enableKeyRotation: true,
      alias: `alias/${appName}-${envName}`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.envKmsKey).add('contract-toaster:env', envName);
    cdk.Tags.of(this.envKmsKey).add('contract-toaster:data-class', 'env-baseline');

    // -----------------------------------------------------------------------
    // Base IAM roles — empty permission sets; filled in by downstream issues.
    //
    // deployRole:
    //   Assumed by CI (CodeBuild / GitHub Actions OIDC) to run `cdk deploy`.
    //   Will receive scoped permissions in #66 (CI pipeline issue).
    //
    // appRunnerTaskRole:
    //   Assumed by the App Runner service container at runtime.
    //   Will receive scoped S3, DynamoDB, Bedrock, Step Functions permissions
    //   in #55 and #71 when the actual App Runner service is defined.
    // -----------------------------------------------------------------------
    this.deployRole = new iam.Role(this, 'DeployRole', {
      roleName: `${appName}-deploy-${envName}`,
      description: `ContractToaster review — CDK deploy role (${envName})`,
      assumedBy: new iam.ServicePrincipal('codebuild.amazonaws.com'),
    });

    this.appRunnerTaskRole = new iam.Role(this, 'AppRunnerTaskRole', {
      roleName: `${appName}-apprunner-task-${envName}`,
      description: `ContractToaster review — App Runner task role (${envName})`,
      assumedBy: new iam.ServicePrincipal('tasks.apprunner.amazonaws.com'),
    });

    cdk.Tags.of(this.deployRole).add('contract-toaster:env', envName);
    cdk.Tags.of(this.appRunnerTaskRole).add('contract-toaster:env', envName);

    // -----------------------------------------------------------------------
    // Nested stacks — each owns a logical infrastructure domain.
    //
    // Order matters for dependency resolution:
    //   1. KmsKeysStack must be instantiated before DataStack so the per-class
    //      key references are available to thread into DataStack props.
    // -----------------------------------------------------------------------
    this.networkStack = new NetworkStack(this, 'Network', { envName });

    // KmsKeysStack: per-data-class CMKs with narrow key policies (#70).
    // Must come before DataStack so keys are available to S3 and DynamoDB.
    // Per-data-class roles (uploadWriterRole, corpusReaderRole, auditWriterRole,
    // outputsRole, dynamodbRole) are created in #55/#71.  Until then, no runtime
    // principal receives any grant — keys-only.
    this.kmsKeysStack = new KmsKeysStack(this, 'KmsKeys', {
      envName,
      appName,
      deployRole: this.deployRole,
      // Per-data-class runtime role props are intentionally omitted here until
      // role-splitting in #55/#71 produces the distinct roles.
    });

    this.dataStack = new DataStack(this, 'Data', {
      envName,
      appName,
      uploadsKey: this.kmsKeysStack.uploadsKey,
      outputsKey: this.kmsKeysStack.outputsKey,
      corpusKey: this.kmsKeysStack.corpusKey,
      auditKey: this.kmsKeysStack.auditKey,
      dynamodbKey: this.kmsKeysStack.dynamodbKey,
    });

    // Non-null assertions below: the throw guard above already verified
    // githubOwner/alarmsEmail/appDomain/adminEmail/hostedDomain are all present.
    this.authStack = new AuthStack(this, 'Auth', {
      envName,
      appName,
      appDomain: appDomain!,
      adminEmail: adminEmail!,
      hostedDomain: hostedDomain!,
    });

    // PipelineStack: Step Functions review pipeline skeleton with a mock
    // review Lambda (issue #59, mock-first MVP scope per epic #123). Created
    // before AppStack references its state machine ARN below.
    this.pipelineStack = new PipelineStack(this, 'Pipeline', {
      envName,
      stateMachineKey: this.kmsKeysStack.stateMachineKey,
      dynamodbKey: this.kmsKeysStack.dynamodbKey,
      uploadsBucket: this.dataStack.uploadsBucket,
      outputsBucket: this.dataStack.outputsBucket,
      reviewsTable: this.dataStack.reviewsTable,
      reviewSubmissionsTable: this.dataStack.reviewSubmissionsTable,
      dailySpendTable: this.dataStack.dailySpendTable,
      costLedgerTable: this.dataStack.costLedgerTable,
      pipelineSemaphoreTable: this.dataStack.pipelineSemaphoreTable,
      retentionSettingsTable: this.dataStack.retentionSettingsTable,
    });

    // CicdStack: CodeBuild project(s), ECR repository with image signing,
    // the digest-promotion mechanism. (Issue #66.)
    //
    // Instantiated here — BEFORE AppStack — so AppStack can be given the real
    // ECR repository URI (issue #224). CicdStack has no dependency on
    // AppStack/WafStack/FrontendStack/ObservabilityStack, so this ordering is
    // safe; those stacks are unaffected.
    //
    // githubOwner / githubRepo: the GitHub source CodeBuild watches for the CI
    // webhook trigger (issue #233 — was hard-coded to contract-opf/contract-toaster,
    // directly contradicting the comment that repo config isn't baked in here).
    // Override via CDK context (--context githubOwner=… / githubRepo=…) for a
    // fork or a differently-named repo.
    //
    // cosignCertificateIdentity / cosignCertificateOidcIssuer: these values must
    // match the signing workflow that produced the image.  The default identity
    // is derived from githubOwner/githubRepo and reflects the GHA ci-pipeline.yml
    // workflow signing job (issue #66 AC #5).  Override via CDK context
    // (--context cosignCertId=… / cosignOidcIssuer=…) for environments that use
    // a different signing workflow.
    this.cicdStack = new CicdStack(this, 'Cicd', {
      envName,
      appName,
      githubOwner: githubOwner!,
      githubRepo,
      deployRole: this.deployRole,
      cosignCertificateIdentity:
        (this.node.tryGetContext('cosignCertId') as string | undefined) ??
        `https://github.com/${githubOwner}/${githubRepo}/.github/workflows/ci-pipeline.yml@refs/heads/main`,
      cosignCertificateOidcIssuer:
        (this.node.tryGetContext('cosignOidcIssuer') as string | undefined) ??
        'https://token.actions.githubusercontent.com',
    });

    // -----------------------------------------------------------------------
    // Digest promotion (issue #224): `--context imageDigest=sha256:<digest>`
    // is the deliberate promotion/rollback mechanism documented in
    // RUNBOOK.md "Deploying a code change" / "Rolling back a code deploy".
    // Threaded straight into AppStack; undefined until the first promotion,
    // in which case AppStack falls back to a public bootstrap image so the
    // day-one deploy still succeeds (see app-stack.ts).
    // -----------------------------------------------------------------------
    const imageDigestContext = this.node.tryGetContext('imageDigest') as string | undefined;

    this.appStack = new AppStack(this, 'App', {
      envName,
      appRunnerTaskRole: this.appRunnerTaskRole,
      vpc: this.networkStack.vpc,
      cognitoUserPoolId: this.authStack.userPool.userPoolId,
      cognitoAppClientId: this.authStack.userPoolClient.userPoolClientId,
      stateMachineKey: this.kmsKeysStack.stateMachineKey,
      ecrImageDigest: imageDigestContext,
      ecrRepositoryUri: this.cicdStack.ecrRepository.repositoryUri,
    });

    // WafStack: WAF v2 WebACL fronting the App Runner service (issue #71 AC4).
    //
    // Controls: AWS managed rule groups (OWASP top-10 + known-bad-inputs),
    // request-size cap (50 MiB), rate limits on upload (10 req/5 min) and
    // polling (60 req/5 min) endpoints.  Per-user concurrency and daily limits
    // are enforced in the application layer (DynamoDB conditional write) and
    // complement the IP-based WAF rate rules here.
    //
    // The App Runner service ARN is passed so the WebACL is associated with
    // the service at deploy time.  The association is conditional; cdk synth
    // succeeds with the placeholder ARN from AppStack.
    //
    // Skipped entirely under profile=minimal (issue #231) -- see the
    // `wafStack` property doc above.
    if (!isMinimalProfile) {
      this.wafStack = new WafStack(this, 'Waf', {
        envName,
        appRunnerServiceArn: this.appStack.appRunnerService.attrServiceArn,
      });
    }

    // FrontendStack (#54; CSP connect-src + SPA rewrite fix #226):
    // cognitoHostedUiDomain / apiOrigin are threaded in so the Amplify CSP
    // connect-src directive can allow the real Cognito hosted-UI token
    // exchange and the cross-origin App Runner API — see frontend-stack.ts
    // for why a bare 'self' breaks both after a real deploy.
    this.frontendStack = new FrontendStack(this, 'Frontend', {
      envName,
      cognitoHostedUiDomain: this.authStack.hostedUiDomain,
      apiOrigin: this.appStack.appRunnerService.attrServiceUrl,
    });

    this.observabilityStack = new ObservabilityStack(this, 'Observability', {
      envName,
      alarmsEmail: alarmsEmail!,
      appRunnerServiceName: this.appStack.appRunnerService.serviceName,
      stateMachine: this.pipelineStack.stateMachine,
      auditArchiveBucket: this.dataStack.auditArchiveBucket,
      auditKey: this.kmsKeysStack.auditKey,
    });

    // -----------------------------------------------------------------------
    // Stack-level tags
    // -----------------------------------------------------------------------
    cdk.Tags.of(this).add('contract-toaster:env', envName);
    cdk.Tags.of(this).add('contract-toaster:service', 'contract-toaster');

    // -----------------------------------------------------------------------
    // Stack outputs
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'EnvKmsKeyArn', {
      value: this.envKmsKey.keyArn,
      description: `Environment-scoped CMK ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-EnvKmsKeyArn`,
    });

    new cdk.CfnOutput(this, 'DeployRoleArn', {
      value: this.deployRole.roleArn,
      description: `CDK deploy role ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-DeployRoleArn`,
    });

    new cdk.CfnOutput(this, 'AppRunnerTaskRoleArn', {
      value: this.appRunnerTaskRole.roleArn,
      description: `App Runner task role ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-AppRunnerTaskRoleArn`,
    });
  }
}
