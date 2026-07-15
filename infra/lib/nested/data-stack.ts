import * as cdk from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3vectors from 'aws-cdk-lib/aws-s3vectors';
import { Construct } from 'constructs';

// ---------------------------------------------------------------------------
// Bedrock Knowledge Base + S3 Vectors (issue #60) -- retrieval-store constants.
// ---------------------------------------------------------------------------

// Embedding model used for corpus ingestion (model-policy/bedrock-us-east-1.json
// models.embedding.model_id). A change to this model requires admin (GC)
// approval and produces a new corpus_snapshot_version (see ARCHITECTURE.md).
const EMBEDDING_MODEL_ID = 'amazon.titan-embed-text-v2:0';
// Titan Embed Text v2 default output dimension.
const EMBEDDING_DIMENSION = 1024;

// AWS documents ~1KB of custom metadata and a 35 metadata-key limit for
// S3 Vectors used as a Bedrock KB vector store (tighter than the raw S3
// Vectors limits) -- see ARCHITECTURE.md "Metadata model (fits the S3
// Vectors limits)". Vector metadata therefore carries ONLY compact IDs and
// filter fields; full clause text/rationale/summaries are NEVER stored
// inline here -- they live in S3/DynamoDB keyed by the immutable clause_id
// and are fetched after retrieval.
//
// Filterable metadata keys (used as retrieval query filters):
//   - clause_id                 immutable clause identifier (fetch key for full text)
//   - source_document_id        the corpus source document this clause came from
//   - document_type             'executed-final' | 'accepted-draft' | 'rejected-draft'
//   - corpus_polarity           'positive' | 'negative' -- positive/negative separation
//     (document_type distinguishes source provenance; corpus_polarity is the
//     retrieval-time hard label that keeps rejected drafts out of the
//     positive top-K context -- rejected clauses are NEVER commingled with
//     positive precedent; they reach the model only via a separate,
//     hard-labeled negative-example channel)
//   - playbook_id                required retrieval filter -- topic IDs collide
//     across playbooks (reconciliation note #45); defaults to 'eiaa' in v1
//   - playbook_topic_id          scoped WITHIN playbook_id
//   - counterparty_name          filter/dedup by counterparty
//   - corpus_snapshot_version    defense-in-depth query filter (not the sole
//     isolation mechanism -- the active/staging split and per-execution
//     physical-store pin are; see ARCHITECTURE.md "Corpus versioning")
// Non-filterable metadata keys (curation fields -- retrievable but not
// searchable/queryable; legal-curated, not every executed agreement is
// positive precedent):
//   - reusable_precedent         legal-curated boolean flag
//   - negotiation_context        free-text curation note
//   - superseded_by              clause_id of the clause that supersedes this one
//   - approved_use_scope         scope in which this clause may be reused
const VECTOR_FILTERABLE_METADATA_KEYS = [
  'clause_id',
  'source_document_id',
  'document_type',
  'corpus_polarity',
  'playbook_id',
  'playbook_topic_id',
  'counterparty_name',
  'date',
  'corpus_snapshot_version',
];
const VECTOR_NON_FILTERABLE_METADATA_KEYS = [
  'reusable_precedent',
  'negotiation_context',
  'superseded_by',
  'approved_use_scope',
];

export interface DataStackProps extends cdk.NestedStackProps {
  readonly envName: string;
  /**
   * Resource-name prefix (issue #233). Defaults to 'contract-toaster' so the
   * existing dev/prod deployments keep their current bucket/table names
   * (stateful resources can't be renamed in place). New deployments can
   * override via CDK context (--context appName=acmecorp) to pick their own
   * prefix without touching source.
   */
  readonly appName?: string;
  /**
   * Per-data-class CMKs (#70).  Each S3 bucket and DynamoDB table references
   * its own key so a compromise of one key cannot expose all data classes.
   *
   * uploadsKey   — incoming contract .docx files (S3 uploads bucket, #51)
   * outputsKey   — generated redlines and result packets (S3 outputs bucket, #51)
   * corpusKey    — standard-form corpus, S3 Vectors, clause-text store (#51, #60, #32)
   * auditKey     — audit log objects and CloudTrail delivery (#51, #57)
   * dynamodbKey  — all DynamoDB tables: users, playbooks, reviews, cost ledger (#52)
   *
   * Undefined under `--context profile=minimal` (issue #231) — KmsKeysStack
   * does not create the per-data-class CMKs in the minimal profile. In that
   * case every S3 bucket and DynamoDB table below falls back to AWS-managed
   * encryption (S3_MANAGED / DynamoDB AWS_MANAGED) instead of a CMK.
   */
  readonly uploadsKey?: kms.IKey;
  readonly outputsKey?: kms.IKey;
  readonly corpusKey?: kms.IKey;
  readonly auditKey?: kms.IKey;
  readonly dynamodbKey?: kms.IKey;
}

/**
 * DataStack — S3 buckets and DynamoDB tables.
 *
 * S3 buckets (issue #51):
 *  - uploads bucket     (contract-toaster-uploads-{env}):       uploadsKey
 *  - outputs bucket     (contract-toaster-outputs-{env}):       outputsKey
 *  - corpus bucket      (contract-toaster-corpus-{env}):        corpusKey
 *  - audit-archive      (contract-toaster-audit-archive-{env}): auditKey
 *
 * DynamoDB tables (issue #52):
 *  - users                PK: cognito_sub; status, last_auth_at; PITR; dynamodbKey
 *  - admin_bootstrap      PK: email; first-admin seed only; dynamodbKey
 *  - playbooks            PK: playbook_id; active_release_bundle_hash; PITR; dynamodbKey
 *  - playbook_versions    PK: playbook_id, SK: version; PITR; dynamodbKey
 *  - reviews              PK: review_id; owner_sub GSI; playbook_id; PITR; dynamodbKey
 *  - review_submissions   PK: idempotency_key; review_id GSI; one row per idempotency
 *                         key; PITR; dynamodbKey
 *  - daily_spend          PK: spend_date (YYYY-MM-DD); atomic reservation counter (#59); dynamodbKey
 *  - cost_ledger          PK: review_id, SK: attempt_id; every model attempt ledgered (#59); dynamodbKey
 *  - pipeline_semaphore   PK: lock_name; concurrency cap + lease TTL (#59); dynamodbKey
 *  - retention_settings   PK: setting_id; retention window + dual-control/delay
 *                         state for retroactive reductions (#61); PITR; dynamodbKey
 *  - audit                time-partitioned PK; actor+review_id GSIs; PITR; auditKey (dedicated)
 *
 * Security invariants (enforced here):
 *  - Per-data-class KMS: each bucket uses its own CMK (no shared env key).
 *  - Block all public access on every bucket.
 *  - Versioning + Object Lock (Governance, 7-year default) on corpus + audit.
 *  - Glacier lifecycle on audit-archive after 1 year.
 *  - No fixed lifecycle-delete rule on uploads/outputs (retention = purge worker).
 *  - Legal-hold enforcement: bucket policy DENY s3:DeleteObject and
 *    s3:PutObject when the contract-toaster:legal-hold tag is 'true'.  Normal app and
 *    purge roles cannot delete or overwrite a held object.
 *  - Governance-bypass break-glass: only the MFA break-glass role can call
 *    s3:BypassGovernanceRetention.  Bypass requires a session tag
 *    contract-toaster:break-glass-reason (reason/ticket), is logged to CloudTrail
 *    automatically (S3 data events), and triggers a CloudWatch alarm (#57).
 *    Any break-glass use must also produce an application audit row (#57).
 *  - Audit immutability: all application roles are DENIED dynamodb:UpdateItem
 *    and dynamodb:DeleteItem on the audit table.  Writes are append-only
 *    PutItem with an attribute_not_exists condition on the key.
 *    Denied/failed mutation attempts on the audit table raise a CloudWatch
 *    alarm (#57).
 *  - Audit streams: DynamoDB Streams on the audit table (NEW_IMAGE) feeds the
 *    object-locked audit-archive S3 bucket.  Phase 4 wiring is folded in here
 *    (stream enabled from day one) per the issue notes.
 *  - Audit substance whitelist: audit rows contain non-substantive proof facts
 *    only — actor/action/target/time/outcome/status/hash/cost/reason codes,
 *    plus retrieved clause_ids per review (reconciliation note #27).  They must
 *    NOT store raw clause text, model rationales, summaries, critic deltas,
 *    prompt bodies, retrieved precedent text, or downloaded document contents.
 */
export class DataStack extends cdk.NestedStack {
  // Public bucket references — consumed by AppStack (#55), download auth (#71),
  // purge worker, and observability stack (#57).
  readonly uploadsBucket: s3.Bucket;
  readonly outputsBucket: s3.Bucket;
  readonly corpusBucket: s3.Bucket;
  readonly auditArchiveBucket: s3.Bucket;

  // Bedrock Knowledge Base + S3 Vectors retrieval store (issue #60).
  readonly corpusVectorBucket: s3vectors.CfnVectorBucket;
  readonly corpusVectorIndex: s3vectors.CfnIndex;
  readonly corpusKnowledgeBase: bedrock.CfnKnowledgeBase;
  readonly corpusDataSource: bedrock.CfnDataSource;
  /**
   * Bedrock KB service role (bedrock.amazonaws.com-assumed). Holds
   * bedrock:InvokeModel scoped ONLY to the embedding-model ARN, plus S3
   * read on the corpus bucket and S3 Vectors data-plane access -- distinct
   * from pipelineReviewRole (#59), which is the sole holder of
   * bedrock:InvokeModel on the primary/critic model ARNs and of the
   * Knowledge Base query actions (Retrieve / RetrieveAndGenerate). See
   * "RECONCILED least-privilege invariant" in pipeline-stack.ts.
   */
  readonly corpusKnowledgeBaseRole: iam.Role;

  // Public DynamoDB table references — consumed by AuthStack (#53),
  // AppStack (#55, #59), API (#84), observability (#57).
  readonly usersTable: dynamodb.Table;
  readonly adminBootstrapTable: dynamodb.Table;
  readonly playbooksTable: dynamodb.Table;
  readonly playbookVersionsTable: dynamodb.Table;
  readonly reviewsTable: dynamodb.Table;
  readonly reviewSubmissionsTable: dynamodb.Table;
  readonly auditTable: dynamodb.Table;
  // Issue #59 — async review pipeline: spend reservation/ledger + semaphore.
  readonly dailySpendTable: dynamodb.Table;
  readonly costLedgerTable: dynamodb.Table;
  readonly pipelineSemaphoreTable: dynamodb.Table;
  // Issue #61 — retention purge worker: admin-configurable retention window
  // + dual-control/delay state, shared with the future admin UI.
  readonly retentionSettingsTable: dynamodb.Table;
  // Issue #92 — admin Users UI: single-row status of the Workspace/SSO
  // deprovisioning sync job, shared between the (future) scheduled sync
  // worker and the admin UI's sync-visibility panel.
  readonly syncStatusTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);

    const { envName, uploadsKey, outputsKey, corpusKey, auditKey, dynamodbKey } = props;
    const appName = props.appName ?? 'contract-toaster';

    // -----------------------------------------------------------------------
    // Deploy profile (issue #231). 'hardened' (default) is byte-for-byte the
    // pre-#231 behavior: every bucket/table below is CMK-encrypted, and
    // corpus/audit-archive get S3 Object Lock. 'minimal' falls back to
    // AWS-managed encryption (no per-data-class CMK -- KmsKeysStack does not
    // create one) and skips Object Lock/versioning on corpus/audit-archive,
    // since Object Lock is meaningful only alongside the CloudTrail/legal-hold
    // posture the minimal profile does not provide.
    // -----------------------------------------------------------------------
    const profile = (this.node.tryGetContext('profile') as string | undefined) ?? 'hardened';
    const isMinimal = profile === 'minimal';
    const s3Encryption = isMinimal ? s3.BucketEncryption.S3_MANAGED : s3.BucketEncryption.KMS;
    const ddbEncryption = isMinimal
      ? dynamodb.TableEncryption.AWS_MANAGED
      : dynamodb.TableEncryption.CUSTOMER_MANAGED;
    // Per-data-class encryptionKey values, named distinctly (not a single
    // shared `isMinimal ? undefined : x` ternary repeated inline) so each
    // bucket/table keeps its own distinct per-class CMK reference under the
    // hardened profile -- per AC B (issue #70), no single shared key may be
    // used for all buckets.
    const uploadsKeyEncryption = isMinimal ? undefined : uploadsKey;
    const outputsKeyEncryption = isMinimal ? undefined : outputsKey;
    const corpusKeyEncryption = isMinimal ? undefined : corpusKey;
    const auditKeyEncryption = isMinimal ? undefined : auditKey;
    const dynamodbKeyEncryption = isMinimal ? undefined : dynamodbKey;

    // -----------------------------------------------------------------------
    // uploads bucket — contract-toaster-uploads-{env}
    //
    // Stores raw uploaded counterparty .docx files.
    // - Private, block-all-public-access.
    // - Encrypted with uploadsKey (the upload-path CMK; distinct from corpus/audit).
    // - NO fixed lifecycle-delete rule: admin-configurable retention is enforced
    //   by the purge worker (separate issue), not a bucket lifecycle rule.
    // - Legal-hold bucket policy: DENY DeleteObject + PutObject when
    //   contract-toaster:legal-hold tag is 'true' (see addLegalHoldPolicy below).
    // -----------------------------------------------------------------------
    this.uploadsBucket = new s3.Bucket(this, 'UploadsBucket', {
      bucketName: `${appName}-uploads-${envName}`,
      encryption: s3Encryption,
      encryptionKey: uploadsKeyEncryption,
      bucketKeyEnabled: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      // No objectLockEnabled — Object Lock legal hold or protected hold tags
      // are managed by the legal-hold policy added below.
      // Versioning off by default; may be enabled if needed for hold tags.
    });

    cdk.Tags.of(this.uploadsBucket).add('contract-toaster:env', envName);
    cdk.Tags.of(this.uploadsBucket).add('contract-toaster:data-class', 'uploads');
    cdk.Tags.of(this.uploadsBucket).add('contract-toaster:legal-hold-enforced', 'bucket-policy');

    this._addLegalHoldPolicy(this.uploadsBucket, 'uploads');

    // -----------------------------------------------------------------------
    // outputs bucket — contract-toaster-outputs-{env}
    //
    // Stores generated redlines and result packets.
    // - Private, block-all-public-access.
    // - Encrypted with outputsKey (distinct from uploads/corpus/audit).
    // - NO fixed lifecycle-delete rule (same rationale as uploads).
    // - Legal-hold bucket policy.
    // -----------------------------------------------------------------------
    this.outputsBucket = new s3.Bucket(this, 'OutputsBucket', {
      bucketName: `${appName}-outputs-${envName}`,
      encryption: s3Encryption,
      encryptionKey: outputsKeyEncryption,
      bucketKeyEnabled: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.outputsBucket).add('contract-toaster:env', envName);
    cdk.Tags.of(this.outputsBucket).add('contract-toaster:data-class', 'outputs');
    cdk.Tags.of(this.outputsBucket).add('contract-toaster:legal-hold-enforced', 'bucket-policy');

    this._addLegalHoldPolicy(this.outputsBucket, 'outputs');

    // -----------------------------------------------------------------------
    // corpus bucket — contract-toaster-corpus-{env}
    //
    // Stores executed Exos agreements (reference corpus) and derived artifacts.
    // Per reconciliation note #32 (2026-06-11 architecture review): corpus key
    // also covers derived artifacts — S3 Vectors store and clause-text store.
    //
    // - Private, block-all-public-access.
    // - Encrypted with corpusKey (distinct from uploads/outputs/audit).
    // - Versioning: enabled (corpus objects are versioned).
    // - Object Lock: GOVERNANCE mode, 7-year (2555-day) retention default.
    //   (Object Lock can only be enabled at bucket-creation time; non-trivial to change.)
    //   Governance (not Compliance) mode: tightly-controlled MFA break-glass holder of
    //   s3:BypassGovernanceRetention can override in a genuine emergency.
    // - Legal-hold bucket policy (in addition to Object Lock legal hold).
    // - Legal-hold corpus objects use S3 Object Lock legal holds as the primary
    //   storage-layer enforcement; the bucket-policy DENY provides defense-in-depth.
    //
    // BREAK-GLASS (governance bypass):
    //   - Requires the MFA break-glass IAM role (assumed via SSO + MFA).
    //   - Session must carry tag: contract-toaster:break-glass-reason=<ticket-or-reason>.
    //   - All s3:BypassGovernanceRetention calls are logged to CloudTrail
    //     (S3 data events are enabled on this bucket via #57 ObservabilityStack).
    //   - Every break-glass use must produce an application audit row (reason/ticket, #57).
    //   - A CloudWatch alarm fires on any CloudTrail event matching
    //     s3:BypassGovernanceRetention on this bucket (#57).
    // -----------------------------------------------------------------------
    this.corpusBucket = new s3.Bucket(this, 'CorpusBucket', {
      bucketName: `${appName}-corpus-${envName}`,
      encryption: s3Encryption,
      encryptionKey: corpusKeyEncryption,
      bucketKeyEnabled: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      // Object Lock (issue #231): minimal profile skips versioning + Object
      // Lock. Object Lock is meaningful alongside the CloudTrail/legal-hold
      // posture the minimal profile does not provide; hardened (default)
      // keeps it unchanged.
      versioned: !isMinimal,
      objectLockEnabled: !isMinimal,
      objectLockDefaultRetention: isMinimal
        ? undefined
        : s3.ObjectLockRetention.governance(
            cdk.Duration.days(2555), // 7 years
          ),
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.corpusBucket).add('contract-toaster:env', envName);
    cdk.Tags.of(this.corpusBucket).add('contract-toaster:data-class', 'corpus');
    cdk.Tags.of(this.corpusBucket).add('contract-toaster:legal-hold-enforced', 'object-lock+bucket-policy');
    // Reconciliation note #32: corpus key also covers S3 Vectors and clause-text store.
    cdk.Tags.of(this.corpusBucket).add('contract-toaster:corpus-covers', 's3-vectors,clause-text');
    // break-glass: standard (MFA required for BypassGovernanceRetention via IAM; alarm in #57).
    cdk.Tags.of(this.corpusBucket).add('contract-toaster:break-glass-policy', 'mfa-required-bypass-governance');

    this._addLegalHoldPolicy(this.corpusBucket, 'corpus');

    // -----------------------------------------------------------------------
    // Bedrock Knowledge Base + S3 Vectors (issue #60) — the retrieval store.
    //
    // Replaces the previously-planned OpenSearch Serverless collection, whose
    // OCU minimum (~$350/mo) broke the cost target. S3 Vectors is pay-per-use
    // with NO idle floor (near-$0 idle for a ~50-document corpus) — see
    // ARCHITECTURE.md "Retrieval — Amazon Bedrock Knowledge Bases (S3 Vectors)".
    //
    // Empty-corpus phase: this issue proves the store exists, is private, and
    // is queryable. A trivial query against an empty active snapshot returns
    // an EMPTY RESULT SET, not an error — the KB and S3 Vectors index are
    // valid, queryable resources from creation, independent of whether any
    // vectors have been ingested yet. Real ingestion / clause extraction is
    // Phase 3 (out of scope here).
    //
    // Resources:
    //   1. corpusVectorBucket  — S3 Vectors bucket (CfnVectorBucket); encrypted
    //      with corpusKey. Distinct from the S3 general-purpose corpusBucket
    //      above (which stores source documents + clause text); the vector
    //      bucket stores ONLY embeddings + compact filter metadata.
    //   2. corpusVectorIndex   — the ACTIVE vector index (CfnIndex) that
    //      reviews query. A candidate corpus snapshot ingests into a SEPARATE
    //      STAGING index (created at ingestion time, not provisioned here —
    //      see "Corpus versioning / activation" below); activation repoints
    //      the application's recorded active KB/index reference from the old
    //      store to the validated staging store. Bedrock KB has no native
    //      snapshot primitive, so this active/staging split, the
    //      per-execution physical-store pin, and the content-addressed
    //      clause-id manifest are built at the application layer (see
    //      ARCHITECTURE.md "Corpus versioning and activation boundaries" and
    //      issue #20). The ingestion interlock (a review refuses to run
    //      against a store mid-ingestion or against a draft/failed/partial/
    //      superseded snapshot) is likewise an application-layer check —
    //      corpus_snapshot_version metadata (below) is a defense-in-depth
    //      query filter, not the sole isolation mechanism.
    //   3. corpusKnowledgeBase — the Bedrock Knowledge Base (CfnKnowledgeBase)
    //      bound to the vector index via VECTOR storage type.
    //   4. corpusDataSource    — a Bedrock DataSource (CfnDataSource) pointing
    //      at the corpus S3 bucket. An admin corpus upload (endpoint stubbed
    //      in Phase 0) triggers a Bedrock KB ingestion job (StartIngestionJob)
    //      against this data source into a DRAFT corpus snapshot. Draft
    //      snapshots are NOT review-queryable — only an activated snapshot
    //      (repointed to the active index) is queried by reviews.
    //   5. corpusKnowledgeBaseRole — dedicated KB service role, assumed by
    //      bedrock.amazonaws.com, distinct from pipelineReviewRole (#59).
    //
    // Metadata model (issue #60 "Corrected metadata model"): vector chunk
    // metadata holds ONLY compact IDs and filter fields (see
    // VECTOR_FILTERABLE_METADATA_KEYS / VECTOR_NON_FILTERABLE_METADATA_KEYS
    // above) — never the clause's full text, rationale, or an LLM-generated
    // summary. Full clause text lives in S3/DynamoDB keyed by the
    // immutable clause_id and is fetched after retrieval. This stays within
    // the ~1KB custom-metadata / 35-metadata-key limit that AWS documents for
    // S3 Vectors used as a Bedrock KB vector store.
    //
    // Positive/negative separation (issue #60): document_type distinguishes
    // 'executed-final' / 'accepted-draft' / 'rejected-draft', and
    // corpus_polarity distinguishes 'positive' / 'negative'. Rejected drafts
    // are never commingled with positive precedent in the same top-K
    // retrieval context — rejected language reaches the model only through a
    // separate, hard-labeled negative-example channel (retrieval-contract
    // enforcement is application-layer; the metadata fields here are what
    // make that filter possible).
    //
    // Curation fields (issue #60): reusable_precedent, negotiation_context,
    // superseded_by, approved_use_scope are legal-curated fields on the
    // clause record — not every executed agreement is positive precedent (a
    // one-off concession must not be treated as authoritative).
    //
    // Private access ONLY (issue #60 AC): the vector bucket, index, and
    // Knowledge Base are never public — reachable only via IAM
    // (corpusKnowledgeBaseRole for ingestion, pipelineReviewRole for query;
    // see pipeline-stack.ts) and a VPC interface endpoint for Bedrock
    // (network-stack.ts). Neither CfnVectorBucket nor CfnKnowledgeBase has a
    // public-access toggle to disable — access is exclusively IAM-mediated by
    // construction (no bucket-policy public grant is ever added here).
    //
    // Idle cost sanity (issue #60 AC): no OpenSearch Serverless collection or
    // other OCU-minimum resource is provisioned anywhere in this stack — S3
    // Vectors + Bedrock KB are pay-per-use with no idle floor.
    // -----------------------------------------------------------------------

    // 1. S3 Vectors bucket — encrypted with corpusKey (same data-class CMK as
    //    the corpus S3 bucket; reconciliation note #32).
    this.corpusVectorBucket = new s3vectors.CfnVectorBucket(this, 'CorpusVectorBucket', {
      vectorBucketName: `${appName}-corpus-vectors-${envName}`,
      // Issue #231: minimal profile omits per-data-class CMKs -- leave
      // encryptionConfiguration unset so the vector bucket falls back to its
      // AWS-managed default (SSE-S3-equivalent) encryption.
      encryptionConfiguration: isMinimal
        ? undefined
        : {
            sseType: 'aws:kms',
            kmsKeyArn: corpusKey!.keyArn,
          },
    });

    // 2. S3 Vectors index — the ACTIVE index reviews query. dataType float32,
    //    dimension matches the embedding model's output dimension, cosine
    //    distance (standard for text embeddings). metadataConfiguration
    //    declares which keys are non-filterable (curation fields) — every
    //    other key on a stored vector is filterable by default.
    this.corpusVectorIndex = new s3vectors.CfnIndex(this, 'CorpusVectorIndex', {
      indexName: `${appName}-corpus-active-${envName}`,
      vectorBucketName: this.corpusVectorBucket.vectorBucketName,
      dataType: 'float32',
      dimension: EMBEDDING_DIMENSION,
      distanceMetric: 'cosine',
      metadataConfiguration: {
        nonFilterableMetadataKeys: VECTOR_NON_FILTERABLE_METADATA_KEYS,
      },
    });
    this.corpusVectorIndex.addDependency(this.corpusVectorBucket);

    // 5. corpusKnowledgeBaseRole — dedicated KB service role (bedrock.amazonaws.com).
    //    Distinct principal from pipelineReviewRole (#59): this role is ONLY
    //    assumable by the Bedrock service (for ingestion), never by the
    //    pipeline's Lambda principal, and it is never used for the
    //    query-time Knowledge Base query actions (those go through
    //    pipelineReviewRole exclusively — see pipeline-stack.ts).
    this.corpusKnowledgeBaseRole = new iam.Role(this, 'CorpusKnowledgeBaseRole', {
      roleName: `${appName}-corpus-kb-${envName}`,
      description:
        `ContractToaster review — Bedrock Knowledge Base service role (${envName}). ` +
        'Assumed by bedrock.amazonaws.com for corpus ingestion ONLY. Holds ' +
        'bedrock:InvokeModel scoped strictly to the embedding-model ARN ' +
        '(never a wildcard, never the primary/critic review model ARNs) — ' +
        'distinct from pipelineReviewRole (issue #59), which is the sole ' +
        'holder of bedrock:InvokeModel on the primary/critic model ARNs and ' +
        'of the Knowledge Base query actions.',
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': cdk.Aws.ACCOUNT_ID },
        },
      }),
    });

    // Ingestion requires the KB's own service role to call bedrock:InvokeModel
    // on the embedding model — this is how Bedrock KB ingestion works, not a
    // design choice (see the reconciliation note in pipeline-stack.ts). Scoped
    // STRICTLY to the embedding-model ARN: never a foundation-model/* wildcard,
    // never the primary/critic model ARNs.
    this.corpusKnowledgeBaseRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockInvokeEmbeddingModelOnly',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [`arn:aws:bedrock:us-east-1::foundation-model/${EMBEDDING_MODEL_ID}`],
      }),
    );

    // Read access to the corpus S3 bucket (ingestion source) and the S3
    // Vectors data plane (write access to store embeddings + metadata).
    this.corpusBucket.grantRead(this.corpusKnowledgeBaseRole, 'corpus/*');
    if (!isMinimal) {
      corpusKey!.grantDecrypt(this.corpusKnowledgeBaseRole);
    }
    this.corpusKnowledgeBaseRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'S3VectorsDataPlane',
        effect: iam.Effect.ALLOW,
        actions: [
          's3vectors:GetVectors',
          's3vectors:PutVectors',
          's3vectors:QueryVectors',
          's3vectors:ListVectors',
          's3vectors:GetIndex',
        ],
        resources: [this.corpusVectorBucket.attrVectorBucketArn, `${this.corpusVectorBucket.attrVectorBucketArn}/*`],
      }),
    );

    cdk.Tags.of(this.corpusKnowledgeBaseRole).add('contract-toaster:env', envName);
    cdk.Tags.of(this.corpusKnowledgeBaseRole).add('contract-toaster:component', 'corpus-kb');

    // 3. Bedrock Knowledge Base — VECTOR type, backed by the S3 Vectors index.
    this.corpusKnowledgeBase = new bedrock.CfnKnowledgeBase(this, 'CorpusKnowledgeBase', {
      name: `${appName}-corpus-${envName}`,
      description:
        `ContractToaster review — corpus retrieval Knowledge Base (${envName}). Private; ` +
        'reachable only via pipelineReviewRole (query) and corpusKnowledgeBaseRole ' +
        '(ingestion), never public. Empty-corpus queries return an empty result set, ' +
        'not an error.',
      roleArn: this.corpusKnowledgeBaseRole.roleArn,
      knowledgeBaseConfiguration: {
        type: 'VECTOR',
        vectorKnowledgeBaseConfiguration: {
          embeddingModelArn: `arn:aws:bedrock:us-east-1::foundation-model/${EMBEDDING_MODEL_ID}`,
        },
      },
      storageConfiguration: {
        type: 'S3_VECTORS',
        s3VectorsConfiguration: {
          vectorBucketArn: this.corpusVectorBucket.attrVectorBucketArn,
          indexName: this.corpusVectorIndex.indexName,
        },
      },
    });
    this.corpusKnowledgeBase.addDependency(this.corpusVectorIndex);

    cdk.Tags.of(this.corpusKnowledgeBase).add('contract-toaster:env', envName);
    cdk.Tags.of(this.corpusKnowledgeBase).add('contract-toaster:data-class', 'corpus');

    // 4. Bedrock DataSource — S3 source pointing at the corpus bucket. An
    //    admin corpus upload (stubbed endpoint in Phase 0) triggers a
    //    StartIngestionJob against this data source, landing in a DRAFT
    //    snapshot that is NOT review-queryable until activated (application-
    //    layer activation repoints the active reference — see above).
    this.corpusDataSource = new bedrock.CfnDataSource(this, 'CorpusDataSource', {
      name: `${appName}-corpus-source-${envName}`,
      description:
        `ContractToaster review — corpus S3 data source (${envName}). Ingestion jobs against ` +
        'this source land in a DRAFT staging snapshot; draft snapshots are not ' +
        'review-queryable until an admin activation step repoints the active ' +
        'index reference (application-layer corpus versioning, issue #20).',
      knowledgeBaseId: this.corpusKnowledgeBase.attrKnowledgeBaseId,
      dataSourceConfiguration: {
        type: 'S3',
        s3Configuration: {
          bucketArn: this.corpusBucket.bucketArn,
          inclusionPrefixes: ['corpus/'],
        },
      },
    });

    // -----------------------------------------------------------------------
    // audit-archive bucket — contract-toaster-audit-archive-{env}
    //
    // Stores CloudTrail logs and audit DB exports (immutable audit archive).
    //
    // - Private, block-all-public-access.
    // - Encrypted with auditKey (tighter break-glass CMK with MFA policy from #70).
    // - Versioning: enabled (audit objects are versioned).
    // - Object Lock: GOVERNANCE mode, 7-year (2555-day) retention default.
    //   Same break-glass constraints as corpus bucket (MFA + reason tag + alarm).
    //   The audit key (#70) additionally enforces MFA for key deletion operations.
    // - Lifecycle: transition to Glacier (GLACIER_INSTANT_RETRIEVAL) after 365 days.
    //   No expiration rule — audit objects are retained indefinitely under Object Lock;
    //   Glacier is a cost-optimization transition only.
    //
    // BREAK-GLASS (governance bypass):
    //   - Same controls as corpus bucket: MFA break-glass role, session tag
    //     contract-toaster:break-glass-reason, CloudTrail event + alarm (#57).
    //   - ADDITIONAL: audit KMS key (#70) has a tighter break-glass: MFA required
    //     for kms:ScheduleKeyDeletion / kms:CancelKeyDeletion (DenyIfMfaFalse policy).
    //   - Every break-glass use must produce an application audit row (#57).
    // -----------------------------------------------------------------------
    this.auditArchiveBucket = new s3.Bucket(this, 'AuditArchiveBucket', {
      bucketName: `${appName}-audit-archive-${envName}`,
      encryption: s3Encryption,
      encryptionKey: auditKeyEncryption,
      bucketKeyEnabled: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      // Object Lock (issue #231): see corpusBucket above -- minimal profile
      // skips versioning + Object Lock.
      versioned: !isMinimal,
      objectLockEnabled: !isMinimal,
      objectLockDefaultRetention: isMinimal
        ? undefined
        : s3.ObjectLockRetention.governance(
            cdk.Duration.days(2555), // 7 years
          ),
      // Lifecycle: transition to Glacier_Instant after 1 year (cost optimization).
      // No expiration — Object Lock governs object lifetime.
      lifecycleRules: [
        {
          id: 'TransitionToGlacierAfter1Year',
          enabled: true,
          transitions: [
            {
              storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
              transitionAfter: cdk.Duration.days(365),
            },
          ],
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.auditArchiveBucket).add('contract-toaster:env', envName);
    cdk.Tags.of(this.auditArchiveBucket).add('contract-toaster:data-class', 'audit');
    cdk.Tags.of(this.auditArchiveBucket).add('contract-toaster:legal-hold-enforced', 'object-lock+bucket-policy');
    // break-glass: TIGHTER — both the KMS key (#70) and BypassGovernanceRetention require MFA.
    cdk.Tags.of(this.auditArchiveBucket).add('contract-toaster:break-glass-policy', 'mfa-required-bypass-governance+kms-mfa');

    this._addLegalHoldPolicy(this.auditArchiveBucket, 'audit');

    // -----------------------------------------------------------------------
    // Stack outputs — export bucket names and ARNs so downstream stacks can
    // reference them without passing bucket objects directly across nested
    // stack boundaries.
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'UploadsBucketName', {
      value: this.uploadsBucket.bucketName,
      description: `Uploads bucket name for ${envName}`,
      exportName: `ContractToaster-${envName}-uploads-BucketName`,
    });

    new cdk.CfnOutput(this, 'UploadsBucketArn', {
      value: this.uploadsBucket.bucketArn,
      description: `Uploads bucket ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-uploads-BucketArn`,
    });

    new cdk.CfnOutput(this, 'OutputsBucketName', {
      value: this.outputsBucket.bucketName,
      description: `Outputs bucket name for ${envName}`,
      exportName: `ContractToaster-${envName}-outputs-BucketName`,
    });

    new cdk.CfnOutput(this, 'OutputsBucketArn', {
      value: this.outputsBucket.bucketArn,
      description: `Outputs bucket ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-outputs-BucketArn`,
    });

    new cdk.CfnOutput(this, 'CorpusBucketName', {
      value: this.corpusBucket.bucketName,
      description: `Corpus bucket name for ${envName}`,
      exportName: `ContractToaster-${envName}-corpus-BucketName`,
    });

    new cdk.CfnOutput(this, 'CorpusBucketArn', {
      value: this.corpusBucket.bucketArn,
      description: `Corpus bucket ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-corpus-BucketArn`,
    });

    // Bedrock Knowledge Base + S3 Vectors outputs (issue #60).
    new cdk.CfnOutput(this, 'CorpusVectorBucketArn', {
      value: this.corpusVectorBucket.attrVectorBucketArn,
      description: `Corpus S3 Vectors bucket ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-corpusVectors-BucketArn`,
    });

    new cdk.CfnOutput(this, 'CorpusVectorIndexName', {
      value: this.corpusVectorIndex.indexName!,
      description: `Corpus S3 Vectors active index name for ${envName}`,
      exportName: `ContractToaster-${envName}-corpusVectors-ActiveIndexName`,
    });

    new cdk.CfnOutput(this, 'CorpusKnowledgeBaseId', {
      value: this.corpusKnowledgeBase.attrKnowledgeBaseId,
      description: `Corpus Bedrock Knowledge Base ID for ${envName} (active store, pinned per execution)`,
      exportName: `ContractToaster-${envName}-corpus-KnowledgeBaseId`,
    });

    new cdk.CfnOutput(this, 'CorpusKnowledgeBaseRoleArn', {
      value: this.corpusKnowledgeBaseRole.roleArn,
      description: `Corpus Bedrock Knowledge Base service role ARN for ${envName} (ingestion only; sole embedding-model InvokeModel grantee)`,
      exportName: `ContractToaster-${envName}-corpusKnowledgeBaseRole-Arn`,
    });

    new cdk.CfnOutput(this, 'AuditArchiveBucketName', {
      value: this.auditArchiveBucket.bucketName,
      description: `Audit archive bucket name for ${envName}`,
      exportName: `ContractToaster-${envName}-audit-BucketName`,
    });

    new cdk.CfnOutput(this, 'AuditArchiveBucketArn', {
      value: this.auditArchiveBucket.bucketArn,
      description: `Audit archive bucket ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-audit-BucketArn`,
    });

    // -----------------------------------------------------------------------
    // DynamoDB tables (issue #52)
    //
    // All tables:
    //  - On-demand billing (no provisioned capacity in v1).
    //  - Encrypted with the dynamodbKey CMK, except the audit table which
    //    uses the dedicated auditKey (tighter break-glass, append-only).
    //  - removalPolicy: RETAIN on both dev and prod (tear-down is not safe
    //    for legal data; dev bootstrap supplies CDK_DESTROY_TABLES=true only
    //    in ephemeral local environments when explicitly opted-in).
    //
    // Reconciliation notes incorporated here:
    //  - #45: reviews rows carry playbook_id (multi-playbook contract).
    //  - #23: QUARANTINED/SUPERSEDED are post-terminal administrative
    //    overlay fields (separate attribute), NOT statuses that break the
    //    status/confidence_state projection.
    //  - #27: audit rows include retrieved clause_ids per review.
    // -----------------------------------------------------------------------

    // -----------------------------------------------------------------------
    // users table — PK: cognito_sub
    //
    // One row per provisioned user.  Created on first sign-in (JIT).
    // Attributes include:
    //   - status: 'active' | 'suspended' | 'deprovisioned'
    //   - last_auth_at: ISO timestamp updated on every successful auth
    //   - is_admin: boolean admin flag (settable only by an existing admin)
    //   - email: user's @teamexos.com email (for display; not the PK)
    //
    // The admin_bootstrap → sub reconciliation:
    //   On first sign-in the backend runs a one-time transaction that:
    //   1. Confirms the verified email matches an unconsumed admin_bootstrap row.
    //   2. Writes the real users row keyed by cognito_sub with is_admin=true.
    //   3. Atomically marks the admin_bootstrap row consumed (conditional write
    //      so the transaction cannot run twice or race).
    //   The bootstrap table is email-keyed; users is sub-keyed — the two key
    //   shapes MUST NOT be mixed in a single table (see ARCHITECTURE.md).
    // -----------------------------------------------------------------------
    this.usersTable = new dynamodb.Table(this, 'UsersTable', {
      tableName: `${appName}-users-${envName}`,
      partitionKey: { name: 'cognito_sub', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: ddbEncryption,
      encryptionKey: dynamodbKeyEncryption,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.usersTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.usersTable).add('contract-toaster:data-class', 'dynamodb');
    cdk.Tags.of(this.usersTable).add('contract-toaster:table', 'users');

    // -----------------------------------------------------------------------
    // admin_bootstrap table — PK: email
    //
    // Stores the first-admin seed row (email of the initial GC account).
    // This table is keyed by EMAIL — intentionally separate from the users
    // table (which is keyed by cognito_sub) so the two key shapes are never
    // mixed (see ARCHITECTURE.md § Authentication).
    //
    // Lifecycle: a single row per bootstrapped email, consumed on first
    // sign-in via the reconciliation transaction (conditional write marks it
    // consumed atomically so it cannot be replayed).  After consumption this
    // table is empty but retained.
    //
    // Note: PITR is enabled as a precaution; the table normally holds 0–1
    // rows.  No GSI required.
    // -----------------------------------------------------------------------
    this.adminBootstrapTable = new dynamodb.Table(this, 'AdminBootstrapTable', {
      tableName: `${appName}-admin-bootstrap-${envName}`,
      partitionKey: { name: 'email', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: ddbEncryption,
      encryptionKey: dynamodbKeyEncryption,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.adminBootstrapTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.adminBootstrapTable).add('contract-toaster:data-class', 'dynamodb');
    cdk.Tags.of(this.adminBootstrapTable).add('contract-toaster:table', 'admin_bootstrap');

    // -----------------------------------------------------------------------
    // playbooks table — PK: playbook_id
    //
    // One row per playbook (e.g. 'eiaa', 'nda').  Includes:
    //   - active_release_bundle_hash: hash of the currently active release
    //     bundle (playbook snapshot + prompt + standard-form + model-policy
    //     + corpus snapshot + eval run + legal approval).
    //   - display_name, description
    // -----------------------------------------------------------------------
    this.playbooksTable = new dynamodb.Table(this, 'PlaybooksTable', {
      tableName: `${appName}-playbooks-${envName}`,
      partitionKey: { name: 'playbook_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: ddbEncryption,
      encryptionKey: dynamodbKeyEncryption,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.playbooksTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.playbooksTable).add('contract-toaster:data-class', 'dynamodb');
    cdk.Tags.of(this.playbooksTable).add('contract-toaster:table', 'playbooks');

    // -----------------------------------------------------------------------
    // playbook_versions table — PK: playbook_id, SK: version
    //
    // Content-addressed version history for each playbook.
    // Each row records:
    //   - playbook_hash: SHA-256 of the playbook JSON
    //   - prompt_hash, canonical_standard_form_hash, model_policy_hash
    //   - active_corpus_snapshot_version, eval_run_id
    //   - legal_approval: GC approval metadata
    //   - release_bundle fields (all hashes + eval run + timestamps)
    // -----------------------------------------------------------------------
    this.playbookVersionsTable = new dynamodb.Table(this, 'PlaybookVersionsTable', {
      tableName: `${appName}-playbook-versions-${envName}`,
      partitionKey: { name: 'playbook_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'version', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: ddbEncryption,
      encryptionKey: dynamodbKeyEncryption,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.playbookVersionsTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.playbookVersionsTable).add('contract-toaster:data-class', 'dynamodb');
    cdk.Tags.of(this.playbookVersionsTable).add('contract-toaster:table', 'playbook_versions');

    // -----------------------------------------------------------------------
    // reviews table — PK: review_id
    //
    // One row per review.  Attributes include:
    //   - owner_sub: Cognito sub of the submitting user (row-level-security
    //     foundation — owner-or-admin reads; see ARCHITECTURE.md)
    //   - access_scope: reserved for future multi-scope access control
    //   - status: canonical ReviewStatus (PENDING | RUNNING | DONE | ERROR |
    //     MANUAL_REVIEW_REQUIRED | ERROR_MANUAL_REVIEW_REQUIRED)
    //   - admin_overlay: separate field for QUARANTINED/SUPERSEDED
    //     (post-terminal administrative overlays — NOT part of the canonical
    //     status; see reconciliation note #23; must not break the
    //     status/confidence_state projection)
    //   - playbook_id: which playbook was used (reconciliation note #45 —
    //     multi-playbook contract; required from day one)
    //   - playbook_hash, prompt_hash, standard_form_hash, model_policy_hash
    //   - primary_model_id, critic_model_id
    //   - active_corpus_snapshot_version
    //   - submission_id, execution_arn, execution_status
    //
    // GSI: owner_sub-index — partition key owner_sub, for "my reviews" queries.
    //
    // Queryable indexes for rollback/quarantine by release-bundle/component hash:
    //   - playbook_hash-index (GSI) — supports rollback/quarantine by playbook
    //     hash across all reviews that used a given bundle.
    // -----------------------------------------------------------------------
    this.reviewsTable = new dynamodb.Table(this, 'ReviewsTable', {
      tableName: `${appName}-reviews-${envName}`,
      partitionKey: { name: 'review_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: ddbEncryption,
      encryptionKey: dynamodbKeyEncryption,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI: owner_sub-index — supports "my reviews" list queries (owner-scoped)
    this.reviewsTable.addGlobalSecondaryIndex({
      indexName: 'owner_sub-index',
      partitionKey: { name: 'owner_sub', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI: playbook_hash-index — supports rollback/quarantine queries by
    // release-bundle component hash (reconciliation: reviews rows carry
    // playbook_id from day one, #45).
    this.reviewsTable.addGlobalSecondaryIndex({
      indexName: 'playbook_hash-index',
      partitionKey: { name: 'playbook_hash', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    cdk.Tags.of(this.reviewsTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.reviewsTable).add('contract-toaster:data-class', 'dynamodb');
    cdk.Tags.of(this.reviewsTable).add('contract-toaster:table', 'reviews');

    // -----------------------------------------------------------------------
    // review_submissions table — PK: idempotency_key
    //
    // One row per idempotency key (not per review — a single review may have
    // multiple submission attempts, each guarded by the same key).
    //
    // Each row records:
    //   - review_id: the canonical review this submission maps to
    //   - upload_pointer: S3 key for the uploaded .docx
    //   - active_release_bundle_hash: release bundle active at submission time
    //   - spend_reservation_id: the reservation record for this review
    //   - execution_arn: Step Functions execution ARN (null until started)
    //   - execution_status: mirrors the execution status
    //   - created_at, updated_at: timestamps
    //
    // Idempotency contract (see ARCHITECTURE.md § Backend):
    //   - Conditional write on idempotency_key ensures exactly-once creation.
    //   - Retries that find an existing row reuse the existing review_id and
    //     reservation — no double-spend, no double-pipeline-run.
    //   - The API checks both the current and the immediately previous 10-minute
    //     bucket key on a boundary-straddling retry.
    // -----------------------------------------------------------------------
    this.reviewSubmissionsTable = new dynamodb.Table(this, 'ReviewSubmissionsTable', {
      tableName: `${appName}-review-submissions-${envName}`,
      partitionKey: { name: 'idempotency_key', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: ddbEncryption,
      encryptionKey: dynamodbKeyEncryption,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.reviewSubmissionsTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.reviewSubmissionsTable).add('contract-toaster:data-class', 'dynamodb');
    cdk.Tags.of(this.reviewSubmissionsTable).add('contract-toaster:table', 'review_submissions');

    // GSI: review_id-index — lets the orphan reconciler (#59) look up the
    // submission record that owns a given review_id without a table scan.
    this.reviewSubmissionsTable.addGlobalSecondaryIndex({
      indexName: 'review_id-index',
      partitionKey: { name: 'review_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // -----------------------------------------------------------------------
    // daily_spend table — PK: spend_date (YYYY-MM-DD)
    //
    // Issue #59 AC: "Atomic, worst-case spend reservation" — a single
    // conditional DynamoDB counter per calendar day that reserves the
    // worst-case upper-bound estimate for a review exactly once, and fails
    // closed (ConditionalCheckFailedException) if reserving would push the
    // day's total over the configured daily cap (default $20; see
    // ARCHITECTURE.md -> Cost shape).
    //
    // Attributes:
    //   - spend_date: YYYY-MM-DD (UTC) partition key
    //   - reserved_usd_cents: running total of active worst-case reservations
    //   - settled_usd_cents: running total of settled actual spend
    //   - daily_cap_usd_cents: the cap in effect for this day (admin-configurable)
    //
    // A single conditional UpdateExpression
    //   SET reserved_usd_cents = reserved_usd_cents + :amount
    //   ConditionExpression: reserved_usd_cents + :amount <= daily_cap_usd_cents
    // is the atomic reserve — this is a single atomic conditional update, not
    // an optimistic read-then-write, so concurrent submissions cannot
    // collectively overshoot the cap before settlement.
    // -----------------------------------------------------------------------
    this.dailySpendTable = new dynamodb.Table(this, 'DailySpendTable', {
      tableName: `${appName}-daily-spend-${envName}`,
      partitionKey: { name: 'spend_date', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: ddbEncryption,
      encryptionKey: dynamodbKeyEncryption,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.dailySpendTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.dailySpendTable).add('contract-toaster:data-class', 'dynamodb');
    cdk.Tags.of(this.dailySpendTable).add('contract-toaster:table', 'daily_spend');

    // -----------------------------------------------------------------------
    // cost_ledger table — PK: review_id, SK: attempt_id
    //
    // Issue #59 AC: "Cost ledger in a finally path" — every model attempt is
    // ledgered: successful invocations, retries, malformed outputs, and
    // aborted/failed executions. Settlement reconciles the reservation
    // against ledgered actuals even on the error path, so a failed or
    // retried review cannot escape the ledger.
    //
    // Attributes: review_id, attempt_id (pass name + attempt number),
    // model_id, input_tokens, output_tokens, cost_usd_cents, outcome
    // ('success' | 'retry' | 'malformed_output' | 'aborted' | 'failed'),
    // timestamp. Non-substantive only — no prompt or model-output text.
    // -----------------------------------------------------------------------
    this.costLedgerTable = new dynamodb.Table(this, 'CostLedgerTable', {
      tableName: `${appName}-cost-ledger-${envName}`,
      partitionKey: { name: 'review_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'attempt_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: ddbEncryption,
      encryptionKey: dynamodbKeyEncryption,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.costLedgerTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.costLedgerTable).add('contract-toaster:data-class', 'dynamodb');
    cdk.Tags.of(this.costLedgerTable).add('contract-toaster:table', 'cost_ledger');

    // -----------------------------------------------------------------------
    // pipeline_semaphore table — PK: lock_name
    //
    // Issue #59 AC: "Concurrency control" — a Step Functions semaphore
    // pattern (see AWS's documented DynamoDB-lock-based semaphore) caps
    // simultaneous pipeline executions so a burst of uploads cannot flood
    // Bedrock or drain the daily cap in parallel.
    //
    // Attributes:
    //   - lock_name: fixed partition key 'contract-toaster-pipeline-semaphore'
    //     for the shared counter item, or 'review-slot#<review_id>' for a
    //     held-slot marker item.
    //   - current_count: number of slots currently held (on the counter item)
    //   - ttl: DynamoDB Time-To-Live attribute (epoch seconds) — every slot
    //     entry carries a lease TTL aligned to the state-machine execution
    //     timeout, so a hard-killed execution's slot self-expires even if
    //     the release (Catch/finally) state never runs. This is the lease/
    //     TTL half of "Semaphore lease / slot-leak recovery" in
    //     ARCHITECTURE.md; the orphan reconciler (#59) is the reaper half.
    // -----------------------------------------------------------------------
    this.pipelineSemaphoreTable = new dynamodb.Table(this, 'PipelineSemaphoreTable', {
      tableName: `${appName}-pipeline-semaphore-${envName}`,
      partitionKey: { name: 'lock_name', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      encryption: ddbEncryption,
      encryptionKey: dynamodbKeyEncryption,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.pipelineSemaphoreTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.pipelineSemaphoreTable).add('contract-toaster:data-class', 'dynamodb');
    cdk.Tags.of(this.pipelineSemaphoreTable).add('contract-toaster:table', 'pipeline_semaphore');

    // -----------------------------------------------------------------------
    // retention_settings table — PK: setting_id (issue #61)
    //
    // Single source of truth for the admin-configurable document-retention
    // window, shared by the purge worker (this issue) and the future admin
    // UI slider (#61 "Out of scope": the UI itself is a later issue; this
    // table is the shared config item both sides read/write).
    //
    // One row today: setting_id = "global" —
    //   - retention_window_days: 0-1095 (3yr), default 90
    //   - pending_reduction: null, or
    //       { new_window_days, requested_by, requested_at } while a
    //       retroactive reduction awaits either second-admin confirmation
    //       or the mandatory 72h delay (purge invariant 5,
    //       docs/data-handling.md). Cleared once applied or cancelled.
    //
    // PITR enabled: the settings row governs a destructive action, so its
    // history (who requested what, when) benefits from point-in-time
    // recoverability like the other config-bearing tables.
    // -----------------------------------------------------------------------
    this.retentionSettingsTable = new dynamodb.Table(this, 'RetentionSettingsTable', {
      tableName: `${appName}-retention-settings-${envName}`,
      partitionKey: { name: 'setting_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: ddbEncryption,
      encryptionKey: dynamodbKeyEncryption,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.retentionSettingsTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.retentionSettingsTable).add('contract-toaster:data-class', 'dynamodb');
    cdk.Tags.of(this.retentionSettingsTable).add('contract-toaster:table', 'retention_settings');

    // -----------------------------------------------------------------------
    // sync_status table — PK: sync_type (issue #92)
    //
    // Single source of truth for "when did the Workspace/SSO deprovisioning
    // sync last run, what did it change, and did it fail closed" — read by
    // the admin Users UI (GET /api/users) and written by the scheduled sync
    // worker described in ARCHITECTURE.md -> "Periodic SSO/Workspace sync"
    // (cadence <= 1 hour; that worker's own scheduling/Lambda is a follow-on
    // issue, same mock-first swap-point pattern as infra/lambda/mock_review).
    //
    // One row today: sync_type = "user_deprovision" —
    //   - last_run_at: epoch-seconds of the last completed sync attempt
    //   - last_run_outcome: 'ok' | 'directory_unavailable' (fail-closed —
    //     the sync makes no changes on an API outage; see ARCHITECTURE.md)
    //   - users_deprovisioned_count: number of users flipped to
    //     'deprovisioned' on the last successful run
    //   - next_run_at: epoch-seconds, informational only (cadence is
    //     enforced by the scheduler, not by this row)
    //
    // PITR enabled: this row underlies a security-relevant "when were we
    // last in sync with Workspace" signal, consistent with the other
    // config-bearing tables above.
    // -----------------------------------------------------------------------
    this.syncStatusTable = new dynamodb.Table(this, 'SyncStatusTable', {
      tableName: `${appName}-sync-status-${envName}`,
      partitionKey: { name: 'sync_type', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: ddbEncryption,
      encryptionKey: dynamodbKeyEncryption,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.syncStatusTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.syncStatusTable).add('contract-toaster:data-class', 'dynamodb');
    cdk.Tags.of(this.syncStatusTable).add('contract-toaster:table', 'sync_status');

    // -----------------------------------------------------------------------
    // audit table — time-partitioned PK, SK: timestamp
    //
    // Encrypted with the dedicated auditKey (NOT dynamodbKey) — tighter
    // break-glass CMK with MFA required for destructive key operations (#70).
    //
    // Key design:
    //   - PK (partition key): YYYY-MM (month bucket) — enables range queries
    //     by time window without hot-partitioning.  Where queries are scoped
    //     to a specific target, target_type#target_id can also be used as PK
    //     (overloaded item type pattern).
    //   - SK (sort key): timestamp (ISO-8601 UTC) — enables efficient range
    //     queries within a month bucket.
    //
    //   DO NOT use event_id as the PK — it makes the timestamp SK useless for
    //   range queries and forces costly scans for time-range audit queries.
    //
    // GSIs:
    //   - actor-index: PK actor (who performed the action) — for "all actions
    //     by this user" admin queries and anomaly detection.
    //   - review_id-index: PK review_id — for "all audit events for a review"
    //     queries (provenance, dispute resolution).
    //
    // Audit substance whitelist (ENFORCED BY CONVENTION + DENY POLICY):
    //   Audit rows contain NON-SUBSTANTIVE proof facts ONLY:
    //     actor, action, target_type, target_id, timestamp, outcome, status,
    //     hash (playbook_hash, prompt_hash, standard_form_hash, model_policy_hash,
    //     corpus_snapshot_version), cost_tokens, cost_usd, reason_code,
    //     retrieved clause_ids per review (reconciliation note #27).
    //   Audit rows MUST NOT store:
    //     raw clause text, model rationales, summaries, critic deltas,
    //     prompt bodies, retrieved precedent text, or downloaded document
    //     contents.  Document content and LLM-generated reasoning live in
    //     retention-governed confidential storage (outputs S3 bucket), not here.
    //
    // Immutability invariants:
    //   - All application roles (App Runner task role, Step Functions task
    //     roles) are explicitly DENIED dynamodb:UpdateItem and
    //     dynamodb:DeleteItem on this table (see _addAuditImmutabilityPolicy).
    //   - The ONLY allowed write path is PutItem with an attribute_not_exists
    //     condition on (pk, sk) so that a replay of an existing audit event is
    //     rejected by DynamoDB, not silently overwritten.
    //   - Denied/failed mutation attempts (UpdateItem, DeleteItem) trigger a
    //     CloudWatch alarm (#57 ObservabilityStack).
    //
    // Streams:
    //   - DynamoDB Streams enabled (NEW_IMAGE) from day one.
    //   - Stream feeds the object-locked audit-archive S3 bucket (#51) via a
    //     Lambda consumer (wired in Phase 4 / #57, but the stream is enabled
    //     here so the event source is available without a table replacement).
    // -----------------------------------------------------------------------
    this.auditTable = new dynamodb.Table(this, 'AuditTable', {
      tableName: `${appName}-audit-${envName}`,
      // PK: YYYY-MM time bucket (or target_type#target_id for target-scoped queries)
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      // SK: ISO-8601 UTC timestamp — enables range queries within a month bucket.
      // NOT event_id (per AC: event_id as PK makes timestamp SK useless for range queries).
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      encryption: ddbEncryption,
      // Audit table uses the dedicated audit CMK — NOT the shared dynamodbKey.
      // The audit CMK has a tighter break-glass policy (MFA required for
      // ScheduleKeyDeletion/CancelKeyDeletion) per #70. Undefined under
      // profile=minimal (issue #231), matching ddbEncryption's fallback.
      encryptionKey: auditKeyEncryption,
      // DynamoDB Streams — NEW_IMAGE only (we record what was written,
      // not before/after deltas; the stream feeds the audit-archive S3 bucket).
      stream: dynamodb.StreamViewType.NEW_IMAGE,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // GSI: actor-index — for "all actions by this actor" queries and anomaly detection
    this.auditTable.addGlobalSecondaryIndex({
      indexName: 'actor-index',
      partitionKey: { name: 'actor', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI: review_id-index — for "all audit events for a review" queries
    this.auditTable.addGlobalSecondaryIndex({
      indexName: 'review_id-index',
      partitionKey: { name: 'review_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    cdk.Tags.of(this.auditTable).add('contract-toaster:env', envName);
    cdk.Tags.of(this.auditTable).add('contract-toaster:data-class', 'audit');
    cdk.Tags.of(this.auditTable).add('contract-toaster:table', 'audit');
    cdk.Tags.of(this.auditTable).add('contract-toaster:immutability', 'append-only-put-item');
    cdk.Tags.of(this.auditTable).add('contract-toaster:streams', 'new-image-to-archive');

    // Add IAM DENY policy for audit table immutability
    this._addAuditImmutabilityPolicy(this.auditTable);

    // -----------------------------------------------------------------------
    // DynamoDB CfnOutputs — export table names and ARNs so downstream stacks
    // can reference them without passing table objects directly across nested
    // stack boundaries.
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'UsersTableName', {
      value: this.usersTable.tableName,
      description: `Users table name for ${envName}`,
      exportName: `ContractToaster-${envName}-users-TableName`,
    });

    new cdk.CfnOutput(this, 'UsersTableArn', {
      value: this.usersTable.tableArn,
      description: `Users table ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-users-TableArn`,
    });

    new cdk.CfnOutput(this, 'AdminBootstrapTableName', {
      value: this.adminBootstrapTable.tableName,
      description: `Admin bootstrap table name for ${envName}`,
      exportName: `ContractToaster-${envName}-adminBootstrap-TableName`,
    });

    new cdk.CfnOutput(this, 'PlaybooksTableName', {
      value: this.playbooksTable.tableName,
      description: `Playbooks table name for ${envName}`,
      exportName: `ContractToaster-${envName}-playbooks-TableName`,
    });

    new cdk.CfnOutput(this, 'PlaybooksTableArn', {
      value: this.playbooksTable.tableArn,
      description: `Playbooks table ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-playbooks-TableArn`,
    });

    new cdk.CfnOutput(this, 'PlaybookVersionsTableName', {
      value: this.playbookVersionsTable.tableName,
      description: `Playbook versions table name for ${envName}`,
      exportName: `ContractToaster-${envName}-playbookVersions-TableName`,
    });

    new cdk.CfnOutput(this, 'ReviewsTableName', {
      value: this.reviewsTable.tableName,
      description: `Reviews table name for ${envName}`,
      exportName: `ContractToaster-${envName}-reviews-TableName`,
    });

    new cdk.CfnOutput(this, 'ReviewsTableArn', {
      value: this.reviewsTable.tableArn,
      description: `Reviews table ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-reviews-TableArn`,
    });

    new cdk.CfnOutput(this, 'ReviewSubmissionsTableName', {
      value: this.reviewSubmissionsTable.tableName,
      description: `Review submissions table name for ${envName}`,
      exportName: `ContractToaster-${envName}-reviewSubmissions-TableName`,
    });

    new cdk.CfnOutput(this, 'DailySpendTableName', {
      value: this.dailySpendTable.tableName,
      description: `Daily spend reservation counter table name for ${envName}`,
      exportName: `ContractToaster-${envName}-dailySpend-TableName`,
    });

    new cdk.CfnOutput(this, 'CostLedgerTableName', {
      value: this.costLedgerTable.tableName,
      description: `Cost ledger table name for ${envName}`,
      exportName: `ContractToaster-${envName}-costLedger-TableName`,
    });

    new cdk.CfnOutput(this, 'PipelineSemaphoreTableName', {
      value: this.pipelineSemaphoreTable.tableName,
      description: `Pipeline concurrency-semaphore table name for ${envName}`,
      exportName: `ContractToaster-${envName}-pipelineSemaphore-TableName`,
    });

    new cdk.CfnOutput(this, 'AuditTableName', {
      value: this.auditTable.tableName,
      description: `Audit table name for ${envName}`,
      exportName: `ContractToaster-${envName}-audit-TableName`,
    });

    new cdk.CfnOutput(this, 'AuditTableArn', {
      value: this.auditTable.tableArn,
      description: `Audit table ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-audit-TableArn`,
    });

    new cdk.CfnOutput(this, 'AuditTableStreamArn', {
      value: this.auditTable.tableStreamArn!,
      description: `Audit table DynamoDB Stream ARN for ${envName} (feeds audit-archive S3)`,
      exportName: `ContractToaster-${envName}-audit-TableStreamArn`,
    });
  }

  // -------------------------------------------------------------------------
  // _addAuditImmutabilityPolicy
  //
  // Adds a resource-based IAM policy to the audit table that DENIES all
  // application principals from calling dynamodb:UpdateItem and
  // dynamodb:DeleteItem.
  //
  // The audit table is append-only:
  //   - Writes: PutItem with an attribute_not_exists condition on the key
  //     (application-layer enforcement; DynamoDB itself does not enforce this,
  //     but the condition on every PutItem call ensures idempotent append-only
  //     behaviour).
  //   - Reads: GetItem, Query, Scan (with attribute projection) for admin
  //     audit queries and the Phase 4 stream consumer.
  //
  // Immutability enforcement:
  //   The DENY statement below ensures that even a compromised App Runner task
  //   role or Step Functions execution role cannot UpdateItem or DeleteItem on
  //   the audit table.  The account root retains access (AWS requirement); the
  //   break-glass role may perform maintenance under MFA (see auditKey policy
  //   in kms-keys-stack.ts).
  //
  // CloudWatch alarm:
  //   Any call that triggers this DENY (UpdateItem/DeleteItem on the audit
  //   table by an application role) is logged to CloudTrail and triggers a
  //   CloudWatch alarm (#57 ObservabilityStack).  The alarm fires on any IAM
  //   AccessDenied event for these actions on this table.
  //
  // Stream + S3 archive:
  //   DynamoDB Streams (NEW_IMAGE) on the audit table feed a Lambda consumer
  //   (wired in Phase 4 / #57) that writes immutable objects to the
  //   object-locked audit-archive S3 bucket (#51).  Together these controls
  //   provide defence-in-depth: an application role cannot mutate the table
  //   (DENY), and the stream consumer writes a tamper-evident archive to S3
  //   (Object Lock GOVERNANCE, 7-year retention).
  // -------------------------------------------------------------------------
  private _addAuditImmutabilityPolicy(table: dynamodb.Table): void {
    // DENY: all AWS principals are denied dynamodb:UpdateItem on the audit table.
    // The account root and break-glass role retain access by IAM precedence
    // (explicit DENY on AnyPrincipal is overridden by root; in practice the
    // break-glass role is not an application role and holds MFA-gated access).
    table.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'DenyAuditUpdateItem',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: ['dynamodb:UpdateItem'],
        resources: [table.tableArn],
        conditions: {
          // Deny unless the caller is the AWS account root (arn:aws:iam::{account}:root).
          // This preserves break-glass access while blocking all normal roles.
          ArnNotLike: {
            'aws:PrincipalArn': `arn:aws:iam::${cdk.Aws.ACCOUNT_ID}:root`,
          },
        },
      }),
    );

    // DENY: all AWS principals are denied dynamodb:DeleteItem on the audit table.
    table.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'DenyAuditDeleteItem',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: ['dynamodb:DeleteItem'],
        resources: [table.tableArn],
        conditions: {
          ArnNotLike: {
            'aws:PrincipalArn': `arn:aws:iam::${cdk.Aws.ACCOUNT_ID}:root`,
          },
        },
      }),
    );
  }

  // -------------------------------------------------------------------------
  // _addLegalHoldPolicy
  //
  // Adds a bucket-policy DENY statement that prevents normal app and purge
  // roles from deleting or overwriting an object that carries the
  // 'contract-toaster:legal-hold' = 'true' tag.
  //
  // This is a defense-in-depth control: held objects must not be deletable
  // by any normal IAM principal (application, purge worker, or admin role
  // operating without the MFA break-glass session).
  //
  // The policy uses s3:ExistingObjectTag as a condition key: if the stored
  // object's tag 'contract-toaster:legal-hold' equals 'true', the request is DENIED.
  //
  // Legal-hold tags are set by the application on the DynamoDB reviews row
  // AND on the S3 object (mirrored) so the bucket policy can evaluate them.
  //
  // Note: for corpus and audit-archive buckets, S3 Object Lock legal holds
  // are the primary enforcement mechanism; this bucket-policy DENY is an
  // additional layer so that the legal-hold invariant is enforced even on
  // buckets where Object Lock is not the selected primitive.
  //
  // Break-glass bypass (governance mode):
  //   A holder of s3:BypassGovernanceRetention may remove Object Lock
  //   retention, but CANNOT bypass this DENY bucket policy (which is
  //   unconditional for non-MFA callers).  The MFA break-glass role must
  //   also satisfy the condition below (MultiFactorAuthPresent = true) to
  //   delete a held object, ensuring no single compromised credential
  //   can remove the legal-hold protection silently.
  //
  // CloudTrail + alarm: all s3:DeleteObject and s3:PutObject attempts on
  // these buckets are logged via S3 data events (#57).  A CloudWatch alarm
  // fires on any DENY matching this policy (#57).
  // -------------------------------------------------------------------------
  private _addLegalHoldPolicy(bucket: s3.Bucket, dataClass: string): void {
    // DENY: delete a held object without MFA.
    bucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: `DenyDeleteHeldObject${dataClass.charAt(0).toUpperCase() + dataClass.slice(1)}`,
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: ['s3:DeleteObject', 's3:DeleteObjectVersion'],
        resources: [`${bucket.bucketArn}/*`],
        conditions: {
          // Deny when the object carries contract-toaster:legal-hold = true
          StringEquals: { 's3:ExistingObjectTag/contract-toaster:legal-hold': 'true' },
          // … unless the caller presents MFA (break-glass exemption).
          BoolIfExists: { 'aws:MultiFactorAuthPresent': 'false' },
        },
      }),
    );

    // DENY: overwrite a held object without MFA.
    bucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: `DenyOverwriteHeldObject${dataClass.charAt(0).toUpperCase() + dataClass.slice(1)}`,
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: ['s3:PutObject'],
        resources: [`${bucket.bucketArn}/*`],
        conditions: {
          StringEquals: { 's3:ExistingObjectTag/contract-toaster:legal-hold': 'true' },
          BoolIfExists: { 'aws:MultiFactorAuthPresent': 'false' },
        },
      }),
    );
  }
}
