import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import { Construct } from 'constructs';
import * as path from 'path';

export interface PipelineStackProps extends cdk.NestedStackProps {
  readonly envName: string;
  /** CMK for the Step Functions state machine (issue #19). */
  readonly stateMachineKey: kms.IKey;
  /**
   * CMK for DynamoDB tables (semaphore, submissions). Undefined under
   * `--context profile=minimal` (issue #231) -- KmsKeysStack does not
   * create the per-data-class CMKs in the minimal profile; the pipeline
   * DynamoDB role then receives no KMS grant (the tables themselves fall
   * back to AWS-managed encryption in DataStack).
   */
  readonly dynamodbKey?: kms.IKey;
  readonly uploadsBucket: s3.IBucket;
  readonly outputsBucket: s3.IBucket;
  readonly reviewsTable: dynamodb.ITable;
  readonly reviewSubmissionsTable: dynamodb.ITable;
  readonly dailySpendTable: dynamodb.ITable;
  readonly costLedgerTable: dynamodb.ITable;
  readonly pipelineSemaphoreTable: dynamodb.ITable;
  /** Retention window + dual-control/delay settings table (issue #61). */
  readonly retentionSettingsTable: dynamodb.ITable;
}

// ---------------------------------------------------------------------------
// RAG / context caps (issue #59 AC): pinned config values, wired even though
// the stages are stubbed in Phase 0. A 1M-token model context window does
// not justify million-token reviews -- these are hard per-review ceilings.
// Values match ARCHITECTURE.md -> Cost shape -> "Per-review token caps".
// ---------------------------------------------------------------------------
const MAX_INPUT_TOKENS = 80_000; // per pass (system + user prompt combined)
const MAX_OUTPUT_TOKENS = 8_000; // per pass (structured JSON response)
const MAX_EXTRACTED_TOKENS = 60_000; // extracted-document text budget, pre-prompt-assembly
const MAX_SECTIONS = 200; // max sections considered per document
const TOP_K = 8; // top-K retrieved precedents per section
const MAX_RETRIES = 1; // max_retries_per_pass -- one bounded structured-output retry

// Primary/critic review model IDs (model-policy/bedrock-us-east-1.json).
// Single-region native IDs only -- never a global./us./eu./apac. prefixed
// cross-region inference profile (see ARCHITECTURE.md -> Model-selection
// policy). pipelineReviewRole's bedrock:InvokeModel grant is scoped to
// EXACTLY these two model ARNs -- never a foundation-model/* wildcard --
// per the issue #59/#60 reconciliation (see Check G below).
const PRIMARY_MODEL_ID = 'anthropic.claude-opus-4-8';
const CRITIC_MODEL_ID = 'anthropic.claude-sonnet-4-6';

/** Maximum plausible review duration (execution-level timeout backstop). */
const EXECUTION_TIMEOUT = cdk.Duration.minutes(15);

/** Concurrency cap -- caps simultaneous pipeline executions (issue #59 AC). */
const MAX_CONCURRENT_EXECUTIONS = 5;

/**
 * PipelineStack -- Step Functions review pipeline skeleton (issue #59).
 *
 * MOCK-FIRST MVP SCOPE (epic #123): this stack builds the real orchestration
 * plumbing -- state machine, idempotency-adjacent infra, concurrency
 * control, cost ledger, pointer-only payloads, least-privilege pipeline
 * role -- with a MOCK review Lambda standing in for the real
 * extract/retrieve/primary-review/adversarial-review/redline LLM stages.
 * The mock task is the single, explicitly-labeled swap point: when #80-#83
 * land behind the #62 eval gate, only the mock-review task target changes.
 *
 * Stage skeleton (each a stubbed Lambda task in Phase 0):
 *   extract -> retrieve -> primary review (mock) -> adversarial review (mock)
 *   -> redline -> persist -> audit
 *
 * Every stage:
 *   - has its own timeout (`timeout` on the LambdaInvoke task) and retry
 *     policy (`.addRetry`).
 *   - is wrapped by a Catch-all (`.addCatch`) that routes to a shared
 *     "TransitionToError" task recording the failing stage name, so no
 *     review is left wedged in PENDING (issue #59 AC).
 *
 * NO SQS anywhere on the review entry path -- the API calls StartExecution
 * directly (see backend/src/reviews.py); Step Functions IS the queue.
 *
 * Pointer-only payloads (issue #19): every state's input/output carries only
 * review_id, playbook_id, S3 keys, and hashes -- never document text, prompt
 * text, or model output. Step Functions execution history has no
 * classification boundary and is console-visible, so nothing substantive may
 * pass through it; substantive content always travels via the encrypted S3
 * buckets keyed by review_id.
 *
 * Concurrency control: a DynamoDB-backed semaphore (the AWS-documented
 * "Step Functions + DynamoDB lock" pattern) caps simultaneous executions at
 * MAX_CONCURRENT_EXECUTIONS. Each held slot carries a TTL lease aligned to
 * the execution-level timeout, so a hard-killed execution (process kill,
 * Lambda OOM, Fargate SIGKILL) cannot leak a slot permanently even though
 * its Catch/finally release state never ran -- the orphan reconciler is the
 * reaper half of this recovery mechanism.
 *
 * Execution-level timeout: in addition to per-step timeouts, the state
 * machine itself carries an overall EXECUTION_TIMEOUT so a pathological
 * execution stuck in a non-retrying wait cannot leak a concurrency slot and
 * spend reservation indefinitely (ARCHITECTURE.md -> "Execution-level
 * timeout").
 *
 * Least privilege + data-class isolation (issue #70 AC B): NO single IAM
 * principal may hold kms:Decrypt on more than one data-class CMK. Because
 * this pipeline's stages collectively touch uploads (read), outputs
 * (read/write), and DynamoDB (read/write), the pipeline is split into
 * PER-DATA-CLASS roles rather than one shared "pipeline task role" —
 * mirroring the split-role pattern ARCHITECTURE.md already prescribes for
 * the API (upload / review-start / read-status / download capabilities).
 * bedrock:InvokeModel / Knowledge-Base query permissions are granted ONLY
 * to the review-stage role (`pipelineReviewRole`), which holds no S3 or
 * DynamoDB grants at all — it is the sole grantee of those Bedrock actions
 * anywhere in this infra tree; the API task role (app-stack.ts) explicitly
 * excludes them.
 *
 * RECONCILED least-privilege invariant (issue #59 Check G x issue #60):
 * pipelineReviewRole's bedrock:InvokeModel grant is scoped to EXACTLY the
 * primary (Opus) and critic (Sonnet) review model ARNs — never a
 * foundation-model/* wildcard. pipelineReviewRole remains the ONLY role
 * anywhere in this infra tree granted bedrock:InvokeModel on those two
 * model ARNs, and the ONLY role granted bedrock:Retrieve /
 * bedrock:RetrieveAndGenerate. Issue #60's `corpusKnowledgeBaseRole`
 * (data-stack.ts, assumed by bedrock.amazonaws.com) separately holds
 * bedrock:InvokeModel scoped ONLY to the embedding-model ARN
 * (amazon.titan-embed-text-v2) for KB ingestion — this is required by how
 * Bedrock Knowledge Base ingestion works (the KB's own service role must
 * invoke the embedding model) and does not grant access to the primary/
 * critic review models. The two grants are resource-ARN-disjoint by
 * construction; no principal other than pipelineReviewRole can invoke the
 * primary/critic models or query the Knowledge Base.
 */
export class PipelineStack extends cdk.NestedStack {
  readonly stateMachine: sfn.StateMachine;
  /** Bedrock InvokeModel / Knowledge-Base query role — no S3/DynamoDB grants. */
  readonly pipelineReviewRole: iam.Role;
  /** Uploads-bucket read-only role (extract stage). */
  readonly pipelineUploadsRole: iam.Role;
  /** Outputs-bucket read/write role (redline stage). */
  readonly pipelineOutputsRole: iam.Role;
  /** DynamoDB read/write role (state bookkeeping: retrieve/persist/audit/semaphore). */
  readonly pipelineDynamoDbRole: iam.Role;
  readonly reconcilerFunction: lambda.Function;
  /** Retention purge worker (issue #61) -- scheduled + on-demand invoke. */
  readonly purgeWorkerFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: PipelineStackProps) {
    super(scope, id, props);

    const {
      envName,
      stateMachineKey,
      dynamodbKey,
      uploadsBucket,
      outputsBucket,
      reviewsTable,
      reviewSubmissionsTable,
      dailySpendTable,
      costLedgerTable,
      pipelineSemaphoreTable,
      retentionSettingsTable,
    } = props;

    const lambdaPrincipal = new iam.ServicePrincipal('lambda.amazonaws.com');

    // -----------------------------------------------------------------------
    // pipelineReviewRole -- Bedrock InvokeModel (primary/critic models ONLY)
    // + Knowledge Base query ONLY. No S3 or DynamoDB grants: this role never
    // touches a data-class key, so it cannot violate AC B no matter what
    // Bedrock/KB permissions it holds. This is the ONLY role in the whole
    // infra tree granted bedrock:InvokeModel scoped to the primary/critic
    // model ARNs, and the ONLY role granted Knowledge Base query permissions
    // (issue #59 AC, reconciled with issue #60 -- see the class docstring's
    // "RECONCILED least-privilege invariant" note).
    // -----------------------------------------------------------------------
    this.pipelineReviewRole = new iam.Role(this, 'PipelineReviewRole', {
      roleName: `contract-toaster-pipeline-review-${envName}`,
      description:
        `ContractToaster review — mock/primary/adversarial review stage role (${envName}). ` +
        'The ONLY role granted bedrock:InvokeModel scoped to the primary/critic model ARNs ' +
        'and Knowledge Base query permissions; holds no S3 or DynamoDB grants ' +
        '(data-class isolation, issue #70 AC B). A separate, disjoint-scoped Bedrock KB ' +
        'service role (data-stack.ts corpusKnowledgeBaseRole) may hold InvokeModel scoped ' +
        'ONLY to the embedding-model ARN for ingestion (issue #60 reconciliation).',
      assumedBy: lambdaPrincipal,
    });
    this.pipelineReviewRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockInvokeModel',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        // Scoped to EXACTLY the primary (Opus) and critic (Sonnet) review
        // model ARNs -- never a foundation-model/* wildcard. This is the
        // ARN-scoped reconciliation of issue #59 Check G against issue #60's
        // KB service role, which separately holds InvokeModel scoped ONLY to
        // the embedding-model ARN (data-stack.ts corpusKnowledgeBaseRole).
        resources: [
          `arn:aws:bedrock:us-east-1::foundation-model/${PRIMARY_MODEL_ID}`,
          `arn:aws:bedrock:us-east-1::foundation-model/${CRITIC_MODEL_ID}`,
        ],
      }),
    );
    this.pipelineReviewRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockKnowledgeBaseQuery',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:Retrieve', 'bedrock:RetrieveAndGenerate'],
        resources: [`arn:aws:bedrock:us-east-1:${cdk.Aws.ACCOUNT_ID}:knowledge-base/*`],
      }),
    );

    // -----------------------------------------------------------------------
    // pipelineUploadsRole -- read-only on the uploads bucket (extract stage).
    // Distinct principal from pipelineOutputsRole and pipelineDynamoDbRole
    // (AC B: must not also hold outputs or dynamodb kms:Decrypt).
    // -----------------------------------------------------------------------
    this.pipelineUploadsRole = new iam.Role(this, 'PipelineUploadsRole', {
      roleName: `contract-toaster-pipeline-uploads-${envName}`,
      description: `ContractToaster review — extract-stage role (${envName}); uploads bucket read-only.`,
      assumedBy: lambdaPrincipal,
    });
    uploadsBucket.grantRead(this.pipelineUploadsRole, 'uploads/*');

    // -----------------------------------------------------------------------
    // pipelineOutputsRole -- read/write on the outputs bucket (redline
    // stage). Distinct principal from pipelineUploadsRole and
    // pipelineDynamoDbRole.
    // -----------------------------------------------------------------------
    this.pipelineOutputsRole = new iam.Role(this, 'PipelineOutputsRole', {
      roleName: `contract-toaster-pipeline-outputs-${envName}`,
      description: `ContractToaster review — redline-stage role (${envName}); outputs bucket read/write.`,
      assumedBy: lambdaPrincipal,
    });
    outputsBucket.grantReadWrite(this.pipelineOutputsRole, 'outputs/*');
    outputsBucket.grantRead(this.pipelineOutputsRole, 'mock-fixtures/*');

    // -----------------------------------------------------------------------
    // pipelineDynamoDbRole -- read/write on the pipeline's own DynamoDB
    // tables (state bookkeeping shared by acquire/release-slot, retrieve,
    // persist, and audit stages). Distinct principal from
    // pipelineUploadsRole and pipelineOutputsRole.
    // -----------------------------------------------------------------------
    this.pipelineDynamoDbRole = new iam.Role(this, 'PipelineDynamoDbRole', {
      roleName: `contract-toaster-pipeline-dynamodb-${envName}`,
      description:
        `ContractToaster review — pipeline DynamoDB bookkeeping role (${envName}); ` +
        'reviews/review_submissions/daily_spend/cost_ledger/pipeline_semaphore read/write.',
      assumedBy: lambdaPrincipal,
    });
    for (const table of [
      reviewsTable,
      reviewSubmissionsTable,
      dailySpendTable,
      costLedgerTable,
      pipelineSemaphoreTable,
    ]) {
      table.grantReadWriteData(this.pipelineDynamoDbRole);
    }
    if (dynamodbKey) {
      dynamodbKey.grantEncryptDecrypt(this.pipelineDynamoDbRole);
    }

    for (const role of [
      this.pipelineReviewRole,
      this.pipelineUploadsRole,
      this.pipelineOutputsRole,
      this.pipelineDynamoDbRole,
    ]) {
      cdk.Tags.of(role).add('contract-toaster:env', envName);
      cdk.Tags.of(role).add('contract-toaster:component', 'pipeline');
    }

    // -----------------------------------------------------------------------
    // Shared Lambda environment — context caps (issue #59 AC: "RAG / context
    // caps wired as state-machine and config limits"). Pinned config values
    // per the reconciliation note: "the caps named here get concrete numbers."
    // -----------------------------------------------------------------------
    const stageEnv: Record<string, string> = {
      REVIEWS_TABLE: reviewsTable.tableName,
      REVIEW_SUBMISSIONS_TABLE: reviewSubmissionsTable.tableName,
      DAILY_SPEND_TABLE: dailySpendTable.tableName,
      COST_LEDGER_TABLE: costLedgerTable.tableName,
      PIPELINE_SEMAPHORE_TABLE: pipelineSemaphoreTable.tableName,
      UPLOADS_BUCKET: uploadsBucket.bucketName,
      OUTPUTS_BUCKET: outputsBucket.bucketName,
      MAX_INPUT_TOKENS: String(MAX_INPUT_TOKENS),
      MAX_OUTPUT_TOKENS: String(MAX_OUTPUT_TOKENS),
      MAX_EXTRACTED_TOKENS: String(MAX_EXTRACTED_TOKENS),
      MAX_SECTIONS: String(MAX_SECTIONS),
      TOP_K: String(TOP_K),
      MAX_RETRIES: String(MAX_RETRIES),
    };

    // -----------------------------------------------------------------------
    // Stubbed stage Lambdas -- extract, retrieve, redline, persist, audit.
    // Phase 0: each is a pass-through stub (no real extraction/retrieval/
    // redline logic; that is Phase 2 / #80-#83). Kept as distinct functions
    // (not sfn.Pass states) so each has its own log group, timeout, and IAM
    // boundary identical in shape to what the real stage will need later --
    // "keep the state machine definition readable; this is the backbone for
    // Phase 2" (issue #59 Notes).
    // -----------------------------------------------------------------------
    const stubHandlerCode = lambda.Code.fromInline(
      `
def handler(event, context):
    """Phase 0 stub -- pass-through. Real logic lands in #80-#83."""
    return event
`.trim(),
    );

    // Each stage gets the role matching the ONE data class it legitimately
    // needs (issue #70 AC B: no shared "do everything" pipeline role).
    const makeStubFunction = (stageName: string, role: iam.IRole): lambda.Function =>
      new lambda.Function(this, `${stageName}StageFunction`, {
        functionName: `contract-toaster-${envName}-${stageName.toLowerCase()}`,
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'index.handler',
        code: stubHandlerCode,
        role,
        timeout: cdk.Duration.seconds(30),
        memorySize: 256,
        environment: stageEnv,
        logRetention: logs.RetentionDays.ONE_MONTH,
      });

    // extract reads the upload only -> uploads role.
    const extractFn = makeStubFunction('Extract', this.pipelineUploadsRole);
    // retrieve is state/config bookkeeping (KB query itself is real-pipeline
    // only, #60) -> dynamodb role for now.
    const retrieveFn = makeStubFunction('Retrieve', this.pipelineDynamoDbRole);
    // audit writes review/audit state -> dynamodb role. Still a Phase 0
    // pass-through stub (real logic lands in #80-#83).
    const auditFn = makeStubFunction('Audit', this.pipelineDynamoDbRole);

    // mark-running (issue #188): transitions the reviews row PENDING ->
    // RUNNING once a slot is held, so the poll loop observes RUNNING during
    // the review window. Real handler (not a stub) -- writes the reviews row,
    // so it needs the DynamoDB bookkeeping role (has reviews read/write; no
    // S3 grant). See infra/lambda/mark_running/handler.py.
    const markRunningFn = new lambda.Function(this, 'MarkRunningStageFunction', {
      functionName: `contract-toaster-${envName}-mark-running`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambda/mark_running')),
      role: this.pipelineDynamoDbRole,
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      environment: stageEnv,
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    // redline (issue #188): no longer a pass-through stub. In the mock
    // pipeline this stage materializes the output document by copying the
    // pre-baked, synthetic eiaa redline fixture (seeded into the outputs
    // bucket under mock-fixtures/ by the BucketDeployment below) into the
    // review's own outputs/<review_id>/ prefix, so GET /output has a real
    // object to serve. The copy touches ONLY the outputs data class
    // (mock-fixtures/* read + outputs/* write, same bucket + CMK), so it
    // stays on pipelineOutputsRole -- no privilege change from the stub. The
    // REAL redline generator (scripts/redline_docx_writer.py) lands with
    // #80-#83. See infra/lambda/redline/handler.py.
    const redlineFn = new lambda.Function(this, 'RedlineStageFunction', {
      functionName: `contract-toaster-${envName}-redline`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambda/redline')),
      role: this.pipelineOutputsRole,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: stageEnv,
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    // Seed the pre-baked, clearly-SYNTHETIC eiaa redline fixture into the
    // outputs bucket at mock-fixtures/eiaa/pre-baked-redline.docx (the key
    // the mock review handler points at and the redline stage copies from).
    // Generated by scripts/gen_mock_eiaa_redline_fixture.py from the real
    // tracked-changes writer. prune:false so this deployment only PUTs its
    // own object and never issues deletes against the outputs bucket (which
    // carries legal-hold / retention controls); it is scoped to the
    // mock-fixtures/ prefix only.
    new s3deploy.BucketDeployment(this, 'MockRedlineFixtureDeployment', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '../../fixtures/mock-outputs'))],
      destinationBucket: outputsBucket,
      destinationKeyPrefix: 'mock-fixtures/',
      prune: false,
      retainOnDelete: true,
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    // persist (issue #189): no longer a generic pass-through stub. The
    // stage's FULL job (writing the review's terminal status/decision,
    // copying the redline output into its permanent location) is still
    // deferred to #80-#83, but it now carries the ONE piece that could not
    // wait for that -- settling the worst-case spend reservation taken at
    // submission time so a completed review's reservation is released back
    // to the day's daily_spend budget instead of held until UTC midnight
    // (see infra/lambda/persist/handler.py for the full contract). Uses
    // pipelineDynamoDbRole -- same role the stub used, no privilege change.
    const persistFn = new lambda.Function(this, 'PersistStageFunction', {
      functionName: `contract-toaster-${envName}-persist`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambda/persist')),
      role: this.pipelineDynamoDbRole,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: stageEnv,
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    // Mock review Lambda -- the ONE swap-point (issue #59 / epic #123).
    // Stands in for BOTH the primary review and adversarial (critic) review
    // stages until the real two-pass pipeline (#80-#83) lands behind #62.
    // Uses pipelineReviewRole ONLY (bedrock + KB; no S3/DynamoDB grants) --
    // it does not read the upload or write the output directly; the
    // separate extract/redline stages own those data-class boundaries.
    const mockReviewFn = new lambda.Function(this, 'MockReviewStageFunction', {
      functionName: `contract-toaster-${envName}-mock-review`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambda/mock_review')),
      role: this.pipelineReviewRole,
      // Mock delay is a few seconds; timeout gives headroom without letting
      // a stuck mock invocation block the concurrency slot for long.
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        ...stageEnv,
        MOCK_REVIEW_DELAY_SECONDS: '3',
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    // -----------------------------------------------------------------------
    // Concurrency-semaphore acquire/release Lambdas (DynamoDB lock pattern).
    //
    // Issue #190: acquisition failure must NOT be silently ignored by the
    // state machine (see the SemaphoreSlotAcquired? Choice state below), and
    // release must never decrement a slot this execution didn't actually
    // hold. `acquire` now also tracks a bounded wait-attempt count and tells
    // the state machine when to give up (`semaphore_give_up`) instead of
    // looping forever; `release` refuses to touch the counter unless the
    // payload's own `semaphore_acquired` flag says the slot was held --
    // belt-and-suspenders alongside the state-machine-level guard, so a
    // future control-flow bug can't reintroduce the negative-drift bug.
    // -----------------------------------------------------------------------
    const semaphoreCode = lambda.Code.fromInline(
      `
import os
import time
import boto3
from botocore.exceptions import ClientError

TABLE = os.environ["PIPELINE_SEMAPHORE_TABLE"]
MAX_CONCURRENCY = int(os.environ["MAX_CONCURRENT_EXECUTIONS"])
LEASE_SECONDS = int(os.environ["LEASE_SECONDS"])
MAX_SEMAPHORE_WAIT_ATTEMPTS = int(os.environ["MAX_SEMAPHORE_WAIT_ATTEMPTS"])
COUNTER_KEY = "contract-toaster-pipeline-semaphore"


def acquire(event, context):
    """Acquire a concurrency slot with a TTL lease.

    Lease/TTL semantics (ARCHITECTURE.md -> Semaphore lease / slot-leak
    recovery): every slot entry carries an expiry aligned to the execution-
    level timeout, so a hard-killed execution's slot self-expires even if
    the release state below never runs.

    On a saturated semaphore (ConditionalCheckFailedException), this does
    NOT raise -- the caller (the state machine's SemaphoreSlotAcquired?
    Choice state) is the one authoritative gate deciding whether to wait and
    retry or give up; this function's job is only to report the outcome
    (semaphore_acquired) and the bounded wait-attempt count
    (semaphore_wait_attempts / semaphore_give_up) truthfully.
    """
    table = boto3.resource("dynamodb").Table(TABLE)
    review_id = event["review_id"]
    ttl = int(time.time()) + LEASE_SECONDS
    attempts = int(event.get("semaphore_wait_attempts", 0))
    try:
        table.update_item(
            Key={"lock_name": COUNTER_KEY},
            UpdateExpression="ADD current_count :one",
            ConditionExpression=(
                "attribute_not_exists(current_count) OR current_count < :max"
            ),
            ExpressionAttributeValues={":one": 1, ":max": MAX_CONCURRENCY},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            attempts += 1
            event["semaphore_acquired"] = False
            event["semaphore_wait_attempts"] = attempts
            event["semaphore_give_up"] = attempts >= MAX_SEMAPHORE_WAIT_ATTEMPTS
            return event
        raise
    table.put_item(
        Item={
            "lock_name": f"review-slot#{review_id}",
            "ttl": ttl,
            "acquired_at": int(time.time()),
        }
    )
    event["semaphore_acquired"] = True
    event["semaphore_wait_attempts"] = attempts
    event["semaphore_give_up"] = False
    return event


def release(event, context):
    """Release a concurrency slot (success-path finally state).

    Guarded on the payload's own semaphore_acquired flag -- NOT just the
    table's current_count > 0 condition -- so an execution that never held a
    slot (semaphore_acquired is falsy or absent) is a strict no-op here even
    if it is ever reachable by a future control-flow change. current_count
    > 0 remains as defense-in-depth against the flag being tampered with or
    missing, but it is no longer the only thing standing between "never
    acquired" and a negative-drifting counter.
    """
    if not event.get("semaphore_acquired"):
        return event
    table = boto3.resource("dynamodb").Table(TABLE)
    review_id = event["review_id"]
    table.update_item(
        Key={"lock_name": COUNTER_KEY},
        UpdateExpression="ADD current_count :neg_one",
        ConditionExpression="current_count > :zero",
        ExpressionAttributeValues={":neg_one": -1, ":zero": 0},
    )
    table.delete_item(Key={"lock_name": f"review-slot#{review_id}"})
    return event
`.trim(),
    );

    // 15s wait x 20 max attempts = 5 minutes of bounded waiting for a slot,
    // leaving comfortable headroom under EXECUTION_TIMEOUT (15 minutes) for
    // the actual pipeline stages once a slot is acquired.
    const SEMAPHORE_WAIT_SECONDS = 15;
    const MAX_SEMAPHORE_WAIT_ATTEMPTS = 20;

    const semaphoreEnv = {
      PIPELINE_SEMAPHORE_TABLE: pipelineSemaphoreTable.tableName,
      MAX_CONCURRENT_EXECUTIONS: String(MAX_CONCURRENT_EXECUTIONS),
      LEASE_SECONDS: String(EXECUTION_TIMEOUT.toSeconds()),
      MAX_SEMAPHORE_WAIT_ATTEMPTS: String(MAX_SEMAPHORE_WAIT_ATTEMPTS),
    };

    const acquireSlotFn = new lambda.Function(this, 'AcquireSemaphoreSlotFunction', {
      functionName: `contract-toaster-${envName}-semaphore-acquire`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.acquire',
      code: semaphoreCode,
      role: this.pipelineDynamoDbRole,
      timeout: cdk.Duration.seconds(10),
      environment: semaphoreEnv,
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    const releaseSlotFn = new lambda.Function(this, 'ReleaseSemaphoreSlotFunction', {
      functionName: `contract-toaster-${envName}-semaphore-release`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.release',
      code: semaphoreCode,
      role: this.pipelineDynamoDbRole,
      timeout: cdk.Duration.seconds(10),
      environment: semaphoreEnv,
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    // -----------------------------------------------------------------------
    // Error-handling terminal state -- shared Catch target for every stage.
    // Records the failing stage on the reviews row and transitions it to
    // ERROR (issue #59 AC: "no review left wedged in PENDING").
    // -----------------------------------------------------------------------
    const errorHandlerFn = new lambda.Function(this, 'TransitionToErrorFunction', {
      functionName: `contract-toaster-${envName}-transition-to-error`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline(
        `
import os
import time
import boto3

REVIEWS_TABLE = os.environ["REVIEWS_TABLE"]


def handler(event, context):
    """Shared Catch target: record the failing stage, transition to ERROR."""
    table = boto3.resource("dynamodb").Table(REVIEWS_TABLE)
    review_id = event.get("review_id") or event.get("Input", {}).get("review_id")
    error_info = event.get("error", event.get("Error", "unknown_error"))
    failing_stage = event.get("failing_stage", "unknown_stage")
    table.update_item(
        Key={"review_id": review_id},
        UpdateExpression=(
            "SET #status = :error, failing_stage = :stage, "
            "error_reason = :reason, updated_at = :now"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":error": "ERROR",
            ":stage": failing_stage,
            ":reason": str(error_info),
            ":now": str(int(time.time())),
        },
    )
    return {"review_id": review_id, "status": "ERROR", "failing_stage": failing_stage}
`.trim(),
      ),
      role: this.pipelineDynamoDbRole,
      timeout: cdk.Duration.seconds(15),
      environment: stageEnv,
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    // -----------------------------------------------------------------------
    // State machine definition.
    //
    // Every LambdaInvoke task below:
    //   - carries its own `timeout` (per-stage timeout, issue #59 AC).
    //   - calls `.addRetry(...)` (per-stage retry policy, issue #59 AC).
    //   - calls `.addCatch(errorState, { resultPath: '$.error' })` so any
    //     unhandled failure routes to the shared error-transition Lambda
    //     with the failing stage name attached (issue #59 AC).
    // -----------------------------------------------------------------------
    const withStageErrorHandling = (
      taskState: sfn.TaskStateBase,
      stageName: string,
      errorTransition: sfn.IChainable,
    ): sfn.TaskStateBase => {
      taskState.addRetry({
        errors: ['States.TaskFailed', 'Lambda.ServiceException'],
        interval: cdk.Duration.seconds(5),
        maxAttempts: 2,
        backoffRate: 2.0,
      });
      taskState.addCatch(errorTransition, {
        errors: ['States.ALL'],
        resultPath: '$.error',
      });
      return taskState;
    };

    const errorTransition = new tasks.LambdaInvoke(this, 'TransitionToError', {
      lambdaFunction: errorHandlerFn,
      payload: sfn.TaskInput.fromObject({
        'review_id.$': '$.review_id',
        'failing_stage': 'pipeline',
        'error.$': '$.error',
      }),
      timeout: cdk.Duration.seconds(15),
    }).next(new sfn.Fail(this, 'PipelineFailed', {
      cause: 'Pipeline execution failed; reviews row transitioned to ERROR.',
    }));

    const acquireSlot = new tasks.LambdaInvoke(this, 'AcquireConcurrencySlot', {
      lambdaFunction: acquireSlotFn,
      payload: sfn.TaskInput.fromJsonPathAt('$'),
      outputPath: '$.Payload',
      timeout: cdk.Duration.seconds(10),
    });
    withStageErrorHandling(acquireSlot, 'acquire_semaphore_slot', errorTransition);

    // Mark-running (issue #188): transitions the reviews row PENDING ->
    // RUNNING so the poll loop sees RUNNING during the review window (rather
    // than PENDING jumping straight to a terminal state). Placed immediately
    // before the mock review stage -- after the (instant, pass-through)
    // extract/retrieve stubs -- so RUNNING is set before the observable
    // review delay. Deliberately NOT the direct target of the
    // semaphore-acquired Choice branch: issue #190's anti-bypass invariant
    // (tests/test_concurrency_semaphore_190.py) requires that branch to route
    // straight to ExtractStage, and this stage sits inside the chain past it.
    const markReviewRunning = new tasks.LambdaInvoke(this, 'MarkReviewRunning', {
      lambdaFunction: markRunningFn,
      payload: sfn.TaskInput.fromJsonPathAt('$'),
      outputPath: '$.Payload',
      timeout: cdk.Duration.seconds(15),
    });
    withStageErrorHandling(markReviewRunning, 'mark_running', errorTransition);

    const extractStage = new tasks.LambdaInvoke(this, 'ExtractStage', {
      lambdaFunction: extractFn,
      payload: sfn.TaskInput.fromJsonPathAt('$'),
      outputPath: '$.Payload',
      timeout: cdk.Duration.minutes(2),
    });
    withStageErrorHandling(extractStage, 'extract', errorTransition);

    const retrieveStage = new tasks.LambdaInvoke(this, 'RetrieveStage', {
      lambdaFunction: retrieveFn,
      payload: sfn.TaskInput.fromJsonPathAt('$'),
      outputPath: '$.Payload',
      timeout: cdk.Duration.minutes(2),
    });
    withStageErrorHandling(retrieveStage, 'retrieve', errorTransition);

    const mockReviewStage = new tasks.LambdaInvoke(this, 'MockReviewStage', {
      lambdaFunction: mockReviewFn,
      payload: sfn.TaskInput.fromJsonPathAt('$'),
      outputPath: '$.Payload',
      // Mock stands in for BOTH primary review and adversarial review
      // (issue #59 AC: "primary review -> adversarial review" stage names
      // both map to this single mock task in Phase 0).
      timeout: cdk.Duration.minutes(3),
    });
    withStageErrorHandling(mockReviewStage, 'primary_review_mock', errorTransition);

    const redlineStage = new tasks.LambdaInvoke(this, 'RedlineStage', {
      lambdaFunction: redlineFn,
      payload: sfn.TaskInput.fromJsonPathAt('$'),
      outputPath: '$.Payload',
      timeout: cdk.Duration.minutes(2),
    });
    withStageErrorHandling(redlineStage, 'redline', errorTransition);

    const persistStage = new tasks.LambdaInvoke(this, 'PersistStage', {
      lambdaFunction: persistFn,
      payload: sfn.TaskInput.fromJsonPathAt('$'),
      outputPath: '$.Payload',
      timeout: cdk.Duration.seconds(30),
    });
    withStageErrorHandling(persistStage, 'persist', errorTransition);

    const auditStage = new tasks.LambdaInvoke(this, 'AuditStage', {
      lambdaFunction: auditFn,
      payload: sfn.TaskInput.fromJsonPathAt('$'),
      outputPath: '$.Payload',
      timeout: cdk.Duration.seconds(30),
    });
    withStageErrorHandling(auditStage, 'audit', errorTransition);

    const releaseSlot = new tasks.LambdaInvoke(this, 'ReleaseConcurrencySlot', {
      lambdaFunction: releaseSlotFn,
      payload: sfn.TaskInput.fromJsonPathAt('$'),
      outputPath: '$.Payload',
      timeout: cdk.Duration.seconds(10),
    });
    withStageErrorHandling(releaseSlot, 'release_semaphore_slot', errorTransition);

    const succeed = new sfn.Succeed(this, 'ReviewComplete');

    // -----------------------------------------------------------------------
    // Issue #190: the acquire Lambda's semaphore_acquired=false outcome must
    // actually gate the state machine -- previously nothing checked it and
    // the chain proceeded straight into extractStage regardless. This Choice
    // state is that gate:
    //   - semaphore_acquired == true  -> proceed into the real pipeline.
    //   - semaphore_give_up == true   -> bounded-wait budget exhausted; fail
    //     the execution and transition the review to ERROR (same shared
    //     errorHandlerFn every other stage uses -- "review row updated
    //     accordingly").
    //   - otherwise                   -> Wait then retry acquisition. Bounded
    //     by MAX_SEMAPHORE_WAIT_ATTEMPTS (via semaphore_give_up above), and
    //     backstopped independently by EXECUTION_TIMEOUT + the orphan
    //     reconciler's slot reaper (ARCHITECTURE.md -> "Semaphore lease /
    //     slot-leak recovery") even if that bound were ever misconfigured.
    // -----------------------------------------------------------------------
    const semaphoreSaturatedTransition = new tasks.LambdaInvoke(
      this,
      'TransitionToErrorSemaphoreSaturated',
      {
        lambdaFunction: errorHandlerFn,
        payload: sfn.TaskInput.fromObject({
          'review_id.$': '$.review_id',
          'failing_stage': 'acquire_semaphore_slot',
          'error': 'concurrency_slot_saturated: max semaphore wait attempts exceeded',
        }),
        timeout: cdk.Duration.seconds(15),
      },
    ).next(new sfn.Fail(this, 'PipelineSemaphoreSaturated', {
      cause:
        'Concurrency slot could not be acquired within the bounded wait window; ' +
        'reviews row transitioned to ERROR.',
    }));

    const waitForSemaphoreSlot = new sfn.Wait(this, 'WaitForSemaphoreSlot', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(SEMAPHORE_WAIT_SECONDS)),
    });
    waitForSemaphoreSlot.next(acquireSlot);

    const semaphoreAcquiredChoice = new sfn.Choice(this, 'SemaphoreSlotAcquired?')
      .when(sfn.Condition.booleanEquals('$.semaphore_acquired', true), extractStage)
      .when(sfn.Condition.booleanEquals('$.semaphore_give_up', true), semaphoreSaturatedTransition)
      .otherwise(waitForSemaphoreSlot);

    // Stage skeleton: extract -> retrieve -> mark-running -> primary review
    // (mock) -> adversarial review (mock, same task) -> redline -> persist ->
    // audit. Only reachable once a slot was actually acquired (the Choice's
    // acquired branch routes straight to ExtractStage -- issue #190's
    // anti-bypass invariant), so releaseSlot below is likewise only reachable
    // for an execution that actually holds a slot to release. mark-running
    // sits just before the mock review stage so the reviews row is RUNNING
    // for the observable review window.
    extractStage
      .next(retrieveStage)
      .next(markReviewRunning)
      .next(mockReviewStage)
      .next(redlineStage)
      .next(persistStage)
      .next(auditStage)
      .next(releaseSlot)
      .next(succeed);

    const definition = acquireSlot.next(semaphoreAcquiredChoice);

    // -----------------------------------------------------------------------
    // The state machine.
    //
    // - stateMachineName: contract-toaster-{env} (issue #59 AC).
    // - timeout: EXECUTION_TIMEOUT -- state-machine-level (execution-level)
    //   timeout, in addition to every stage's own per-task timeout
    //   (ARCHITECTURE.md -> "Execution-level timeout").
    // - encryptionConfiguration: CMK-encrypted execution history (issue #19).
    // -----------------------------------------------------------------------
    this.stateMachine = new sfn.StateMachine(this, 'ReviewStateMachine', {
      stateMachineName: `contract-toaster-${envName}`,
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      timeout: EXECUTION_TIMEOUT,
      encryptionConfiguration: new sfn.CustomerManagedEncryptionConfiguration(stateMachineKey),
      tracingEnabled: true,
      logs: {
        destination: new logs.LogGroup(this, 'StateMachineLogGroup', {
          logGroupName: `/aws/vendedlogs/states/contract-toaster-${envName}`,
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: cdk.RemovalPolicy.RETAIN,
        }),
        level: sfn.LogLevel.ERROR, // ERROR only -- state input/output would
        // otherwise duplicate execution-history content into CloudWatch;
        // pointer-only payload rule applies to both destinations.
        includeExecutionData: false,
      },
    });

    cdk.Tags.of(this.stateMachine).add('contract-toaster:env', envName);
    cdk.Tags.of(this.stateMachine).add('contract-toaster:component', 'pipeline');

    // -----------------------------------------------------------------------
    // Orphan reconciler (issue #59 AC: "Retry-safe execution start + orphan
    // reconciler"). Runs on a short EventBridge schedule; see
    // infra/lambda/orphan_reconciler/handler.py for the full contract.
    // -----------------------------------------------------------------------
    this.reconcilerFunction = new lambda.Function(this, 'OrphanReconcilerFunction', {
      functionName: `contract-toaster-${envName}-orphan-reconciler`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambda/orphan_reconciler')),
      timeout: cdk.Duration.minutes(2),
      memorySize: 256,
      environment: {
        REVIEWS_TABLE: reviewsTable.tableName,
        REVIEW_SUBMISSIONS_TABLE: reviewSubmissionsTable.tableName,
        SEMAPHORE_TABLE: pipelineSemaphoreTable.tableName,
        DAILY_SPEND_TABLE: dailySpendTable.tableName,
        STATE_MACHINE_ARN: this.stateMachine.stateMachineArn,
        STALE_PENDING_THRESHOLD_SECONDS: '120',
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    // Reconciler needs its own scoped grants (separate role from the
    // pipeline task role -- it drives StartExecution/DescribeExecution
    // rather than running inside an execution).
    reviewsTable.grantReadWriteData(this.reconcilerFunction);
    reviewSubmissionsTable.grantReadWriteData(this.reconcilerFunction);
    pipelineSemaphoreTable.grantReadWriteData(this.reconcilerFunction);
    dailySpendTable.grantReadWriteData(this.reconcilerFunction);
    this.stateMachine.grantRead(this.reconcilerFunction);
    this.stateMachine.grantStartExecution(this.reconcilerFunction);
    this.reconcilerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'DescribeAnyReviewExecution',
        effect: iam.Effect.ALLOW,
        actions: ['states:DescribeExecution'],
        resources: [`arn:aws:states:*:*:execution:contract-toaster-${envName}:*`],
      }),
    );

    // Short-interval schedule -- re-runs "ensure execution started" for
    // stale ARN-less submissions, and DescribeExecution-reconciles dead
    // executions, before either alarms a human (issue #59 AC).
    new events.Rule(this, 'OrphanReconcilerSchedule', {
      ruleName: `contract-toaster-${envName}-orphan-reconciler-schedule`,
      schedule: events.Schedule.rate(cdk.Duration.minutes(2)),
      targets: [new targets.LambdaFunction(this.reconcilerFunction)],
    });

    cdk.Tags.of(this.reconcilerFunction).add('contract-toaster:env', envName);
    cdk.Tags.of(this.reconcilerFunction).add('contract-toaster:component', 'pipeline');

    // -----------------------------------------------------------------------
    // Retention purge worker (issue #61). Runs on a scheduled + on-demand
    // basis (see infra/lambda/purge_worker/handler.py for the full
    // contract): deletes uploads/outputs documents past the retention
    // window and clears matching Confidential substance fields on
    // terminal `reviews` rows, honoring legal hold and the snapshot-at-
    // creation window unconditionally.
    //
    // Least-privilege (issue #61 AC): this role can delete objects ONLY in
    // the uploads/outputs buckets -- it has no grant on the corpus or
    // audit-archive buckets, and no Bedrock/Step Functions permissions.
    // A held object (contract-toaster:legal-hold=true tag) is denied at the S3
    // bucket-policy layer (data-stack.ts _addLegalHoldPolicy) regardless of
    // what this role is granted, so the storage layer -- not just this
    // Lambda's own logic -- prevents deleting a held document.
    // -----------------------------------------------------------------------
    this.purgeWorkerFunction = new lambda.Function(this, 'PurgeWorkerFunction', {
      functionName: `contract-toaster-${envName}-retention-purge-worker`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambda/purge_worker')),
      timeout: cdk.Duration.minutes(5),
      memorySize: 256,
      environment: {
        REVIEWS_TABLE: reviewsTable.tableName,
        RETENTION_SETTINGS_TABLE: retentionSettingsTable.tableName,
        UPLOADS_BUCKET: uploadsBucket.bucketName,
        OUTPUTS_BUCKET: outputsBucket.bucketName,
        RETROACTIVE_REDUCTION_DELAY_SECONDS: String(72 * 3600),
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    reviewsTable.grantReadWriteData(this.purgeWorkerFunction);
    retentionSettingsTable.grantReadWriteData(this.purgeWorkerFunction);

    // s3:ListBucket + s3:DeleteObject only -- deliberately NOT grantRead()
    // (which would add s3:GetObject and, on an SSE-KMS bucket, a
    // kms:Decrypt grant on BOTH the uploads and outputs CMKs). Neither
    // listing nor deleting an S3 object requires decrypting its content, so
    // omitting GetObject keeps this role's kms:Decrypt footprint to ONLY the
    // dynamodbKey grant below -- preserving the per-data-class KMS principal
    // isolation invariant (issue #70 AC B) even though this single role
    // legitimately spans the uploads/outputs S3 data classes for deletion
    // (ARCHITECTURE.md: "the retention purge worker can delete only in
    // uploads/outputs").
    for (const bucket of [uploadsBucket, outputsBucket]) {
      bucket.grantDelete(this.purgeWorkerFunction);
      this.purgeWorkerFunction.addToRolePolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['s3:ListBucket'],
          resources: [bucket.bucketArn],
        }),
      );
    }

    // Scheduled sweep -- daily, ahead of the admin-facing on-demand
    // invocation triggered by a settings save (that invocation path is
    // wired by the future admin API, #61 "Out of scope"; this Lambda
    // already supports on-demand invoke via the same handler entry point).
    new events.Rule(this, 'RetentionPurgeWorkerSchedule', {
      ruleName: `contract-toaster-${envName}-retention-purge-schedule`,
      schedule: events.Schedule.rate(cdk.Duration.days(1)),
      targets: [new targets.LambdaFunction(this.purgeWorkerFunction)],
    });

    cdk.Tags.of(this.purgeWorkerFunction).add('contract-toaster:env', envName);
    cdk.Tags.of(this.purgeWorkerFunction).add('contract-toaster:component', 'pipeline');

    // -----------------------------------------------------------------------
    // Stack outputs
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'StateMachineArn', {
      value: this.stateMachine.stateMachineArn,
      description: `Review Step Functions state machine ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-pipeline-StateMachineArn`,
    });

    new cdk.CfnOutput(this, 'PipelineReviewRoleArn', {
      value: this.pipelineReviewRole.roleArn,
      description: `Pipeline review-stage role ARN for ${envName} (sole bedrock:InvokeModel grantee)`,
      exportName: `ContractToaster-${envName}-pipeline-ReviewRoleArn`,
    });

    new cdk.CfnOutput(this, 'PurgeWorkerFunctionArn', {
      value: this.purgeWorkerFunction.functionArn,
      description: `Retention purge worker Lambda ARN for ${envName} (issue #61)`,
      exportName: `ContractToaster-${envName}-pipeline-PurgeWorkerFunctionArn`,
    });
  }
}
