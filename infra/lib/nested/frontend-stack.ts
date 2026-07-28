import * as cdk from 'aws-cdk-lib';
import * as amplify from 'aws-cdk-lib/aws-amplify';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface FrontendStackProps extends cdk.NestedStackProps {
  readonly envName: string;
  /**
   * Cognito hosted-UI domain (bare host, no scheme), e.g.
   * `contract-toaster-dev.auth.us-east-1.amazoncognito.com`.
   *
   * Issue #226: the OAuth code→token exchange after the hosted-UI redirect
   * is an XHR from the SPA origin to this domain
   * (`https://<domain>/oauth2/token`). It must be present in the CSP
   * connect-src directive or the browser blocks the exchange and sign-in
   * fails silently after the redirect back from Cognito.
   */
  readonly cognitoHostedUiDomain: string;
  /**
   * App Runner API origin (bare host, no scheme) that the SPA calls for
   * `/api/*` and `/version` (see frontend/src/App.tsx — `VITE_API_BASE_URL`).
   * Typically `AppStack.appRunnerService.attrServiceUrl`, a deploy-time
   * CloudFormation attribute (Fn::GetAtt), not a synth-time literal.
   *
   * Issue #226: the API is NOT proxied through the Amplify origin — it is a
   * genuine cross-origin call to the App Runner domain. It must be present
   * in the CSP connect-src directive or the browser blocks every API call.
   */
  readonly apiOrigin: string;
}

/**
 * FrontendStack — Amplify Hosting app for the ContractToaster Review Tool React SPA.
 *
 * Issue #54: Amplify Hosting + empty React app
 *
 * Resources defined here:
 *  1. IAM service role for Amplify Hosting (read ECR / deploy artifacts).
 *  2. Amplify Hosting app (CfnApp):
 *       - DEV: auto-build on push to `main` is ENABLED (branch auto-build
 *         and auto-publish so every push to main deploys to the dev URL).
 *       - PROD: auto-build on push to `main` is DISABLED.  Production is
 *         advanced by a deliberate promotion of a specific signed, built
 *         frontend artifact — never by a merge to main.  This is consistent
 *         with the digest-pinned App Runner backend (see ARCHITECTURE.md →
 *         Frontend / Infrastructure).
 *  3. Amplify branch (`main`) wired to the Amplify app:
 *       - DEV:  autoBuild = true  (continuous deployment from main)
 *       - PROD: autoBuild = false (deliberate artifact promotion only)
 *
 * Security invariants:
 *  - Content-Security-Policy header set on all Amplify responses (#72).
 *    Policy: default-src 'self'; script-src 'self'; no unsafe-inline/unsafe-eval
 *    for scripts; connect-src 'self' + the Cognito hosted-UI domain + the App
 *    Runner API origin (#226 — a bare 'self' blocks token exchange and every
 *    API call once deployed); frame-ancestors 'none'; object-src 'none'.
 *  - SPA rewrite rule (#226): all non-asset paths (including the Cognito
 *    OAuth callback deep-link) rewrite to /index.html instead of 404ing.
 *  - Model-generated text rendered as escaped text only — never as innerHTML
 *    (frontend code convention; enforced via code review).
 *  - The attorney-approval watermark is present on every output state
 *    (ARCHITECTURE.md § Frontend).
 *  - PROD does NOT auto-publish on merge: a merge to main must not silently
 *    change prod presentation.  The frontend is legal-facing (renders the
 *    attorney-approval watermark and the ACCEPT framing).
 *
 * Build configuration:
 *  - `npm run build` in frontend/ runs `vite build` and outputs to `dist/`.
 *  - The built artifact (dist/) is the deployment unit for Amplify Hosting.
 *  - CI (CodeBuild, issue #66) runs the build, signs the artifact, and
 *    promotes it to the Amplify app.  Dev uses branch auto-build; prod uses
 *    a deliberate promotion of a specific named build.
 */
export class FrontendStack extends cdk.NestedStack {
  /** Amplify Hosting app. */
  readonly amplifyApp: amplify.CfnApp;
  /** Amplify branch (main) wired to the app. */
  readonly mainBranch: amplify.CfnBranch;
  /** IAM service role assumed by the Amplify Hosting service. */
  readonly amplifyServiceRole: iam.Role;

  constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, props);

    const { envName, cognitoHostedUiDomain, apiOrigin } = props;

    // -----------------------------------------------------------------------
    // DEV vs. PROD auto-publish behavior.
    //
    // ARCHITECTURE.md → Frontend / Infrastructure:
    //   "Branch auto-build/auto-publish on push to `main` is allowed in the
    //    DEV account only.  The prod Amplify app does NOT auto-publish on
    //    merge — prod is advanced by a deliberate promotion of a specific
    //    built frontend artifact."
    //
    // This is the same non-auto-mutate policy as the digest-pinned App Runner
    // backend.  A merge to main must never silently change prod presentation.
    // -----------------------------------------------------------------------
    const isDevEnv = envName === 'dev';

    // -----------------------------------------------------------------------
    // Amplify Hosting service role
    //
    // Amplify Hosting assumes this role to build and deploy the SPA.
    // The role is least-privilege: only the permissions Amplify needs to
    // pull the build artifacts and serve them.  Additional permissions
    // (e.g. reading Secrets Manager for the Cognito client ID) are added
    // in later issues as needed.
    // -----------------------------------------------------------------------
    this.amplifyServiceRole = new iam.Role(this, 'AmplifyServiceRole', {
      roleName: `contract-toaster-amplify-${envName}`,
      description:
        `ContractToaster review — Amplify Hosting service role (${envName}). ` +
        'Assumed by the Amplify Hosting service to build and deploy the React SPA.',
      assumedBy: new iam.ServicePrincipal('amplify.amazonaws.com'),
    });

    cdk.Tags.of(this.amplifyServiceRole).add('contract-toaster:env', envName);
    cdk.Tags.of(this.amplifyServiceRole).add('contract-toaster:component', 'frontend');

    // -----------------------------------------------------------------------
    // Amplify Hosting app (CfnApp)
    //
    // CfnApp is used (rather than the L2 App construct) because the L2
    // construct does not yet expose all the configuration knobs we need
    // (e.g. per-environment auto-publish behavior, custom headers for CSP).
    //
    // Build spec:
    //   - frontend: npm run build (runs `vite build`, outputs to dist/)
    //   - The built artifact is dist/ (baseDirectory for Amplify)
    //
    // Custom headers (CSP — issue #72):
    //   - The placeholder customHeaders field is defined here; the full CSP
    //     header will be wired in issue #72.
    //   - Header: Content-Security-Policy (see docs/threat-model.md).
    //
    // Auto-build/auto-publish:
    //   - DEV:  enabled (branch auto-build + auto-publish)
    //   - PROD: disabled (deliberate artifact promotion only)
    // -----------------------------------------------------------------------
    this.amplifyApp = new amplify.CfnApp(this, 'AmplifyApp', {
      name: `contract-toaster-${envName}`,
      description:
        `ContractToaster Review Tool — React SPA (${envName}). ` +
        'Sign-in and version display. See ARCHITECTURE.md → Frontend.',
      iamServiceRole: this.amplifyServiceRole.roleArn,

      // Build specification: runs `npm run build` in frontend/,
      // which executes `vite build` and outputs to dist/.
      buildSpec: [
        'version: 1',
        'frontend:',
        '  phases:',
        '    preBuild:',
        '      commands:',
        '        - cd frontend',
        '        - npm ci',
        '    build:',
        '      commands:',
        '        - npm run build',
        '  artifacts:',
        '    baseDirectory: frontend/dist',
        '    files:',
        '      - "**/*"',
        '  cache:',
        '    paths:',
        '      - frontend/node_modules/**/*',
      ].join('\n'),

      // Custom headers — Content-Security-Policy + hardening headers (issue #72).
      //
      // Content-Security-Policy rationale (docs/threat-model.md §Frontend security posture):
      //   - 'default-src \'self\'' — baseline: only load resources from the same origin.
      //   - 'script-src \'self\'' — no unsafe-inline, no unsafe-eval; Amplify-built JS
      //     bundles are served from the same origin so this does not require 'unsafe-inline'.
      //   - 'style-src \'self\' \'unsafe-inline\'' — Amplify UI / React inline styles
      //     require unsafe-inline for styles; this is acceptable because CSS injection is
      //     low-severity compared with script injection.  A future hardening pass can
      //     eliminate inline styles and tighten to 'self' only.
      //   - 'connect-src \'self\' https://<cognito hosted-UI domain> https://<App Runner
      //     API origin>' — issue #226: the API is NOT proxied through the Amplify origin
      //     (it is a genuine cross-origin call to the App Runner domain — see App.tsx
      //     VITE_API_BASE_URL), and the OAuth code→token exchange after the hosted-UI
      //     redirect is an XHR to the Cognito domain's /oauth2/token endpoint. A bare
      //     'self' here blocks sign-in (token exchange) and every API call once deployed
      //     — see issue #226 for the incident this caused. Both origins are threaded in
      //     as stack props (cognitoHostedUiDomain, apiOrigin) so they stay deploy-time
      //     accurate per environment.
      //   - 'img-src \'self\' data:' — data: URIs are required by Amplify UI icons.
      //   - 'font-src \'self\' data:' — data: URIs are required by Amplify UI fonts.
      //   - 'frame-ancestors \'none\'' — no embedding in iframes (defense-in-depth;
      //     paired with X-Frame-Options: DENY).
      //   - 'object-src \'none\'' — no plugins (Flash, Java applets, etc.).
      //   - 'base-uri \'self\'' — prevents base-tag injection attacks.
      //   - 'form-action \'self\'' — form submissions must target the same origin only.
      //
      // Trusted Types: the policy does not yet set a require-trusted-types-for directive
      // because the current Amplify UI library version does not yet support Trusted Types
      // without polyfilling.  A future hardening pass will add
      // 'require-trusted-types-for \'script\'' once Amplify UI ships Trusted Types
      // support (see docs/threat-model.md §Frontend security posture — Trusted Types).
      //
      // Token storage (docs/threat-model.md §Frontend security posture):
      //   Cognito tokens are held in memory by the Amplify Auth library, not in
      //   localStorage.  The CSP is a defense-in-depth layer; the primary control
      //   is that model-generated text and all document-derived text are rendered
      //   as escaped text only — no dangerouslySetInnerHTML anywhere in the app.
      customHeaders: [
        '  customHeaders:',
        '    - pattern: "**/*"',
        '      headers:',
        '        - key: "Content-Security-Policy"',
        `          value: "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' https://${cognitoHostedUiDomain} https://${apiOrigin}; img-src 'self' data:; font-src 'self' data:; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"`,
        '        - key: "X-Content-Type-Options"',
        '          value: "nosniff"',
        '        - key: "X-Frame-Options"',
        '          value: "DENY"',
        '        - key: "Referrer-Policy"',
        '          value: "strict-origin-when-cross-origin"',
      ].join('\n'),

      // SPA rewrite rule (issue #226).
      //
      // The React app is a single-page app: React Router (client-side) owns
      // every path except the built static assets, and the Cognito hosted-UI
      // OAuth callback deep-links directly to a path like
      // `/?code=...&state=...` that has no matching object in the Amplify
      // Hosting bucket. Without a rewrite rule, Amplify Hosting returns a
      // bare 404 for any path that isn't a literal built file — including
      // the OAuth callback — instead of serving index.html and letting the
      // client-side app handle the route.
      //
      // Standard AWS Amplify Console SPA rewrite: any request path that is
      // NOT a dot-file with a known static-asset extension is rewritten
      // (200, not redirected) to /index.html.
      customRules: [
        {
          source:
            '</^[^.]+$|\\.(?!(css|gif|ico|jpg|jpeg|js|png|txt|svg|woff|woff2|ttf|map|json)$)([^.]+$)/>',
          target: '/index.html',
          status: '200',
        },
      ],

      // Environment variables for the Amplify build.
      // Cognito IDs are injected at deploy time via CDK stack outputs.
      // NEVER hard-code Cognito IDs, OAuth secrets, or API URLs here.
      // The full env-var wiring (Cognito outputs → Amplify build env)
      // is done in the generate-aws-exports.ts script (scripts/) and
      // the CI promotion step (issue #66).
      environmentVariables: [
        {
          name: 'AMPLIFY_DIFF_DEPLOY',
          value: isDevEnv ? 'false' : 'false', // deterministic build
        },
        {
          name: 'VITE_ENV',
          value: envName,
        },
      ],

      // DEV: configure auto-branch-creation with autoBuild enabled so that a
      // push to main triggers a build and deploys to the dev Amplify URL.
      // PROD: omit autoBranchCreationConfig entirely — no auto-mutation on merge.
      autoBranchCreationConfig: isDevEnv
        ? {
            enableAutoBuild: true,
            // Auto-publish on build success (DEV only).
            enablePullRequestPreview: false,
          }
        : undefined,
    });

    cdk.Tags.of(this.amplifyApp).add('contract-toaster:env', envName);
    cdk.Tags.of(this.amplifyApp).add('contract-toaster:component', 'frontend');

    // -----------------------------------------------------------------------
    // Amplify branch — main
    //
    // DEV:  autoBuild = true  → every push to `main` triggers a build and
    //                           deploys the new artifact to the dev Amplify URL.
    // PROD: autoBuild = false → a push to main does NOT deploy to production.
    //                           Production is advanced by a deliberate promotion
    //                           step (CI issue #66) that selects a specific
    //                           named build artifact.  This ensures a merge to
    //                           main never silently changes prod presentation.
    //
    // The branch is the deployable unit in Amplify Hosting.  The Amplify app
    // URL is: https://<branchName>.<amplifyAppId>.amplifyapp.com
    // -----------------------------------------------------------------------
    this.mainBranch = new amplify.CfnBranch(this, 'MainBranch', {
      appId: this.amplifyApp.attrAppId,
      branchName: 'main',
      description:
        isDevEnv
          ? 'main branch — auto-build enabled (dev account only).'
          : 'main branch — auto-build DISABLED (prod: deliberate promotion only).',
      // DEV: auto-build on push to main; PROD: deliberate promotion only.
      enableAutoBuild: isDevEnv,
      // Pull-request previews are off for both environments (not needed at v1).
      enablePullRequestPreview: false,
      // Stage: PRODUCTION for prod, DEVELOPMENT for dev.
      stage: isDevEnv ? 'DEVELOPMENT' : 'PRODUCTION',
    });

    cdk.Tags.of(this.mainBranch).add('contract-toaster:env', envName);

    // -----------------------------------------------------------------------
    // Stack outputs
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'AmplifyAppId', {
      value: this.amplifyApp.attrAppId,
      description: `Amplify Hosting app ID for ${envName}`,
      exportName: `ContractToaster-${envName}-AmplifyAppId`,
    });

    new cdk.CfnOutput(this, 'AmplifyDefaultDomain', {
      value: this.amplifyApp.attrDefaultDomain,
      description: `Amplify Hosting default domain for ${envName}`,
      exportName: `ContractToaster-${envName}-AmplifyDefaultDomain`,
    });

    new cdk.CfnOutput(this, 'AmplifyAppUrl', {
      value: `https://main.${this.amplifyApp.attrDefaultDomain}`,
      description:
        isDevEnv
          ? `Amplify Hosting URL for ${envName} (auto-published on push to main)`
          : `Amplify Hosting URL for ${envName} (requires deliberate artifact promotion)`,
      exportName: `ContractToaster-${envName}-AmplifyAppUrl`,
    });

    cdk.Tags.of(this).add('contract-toaster:env', envName);
    cdk.Tags.of(this).add('contract-toaster:component', 'frontend');
  }
}
