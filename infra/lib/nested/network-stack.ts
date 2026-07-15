import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';

export interface NetworkStackProps extends cdk.NestedStackProps {
  readonly envName: string;
}

/**
 * NetworkStack — VPC, subnets, and security groups for the ContractToaster Review Tool.
 *
 * Issue #55: App Runner + hello-world container (VPC connector — security Phase 0).
 *
 * Resources defined here:
 *  1. VPC (contract-toaster-vpc-{envName}):
 *       - Two AZs for redundancy.
 *       - Public subnets: App Runner VPC connector ingress only.
 *       - Private subnets with NAT Gateway: App Runner outbound + data plane.
 *       - No intra VPC traffic beyond what security groups allow.
 *  2. VPC endpoints for private AWS service access:
 *       - S3 Gateway endpoint (free, avoids NAT for S3 traffic).
 *       - DynamoDB Gateway endpoint (free, avoids NAT for DynamoDB traffic).
 *       - Bedrock Interface endpoints (issue #60): BEDROCK_RUNTIME (model
 *         invocation) and BEDROCK_AGENT_RUNTIME (Knowledge Base Retrieve /
 *         RetrieveAndGenerate) so the pipeline task role and the Bedrock KB
 *         service role never traverse the public internet to reach Bedrock.
 *         Farther endpoints (Secrets Manager, ECR) added in later issues when
 *         those services are wired.
 *
 * Security invariants:
 *  - The App Runner VPC connector uses only the private subnets so that
 *    the service container reaches S3, DynamoDB, Step Functions, and the
 *    Bedrock Knowledge Base privately — never over the public internet.
 *  - NAT Gateway is required for the App Runner container to reach the
 *    Cognito JWKS endpoint (public) for JWT key material.  Future hardening:
 *    replace the JWKS HTTP call with a VPC-internal Secrets Manager cache.
 *  - Flow logs written to CloudWatch Logs for audit visibility (#57).
 */
export class NetworkStack extends cdk.NestedStack {
  /** The VPC consumed by AppStack to create the App Runner VPC connector. */
  readonly vpc: ec2.Vpc;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    const { envName } = props;

    // -----------------------------------------------------------------------
    // Deploy profile (issue #231). 'hardened' (default) is byte-for-byte the
    // pre-#231 behavior. 'minimal' drops the NAT gateway(s) and the Bedrock
    // interface endpoints -- the two idle-cost line items called out in
    // issue #231 (1 NAT ~$32/mo + 2 interface endpoints ~$15/mo before any
    // traffic) -- so a demo/OSS deploy has no NAT-gateway idle floor. The VPC
    // itself, subnets, and the free S3/DynamoDB gateway endpoints are kept in
    // both profiles so AppStack's VPC connector wiring is unchanged.
    // -----------------------------------------------------------------------
    const profile = (this.node.tryGetContext('profile') as string | undefined) ?? 'hardened';
    const isMinimal = profile === 'minimal';

    // -----------------------------------------------------------------------
    // VPC — two AZs, public + private subnets.
    //
    // Public subnets host the NAT Gateways only (App Runner itself sits in
    // private subnets via the VPC connector).
    // Private subnets host the App Runner VPC connector and any future
    // Lambda / Fargate task ENIs.
    // -----------------------------------------------------------------------
    this.vpc = new ec2.Vpc(this, 'Vpc', {
      vpcName: `contract-toaster-vpc-${envName}`,
      // Explicit AZs so synth does not require an AWS credentials context lookup.
      // NOTE: do not combine availabilityZones with maxAzs (CDK constraint).
      availabilityZones: [`${cdk.Stack.of(this).region}a`, `${cdk.Stack.of(this).region}b`],
      // One NAT gateway per AZ for HA; reduce to 1 for cost savings in dev.
      // Zero under profile=minimal (issue #231) -- the private subnets carry
      // no NAT-routed egress; App Runner reaches the internet via its own
      // public ingress/egress path instead.
      natGateways: isMinimal ? 0 : envName === 'prod' ? 2 : 1,
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'public',
          subnetType: ec2.SubnetType.PUBLIC,
        },
        {
          cidrMask: 24,
          name: 'private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        },
      ],
    });

    // -----------------------------------------------------------------------
    // VPC Gateway endpoints — free; route AWS service traffic through the
    // VPC backbone rather than the NAT gateway. Kept in both profiles (no
    // idle cost).
    // -----------------------------------------------------------------------
    this.vpc.addGatewayEndpoint('S3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });
    this.vpc.addGatewayEndpoint('DynamoDbEndpoint', {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
    });

    // -----------------------------------------------------------------------
    // VPC Interface endpoints — Bedrock (issue #60). Keeps the Bedrock
    // Knowledge Base and model-invocation traffic on the AWS network backbone
    // rather than the public internet, so the KB / S3 Vectors store is
    // reachable only via IAM (pipelineReviewRole, corpusKnowledgeBaseRole)
    // and this VPC endpoint — never public. Skipped under profile=minimal
    // (issue #231) -- interface endpoints have an hourly idle cost
    // (~$7.50/mo each) that the minimal profile is scoped to avoid.
    // -----------------------------------------------------------------------
    if (!isMinimal) {
      this.vpc.addInterfaceEndpoint('BedrockRuntimeEndpoint', {
        service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
        privateDnsEnabled: true,
      });
      this.vpc.addInterfaceEndpoint('BedrockAgentRuntimeEndpoint', {
        service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENT_RUNTIME,
        privateDnsEnabled: true,
      });
    }

    // -----------------------------------------------------------------------
    // Tags
    // -----------------------------------------------------------------------
    cdk.Tags.of(this.vpc).add('contract-toaster:env', envName);
    cdk.Tags.of(this.vpc).add('contract-toaster:component', 'network');

    // -----------------------------------------------------------------------
    // Stack outputs — VPC ID exported so other stacks (and operators) can
    // reference it.
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'VpcId', {
      value: this.vpc.vpcId,
      description: `ContractToaster Review VPC ID (${envName})`,
      exportName: `ContractToaster-${envName}-VpcId`,
    });
  }
}
