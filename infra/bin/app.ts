#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { Prompt2TestAgentStack } from "../lib/prompt2test-agent-stack";

const app = new cdk.App();

new Prompt2TestAgentStack(app, "Prompt2TestAgentStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? "us-east-1",
  },
  description: "Prompt2Test — Bedrock AgentCore Runtime + API Gateway",
});
