#!/usr/bin/env node
/**
 * CDK App entry point for contract-toaster infrastructure.
 *
 * Usage:
 *   npx cdk synth --context env=dev
 *   npx cdk deploy --context env=dev
 *   npx cdk deploy --context env=prod   # requires prod AWS account credentials
 *
 * The `env` context key selects the environment.  Account and region are
 * resolved from cdk.json context — NOT hard-coded here.  Dev and prod are
 * always separate AWS accounts.
 *
 * Deploy profile (issue #231):
 *   npx cdk synth --context env=dev --context profile=minimal
 *   npx cdk synth --context env=dev --context profile=hardened   # default
 *
 * `profile=hardened` (the default; identical to pre-#231 behavior) keeps
 * every current control: per-data-class CMKs, the 2-AZ VPC with NAT +
 * Bedrock interface endpoints, the WAF WebACL, and the CloudTrail trail with
 * Object-Lock archival. This is the only supported profile for the
 * production legal-data story.
 *
 * `profile=minimal` trims the stack to a demo/onboarding footprint so an
 * open-source adopter or a pilot user can stand up ContractToaster without
 * Google Workspace DWD, a cosign signing pipeline, or NAT/interface-endpoint
 * idle cost: no WAF WebACL, no NAT gateway or Bedrock interface endpoints
 * (App Runner egresses publicly), no CloudTrail trail, and S3/DynamoDB use
 * AWS-managed (not customer-managed) encryption. See infra/README.md
 * "Deploy profiles" for the full list of what each profile does and does
 * not include. `profile` is read via CDK context in each nested stack
 * (`node.tryGetContext('profile')`), so it propagates to every construct in
 * the tree without being threaded through every stack's props.
 */
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { ContractToasterStack } from '../lib/contract-toaster-stack';

const app = new cdk.App();

// Resolve environment name from context.  Defaults to 'dev' if not specified.
// Context key: --context env=dev | --context env=prod
const envName: string = (app.node.tryGetContext('env') as string | undefined) ?? 'dev';

const envContext = app.node.tryGetContext(envName) as {
  account: string;
  region: string;
  stackName?: string;
} | undefined;

if (!envContext) {
  throw new Error(
    `CDK context key '${envName}' not found in cdk.json. ` +
    `Expected keys: 'dev' and 'prod'. ` +
    `Run: npx cdk synth --context env=dev`,
  );
}

if (!envContext.account || !envContext.region) {
  throw new Error(
    `cdk.json context.${envName} must set both 'account' and 'region'. ` +
    `Edit infra/cdk.json and replace the placeholder account IDs.`,
  );
}

// Deploy profile (issue #231): 'minimal' | 'hardened'. Defaults to 'hardened'
// so an unmodified `cdk synth` / `cdk deploy` is byte-for-byte the same as
// before this issue. Validated here (fail fast) even though every nested
// stack re-resolves it independently via node.tryGetContext('profile').
const profile: string = (app.node.tryGetContext('profile') as string | undefined) ?? 'hardened';
if (profile !== 'minimal' && profile !== 'hardened') {
  throw new Error(
    `CDK context 'profile' must be 'minimal' or 'hardened' (got '${profile}'). ` +
    `Run: npx cdk synth --context env=dev --context profile=minimal`,
  );
}

// Stack name convention: 'contract-toaster-{envName}' (e.g. contract-toaster-dev)
const stackName = envContext.stackName ?? `contract-toaster-${envName}`;

new ContractToasterStack(app, stackName, {
  envName,
  stackName,
  description: `ContractToaster contract review tool — ${envName} infrastructure`,
  // Account and region from context, not hard-coded.
  // Each environment resolves to its own AWS account — never the same as the other.
  env: {
    account: envContext.account,
    region: envContext.region,
  },
});
