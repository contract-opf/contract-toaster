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
 *        Its SizeRestrictions_BODY rule (bodies over 8 KB) is set to COUNT and
 *        re-applied by rule 1b everywhere except the upload routes: left as
 *        is it blocked every .docx upload (QA 2026-10-31 item 14).
 *      - AWSManagedRulesKnownBadInputsRuleSet — known bad inputs / exploits.
 *
 *   2. Request-size cap:
 *      - A regex on the Content-Length header blocks any request declaring a
 *        body over UPLOAD_MAX_BODY_BYTES (50 MiB) before it reaches the App
 *        Runner container. Not a body SizeConstraintStatement: a regional
 *        WAF inspects at most the first 8 KB of a body, so a body-size rule
 *        could never see 50 MiB. A body sent without Content-Length (chunked)
 *        is not capped here; the application's 25 MiB upload cap
 *        (upload_validation.MAX_UPLOAD_SIZE_BYTES) is the authoritative check.
 *
 *   3. Rate limits (per-IP, per-user fallback):
 *      - RateBasedStatement on POST /api/reviews (upload + start) — 10 req/5 min.
 *        Scope-down is (URI path prefix) AND (method == POST) so GET status
 *        polling never matches this rule (issue #227).
 *        Prevents a single caller from firing reviews in a tight loop.
 *      - RateBasedStatement on GET /api/reviews (polling) — 300 req/5 min.
 *        Sized so the UI's adaptive poll (3 s for a review's first two
 *        minutes, then 10 s: at most 58 GETs in any 5-minute window) never
 *        trips it for a healthy review over the whole 15-minute execution
 *        timeout (issue #50). Because the key is the source IP, this is a
 *        budget of roughly five concurrent reviews per egress address; there
 *        is NO application-layer limit on this GET, so the rule is the only
 *        thing that stops a scripted flood of status reads.
 *
 * Per-user limits live in the application layer rather than the WAF, since
 * WAF rate rules are keyed on IP, not on authenticated Cognito identity: a
 * per-user daily download count (`backend/src/download.py`, a conditional
 * write on the users table) and a global daily spend cap
 * (`backend/src/reviews.py`, `reserve_spend`). Pipeline concurrency is a
 * process-wide `PIPELINE_MAX_CONCURRENCY`, not per user. The WAF rules here
 * are the first line of defence for unauthenticated and obviously-abusive
 * traffic before the JWT is validated.
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

    // Upload body size cap: 50 MiB — an outer net above the application's
    // 25 MiB upload cap, which stays authoritative. WAF drops a request that
    // declares a larger body before the container has to read it.
    const UPLOAD_MAX_BODY_BYTES = 50 * 1024 * 1024; // 50 MiB = 52428800
    // A decimal Content-Length strictly greater than 52428800, leading zeros
    // allowed. WAF has no numeric comparison on a header's value (a
    // SizeConstraintStatement on a header measures the string's length), so
    // the comparison is spelled digit by digit. Pinned exhaustively by
    // tests/test_infra_waf_upload_body.py; change both together.
    const CONTENT_LENGTH_OVER_CAP_REGEX =
      '^0*(?:[1-9][0-9]{8,}|[6-9][0-9]{7}|5[3-9][0-9]{6}|52[5-9][0-9]{5}|524[3-9][0-9]{4}' +
      '|52429[0-9]{3}|524289[0-9]{2}|524288[1-9][0-9]|5242880[1-9])$';
    if (UPLOAD_MAX_BODY_BYTES !== 52428800) {
      throw new Error('CONTENT_LENGTH_OVER_CAP_REGEX encodes 52428800; update it with UPLOAD_MAX_BODY_BYTES');
    }

    // The POST routes that carry a file (backend/src/review_routes.py,
    // backend/src/main.py): the only requests allowed a body over the
    // managed rule's 8 KB.
    const lower = [{ priority: 0, type: 'LOWERCASE' }];
    const uploadRoute: wafv2.CfnWebACL.StatementProperty = {
      andStatement: {
        statements: [
          {
            byteMatchStatement: {
              fieldToMatch: { method: {} },
              positionalConstraint: 'EXACTLY',
              searchString: 'POST',
              textTransformations: [{ priority: 0, type: 'NONE' }],
            },
          },
          {
            orStatement: {
              statements: [
                // POST /api/reviews — start a review (the .docx).
                {
                  byteMatchStatement: {
                    fieldToMatch: { uriPath: {} },
                    positionalConstraint: 'EXACTLY',
                    searchString: '/api/reviews',
                    textTransformations: lower,
                  },
                },
                // POST /api/reviews/preflight — classify before submitting.
                {
                  byteMatchStatement: {
                    fieldToMatch: { uriPath: {} },
                    positionalConstraint: 'EXACTLY',
                    searchString: '/api/reviews/preflight',
                    textTransformations: lower,
                  },
                },
                // POST /api/admin/playbooks — create a playbook from a file.
                {
                  byteMatchStatement: {
                    fieldToMatch: { uriPath: {} },
                    positionalConstraint: 'EXACTLY',
                    searchString: '/api/admin/playbooks',
                    textTransformations: lower,
                  },
                },
                // POST /api/admin/playbooks/{id}/versions — upload a version.
                {
                  andStatement: {
                    statements: [
                      {
                        byteMatchStatement: {
                          fieldToMatch: { uriPath: {} },
                          positionalConstraint: 'STARTS_WITH',
                          searchString: '/api/admin/playbooks/',
                          textTransformations: lower,
                        },
                      },
                      {
                        byteMatchStatement: {
                          fieldToMatch: { uriPath: {} },
                          positionalConstraint: 'ENDS_WITH',
                          searchString: '/versions',
                          textTransformations: lower,
                        },
                      },
                    ],
                  },
                },
              ],
            },
          },
        ],
      },
    };

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
              // COUNT, not BLOCK: the rule still labels the request, and
              // rule 1b blocks on that label outside the upload routes.
              ruleActionOverrides: [{ name: 'SizeRestrictions_BODY', actionToUse: { count: {} } }],
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: `contract-toaster-waf-common-${envName}`,
            sampledRequestsEnabled: true,
          },
        },
        // ----------------------------------------------------------------
        // Rule 1b: the CommonRuleSet's 8 KB body limit, everywhere except a
        // POST to an upload route. Must run after rule 1 (which adds the
        // label) — hence priority 15.
        // ----------------------------------------------------------------
        {
          name: 'BlockLargeBodyOutsideUploadRoutes',
          priority: 15,
          action: { block: {} },
          statement: {
            andStatement: {
              statements: [
                {
                  labelMatchStatement: {
                    scope: 'LABEL',
                    key: 'awswaf:managed:aws:core-rule-set:SizeRestrictions_Body',
                  },
                },
                { notStatement: { statement: uploadRoute } },
              ],
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: `contract-toaster-waf-body-8kb-${envName}`,
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
        // Rule 3: Request-size cap — block a declared body over 50 MiB.
        //
        // Reads Content-Length (the whole body's size) rather than the body,
        // which a regional WAF inspects only up to 8 KB — see the class doc.
        // ----------------------------------------------------------------
        {
          name: 'BlockOversizedRequestBody',
          priority: 30,
          action: { block: {} },
          statement: {
            regexMatchStatement: {
              fieldToMatch: { singleHeader: { Name: 'content-length' } },
              regexString: CONTENT_LENGTH_OVER_CAP_REGEX,
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
        // GET /api/reviews/{id} — 300 requests per 5-minute window per IP.
        // Sized against the UI's adaptive poll (3 s for a review's first two
        // minutes, then 10 s: at most 58 polls in any 5-minute window,
        // asserted by frontend/src/__tests__/poll-budget-waf.test.tsx), so
        // one healthy 15-minute review plus its output download stays far
        // inside it, and about five concurrent reviews behind one egress IP
        // fit (issue #50). Cancel and the other POSTs under the prefix are
        // governed by rule 4's 10/5 min, not by this rule.
        // ----------------------------------------------------------------
        {
          name: 'RateLimitPollingEndpoint',
          priority: 50,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 300,
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
