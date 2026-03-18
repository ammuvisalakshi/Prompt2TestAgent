import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as codepipeline from "aws-cdk-lib/aws-codepipeline";
import * as codepipeline_actions from "aws-cdk-lib/aws-codepipeline-actions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";

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
                `arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5*`,
                `arn:aws:bedrock:*:${this.account}:inference-profile/us.anthropic.claude-sonnet-4-5*`,
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

    // ════════════════════════════════════════════════════════════════════
    // PLAYWRIGHT MCP SERVER — separate ECS Fargate service
    // ════════════════════════════════════════════════════════════════════

    // ── ECR Repo for Playwright MCP image ────────────────────────────────
    const playwrightEcrRepo = new ecr.Repository(this, "PlaywrightMCPRepository", {
      repositoryName: "prompt2test-playwright-mcp",
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [{ maxImageCount: 5, description: "Keep last 5 images" }],
    });

    // ── CodeBuild — ARM64 build for Playwright MCP ───────────────────────
    const playwrightBuildProject = new codebuild.PipelineProject(this, "PlaywrightBuildProject", {
      projectName: "prompt2test-playwright-mcp-build",
      role: codeBuildRole,
      environment: {
        buildImage: codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
        computeType: codebuild.ComputeType.SMALL,
        privileged: true,
      },
      environmentVariables: {
        AWS_DEFAULT_REGION: { value: this.region },
        IMAGE_REPO_NAME: { value: playwrightEcrRepo.repositoryName },
      },
      buildSpec: codebuild.BuildSpec.fromSourceFilename("playwright-mcp-server/buildspec.yml"),
      logging: { cloudWatch: { logGroup, prefix: "playwright-codebuild" } },
    });

    // ── CodePipeline — Playwright MCP ────────────────────────────────────
    const playwrightSourceOutput = new codepipeline.Artifact("PlaywrightSourceOutput");
    const playwrightBuildOutput  = new codepipeline.Artifact("PlaywrightBuildOutput");

    new codepipeline.Pipeline(this, "PlaywrightPipeline", {
      pipelineName: "prompt2test-playwright-mcp-pipeline",
      stages: [
        {
          stageName: "Source",
          actions: [
            new codepipeline_actions.CodeStarConnectionsSourceAction({
              actionName: "GitHub_Source",
              owner: "ammuvisalakshi",
              repo: "Prompt2TestAgent",
              branch: "master",
              connectionArn,
              output: playwrightSourceOutput,
            }),
          ],
        },
        {
          stageName: "Build",
          actions: [
            new codepipeline_actions.CodeBuildAction({
              actionName: "Build_Playwright_MCP",
              project: playwrightBuildProject,
              input: playwrightSourceOutput,
              outputs: [playwrightBuildOutput],
            }),
          ],
        },
      ],
    });

    // ── VPC — public subnets only (keeps costs minimal) ──────────────────
    const vpc = new ec2.Vpc(this, "Prompt2TestVpc", {
      vpcName: "prompt2test-vpc",
      maxAzs: 2,
      natGateways: 0, // no NAT = no cost; ECS tasks get public IPs
      subnetConfiguration: [
        { name: "public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
      ],
    });

    // ── Security group for Playwright MCP ECS tasks ───────────────────────
    const playwrightSG = new ec2.SecurityGroup(this, "PlaywrightMCPSecurityGroup", {
      vpc,
      securityGroupName: "prompt2test-playwright-mcp-sg",
      description: "Allow MCP (3000) and noVNC (6080) inbound",
    });
    // MCP SSE endpoint — agent connects here
    playwrightSG.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(3000), "MCP SSE port");
    // noVNC viewer — QA watches browser live (headed mode only)
    playwrightSG.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(6080), "noVNC web viewer");

    // ── ECS Cluster ───────────────────────────────────────────────────────
    const cluster = new ecs.Cluster(this, "Prompt2TestCluster", {
      clusterName: "prompt2test-cluster",
      vpc,
    });

    // ── IAM Role for ECS Task ─────────────────────────────────────────────
    const ecsTaskRole = new iam.Role(this, "PlaywrightECSTaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: "Task role for Playwright MCP ECS service",
    });

    const ecsExecutionRole = new iam.Role(this, "PlaywrightECSExecutionRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AmazonECSTaskExecutionRolePolicy"
        ),
      ],
    });
    // Allow pulling from the playwright ECR repo
    playwrightEcrRepo.grantPull(ecsExecutionRole);

    // ── ECS Task Definition (ARM64 / Graviton) ────────────────────────────
    const playwrightTaskDef = new ecs.FargateTaskDefinition(this, "PlaywrightTaskDef", {
      family: "prompt2test-playwright-mcp",
      cpu: 1024,     // 1 vCPU
      memoryLimitMiB: 2048,  // 2 GB — Chromium needs memory
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
      taskRole: ecsTaskRole,
      executionRole: ecsExecutionRole,
    });

    // Container — image tag is :latest; update after new build
    playwrightTaskDef.addContainer("playwright-mcp", {
      image: ecs.ContainerImage.fromEcrRepository(playwrightEcrRepo, "latest"),
      containerName: "playwright-mcp",
      portMappings: [
        { containerPort: 3000, name: "mcp"   },
        { containerPort: 6080, name: "novnc" },
      ],
      environment: {
        // Override to "headed" for DEV environment to watch browser via noVNC
        BROWSER_MODE: "headless",
        MCP_PORT: "3000",
        NOVNC_PORT: "6080",
      },
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "playwright-mcp",
      }),
      healthCheck: {
        command: ["CMD-SHELL",
          "node -e \"require('http').get('http://localhost:3000/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))\""],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(30),
      },
    });

    // ── ALB — stable DNS endpoint for the agent to reach ─────────────────
    const playwrightALB = new elbv2.ApplicationLoadBalancer(this, "PlaywrightALB", {
      loadBalancerName: "prompt2test-playwright-mcp",
      vpc,
      internetFacing: true,
      securityGroup: playwrightSG,
    });

    const playwrightTargetGroup = new elbv2.ApplicationTargetGroup(this, "PlaywrightTargetGroup", {
      vpc,
      port: 3000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        path: "/health",
        interval: cdk.Duration.seconds(30),
        healthyHttpCodes: "200",
      },
    });

    playwrightALB.addListener("PlaywrightListener", {
      port: 3000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      defaultTargetGroups: [playwrightTargetGroup],
    });

    // ── ECS Fargate Service ───────────────────────────────────────────────
    const playwrightService = new ecs.FargateService(this, "PlaywrightService", {
      serviceName: "prompt2test-playwright-mcp",
      cluster,
      taskDefinition: playwrightTaskDef,
      desiredCount: 1,
      assignPublicIp: true, // needed when natGateways=0
      securityGroups: [playwrightSG],
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });

    playwrightService.attachToApplicationTargetGroup(playwrightTargetGroup);

    // ── Outputs ──────────────────────────────────────────────────────────
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

    new cdk.CfnOutput(this, "PlaywrightECRUri", {
      value: playwrightEcrRepo.repositoryUri,
      description: "ECR repository — stores Playwright MCP Docker images",
      exportName: "Prompt2TestPlaywrightECRUri",
    });

    new cdk.CfnOutput(this, "PlaywrightMCPEndpoint", {
      value: `http://${playwrightALB.loadBalancerDnsName}:3000`,
      description: "Playwright MCP SSE endpoint — set as PLAYWRIGHT_MCP_ENDPOINT in AgentCore",
      exportName: "Prompt2TestPlaywrightMCPEndpoint",
    });

    new cdk.CfnOutput(this, "PlaywrightNoVNCUrl", {
      value: `http://${playwrightALB.loadBalancerDnsName}:6080/vnc.html`,
      description: "noVNC web viewer URL — open in browser to watch headed Chromium (DEV only)",
    });

    new cdk.CfnOutput(this, "PlaywrightPipelineUrl", {
      value: `https://${this.region}.console.aws.amazon.com/codesuite/codepipeline/pipelines/prompt2test-playwright-mcp-pipeline/view`,
      description: "CodePipeline console — monitor Playwright MCP deployments",
    });
  }
}
