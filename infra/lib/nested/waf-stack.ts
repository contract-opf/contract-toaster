import * as cdk from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';

export interface WafStackProps extends cdk.NestedStackProps {
  readonly envName: string;
  /**
   * App Runner service ARN to associate the WebACL with.
   * App Runner uses REGIONAL scope for WAF associations.
   * Optional: may be omitted during local synth before the App Runner
   * service ARN is available; the WebACL is still created.
   */
  readonly appRunnerServiceArn?: string;
}

/**
 * WafStack — AWS WAF v2 WebACL fronting the App Runner API service.
 *
 * Issue #71 AC4: WAF + abuse limits.
 *
 * Controls deployed by this stack:
 *
 *   1. AWS Managed Rule Groups:
 *      - AWSManagedRulesCommonRuleSet     — OWASP top-10 / common attacks.
 *      - AWSManagedRulesKnownBadInputsRuleSet — known bad inputs / exploits.
 *
 *   2. Request-size cap:
 *      - SizeConstraintStatement on the request body:
 *        hard limit UPLOAD_MAX_BODY_BYTES (50 MiB) so oversized uploads are
 *        dropped at the WAF before reaching the App Runner container.
 *        (Complements the decompressed-size cap in the application layer.)
 *
 *   3. Rate limits (per-IP, per-user fallback):
 *      - RateBasedStatement on POST /api/reviews (upload + start) — 10 req/5 min.
 *        Scope-down is (URI path prefix) AND (method == POST) so GET status
 *        polling never matches this rule (issue #227).
 *        Prevents a single caller from firing reviews in a tight loop.
 *      - RateBasedStatement on GET /api/reviews (polling) — 60 req/5 min.
 *        Prevents a tight polling loop from amplifying load.
 *
 * Per-user review-concurrency and daily-limit enforcement is performed in
 * the application layer (DynamoDB conditional write on the users table) rather
 * than the WAF, since WAF rate rules are keyed on IP, not on authenticated
 * Cognito identity.  The WAF rules here are the first line of defence for
 * unauthenticated and obviously-abusive traffic before the JWT is validated.
 *
 * KMS encryption-context enforcement (issue #71 AC1):
 *   The outputs CMK key policy (KmsKeysStack.outputsKey) requires the caller
 *   to supply the encryption context {"contract-toaster:data-class":"outputs",
 *   "contract-toaster:review-id":"<id>"} on every GenerateDataKey / Decrypt call.  This
 *   is enforced via a kms:EncryptionContextKeys condition on the key policy
 *   (added by KmsKeysStack or via addToResourcePolicy in the consuming stack).
 *   The WAF does not participate in KMS context enforcement; it is listed here
 *   because it shares the same issue and the same AC.
 *
 * Scope: REGIONAL (required for App Runner; CLOUDFRONT is for CloudFront-only).
 *
 * Association: the WebACL is associated with the App Runner service ARN via
 * CfnWebACLAssociation.  The association is conditional on the service ARN being
 * available; the WebACL is created regardless.
 *
 * Security invariants:
 *   - Default action: ALLOW (managed rules count/block; explicit rate rules block).
 *   - All rules use BLOCK action (not COUNT-only) so abusive traffic is dropped.
 *   - CloudWatch metrics are enabled on every rule so throttled/blocked requests
 *     are visible in the WAF dashboard (#57 observability stack).
 *   - This stack is independent of AppStack so it can be updated without
 *     redeploying the App Runner service.
 */
export class WafStack extends cdk.NestedStack {
  /** WAF v2 WebACL resource. */
  readonly webAcl: wafv2.CfnWebACL;

  constructor(scope: Construct, id: string, props: WafStackProps) {
    super(scope, id, props);

    const { envName, appRunnerServiceArn } = props;

    // Upload body size cap: 50 MiB.
    // This matches the application-layer limit; WAF drops the request before
    // the container has to read the body.
    const UPLOAD_MAX_BODY_BYTES = 50 * 1024 * 1024; // 50 MiB

    // -----------------------------------------------------------------------
    // WebACL — REGIONAL scope (App Runner lives in a region, not at the edge).
    // -----------------------------------------------------------------------
    this.webAcl = new wafv2.CfnWebACL(this, 'ApiWebAcl', {
      name: `contract-toaster-api-waf-${envName}`,
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      description:
        `ContractToaster Review API WAF WebACL (${envName}). ` +
        'Managed rules + request-size cap + rate limits on upload and polling ' +
        'endpoints (issue #71 AC4).',
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `contract-toaster-api-waf-${envName}`,
        sampledRequestsEnabled: true,
      },
      rules: [
        // ----------------------------------------------------------------
        // Rule 1: AWS managed common rule set (OWASP top-10 / common attacks).
        // ----------------------------------------------------------------
        {
          name: 'AWSManagedRulesCommonRuleSet',
          priority: 10,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesCommonRuleSet',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: `contract-toaster-waf-common-${envName}`,
            sampledRequestsEnabled: true,
          },
        },
        // ----------------------------------------------------------------
        // Rule 2: AWS managed known-bad-inputs rule set.
        // ----------------------------------------------------------------
        {
          name: 'AWSManagedRulesKnownBadInputsRuleSet',
          priority: 20,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesKnownBadInputsRuleSet',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: `contract-toaster-waf-bad-inputs-${envName}`,
            sampledRequestsEnabled: true,
          },
        },
        // ----------------------------------------------------------------
        // Rule 3: Request-size cap — block uploads larger than 50 MiB.
        //
        // SizeConstraintStatement on the request body (BODY component).
        // Prevents oversized payloads from reaching the container, complementing
        // the decompressed-size cap in the application layer.
        // ----------------------------------------------------------------
        {
          name: 'BlockOversizedRequestBody',
          priority: 30,
          action: { block: {} },
          statement: {
            sizeConstraintStatement: {
              fieldToMatch: { body: {} },
              comparisonOperator: 'GT',
              size: UPLOAD_MAX_BODY_BYTES,
              textTransformations: [{ priority: 0, type: 'NONE' }],
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: `contract-toaster-waf-size-cap-${envName}`,
            sampledRequestsEnabled: true,
          },
        },
        // ----------------------------------------------------------------
        // Rule 4: Rate limit on upload/start-review endpoint.
        //
        // POST /api/reviews — 10 requests per 5-minute window per IP.
        // Prevents a script from firing reviews in a tight loop.
        // Per-user concurrency and daily limits are enforced in the
        // application layer (DynamoDB conditional write) and complement
        // this IP-based rate rule.
        //
        // Scope-down is an AndStatement of (URI path prefix) AND (HTTP
        // method == POST). Without the method constraint, GET
        // /api/reviews/{id} status polling also matches this rule — a UI
        // polling every few seconds burns through the 10-request budget in
        // well under a minute and gets its IP blocked before rule 5 (the
        // dedicated, higher-limit polling rule, priority 50) ever applies.
        // See issue #227.
        // ----------------------------------------------------------------
        {
          name: 'RateLimitUploadEndpoint',
          priority: 40,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 10,
              aggregateKeyType: 'IP',
              scopeDownStatement: {
                andStatement: {
                  statements: [
                    {
                      byteMatchStatement: {
                        fieldToMatch: { uriPath: {} },
                        positionalConstraint: 'STARTS_WITH',
                        searchString: '/api/reviews',
                        textTransformations: [{ priority: 0, type: 'LOWERCASE' }],
                      },
                    },
                    {
                      byteMatchStatement: {
                        fieldToMatch: { method: {} },
                        positionalConstraint: 'EXACTLY',
                        searchString: 'POST',
                        textTransformations: [{ priority: 0, type: 'NONE' }],
                      },
                    },
                  ],
                },
              },
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: `contract-toaster-waf-rate-upload-${envName}`,
            sampledRequestsEnabled: true,
          },
        },
        // ----------------------------------------------------------------
        // Rule 5: Rate limit on polling endpoint.
        //
        // GET /api/reviews/{id} — 60 requests per 5-minute window per IP.
        // Prevents a tight polling loop from amplifying load on the API
        // and the DynamoDB status table.
        // ----------------------------------------------------------------
        {
          name: 'RateLimitPollingEndpoint',
          priority: 50,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 60,
              aggregateKeyType: 'IP',
              scopeDownStatement: {
                byteMatchStatement: {
                  fieldToMatch: { uriPath: {} },
                  positionalConstraint: 'STARTS_WITH',
                  searchString: '/api/reviews/',
                  textTransformations: [{ priority: 0, type: 'LOWERCASE' }],
                },
              },
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: `contract-toaster-waf-rate-poll-${envName}`,
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    cdk.Tags.of(this.webAcl).add('contract-toaster:env', envName);
    cdk.Tags.of(this.webAcl).add('contract-toaster:component', 'waf');

    // -----------------------------------------------------------------------
    // WebACL association — link the WebACL to the App Runner service.
    //
    // App Runner supports WAF WebACL associations via CfnWebACLAssociation.
    // The resource ARN must be the App Runner service ARN (not the service URL).
    //
    // The association is conditional on the service ARN being present so
    // `cdk synth` succeeds before the App Runner service is deployed.
    // -----------------------------------------------------------------------
    if (appRunnerServiceArn) {
      const assoc = new wafv2.CfnWebACLAssociation(this, 'ApiWebAclAssociation', {
        webAclArn: this.webAcl.attrArn,
        resourceArn: appRunnerServiceArn,
      });
      cdk.Tags.of(assoc).add('contract-toaster:env', envName);
    }

    // -----------------------------------------------------------------------
    // Stack outputs
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'WebAclArn', {
      value: this.webAcl.attrArn,
      description: `WAF WebACL ARN for the ContractToaster Review API (${envName})`,
      exportName: `ContractToaster-${envName}-WafWebAclArn`,
    });
  }
}
