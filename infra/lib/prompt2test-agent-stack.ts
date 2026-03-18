import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";

export class Prompt2TestAgentStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Image URI passed in from GitHub Actions via --context imageUri=...
    const imageUri = this.node.tryGetContext("imageUri") as string | undefined;

    // ── ECR Repository ───────────────────────────────────────────────────
    // Stores Docker images built by GitHub Actions
    const ecrRepo = new ecr.Repository(this, "AgentRepository", {
      repositoryName: "prompt2test-agent",
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          // Keep only last 5 images to save costs
          maxImageCount: 5,
          description: "Keep last 5 images",
        },
      ],
    });

    // ── IAM Role for AgentCore ───────────────────────────────────────────
    // AgentCore assumes this role when running the agent container
    const agentRole = new iam.Role(this, "AgentCoreRole", {
      roleName: "prompt2test-agentcore-role",
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal("bedrock.amazonaws.com"),
        new iam.ServicePrincipal("ecs-tasks.amazonaws.com")
      ),
      description: "Role for Prompt2Test Bedrock AgentCore runtime",
      inlinePolicies: {
        BedrockInvoke: new iam.PolicyDocument({
          statements: [
            // Allow Claude claude-sonnet-4-5 (DEV only — deny added for QA/UAT/PROD separately)
            new iam.PolicyStatement({
              sid: "AllowBedrockInvoke",
              actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
              resources: [
                `arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5*`,
                `arn:aws:bedrock:*:${this.account}:inference-profile/us.anthropic.claude-sonnet-4-5*`,
              ],
            }),
            // Pull container image from ECR
            new iam.PolicyStatement({
              sid: "AllowECRPull",
              actions: [
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchGetImage",
                "ecr:GetAuthorizationToken",
              ],
              resources: ["*"],
            }),
            // Write logs
            new iam.PolicyStatement({
              sid: "AllowLogs",
              actions: [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
              ],
              resources: ["*"],
            }),
            // SSM — read team config (4-level config store)
            new iam.PolicyStatement({
              sid: "AllowSSMRead",
              actions: ["ssm:GetParameter", "ssm:GetParametersByPath"],
              resources: [
                `arn:aws:ssm:${this.region}:${this.account}:parameter/prompt2test/*`,
              ],
            }),
            // Secrets Manager — account credentials
            new iam.PolicyStatement({
              sid: "AllowSecretsRead",
              actions: ["secretsmanager:GetSecretValue"],
              resources: [
                `arn:aws:secretsmanager:${this.region}:${this.account}:secret:prompt2test/*`,
              ],
            }),
          ],
        }),
      },
    });

    // ── CloudWatch Log Group ─────────────────────────────────────────────
    const logGroup = new logs.LogGroup(this, "AgentLogGroup", {
      logGroupName: "/prompt2test/agentcore",
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Outputs ──────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "ECRRepositoryUri", {
      value: ecrRepo.repositoryUri,
      description: "ECR repository URI — used by GitHub Actions to push images",
      exportName: "Prompt2TestAgentECRUri",
    });

    new cdk.CfnOutput(this, "AgentRoleArn", {
      value: agentRole.roleArn,
      description: "IAM role ARN for Bedrock AgentCore",
      exportName: "Prompt2TestAgentRoleArn",
    });

    new cdk.CfnOutput(this, "LogGroupName", {
      value: logGroup.logGroupName,
      description: "CloudWatch log group for agent runtime logs",
    });

    if (imageUri) {
      new cdk.CfnOutput(this, "DeployedImageUri", {
        value: imageUri,
        description: "Currently deployed container image",
      });
    }
  }
}
