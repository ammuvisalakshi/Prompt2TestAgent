import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as codepipeline from "aws-cdk-lib/aws-codepipeline";
import * as codepipeline_actions from "aws-cdk-lib/aws-codepipeline-actions";
import * as cognito from "aws-cdk-lib/aws-cognito";

export class Prompt2TestAgentStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── ECR Repository ───────────────────────────────────────────────────
    // Stores Docker images built by CodeBuild
    const ecrRepo = new ecr.Repository(this, "AgentRepository", {
      repositoryName: "prompt2test-agent",
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          maxImageCount: 5,
          description: "Keep last 5 images",
        },
      ],
    });

    // ── IAM Role for AgentCore ───────────────────────────────────────────
    const agentRole = new iam.Role(this, "AgentCoreRole", {
      roleName: "prompt2test-agentcore-role",
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal("bedrock.amazonaws.com"),
        new iam.ServicePrincipal("ecs-tasks.amazonaws.com")
      ),
      description: "Role for Prompt2Test Bedrock AgentCore runtime",
      inlinePolicies: {
        AgentCorePolicy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              sid: "AllowBedrockInvoke",
              actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
              resources: [
                `arn:aws:bedrock:*::foundation-model/anthropic.claude*`,
                `arn:aws:bedrock:*:${this.account}:inference-profile/us.anthropic.claude*`,
              ],
            }),
            new iam.PolicyStatement({
              sid: "AllowECRPull",
              actions: [
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchGetImage",
                "ecr:GetAuthorizationToken",
              ],
              resources: ["*"],
            }),
            new iam.PolicyStatement({
              sid: "AllowLogs",
              actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
              resources: ["*"],
            }),
            new iam.PolicyStatement({
              sid: "AllowSSMRead",
              actions: ["ssm:GetParameter", "ssm:GetParametersByPath"],
              resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter/prompt2test/*`],
            }),
            new iam.PolicyStatement({
              sid: "AllowSecretsRead",
              actions: ["secretsmanager:GetSecretValue"],
              resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:prompt2test/*`],
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

    // ── IAM Role for CodeBuild ───────────────────────────────────────────
    const codeBuildRole = new iam.Role(this, "CodeBuildRole", {
      assumedBy: new iam.ServicePrincipal("codebuild.amazonaws.com"),
      inlinePolicies: {
        CodeBuildPolicy: new iam.PolicyDocument({
          statements: [
            // Push images to ECR
            new iam.PolicyStatement({
              actions: [
                "ecr:GetAuthorizationToken",
                "ecr:BatchCheckLayerAvailability",
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchGetImage",
                "ecr:InitiateLayerUpload",
                "ecr:UploadLayerPart",
                "ecr:CompleteLayerUpload",
                "ecr:PutImage",
              ],
              resources: ["*"],
            }),
            // Write build logs
            new iam.PolicyStatement({
              actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
              resources: ["*"],
            }),
            // Read caller identity (for account ID in buildspec)
            new iam.PolicyStatement({
              actions: ["sts:GetCallerIdentity"],
              resources: ["*"],
            }),
            // Update AgentCore runtime after image push
            new iam.PolicyStatement({
              actions: ["bedrock-agentcore:UpdateAgentRuntime", "iam:PassRole"],
              resources: ["*"],
            }),
          ],
        }),
      },
    });

    // ── CodeBuild Project ────────────────────────────────────────────────
    // Reads buildspec.yml from the repo — builds Docker image and pushes to ECR
    const buildProject = new codebuild.PipelineProject(this, "AgentBuildProject", {
      projectName: "prompt2test-agent-build",
      role: codeBuildRole,
      environment: {
        // ARM64 — AgentCore only supports arm64 architecture
        buildImage: codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
        computeType: codebuild.ComputeType.SMALL,
        privileged: true, // Required for Docker builds
      },
      environmentVariables: {
        AWS_DEFAULT_REGION: { value: this.region },
        IMAGE_REPO_NAME: { value: ecrRepo.repositoryName },
      },
      buildSpec: codebuild.BuildSpec.fromSourceFilename("buildspec.yml"),
      logging: {
        cloudWatch: {
          logGroup,
          prefix: "codebuild",
        },
      },
    });

    // ── CodePipeline ─────────────────────────────────────────────────────
    // Source: GitHub → Build: CodeBuild (Docker build + ECR push)
    //
    // NOTE: The GitHub connection (CodeStar) must be manually authorized
    // in the AWS Console once after first deploy.
    // Go to: Developer Tools → Connections → prompt2test-github → Authorize

    const sourceOutput = new codepipeline.Artifact("SourceOutput");
    const buildOutput = new codepipeline.Artifact("BuildOutput");

    // GitHub connection ARN — created by CDK, authorized manually in console
    const connectionArn = `arn:aws:codeconnections:${this.region}:${this.account}:connection/b49882b2-aec0-4020-a219-fc3978a8cb89`;

    new codepipeline.Pipeline(this, "AgentPipeline", {
      pipelineName: "prompt2test-agent-pipeline",
      stages: [
        // Stage 1 — Pull from GitHub
        {
          stageName: "Source",
          actions: [
            new codepipeline_actions.CodeStarConnectionsSourceAction({
              actionName: "GitHub_Source",
              owner: "ammuvisalakshi",
              repo: "Prompt2TestAgent",
              branch: "master",
              connectionArn,
              output: sourceOutput,
            }),
          ],
        },
        // Stage 2 — Build Docker image + push to ECR
        {
          stageName: "Build",
          actions: [
            new codepipeline_actions.CodeBuildAction({
              actionName: "Build_and_Push_to_ECR",
              project: buildProject,
              input: sourceOutput,
              outputs: [buildOutput],
            }),
          ],
        },
      ],
    });

    // ── Cognito User Pool ─────────────────────────────────────────────────
    const userPool = new cognito.UserPool(this, "UserPool", {
      userPoolName: "prompt2test-users",
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      passwordPolicy: {
        minLength: 8,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const userPoolClient = new cognito.UserPoolClient(this, "UserPoolClient", {
      userPool,
      userPoolClientName: "prompt2test-web-client",
      authFlows: { userPassword: true, userSrp: true },
      generateSecret: false,
    });

    // ── Cognito Identity Pool ─────────────────────────────────────────────
    const identityPool = new cognito.CfnIdentityPool(this, "IdentityPool", {
      identityPoolName: "prompt2test_identity",
      allowUnauthenticatedIdentities: false,
      cognitoIdentityProviders: [{
        clientId: userPoolClient.userPoolClientId,
        providerName: userPool.userPoolProviderName,
      }],
    });

    // IAM role for authenticated users — can invoke AgentCore
    const authenticatedRole = new iam.Role(this, "CognitoAuthRole", {
      assumedBy: new iam.FederatedPrincipal(
        "cognito-identity.amazonaws.com",
        {
          StringEquals: { "cognito-identity.amazonaws.com:aud": identityPool.ref },
          "ForAnyValue:StringLike": { "cognito-identity.amazonaws.com:amr": "authenticated" },
        },
        "sts:AssumeRoleWithWebIdentity"
      ),
      inlinePolicies: {
        AgentCoreInvoke: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ["bedrock-agentcore:InvokeAgentRuntime"],
              resources: [`arn:aws:bedrock-agentcore:${this.region}:${this.account}:runtime/Prompt2TestAgent-YTVbD4GrTi*`],
            }),
          ],
        }),
      },
    });

    new cognito.CfnIdentityPoolRoleAttachment(this, "IdentityPoolRoles", {
      identityPoolId: identityPool.ref,
      roles: { authenticated: authenticatedRole.roleArn },
    });

    // ── Outputs ──────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "UserPoolId", {
      value: userPool.userPoolId,
      description: "Cognito User Pool ID",
      exportName: "Prompt2TestUserPoolId",
    });

    new cdk.CfnOutput(this, "UserPoolClientId", {
      value: userPoolClient.userPoolClientId,
      description: "Cognito User Pool Client ID",
      exportName: "Prompt2TestUserPoolClientId",
    });

    new cdk.CfnOutput(this, "IdentityPoolId", {
      value: identityPool.ref,
      description: "Cognito Identity Pool ID",
      exportName: "Prompt2TestIdentityPoolId",
    });

    new cdk.CfnOutput(this, "ECRRepositoryUri", {
      value: ecrRepo.repositoryUri,
      description: "ECR repository — stores agent Docker images",
      exportName: "Prompt2TestAgentECRUri",
    });

    new cdk.CfnOutput(this, "AgentRoleArn", {
      value: agentRole.roleArn,
      description: "IAM role for Bedrock AgentCore",
      exportName: "Prompt2TestAgentRoleArn",
    });

    new cdk.CfnOutput(this, "PipelineConsoleUrl", {
      value: `https://${this.region}.console.aws.amazon.com/codesuite/codepipeline/pipelines/prompt2test-agent-pipeline/view`,
      description: "CodePipeline console URL — monitor agent deployments",
    });
  }
}
