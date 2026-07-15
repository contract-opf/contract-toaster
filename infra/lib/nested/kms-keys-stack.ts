import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';

export interface KmsKeysStackProps extends cdk.NestedStackProps {
  readonly envName: string;
  /**
   * Resource-name prefix (issue #233). Defaults to 'contract-toaster' so the
   * existing dev/prod key aliases are unchanged. New deployments can override
   * via CDK context (--context appName=acmecorp).
   */
  readonly appName?: string;
  /** Deploy role — needs no data-plane KMS access; included for key-policy root only. */
  readonly deployRole: iam.IRole;
  /**
   * Per-data-class runtime roles for grant binding.
   *
   * Role-splitting (#55/#71) is NOT yet complete; these props are optional so
   * this stack can be instantiated today without grants.  Once each role exists,
   * pass it here and the corresponding key will receive its narrow grant.
   *
   * No field is the same role for two keys — that would collapse the isolation
   * boundary AC B requires.  If role-splitting has not been done yet, leave the
   * fields undefined and grant nothing (keys-only, no runtime principal).
   */
  /** Upload-path role: may encrypt/decrypt uploads only.  Must NOT be corpusReaderRole. */
  readonly uploadWriterRole?: iam.IRole;
  /** Corpus-reader role: may decrypt corpus only.  Must NOT be uploadWriterRole. */
  readonly corpusReaderRole?: iam.IRole;
  /** Audit-writer role: may encrypt audit records only (append-only). */
  readonly auditWriterRole?: iam.IRole;
  /** Outputs role: may encrypt/decrypt outputs (redlines + result packets). */
  readonly outputsRole?: iam.IRole;
  /** DynamoDB role: may encrypt/decrypt DynamoDB items. */
  readonly dynamodbRole?: iam.IRole;
}

/**
 * KmsKeysStack — per-data-class customer-managed KMS keys.
 *
 * Issue #70: One CMK per data class.  AC B requires that NO single IAM
 * principal can decrypt more than one data class.  Grants are therefore
 * split by data class: each key accepts a distinct, dedicated role.
 *
 * Runtime grants are deferred until role-splitting in #55/#71 produces
 * the distinct per-data-class roles.  Until then, no runtime principal
 * receives any grant (keys-only).  Pass the optional role props once
 * each role is created.
 *
 * Data classes:
 *
 *   uploads   — S3 bucket for incoming contract .docx files (#51).
 *               Grant: uploadWriterRole (encrypt/decrypt) — a role that
 *               MUST NOT also hold corpus or audit access.
 *
 *   outputs   — S3 bucket for generated redlines and result packets (#51).
 *               Grant: outputsRole (encrypt/decrypt).
 *
 *   corpus    — S3 bucket for standard-form corpus + S3 Vectors / clause-text
 *               store (#51, #60, #32).  The corpus key covers S3 Vectors and
 *               the clause-text store per the 2026-06-11 architecture review
 *               reconciliation (finding #32).
 *               Grant: corpusReaderRole (decrypt only) — a role that
 *               MUST NOT also hold uploads or audit access.
 *
 *   audit     — S3 bucket for immutable audit log objects and CloudTrail
 *               delivery (#51, #57).  Tightest break-glass policy: only the
 *               designated legal-hold admin can cancel or schedule key deletion.
 *               Grant: auditWriterRole (encrypt only — append-only semantics).
 *
 *   dynamodb  — DynamoDB tables: users, playbooks, reviews, cost ledger (#52).
 *               Grant: dynamodbRole (encrypt/decrypt).
 *
 * Break-glass policy (enforced by comments + IAM condition keys):
 *   All keys:   Only the AWS account root + designated break-glass principal
 *               can call kms:ScheduleKeyDeletion / kms:CancelKeyDeletion.
 *   Audit key:  TIGHTER — requires an explicit MFA condition on destructive
 *               operations; break-glass access is logged to CloudTrail and
 *               triggers a CloudWatch alarm (#57).
 *   Other keys: Standard break-glass; access logged but no MFA requirement.
 *
 * Key rotation: enabled (annual) on all keys.
 * Removal policy: RETAIN on all keys (prevent accidental data loss).
 *
 * Downstream issues that consume these keys:
 *   #51 — S3 buckets (uploads, outputs, corpus, audit)
 *   #52 — DynamoDB tables (dynamodb key)
 *   #55 — App Runner (creates uploadWriterRole, corpusReaderRole, auditWriterRole, etc.)
 *   #60 — S3 Vectors / clause-text store (corpus key)
 *   #71 — Scoped download auth (outputsRole)
 */
export class KmsKeysStack extends cdk.NestedStack {
  /**
   * CMK for the uploads S3 bucket (incoming .docx contracts).
   * Undefined under `--context profile=minimal` (issue #231): the minimal
   * deploy profile uses AWS-managed (not customer-managed) encryption for
   * S3/DynamoDB to cut the per-key cost and key-policy complexity a
   * demo/OSS adopter does not need. See DataStack for the encryption
   * fallback.
   */
  readonly uploadsKey?: kms.Key;
  /** CMK for the outputs S3 bucket (generated redlines + result packets). Undefined under profile=minimal (see uploadsKey). */
  readonly outputsKey?: kms.Key;
  /**
   * CMK for the corpus S3 bucket, S3 Vectors knowledge base, and
   * clause-text store (per reconciliation note #32, 2026-06-11 review).
   * Undefined under profile=minimal (see uploadsKey).
   */
  readonly corpusKey?: kms.Key;
  /**
   * CMK for audit S3 objects and CloudTrail delivery.
   * Break-glass is TIGHTER than other keys (MFA required for destructive ops).
   * Undefined under profile=minimal (see uploadsKey).
   */
  readonly auditKey?: kms.Key;
  /** CMK for all DynamoDB tables (users, playbooks, reviews, cost ledger). Undefined under profile=minimal (see uploadsKey). */
  readonly dynamodbKey?: kms.Key;
  /**
   * CMK for the Step Functions state machine (issue #19, wired in #59).
   *
   * Encrypts execution-history records (state input/output) at rest. State
   * payloads are pointer-only (S3 keys, hashes, non-substantive metadata) by
   * convention — see infra/lib/nested/pipeline-stack.ts — but this key adds
   * encryption-at-rest as a second layer regardless.
   */
  readonly stateMachineKey: kms.Key;

  constructor(scope: Construct, id: string, props: KmsKeysStackProps) {
    super(scope, id, props);

    const {
      envName,
      uploadWriterRole,
      corpusReaderRole,
      auditWriterRole,
      outputsRole,
      dynamodbRole,
    } = props;
    const appName = props.appName ?? 'contract-toaster';

    // -----------------------------------------------------------------------
    // Deploy profile (issue #231). 'hardened' (default) creates all five
    // per-data-class CMKs below, unchanged from pre-#231 behavior. 'minimal'
    // skips them entirely -- DataStack falls back to AWS-managed encryption
    // for S3/DynamoDB, cutting per-key cost and key-policy complexity for a
    // demo/OSS deploy. The Step Functions state-machine CMK below is NOT
    // gated by profile -- it is unrelated to the S3/DynamoDB data-class
    // CMKs this issue targets.
    // -----------------------------------------------------------------------
    const profile = (this.node.tryGetContext('profile') as string | undefined) ?? 'hardened';
    const isMinimal = profile === 'minimal';

    if (!isMinimal) {
    // -----------------------------------------------------------------------
    // uploads key
    //
    // Used by: uploads S3 bucket (#51).
    // Grantees: uploadWriterRole (encrypt/decrypt) — wired once created in #55.
    //           This role must be DISTINCT from corpusReaderRole and auditWriterRole;
    //           the same principal MUST NOT hold grants on both this key and the
    //           corpus key (AC B).
    // Break-glass: standard (account root + legal-hold-admin principal, logged).
    // -----------------------------------------------------------------------
    this.uploadsKey = new kms.Key(this, 'UploadsKey', {
      description: `ContractToaster review — uploads data-class CMK (${envName})`,
      enableKeyRotation: true,
      alias: `alias/${appName}-${envName}-uploads`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.uploadsKey).add('contract-toaster:env', envName);
    cdk.Tags.of(this.uploadsKey).add('contract-toaster:data-class', 'uploads');
    // break-glass: standard — account root + legal-hold-admin; no MFA requirement.
    cdk.Tags.of(this.uploadsKey).add('contract-toaster:break-glass-policy', 'standard');

    // Narrow upload grant: only the dedicated upload-writer role (distinct from
    // corpusReaderRole) may encrypt/decrypt uploads.  Deferred to #55 — no grant
    // until the per-data-class role exists.
    if (uploadWriterRole) {
      this.uploadsKey.grantEncryptDecrypt(uploadWriterRole);
    }

    // KMS encryption-context enforcement for the uploads key (issue #71 AC1).
    // Any principal attempting GenerateDataKey or Encrypt on this key MUST
    // supply exactly {contract-toaster:data-class, contract-toaster:review-id} as context keys.
    // This is a DENY-unless condition: a Null condition check is used so
    // requests that omit either key are denied regardless of IAM role grants.
    this.uploadsKey.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'UploadsKeyEncryptionContextRequired',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: [
          'kms:GenerateDataKey',
          'kms:GenerateDataKeyWithoutPlaintext',
          'kms:Encrypt',
        ],
        resources: ['*'],
        conditions: {
          // Deny when contract-toaster:data-class context key is absent or not "uploads".
          StringNotEquals: {
            'kms:EncryptionContext:contract-toaster:data-class': 'uploads',
          },
        },
      }),
    );
    this.uploadsKey.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'UploadsKeyReviewIdContextRequired',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: [
          'kms:GenerateDataKey',
          'kms:GenerateDataKeyWithoutPlaintext',
          'kms:Encrypt',
        ],
        resources: ['*'],
        conditions: {
          // Deny when contract-toaster:review-id context key is absent (null check).
          Null: {
            'kms:EncryptionContext:contract-toaster:review-id': 'true',
          },
        },
      }),
    );

    // -----------------------------------------------------------------------
    // outputs key
    //
    // Used by: outputs S3 bucket (redlines, result packets) (#51).
    // Grantees: outputsRole (encrypt/decrypt) — wired once created in #71.
    // Break-glass: standard.
    // -----------------------------------------------------------------------
    this.outputsKey = new kms.Key(this, 'OutputsKey', {
      description: `ContractToaster review — outputs data-class CMK (${envName})`,
      enableKeyRotation: true,
      alias: `alias/${appName}-${envName}-outputs`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.outputsKey).add('contract-toaster:env', envName);
    cdk.Tags.of(this.outputsKey).add('contract-toaster:data-class', 'outputs');
    // break-glass: standard — account root + legal-hold-admin; no MFA requirement.
    cdk.Tags.of(this.outputsKey).add('contract-toaster:break-glass-policy', 'standard');

    // Narrow outputs grant: only the dedicated outputs role may encrypt/decrypt
    // outputs.  Deferred to #71 — no grant until the per-data-class role exists.
    if (outputsRole) {
      this.outputsKey.grantEncryptDecrypt(outputsRole);
    }

    // KMS encryption-context enforcement for the outputs key (issue #71 AC1).
    // Any principal attempting GenerateDataKey or Decrypt on this key MUST
    // supply exactly {contract-toaster:data-class: "outputs", contract-toaster:review-id: "<id>"}.
    // Prevents cross-review and cross-data-class decryption in the download
    // and presigned URL paths (AC2 defence-in-depth).
    this.outputsKey.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'OutputsKeyEncryptionContextRequired',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: [
          'kms:GenerateDataKey',
          'kms:GenerateDataKeyWithoutPlaintext',
          'kms:Decrypt',
        ],
        resources: ['*'],
        conditions: {
          // Deny when contract-toaster:data-class context key is absent or not "outputs".
          StringNotEquals: {
            'kms:EncryptionContext:contract-toaster:data-class': 'outputs',
          },
        },
      }),
    );
    this.outputsKey.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'OutputsKeyReviewIdContextRequired',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: [
          'kms:GenerateDataKey',
          'kms:GenerateDataKeyWithoutPlaintext',
          'kms:Decrypt',
        ],
        resources: ['*'],
        conditions: {
          // Deny when contract-toaster:review-id context key is absent (null check).
          Null: {
            'kms:EncryptionContext:contract-toaster:review-id': 'true',
          },
        },
      }),
    );

    // -----------------------------------------------------------------------
    // corpus key
    //
    // Used by: corpus S3 bucket, S3 Vectors knowledge base, and the
    //          clause-text store (per reconciliation note from the 2026-06-11
    //          architecture review, finding #32 — these stores are in the
    //          corpus key domain).
    // Grantees: corpusReaderRole (decrypt only) — wired once created in #55.
    //           This role must be DISTINCT from uploadWriterRole; the same
    //           principal MUST NOT hold grants on both this key and the uploads
    //           key (AC B).
    // Break-glass: standard.
    // -----------------------------------------------------------------------
    this.corpusKey = new kms.Key(this, 'CorpusKey', {
      description: `ContractToaster review — corpus data-class CMK (${envName}); ` +
                   `covers corpus S3 bucket, S3 Vectors store, clause-text store`,
      enableKeyRotation: true,
      alias: `alias/${appName}-${envName}-corpus`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.corpusKey).add('contract-toaster:env', envName);
    cdk.Tags.of(this.corpusKey).add('contract-toaster:data-class', 'corpus');
    // break-glass: standard — account root + legal-hold-admin; no MFA requirement.
    cdk.Tags.of(this.corpusKey).add('contract-toaster:break-glass-policy', 'standard');
    // Reconciliation note #32: this key also covers vector and clause-text stores.
    cdk.Tags.of(this.corpusKey).add('contract-toaster:corpus-covers', 's3-vectors,clause-text');

    // Narrow corpus grant: only the dedicated corpus-reader role (distinct from
    // uploadWriterRole) may decrypt corpus data.  Deferred to #55 — no grant
    // until the per-data-class role exists.
    if (corpusReaderRole) {
      this.corpusKey.grantDecrypt(corpusReaderRole);
    }

    // -----------------------------------------------------------------------
    // audit key
    //
    // Used by: audit S3 bucket (immutable audit log objects, CloudTrail
    //          delivery) (#51, #57).
    // Grantees: App Runner task role (write audit rows only — append-only).
    // Break-glass: TIGHTER — requires MFA for kms:ScheduleKeyDeletion and
    //              kms:CancelKeyDeletion.  Access triggers a CloudWatch alarm
    //              (#57).  Only the designated legal-hold admin principal may
    //              request these operations under MFA.
    // -----------------------------------------------------------------------
    this.auditKey = new kms.Key(this, 'AuditKey', {
      description: `ContractToaster review — audit data-class CMK (${envName}); ` +
                   `tighter break-glass: MFA required for destructive operations`,
      enableKeyRotation: true,
      alias: `alias/${appName}-${envName}-audit`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.auditKey).add('contract-toaster:env', envName);
    cdk.Tags.of(this.auditKey).add('contract-toaster:data-class', 'audit');
    // break-glass: TIGHTER — MFA required for ScheduleKeyDeletion / CancelKeyDeletion.
    // Only the designated legal-hold admin may invoke these under MFA.
    // Access is logged to CloudTrail and triggers a CloudWatch alarm (#57).
    cdk.Tags.of(this.auditKey).add('contract-toaster:break-glass-policy', 'mfa-required');

    // Add a resource policy statement enforcing the tighter break-glass for
    // audit: kms:ScheduleKeyDeletion and kms:CancelKeyDeletion require MFA.
    //
    // Note: in CDK the default key policy grants the account root full access.
    // We overlay an additional explicit DENY for these destructive operations
    // unless MFA is present, reducing the blast radius for the audit log key.
    this.auditKey.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AuditKeyBreakGlassMfaRequired',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: ['kms:ScheduleKeyDeletion', 'kms:CancelKeyDeletion'],
        resources: ['*'],
        conditions: {
          // Deny if MFA is NOT present (i.e. allow only when MFA is present).
          BoolIfExists: { 'aws:MultiFactorAuthPresent': 'false' },
        },
      }),
    );

    // Narrow audit grant: only the dedicated audit-writer role may encrypt audit
    // records (append-only — no Decrypt granted here).  Deferred to #55 — no
    // grant until the per-data-class role exists.
    if (auditWriterRole) {
      this.auditKey.grantEncrypt(auditWriterRole);
    }

    // -----------------------------------------------------------------------
    // dynamodb key
    //
    // Used by: all DynamoDB tables (users, playbooks, reviews, cost ledger) (#52).
    // Grantees: App Runner task role (read/write reviews and cost ledger).
    // Break-glass: standard.
    // -----------------------------------------------------------------------
    this.dynamodbKey = new kms.Key(this, 'DynamodbKey', {
      description: `ContractToaster review — dynamodb data-class CMK (${envName})`,
      enableKeyRotation: true,
      alias: `alias/${appName}-${envName}-dynamodb`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.dynamodbKey).add('contract-toaster:env', envName);
    cdk.Tags.of(this.dynamodbKey).add('contract-toaster:data-class', 'dynamodb');
    // break-glass: standard — account root + legal-hold-admin; no MFA requirement.
    cdk.Tags.of(this.dynamodbKey).add('contract-toaster:break-glass-policy', 'standard');

    // Narrow DynamoDB grant: only the dedicated DynamoDB role may encrypt/decrypt
    // DynamoDB items.  Deferred to #55 — no grant until the per-data-class role exists.
    if (dynamodbRole) {
      this.dynamodbKey.grantEncryptDecrypt(dynamodbRole);
    }
    } // end if (!isMinimal) -- five per-data-class CMKs (issue #231)

    // -----------------------------------------------------------------------
    // state machine key
    //
    // Used by: the Step Functions state machine (#59). Encrypts execution
    // history at rest as a second layer of defense; the primary control for
    // preventing document substance from landing in execution history is the
    // pointer-only payload convention enforced in pipeline-stack.ts.
    // Break-glass: standard.
    // -----------------------------------------------------------------------
    this.stateMachineKey = new kms.Key(this, 'StateMachineKey', {
      description: `ContractToaster review — Step Functions state machine CMK (${envName})`,
      enableKeyRotation: true,
      alias: `alias/${appName}-${envName}-state-machine`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.stateMachineKey).add('contract-toaster:env', envName);
    cdk.Tags.of(this.stateMachineKey).add('contract-toaster:data-class', 'state-machine');
    // break-glass: standard — account root + legal-hold-admin; no MFA requirement.
    cdk.Tags.of(this.stateMachineKey).add('contract-toaster:break-glass-policy', 'standard');

    // -----------------------------------------------------------------------
    // Stack outputs — export key ARNs so #51 (S3 buckets) and #52 (DynamoDB)
    // can reference them without hard-coding ARNs. Skipped under
    // profile=minimal (issue #231): the five keys above are not created.
    // -----------------------------------------------------------------------
    if (!isMinimal) {
    new cdk.CfnOutput(this, 'UploadsKeyArn', {
      value: this.uploadsKey!.keyArn,
      description: `Uploads CMK ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-uploads-KmsKeyArn`,
    });

    new cdk.CfnOutput(this, 'OutputsKeyArn', {
      value: this.outputsKey!.keyArn,
      description: `Outputs CMK ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-outputs-KmsKeyArn`,
    });

    new cdk.CfnOutput(this, 'CorpusKeyArn', {
      value: this.corpusKey!.keyArn,
      description: `Corpus CMK ARN for ${envName} (covers S3 Vectors + clause-text)`,
      exportName: `ContractToaster-${envName}-corpus-KmsKeyArn`,
    });

    new cdk.CfnOutput(this, 'AuditKeyArn', {
      value: this.auditKey!.keyArn,
      description: `Audit CMK ARN for ${envName} (tighter break-glass)`,
      exportName: `ContractToaster-${envName}-audit-KmsKeyArn`,
    });

    new cdk.CfnOutput(this, 'DynamodbKeyArn', {
      value: this.dynamodbKey!.keyArn,
      description: `DynamoDB CMK ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-dynamodb-KmsKeyArn`,
    });
    } // end if (!isMinimal) -- per-data-class CMK outputs (issue #231)

    new cdk.CfnOutput(this, 'StateMachineKeyArn', {
      value: this.stateMachineKey.keyArn,
      description: `Step Functions state machine CMK ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-stateMachine-KmsKeyArn`,
    });
  }
}
