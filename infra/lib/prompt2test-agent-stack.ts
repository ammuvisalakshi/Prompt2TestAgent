import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as path from "path";

export class Prompt2TestAgentStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── IAM Role for Lambda ──────────────────────────────────────────────
    const lambdaRole = new iam.Role(this, "RouterLambdaRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole"
        ),
      ],
      inlinePolicies: {
        BedrockInvoke: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ["bedrock:InvokeModel"],
              // Allow Claude claude-sonnet-4-5 only (cross-region inference profile)
              resources: [
                `arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5*`,
                `arn:aws:bedrock:*:${this.account}:inference-profile/us.anthropic.claude-sonnet-4-5*`,
              ],
            }),
            // SSM — read team config (Phase 2)
            new iam.PolicyStatement({
              actions: ["ssm:GetParameter", "ssm:GetParametersByPath"],
              resources: [
                `arn:aws:ssm:${this.region}:${this.account}:parameter/prompt2test/*`,
              ],
            }),
            // Secrets Manager — read account credentials (Phase 2)
            new iam.PolicyStatement({
              actions: ["secretsmanager:GetSecretValue"],
              resources: [
                `arn:aws:secretsmanager:${this.region}:${this.account}:secret:prompt2test/*`,
              ],
            }),
          ],
        }),
      },
    });

    // ── Lambda — Router ──────────────────────────────────────────────────
    const routerFn = new lambda.Function(this, "RouterFunction", {
      functionName: "prompt2test-router",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "lambda.router.handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../"), {
        exclude: [
          "infra/**",
          "scripts/**",
          ".venv/**",
          "**/__pycache__/**",
          "*.md",
          ".git/**",
          "node_modules/**",
        ],
      }),
      role: lambdaRole,
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      environment: {
        BEDROCK_MODEL_ID: "us.anthropic.claude-sonnet-4-5",
        ALLOWED_ORIGIN: "*", // Restrict to Amplify domain after deploy
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    // ── API Gateway ──────────────────────────────────────────────────────
    const api = new apigateway.RestApi(this, "Prompt2TestApi", {
      restApiName: "prompt2test-api",
      description: "Prompt2Test — single Run API",
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: ["Content-Type", "Authorization"],
      },
      deployOptions: {
        stageName: "dev",
        throttlingRateLimit: 10,
        throttlingBurstLimit: 20,
      },
    });

    // POST /api/run
    const apiResource = api.root.addResource("api");
    const runResource = apiResource.addResource("run");
    runResource.addMethod(
      "POST",
      new apigateway.LambdaIntegration(routerFn, {
        timeout: cdk.Duration.seconds(29), // API GW max
      })
    );

    // ── Outputs ──────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "ApiEndpoint", {
      value: `${api.url}api/run`,
      description: "POST /api/run endpoint — connect this to Prompt2TestUI",
    });

    new cdk.CfnOutput(this, "LambdaArn", {
      value: routerFn.functionArn,
    });
  }
}
