# Prompt2Test Agent — Creation & Deployment Guide

## Table of Contents
1. [What is this Agent?](#1-what-is-this-agent)
2. [Architecture Overview](#2-architecture-overview)
3. [How the Agent is Built](#3-how-the-agent-is-built)
4. [Project Structure](#4-project-structure)
5. [Code Walkthrough](#5-code-walkthrough)
6. [Deployment Architecture](#6-deployment-architecture)
7. [Step-by-Step Deployment Guide](#7-step-by-step-deployment-guide)
8. [How to Update the Agent](#8-how-to-update-the-agent)
9. [Connecting the UI to the Agent](#9-connecting-the-ui-to-the-agent)
10. [Phase 2 — Automate Mode](#10-phase-2--automate-mode)
11. [Troubleshooting](#11-troubleshooting)
12. [AWS Resources Created](#12-aws-resources-created)

---

## 1. What is this Agent?

Prompt2Test Agent is an AI-powered test authoring and execution agent. It receives a plain-English test description from the UI, generates a structured test plan, then executes it in a real browser via Playwright MCP — capturing every tool call for replay.

**Example:**
```
User types:  "Test that Best Buy search returns MacBook results"

Agent plans:
  Step 1 → Navigate to bestbuy.com
  Step 2 → Locate the search box
  Step 3 → Type "latest MacBook" and submit
  Step 4 → Assert search results appear

Agent automates (playwright MCP calls captured):
  playwright_navigate   → { url: "https://www.bestbuy.com" }
  playwright_snapshot   → {}
  playwright_fill       → { selector: "input[name=query]", value: "latest MacBook" }
  playwright_click      → { selector: "button[type=submit]" }
  playwright_snapshot   → {}
  → PASSED
```

### Build Phases

| Phase | What it does | Status |
|---|---|---|
| **Phase 1 — Plan Mode** | Agent reads prompt → Claude generates test plan → Returns to UI | ✅ Done |
| **Phase 2 — Automate Mode** | Agent executes plan via Playwright MCP (live browser); captures MCP call log | ✅ Done |

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                  Prompt2TestUI (React / Amplify)             │
│  AgentPage: plan + live browser iframe + result display      │
│  TestCasePage: Plan Steps tab + Automated Steps tab          │
└──────────────────────────┬──────────────────────────────────┘
                           │  InvokeAgentRuntime (Bedrock SDK)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              Amazon Bedrock AgentCore Runtime                │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐   │
│   │            Docker Container (ARM64)                 │   │
│   │                                                     │   │
│   │  agent/main.py          FastAPI HTTP server         │   │
│   │       │  mode: plan / start_session / automate      │   │
│   │  agent/agent_runner.py  Strands SDK Agent           │   │
│   │       │                                             │   │
│   │  Amazon Bedrock   Claude claude-sonnet-4-5          │   │
│   │       │                                             │   │
│   │  Playwright MCP (SSE) ──► ECS Fargate sidecar       │   │
│   │                              Chromium + noVNC        │   │
│   └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                           │  updateTestCaseSteps / updateReplayScript
                           ▼
                    Lambda + Aurora / DynamoDB
                    (test cases + replay scripts)
```

### Three Operation Modes

| Mode | Triggered by | What happens |
|---|---|---|
| `plan` | User submits prompt | Claude generates JSON plan (steps + expected results) |
| `start_session` | User clicks "Save & Automate" | ECS task provisioned; noVNC URL returned to UI |
| `automate` | Immediately after start_session | Agent runs plan via Playwright MCP; captures every tool call |

### Playwright Call Capture

During `automate` mode, the agent instructs Claude to record every Playwright MCP tool it calls — tool name and all arguments — in the JSON response alongside each plan step. These are aggregated into a `replay_script` and saved to the backend. The UI shows them in the **Automated Steps** tab with full parameter detail.

---

## 3. How the Agent is Built

### Technology Stack

| Component | Technology | Why |
|---|---|---|
| Agent Framework | **AWS Strands SDK** | Purpose-built for AWS agents — handles tool loop, MCP, sessions |
| LLM | **Claude claude-sonnet-4-5** via Amazon Bedrock | Best reasoning for test generation |
| HTTP Server | **FastAPI + uvicorn** | Lightweight, fast Python HTTP server |
| Container | **Docker (ARM64)** | AgentCore requires ARM64 architecture |
| Runtime | **Amazon Bedrock AgentCore** | Managed agent hosting — handles scaling, sessions, health |
| Container Registry | **Amazon ECR** | Stores built Docker images |
| CI/CD | **AWS CodePipeline + CodeBuild** | Pulls from GitHub, builds, pushes to ECR automatically |
| Infrastructure | **AWS CDK (TypeScript)** | Infrastructure as code |

### Why Strands SDK over plain boto3?

| | Plain boto3 | Strands SDK |
|---|---|---|
| Tool calling loop | Write manually | Built-in |
| MCP connections | Manual wiring | `MCPClient` built-in |
| Session memory | You manage | Built-in |
| Streaming | You implement | Built-in |
| Code complexity | ~150 lines | ~50 lines |

### Why AgentCore over Lambda?

| | AWS Lambda | Bedrock AgentCore |
|---|---|---|
| Built for | Short functions | Long-running AI agents |
| Session memory | Stateless | Built-in |
| MCP tool support | Manual | Native |
| Timeout | 15 min max | No limit |
| Agent lifecycle | You manage | Managed |

---

## 4. Project Structure

```
Prompt2TestAgent/
│
├── Dockerfile                    ← Agent container (Python 3.12, ARM64)
├── buildspec.yml                 ← CodeBuild instructions (build + push to ECR)
├── requirements.txt              ← Python dependencies
├── README.md                     ← Project overview
├── AGENT_DEPLOYMENT_GUIDE.md     ← This document
│
├── agent/
│   ├── __init__.py
│   ├── main.py                   ← FastAPI HTTP server (AgentCore entry point)
│   ├── agent_runner.py           ← Strands agent + Claude invocation
│   ├── config/
│   │   └── agent_config.yaml     ← Agent + MCP + SSM config
│   └── tools/
│       ├── __init__.py
│       ├── playwright_mcp.py     ← Playwright MCP (Phase 2)
│       └── rest_client_mcp.py    ← REST Client MCP (Phase 2)
│
├── infra/
│   ├── bin/app.ts                ← CDK app entry point
│   ├── lib/
│   │   └── prompt2test-agent-stack.ts  ← CDK stack (ECR + Pipeline + IAM)
│   ├── package.json
│   ├── tsconfig.json
│   └── cdk.json
│
└── scripts/
    └── test_local.py             ← Local test script (no deploy needed)
```

---

## 5. Code Walkthrough

### agent/main.py — HTTP Entry Point

AgentCore calls your container via HTTP. This file sets up the two endpoints AgentCore needs:

```
GET  /health  → AgentCore pings this before routing traffic
               Must return 200 { "status": "healthy" }

POST /invoke  → Called with the user's prompt
               Returns the structured test plan
```

**Request format (from UI):**
```json
{
  "inputText": "Test that Best Buy search returns MacBook results",
  "sessionId": "abc-123",
  "mode": "plan | start_session | automate",
  "taskId": "<ECS task ID — automate mode only>",
  "novncUrl": "<noVNC URL — automate mode only>"
}
```

**Response — plan mode:**
```json
{
  "sessionId": "abc-123",
  "mode": "plan",
  "plan": {
    "summary": "Verify Best Buy search returns MacBook results",
    "steps": [
      { "stepNumber": 1, "action": "Navigate to bestbuy.com", "expectedResult": "Homepage loads" }
    ],
    "configNeeded": []
  }
}
```

**Response — start_session mode:**
```json
{
  "sessionId": "abc-123",
  "mode": "start_session",
  "taskId": "arn:aws:ecs:...:task/...",
  "novncUrl": "http://<task-ip>:6080/vnc.html"
}
```

**Response — automate mode:**
```json
{
  "sessionId": "abc-123",
  "mode": "automate",
  "result": {
    "summary": "Best Buy MacBook search test passed",
    "passed": true,
    "steps": [
      {
        "stepNumber": 1,
        "action": "Navigate to bestbuy.com",
        "status": "passed",
        "detail": "Homepage loaded",
        "playwright_calls": [
          { "tool": "playwright_navigate", "params": { "url": "https://www.bestbuy.com" } }
        ]
      }
    ],
    "replay_script": [
      { "tool": "playwright_navigate", "params": { "url": "https://www.bestbuy.com" } },
      { "tool": "playwright_snapshot", "params": {} }
    ],
    "error": null
  }
}
```

---

### agent/agent_runner.py — Strands Agent

The core agent logic. Three generators handle the three modes:

**plan_stream** — returns a JSON plan:
```python
agent = Agent(
    model=BedrockModel(model_id="us.anthropic.claude-sonnet-4-5"),
    system_prompt=PLAN_SYSTEM_PROMPT,
    tools=[resolve_config, resolve_secret],
)
response = agent(prompt)
# Returns: { summary, steps: [{stepNumber, action, expectedResult}], configNeeded }
```

**start_session_stream** — provisions a browser task:
```python
# Starts ECS Fargate task (Playwright MCP sidecar with noVNC)
# Returns: { taskId, novncUrl }
```

**automate_stream** — executes the plan:
```python
# Connects to Playwright MCP via SSE on the ECS task
# System prompt instructs Claude to record playwright_calls per step
agent = Agent(
    model=BedrockModel(...),
    system_prompt=AUTOMATE_SYSTEM_PROMPT,   # includes playwright_calls instruction
    tools=mcp_tools,                         # playwright MCP tools
)
response = agent(plan_json)
# Returns: { summary, passed, steps: [{..., playwright_calls: [{tool, params}]}],
#            replay_script: [{tool, params}, ...], error }
```

The `replay_script` is the flat ordered list of all Playwright MCP calls — saved to the backend and shown in the UI Automated Steps tab.

---

### Dockerfile — Container Definition

Two-stage build to keep the image small:

```
Stage 1 (builder): Install Python packages
Stage 2 (runtime): Copy only what's needed — result is ~120MB ARM64 image
```

Key requirements:
- Must be **ARM64** — AgentCore only supports arm64 architecture
- Must use **ECR Public** for base image — Docker Hub has rate limits in CodeBuild
- Must **expose port 8080** — AgentCore's required port
- Must have a **/health endpoint** — AgentCore health check

---

### buildspec.yml — CI/CD Instructions

CodeBuild reads this file and:
1. Logs in to ECR
2. Builds the ARM64 Docker image tagged with the git commit SHA
3. Also tags it as `:latest`
4. Pushes both tags to ECR
5. **Automatically calls `update-agent-runtime`** — no manual step needed after each deploy

---

## 6. Deployment Architecture

```
Developer pushes code to GitHub
              │
              ▼
     AWS CodePipeline
     (watches GitHub master branch)
              │
              ▼
     AWS CodeBuild
     (ARM64 instance — runs buildspec.yml)
       ├── docker build (ARM64 image)
       └── docker push → ECR
              │
              ▼
     Amazon ECR
     └── prompt2test-agent:latest  (120MB ARM64 image)
              │
              ▼
     Amazon Bedrock AgentCore Runtime
     Agent ID: Prompt2TestAgent-YTVbD4GrTi
     Status: READY
```

### AWS Account Details

| Resource | Value |
|---|---|
| AWS Account | `590183962483` |
| Region | `us-east-1` |
| Agent Runtime ID | `Prompt2TestAgent-YTVbD4GrTi` |
| Agent Runtime ARN | `arn:aws:bedrock-agentcore:us-east-1:590183962483:runtime/Prompt2TestAgent-YTVbD4GrTi` |
| ECR Repository | `590183962483.dkr.ecr.us-east-1.amazonaws.com/prompt2test-agent` |
| IAM Role | `arn:aws:iam::590183962483:role/prompt2test-agentcore-role` |
| CodePipeline | `prompt2test-agent-pipeline` |
| CloudWatch Logs | `/prompt2test/agentcore` |

---

## 7. Step-by-Step Deployment Guide

### Prerequisites

| Tool | Version | Check |
|---|---|---|
| AWS CLI | v2+ | `aws --version` |
| Node.js | v18+ | `node --version` |
| npm | v9+ | `npm --version` |
| Git | Any | `git --version` |

---

### Step 1 — Clone the Repository

```bash
git clone https://github.com/ammuvisalakshi/Prompt2TestAgent.git
cd Prompt2TestAgent
```

---

### Step 2 — Configure AWS CLI

```bash
aws configure
# Enter:
#   AWS Access Key ID:     <your key>
#   AWS Secret Access Key: <your secret>
#   Default region name:   us-east-1
#   Default output format: json
```

Verify it works:
```bash
aws sts get-caller-identity
```

---

### Step 3 — Install CDK Dependencies

```bash
cd infra
npm install
```

---

### Step 4 — Bootstrap CDK (One-Time Per AWS Account)

```bash
npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1
```

This creates a CDK staging bucket and roles in your account. Only needed once.

---

### Step 5 — Deploy the CDK Stack

```bash
npx cdk deploy --require-approval never
```

This creates:
- ✅ ECR repository (`prompt2test-agent`)
- ✅ CodePipeline (`prompt2test-agent-pipeline`)
- ✅ CodeBuild project (`prompt2test-agent-build`)
- ✅ IAM roles (AgentCore + CodeBuild)
- ✅ CloudWatch log group (`/prompt2test/agentcore`)

---

### Step 6 — Authorize GitHub Connection (One-Time)

After CDK deploy, go to AWS Console:

**Developer Tools → Connections → prompt2test-github → Update pending connection → Authorize**

This allows CodePipeline to pull from your GitHub repository automatically.

---

### Step 7 — Trigger the Pipeline

Push any change to GitHub, or trigger manually:

```bash
aws codepipeline start-pipeline-execution --name prompt2test-agent-pipeline
```

Monitor progress:
```bash
aws codepipeline get-pipeline-state --name prompt2test-agent-pipeline \
  --query "stageStates[*].{Stage:stageName,Status:latestExecution.status}" \
  --output table
```

Expected result:
```
+---------+-------------+
|  Stage  |   Status    |
+---------+-------------+
|  Source |  Succeeded  |
|  Build  |  Succeeded  |
+---------+-------------+
```

---

### Step 8 — Create the AgentCore Runtime (One-Time)

After the image is in ECR:

```bash
aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name "Prompt2TestAgent" \
  --description "Prompt2Test AI test authoring agent (Strands SDK + Claude)" \
  --agent-runtime-artifact '{"containerConfiguration":{"containerUri":"<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/prompt2test-agent:latest"}}' \
  --role-arn "arn:aws:iam::<ACCOUNT_ID>:role/prompt2test-agentcore-role" \
  --network-configuration '{"networkMode":"PUBLIC"}' \
  --environment-variables '{"BEDROCK_MODEL_ID":"us.anthropic.claude-sonnet-4-5","AWS_REGION":"us-east-1"}' \
  --region us-east-1
```

Check status:
```bash
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "Prompt2TestAgent-YTVbD4GrTi" \
  --query "{Status:status,Name:agentRuntimeName}" \
  --output json
```

Expected: `"Status": "READY"`

---

### Step 9 — Verify the Agent is Running

```bash
aws bedrock-agentcore-control list-agent-runtimes \
  --query "agentRuntimes[*].{Name:agentRuntimeName,Status:status,ID:agentRuntimeId}" \
  --output table
```

---

## 8. How to Update the Agent

After the initial setup, updating is simply:

```bash
# Make your code changes, then:
git add -A
git commit -m "Your change description"
git push origin master
```

CodePipeline automatically:
1. Detects the push
2. Pulls the new code
3. Builds a new ARM64 Docker image
4. Pushes to ECR with `:latest` tag

Then update AgentCore to use the new image:
```bash
aws bedrock-agentcore-control update-agent-runtime \
  --agent-runtime-id "Prompt2TestAgent-YTVbD4GrTi" \
  --agent-runtime-artifact '{"containerConfiguration":{"containerUri":"<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/prompt2test-agent:latest"}}' \
  --region us-east-1
```

---

## 9. Connecting the UI to the Agent

The UI (`Prompt2TestUI`) needs to call the AgentCore endpoint when a user submits a prompt.

**Invoke the agent:**
```bash
aws bedrock-agentcore runtime invoke-agent-runtime \
  --agent-runtime-id "Prompt2TestAgent-YTVbD4GrTi" \
  --payload '{"inputText":"Test billing plan shows Enterprise","mode":"plan"}' \
  --region us-east-1
```

In the React UI (`AgentPage.tsx`), add a call to the Bedrock AgentCore API when the user submits a prompt.

---

## 10. Automate Mode — How It Works

Automate mode is fully implemented. The agent connects to a Playwright MCP server running in an ECS Fargate sidecar and executes the test plan in a real browser.

### Flow

```
UI: "Save & Automate"
  │
  ├─► start_session → ECS task started (Playwright MCP + noVNC)
  │                   noVNC URL returned → embedded in UI iframe
  │
  └─► automate     → agent_runner connects to Playwright MCP via SSE
                     Claude executes each plan step using playwright tools
                     Every tool call + arguments captured in playwright_calls
                     Final JSON: { passed, steps[playwright_calls], replay_script }
                     UI shows live browser; result saved to backend
```

### ECS Sidecar Architecture

```
ECS Fargate Task (ARM64)
  ├── playwright-mcp server  — MCP protocol over SSE
  ├── Chromium               — headed browser
  ├── Xvfb                   — virtual display
  └── noVNC                  — WebSocket → HTTP live browser URL
```

### Playwright Call Capture Strategy

The `AUTOMATE_SYSTEM_PROMPT` instructs Claude to include a `playwright_calls` array in each step of its JSON response — listing every MCP tool it called (name + arguments) while executing that step. These are extracted and aggregated into `replay_script` for storage and display.

---

## 11. Troubleshooting

### Pipeline Source stage fails

**Error:** `No Connection found with ARN`

**Fix:** Go to AWS Console → Developer Tools → Connections → Authorize the GitHub connection.

---

### Pipeline Build stage fails — Docker Hub 429

**Error:** `429 Too Many Requests from registry-1.docker.io`

**Fix:** The Dockerfile base image must use ECR Public, not Docker Hub:
```dockerfile
# Wrong
FROM python:3.12-slim

# Correct
FROM public.ecr.aws/docker/library/python:3.12-slim
```

---

### AgentCore create fails — Architecture incompatible

**Error:** `Architecture incompatible. Supported architectures: [arm64]`

**Fix:** CodeBuild must use an ARM64 build image in the CDK stack:
```typescript
buildImage: codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
```

---

### AgentCore create fails — Role validation

**Error:** `Role validation failed. Trust policy allows assumption by this service`

**Fix:** The IAM role trust policy must include `bedrock-agentcore.amazonaws.com`:
```json
{
  "Principal": {
    "Service": [
      "bedrock.amazonaws.com",
      "bedrock-agentcore.amazonaws.com",
      "ecs-tasks.amazonaws.com"
    ]
  }
}
```

---

### Check build logs

```bash
aws logs get-log-events \
  --log-group-name /prompt2test/agentcore \
  --log-stream-name codebuild/<BUILD_ID> \
  --region us-east-1
```

---

## 12. AWS Resources Created

| Service | Resource Name | Purpose |
|---|---|---|
| **Bedrock AgentCore** | `Prompt2TestAgent-YTVbD4GrTi` | Runs the agent container |
| **ECR** | `prompt2test-agent` | Stores Docker images |
| **CodePipeline** | `prompt2test-agent-pipeline` | CI/CD — pulls from GitHub |
| **CodeBuild** | `prompt2test-agent-build` | Builds ARM64 Docker image |
| **IAM Role** | `prompt2test-agentcore-role` | AgentCore permissions (Bedrock, ECR, SSM, Secrets) |
| **IAM Role** | `CodeBuildRole` | CodeBuild permissions (ECR push) |
| **CloudWatch** | `/prompt2test/agentcore` | Agent + build logs |
| **S3 Bucket** | `cdk-hnb659fds-...` | CDK staging bucket (auto-created by bootstrap) |
| **CloudFormation** | `Prompt2TestAgentStack` | Manages all above resources |
| **CodeStar Connection** | `prompt2test-github` | GitHub → AWS authorization |
