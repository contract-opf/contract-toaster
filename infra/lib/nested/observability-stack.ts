import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as cdk from 'aws-cdk-lib';
import * as cloudtrail from 'aws-cdk-lib/aws-cloudtrail';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import { Construct } from 'constructs';

export interface ObservabilityStackProps extends cdk.NestedStackProps {
  readonly envName: string;
  /**
   * Subscriber email for the alarms SNS topic (issue #57 AC: "Topic
   * subscription is a placeholder email for now"; see RUNBOOK.md -> "Confirm
   * the SNS email subscription"). REQUIRED — no internal default (issue
   * #349: the top-level ContractToasterStack already requires `alarmsEmail`
   * via CDK context with no internal tenant-mailbox fallback; this prop-level
   * default was the one place that fallback still lived).
   */
  readonly alarmsEmail: string;
  /** Monthly AWS Budgets ceiling in USD (issue #61 AC: target <= $100/mo dev). */
  readonly monthlyBudgetUsd?: number;
  /**
   * App Runner service name (issue #57 AC: request rate / 4xx / 5xx / p99
   * latency tiles + the 5xx alarm). Defaults to the AppStack naming
   * convention `contract-toaster-api-{envName}` so `cdk synth` succeeds
   * standalone; the real dependency is threaded from AppStack in
   * contract-toaster-stack.ts.
   */
  readonly appRunnerServiceName?: string;
  /**
   * Review pipeline state machine (issue #57 AC: "Step Functions stage
   * failures" tile). Optional -- omitted, the tile/metric is still rendered
   * against the well-known state-machine name so `cdk synth` succeeds before
   * PipelineStack is wired in.
   */
  readonly stateMachine?: sfn.IStateMachine;
  /**
   * audit-archive S3 bucket that the CloudTrail trail delivers to (issue #57
   * AC: "CloudTrail trail created, logging to `audit-archive` bucket").
   * Optional for standalone synth -- defaults to importing the bucket by the
   * DataStack naming convention `contract-toaster-audit-archive-{envName}`.
   */
  readonly auditArchiveBucket?: s3.IBucket;
  /**
   * CMK used to encrypt the audit-archive bucket (issue #70). When supplied,
   * the CloudTrail trail encrypts its delivered logs with this same key so
   * CloudTrail objects share the audit data-class key policy (including its
   * MFA-gated break-glass controls).
   */
  readonly auditKey?: kms.IKey;
  /** Bedrock model IDs to key the per-day-invocations / error metrics off of. */
  readonly bedrockModelIds?: string[];
}

/**
 * ObservabilityStack — CloudWatch dashboards/alarms (#57, still a
 * placeholder for the dashboard/CloudTrail resources) plus the
 * environment's alarms SNS topic and the AWS Budgets monthly-cost
 * guardrail (issue #61).
 *
 * Resources added here (issue #57):
 *  - CloudWatch dashboard `contract-toaster-{env}` with tiles for: deployed
 *    version (text widget), App Runner request rate, App Runner 4xx/5xx
 *    error rate (split), App Runner p99 latency, Bedrock invocations/day
 *    (per pinned model), Step Functions stage failures (native SFN metric).
 *    Cost-to-date (Cost Explorer), stale PENDING/RUNNING reviews, abandoned
 *    spend reservations, release-bundle activation/rollback audit events,
 *    manual-review-state counts (#37), and audit-archive stream lag all
 *    require **custom application metrics that do not exist yet** -- #57's
 *    "Out of scope" section explicitly excludes building that backend
 *    instrumentation. Those tiles render as placeholder text widgets naming
 *    the metric namespace they will bind to once the corresponding backend
 *    issue (see ARCHITECTURE.md / RUNBOOK.md -> Observability) ships the
 *    `PutMetricData` call, rather than fabricate a metric expression against
 *    a namespace nothing publishes to.
 *  - CloudTrail trail logging to the `audit-archive` bucket (issue #51),
 *    encrypted with the audit CMK (issue #70) so trail objects share the
 *    audit data-class key policy and its MFA-gated break-glass controls.
 *  - Two required alarms, both routed to the `contract-toaster-alarms` SNS topic:
 *      - App Runner 5xx rate > 5% for 5 minutes.
 *      - Any genuine Bedrock invocation error (ThrottlingException retries
 *        are split out per issue #17's reconciliation note -- a companion
 *        alarm tracks throttles separately so retry noise never pages
 *        on-call under the "Bedrock errors > 0" name).
 *
 * Resources added here (issue #61):
 *  - `contract-toaster-alarms` SNS topic — the shared alarms destination named in
 *    ARCHITECTURE.md / RUNBOOK.md. Built here (rather than deferred to #57)
 *    because the AWS Budgets alarm below needs a concrete topic to route
 *    to; #57's dashboard alarms will publish to this same topic when they
 *    land.
 *  - AWS Budgets monthly cost budget (target <= $100/mo dev per issue #61
 *    AC and ARCHITECTURE.md -> Cost shape "Guardrail") with an
 *    ACTUAL-and-FORECASTED notification pair, routed to the alarms topic.
 *    This is the account-level backstop distinct from, and layered on top
 *    of, the in-app $20/day Bedrock spend reservation (backend/src/reviews.py).
 *
 * Security invariant:
 *  - CloudWatch must never log document content, rationales, or PII.
 *    Log groups are configured with no-content retention policies.
 */
export class ObservabilityStack extends cdk.NestedStack {
  /** Shared alarms destination -- CloudWatch alarms (#57) and AWS Budgets
   * (#61) both publish here. */
  readonly alarmsTopic: sns.Topic;
  readonly monthlyBudget: budgets.CfnBudget;
  readonly dashboard: cloudwatch.Dashboard;
  /**
   * CloudTrail trail. Undefined under `--context profile=minimal` (issue
   * #231) -- the minimal deploy profile omits CloudTrail entirely (it is
   * meaningful alongside the Object-Lock audit-archive posture the minimal
   * profile does not provide).
   */
  readonly trail?: cloudtrail.Trail;
  readonly appRunner5xxAlarm: cloudwatch.Alarm;
  readonly bedrockErrorAlarm: cloudwatch.Alarm;
  readonly bedrockThrottleAlarm: cloudwatch.Alarm;

  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);

    const { envName, alarmsEmail } = props;
    // Deploy profile (issue #231). See kms-keys-stack.ts / data-stack.ts for
    // the matching profile resolution pattern.
    const profile = (this.node.tryGetContext('profile') as string | undefined) ?? 'hardened';
    const isMinimal = profile === 'minimal';
    const monthlyBudgetUsd = props.monthlyBudgetUsd ?? 100;
    const appRunnerServiceName = props.appRunnerServiceName ?? `contract-toaster-api-${envName}`;
    const bedrockModelIds = props.bedrockModelIds ?? [
      'anthropic.claude-opus-4-8',
      'anthropic.claude-sonnet-4-6',
    ];

    // -----------------------------------------------------------------------
    // contract-toaster-alarms SNS topic (issue #57 naming; issue #61 is the first
    // consumer to actually build it). Placeholder email subscription per
    // #57 AC -- swap/add subscribers via the admin/ops workflow later.
    // -----------------------------------------------------------------------
    this.alarmsTopic = new sns.Topic(this, 'AlarmsTopic', {
      topicName: `contract-toaster-alarms-${envName}`,
      displayName: `ContractToaster review alarms (${envName})`,
    });
    this.alarmsTopic.addSubscription(new subscriptions.EmailSubscription(alarmsEmail));

    cdk.Tags.of(this.alarmsTopic).add('contract-toaster:env', envName);
    cdk.Tags.of(this.alarmsTopic).add('contract-toaster:component', 'observability');

    // -----------------------------------------------------------------------
    // AWS Budgets — monthly cost guardrail (issue #61 AC).
    //
    // Target <= $100/mo dev (ARCHITECTURE.md -> Cost shape "Guardrail").
    // Two notifications, both routed to the alarms SNS topic:
    //   - ACTUAL spend > 80% of budget    (early warning)
    //   - FORECASTED spend > 100% of budget (this month is on track to
    //     exceed the ceiling even before it happens)
    // This is an account-level backstop on top of, not instead of, the
    // in-app $20/day Bedrock spend reservation (backend/src/reviews.py
    // reserve_spend) -- Budgets covers the WHOLE account's AWS bill
    // (compute, storage, data transfer, etc.), not just Bedrock tokens.
    // -----------------------------------------------------------------------
    this.monthlyBudget = new budgets.CfnBudget(this, 'MonthlyCostBudget', {
      budget: {
        budgetName: `contract-toaster-${envName}-monthly-cost`,
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: {
          amount: monthlyBudgetUsd,
          unit: 'USD',
        },
      },
      notificationsWithSubscribers: [
        {
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold: 80,
            thresholdType: 'PERCENTAGE',
          },
          subscribers: [
            {
              subscriptionType: 'SNS',
              address: this.alarmsTopic.topicArn,
            },
          ],
        },
        {
          notification: {
            notificationType: 'FORECASTED',
            comparisonOperator: 'GREATER_THAN',
            threshold: 100,
            thresholdType: 'PERCENTAGE',
          },
          subscribers: [
            {
              subscriptionType: 'SNS',
              address: this.alarmsTopic.topicArn,
            },
          ],
        },
      ],
    });

    // AWS Budgets requires an explicit resource policy granting the
    // budgets service principal permission to publish to the topic.
    this.alarmsTopic.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowAwsBudgetsPublish',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('budgets.amazonaws.com')],
        actions: ['sns:Publish'],
        resources: [this.alarmsTopic.topicArn],
      }),
    );

    new cdk.CfnOutput(this, 'AlarmsTopicArn', {
      value: this.alarmsTopic.topicArn,
      description: `Shared alarms SNS topic ARN for ${envName}`,
      exportName: `ContractToaster-${envName}-observability-AlarmsTopicArn`,
    });

    // -----------------------------------------------------------------------
    // CloudTrail — logs all AWS management API calls (including Bedrock
    // InvokeModel/Converse, which AWS documents as management events) to the
    // audit-archive bucket (issue #57 AC).
    //
    // Per-object S3 data events remain OFF by default (cost; see RUNBOOK.md
    // -> Observability -> CloudTrail). Encrypted with the audit CMK (#70) so
    // trail objects share the audit data-class key policy, including its
    // MFA-gated break-glass controls -- the Trail L2 construct grants
    // CloudTrail the necessary key + bucket permissions automatically.
    // -----------------------------------------------------------------------
    // Skipped entirely under profile=minimal (issue #231) -- see the `trail`
    // property doc above.
    if (!isMinimal) {
      const auditArchiveBucket =
        props.auditArchiveBucket ??
        s3.Bucket.fromBucketName(
          this,
          'ImportedAuditArchiveBucket',
          `contract-toaster-audit-archive-${envName}`,
        );

      this.trail = new cloudtrail.Trail(this, 'AuditTrail', {
        trailName: `contract-toaster-${envName}`,
        bucket: auditArchiveBucket,
        encryptionKey: props.auditKey,
        isMultiRegionTrail: false,
        includeGlobalServiceEvents: true,
        enableFileValidation: true,
        sendToCloudWatchLogs: false, // management-event JSON lands in S3 only; no doc/prompt content is ever in scope (see threat-model.md)
      });
      cdk.Tags.of(this.trail).add('contract-toaster:env', envName);
      cdk.Tags.of(this.trail).add('contract-toaster:component', 'observability');

      new cdk.CfnOutput(this, 'AuditTrailArn', {
        value: this.trail.trailArn,
        description: `CloudTrail trail ARN for ${envName}`,
        exportName: `ContractToaster-${envName}-observability-AuditTrailArn`,
      });
    }

    // -----------------------------------------------------------------------
    // App Runner metrics (native `AWS/AppRunner` namespace -- no CDK L2
    // metric helper exists for App Runner, so these are raw cloudwatch.Metric
    // objects keyed on ServiceName).
    // -----------------------------------------------------------------------
    const appRunnerMetric = (metricName: string, statistic = 'Sum') =>
      new cloudwatch.Metric({
        namespace: 'AWS/AppRunner',
        metricName,
        dimensionsMap: { ServiceName: appRunnerServiceName },
        statistic,
        period: cdk.Duration.minutes(5),
      });

    const requestCount = appRunnerMetric('RequestCount');
    const http4xx = appRunnerMetric('4xxStatusResponses');
    const http5xx = appRunnerMetric('5xxStatusResponses');
    const p99Latency = appRunnerMetric('RequestLatency', 'p99');

    // 5xx rate = 5xxStatusResponses / RequestCount. CloudWatch math expression
    // so the alarm and dashboard both threshold on a *rate*, not a raw count
    // (issue #57 AC: "5xx > 5% for 5 minutes").
    const http5xxRate = new cloudwatch.MathExpression({
      expression: '(m5xx / MAX([m5xx, mReq, 1])) * 100',
      usingMetrics: { m5xx: http5xx, mReq: requestCount },
      period: cdk.Duration.minutes(5),
      label: 'App Runner 5xx rate (%)',
    });

    // -----------------------------------------------------------------------
    // Bedrock invocation metrics, split by model (per pinned model in
    // model-policy/bedrock-us-east-1.json). Errors and throttles are tracked
    // as SEPARATE metrics/alarms per issue #17's reconciliation note: a
    // ThrottlingException retry is expected quota pressure, not a genuine
    // failure, and must not fire the "Bedrock invocation errors > 0" alarm.
    // -----------------------------------------------------------------------
    const bedrockInvocationMetrics = bedrockModelIds.map(
      (modelId) =>
        new cloudwatch.Metric({
          namespace: 'AWS/Bedrock',
          metricName: 'Invocations',
          dimensionsMap: { ModelId: modelId },
          statistic: 'Sum',
          period: cdk.Duration.days(1),
        }),
    );

    const bedrockGenuineErrorMetrics = bedrockModelIds.map(
      (modelId) =>
        new cloudwatch.Metric({
          namespace: 'AWS/Bedrock',
          metricName: 'InvocationServerErrors',
          dimensionsMap: { ModelId: modelId },
          statistic: 'Sum',
          period: cdk.Duration.minutes(5),
        }),
    );

    const bedrockThrottleMetrics = bedrockModelIds.map(
      (modelId) =>
        new cloudwatch.Metric({
          namespace: 'AWS/Bedrock',
          metricName: 'InvocationThrottles',
          dimensionsMap: { ModelId: modelId },
          statistic: 'Sum',
          period: cdk.Duration.minutes(5),
        }),
    );

    const bedrockGenuineErrorSum = new cloudwatch.MathExpression({
      expression: bedrockGenuineErrorMetrics.map((_, i) => `e${i}`).join(' + '),
      usingMetrics: Object.fromEntries(
        bedrockGenuineErrorMetrics.map((m, i) => [`e${i}`, m]),
      ),
      period: cdk.Duration.minutes(5),
      label: 'Bedrock invocation errors (excludes throttles)',
    });

    const bedrockThrottleSum = new cloudwatch.MathExpression({
      expression: bedrockThrottleMetrics.map((_, i) => `t${i}`).join(' + '),
      usingMetrics: Object.fromEntries(
        bedrockThrottleMetrics.map((m, i) => [`t${i}`, m]),
      ),
      period: cdk.Duration.minutes(5),
      label: 'Bedrock ThrottlingException retries',
    });

    // -----------------------------------------------------------------------
    // Step Functions stage failures (native metric -- issue #57 AC).
    // Falls back to importing the well-known state-machine ARN so `cdk
    // synth` succeeds before PipelineStack wiring lands the real reference.
    // -----------------------------------------------------------------------
    const stateMachine =
      props.stateMachine ??
      sfn.StateMachine.fromStateMachineArn(
        this,
        'ImportedReviewStateMachine',
        cdk.Stack.of(this).formatArn({
          service: 'states',
          resource: 'stateMachine',
          resourceName: `contract-toaster-${envName}`,
        }),
      );
    const stageFailures = stateMachine.metricFailed({ period: cdk.Duration.minutes(5) });

    // -----------------------------------------------------------------------
    // Alarms (issue #57 AC: exactly two required; #17 adds the throttle
    // split as a companion, non-paging metric alongside the genuine-error
    // alarm).
    // -----------------------------------------------------------------------
    this.appRunner5xxAlarm = new cloudwatch.Alarm(this, 'AppRunner5xxRateAlarm', {
      alarmName: `contract-toaster-${envName}-apprunner-5xx-rate`,
      alarmDescription:
        'App Runner 5xx rate > 5% for 5 minutes (issue #57 AC). ' +
        'See RUNBOOK.md -> Incident response -> Total outage.',
      metric: http5xxRate,
      threshold: 5,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    this.appRunner5xxAlarm.addAlarmAction(new cwActions.SnsAction(this.alarmsTopic));

    this.bedrockErrorAlarm = new cloudwatch.Alarm(this, 'BedrockInvocationErrorAlarm', {
      alarmName: `contract-toaster-${envName}-bedrock-invocation-errors`,
      alarmDescription:
        'Any genuine Bedrock invocation error (excludes ThrottlingException ' +
        'retries -- see the companion throttle alarm and issue #17). ' +
        'See RUNBOOK.md -> Incident response -> Bedrock returns errors.',
      metric: bedrockGenuineErrorSum,
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    this.bedrockErrorAlarm.addAlarmAction(new cwActions.SnsAction(this.alarmsTopic));

    // Throttle alarm is deliberately NOT wired to the same paging topic at
    // the same severity -- issue #17: throttle-driven retries are expected
    // quota pressure, not an incident. It still exists (and is visible on
    // the dashboard) so a sustained throttle storm is diagnosable, but it is
    // classified separately from the genuine-error alarm above.
    this.bedrockThrottleAlarm = new cloudwatch.Alarm(this, 'BedrockThrottleAlarm', {
      alarmName: `contract-toaster-${envName}-bedrock-throttles`,
      alarmDescription:
        'Bedrock ThrottlingException retries observed (quota-pressure signal, ' +
        'NOT a genuine-error page -- issue #17). Informational: review ' +
        'model-policy/bedrock-us-east-1.json granted quota if sustained.',
      metric: bedrockThrottleSum,
      threshold: 0,
      evaluationPeriods: 3,
      datapointsToAlarm: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    this.bedrockThrottleAlarm.addAlarmAction(new cwActions.SnsAction(this.alarmsTopic));

    // -----------------------------------------------------------------------
    // CloudWatch dashboard `contract-toaster-{env}` (issue #57 AC).
    //
    // Tiles backed by real, currently-emitted metrics: App Runner request
    // rate / 4xx-5xx / p99 latency, Bedrock invocations-per-day (per model),
    // Bedrock genuine-error vs throttle split, Step Functions stage failures.
    //
    // Tiles that the AC list asks for but that require custom application
    // metrics NOT YET EMITTED anywhere in the backend (explicitly out of
    // scope for #57 -- "Custom metrics from the backend (we don't have any
    // yet)") render as placeholder text widgets rather than a graph bound to
    // a namespace nothing publishes to: cost-to-date (Cost Explorer
    // integration), stale PENDING/RUNNING reviews, manual-review-state
    // counts (#37), abandoned spend reservations, release-bundle
    // activation/rollback audit events, and audit-archive stream lag.
    // -----------------------------------------------------------------------
    this.dashboard = new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: `contract-toaster-${envName}`,
    });

    this.dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown:
          `# ContractToaster Review — ${envName}\n\n` +
          '**Deployed version:** see the App Runner console (Service ' +
          `contract-toaster-api-${envName}` +
          ') -> "Source and deployment" for the active image digest/commit ' +
          '(App Runner does not expose deployment metadata as a CloudWatch ' +
          'metric). CI records VERSION/COMMIT_SHA/IMAGE_DIGEST at build time.',
        width: 24,
        height: 3,
      }),
    );

    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'App Runner request rate (per minute)',
        left: [requestCount],
        width: 8,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'App Runner error rate (4xx vs 5xx)',
        left: [http4xx, http5xx],
        width: 8,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'App Runner p99 latency',
        left: [p99Latency],
        width: 8,
        height: 6,
      }),
    );

    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Bedrock invocations per day (by model)',
        left: bedrockInvocationMetrics,
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'Bedrock errors vs throttles (genuine errors page; throttles do not — #17)',
        left: [bedrockGenuineErrorSum],
        right: [bedrockThrottleSum],
        width: 12,
        height: 6,
      }),
    );

    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Step Functions stage failures',
        left: [stageFailures],
        width: 12,
        height: 6,
      }),
      new cloudwatch.TextWidget({
        markdown:
          '**Pending custom backend metrics (out of scope for #57):**\n' +
          '- Stale `PENDING`/`RUNNING` reviews — see RUNBOOK.md -> Incident ' +
          'response -> "Reviews are stuck in PENDING / RUNNING".\n' +
          '- `MANUAL_REVIEW_REQUIRED` / `ERROR_MANUAL_REVIEW_REQUIRED` counts ' +
          '— owner + daily SLA in RUNBOOK.md -> Observability -> ' +
          '"Manual-review filter: owner and SLA" (#37).\n' +
          '- Abandoned spend reservations; release-bundle activation/rollback ' +
          'audit events.\n' +
          '- Audit-archive DynamoDB Stream lag.\n\n' +
          'These will bind to real metric expressions once the backend emits ' +
          '`PutMetricData` for them (see ARCHITECTURE.md -> Observability).',
        width: 12,
        height: 6,
      }),
    );

    this.dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown:
          '**Cost-to-date for the Bedrock model.** Sourced from AWS Cost ' +
          'Explorer (Bedrock service filter), not a native CloudWatch ' +
          'metric — view in the Billing console or `aws ce get-cost-and-usage`. ' +
          'The in-app daily ceiling and settled-spend ledger (per-review, ' +
          "cheaper to query) are on the admin dashboard; see ARCHITECTURE.md " +
          '-> Cost shape.',
        width: 24,
        height: 3,
      }),
    );

    new cdk.CfnOutput(this, 'DashboardName', {
      value: this.dashboard.dashboardName,
      description: `CloudWatch dashboard name for ${envName}`,
      exportName: `ContractToaster-${envName}-observability-DashboardName`,
    });
  }
}
