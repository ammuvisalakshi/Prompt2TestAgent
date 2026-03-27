# Prompt2Test Agent — Backend

AI-powered test authoring and execution agent running on **Amazon Bedrock AgentCore Runtime**.
Built with **AWS Strands Agents SDK**.

## Deployment Flow

```
Push to GitHub (master)
      ↓  AWS CodePipeline watches GitHub and pulls automatically
AWS CodeBuild builds ARM64 Docker image
      ↓
Pushes image to Amazon ECR
      ↓
buildspec.yml calls update-agent-runtime automatically
      ↓
Agent live on Bedrock AgentCore Runtime
```

No AWS credentials needed in GitHub — AWS pulls the code itself.

## Architecture

```
UI (Prompt2TestUI)
      │  InvokeAgentRuntime (Bedrock SDK)
      ▼
Bedrock AgentCore Runtime  (Docker container, ARM64)
      │  Strands SDK
      ├── Claude claude-sonnet-4-5  (LLM via Amazon Bedrock)
      └── Playwright MCP  (SSE → ECS Fargate sidecar)
                │
                ├── Chromium (headless browser)
                ├── Xvfb (virtual display)
                └── noVNC  (live browser URL streamed to UI)
```

## Modes

The agent handles three operation modes sent in the request payload:

| Mode | What it does |
|---|---|
| `plan` | Claude reads the prompt and returns a structured JSON test plan |
| `start_session` | Provisions an ECS task (browser container); returns noVNC URL |
| `automate` | Connects to Playwright MCP via SSE and executes the plan; returns pass/fail + MCP call log |

### Request format

```json
{
  "inputText": "Test that Best Buy search returns MacBook results",
  "sessionId": "abc-123",
  "mode": "plan | start_session | automate",
  "taskId": "<ECS task ID — automate only>",
  "novncUrl": "<noVNC URL — automate only>"
}
```

### Response format — plan mode

```json
{
  "sessionId": "abc-123",
  "mode": "plan",
  "plan": {
    "summary": "Verify Best Buy search returns MacBook results",
    "steps": [
      { "stepNumber": 1, "action": "Navigate to bestbuy.com", "expectedResult": "Homepage loads" }
    ],
    "configNeeded": [],
    "estimatedTokens": 380
  }
}
```

### Response format — automate mode

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
        "detail": "Homepage loaded successfully",
        "playwright_calls": [
          { "tool": "playwright_navigate", "params": { "url": "https://www.bestbuy.com" } },
          { "tool": "playwright_snapshot", "params": {} }
        ]
      }
    ],
    "replay_script": [
      { "tool": "playwright_navigate", "params": { "url": "https://www.bestbuy.com" } },
      { "tool": "playwright_snapshot", "params": {} },
      { "tool": "playwright_fill", "params": { "selector": "input[name=query]", "value": "MacBook" } }
    ],
    "error": null
  }
}
```

The `replay_script` is a flat list of all Playwright MCP calls made during the run, in order. It is saved to the backend and displayed in the Automated Steps tab of the UI.

## Project Structure

| Path | Purpose |
|---|---|
| `Dockerfile` | Agent container — Python 3.12 + FastAPI + Strands SDK (ARM64) |
| `buildspec.yml` | CodeBuild instructions — build image, push to ECR, update AgentCore |
| `agent/main.py` | AgentCore HTTP entry point (`POST /invoke`, `GET /health`) |
| `agent/agent_runner.py` | Strands agent — plan, start_session, and automate logic |
| `agent/config/agent_config.yaml` | Agent + MCP + SSM config |
| `infra/` | CDK stack — ECR + CodePipeline + CodeBuild + IAM + Cognito |
| `scripts/test_local.py` | Local test without deploying |

## One-Time Setup (in AWS Console)

### Step 1 — Deploy CDK infrastructure
```bash
cd infra
npm install
npx cdk bootstrap    # first time only per AWS account
npx cdk deploy
```

### Step 2 — Authorize GitHub connection
After CDK deploy, go to AWS Console:
**Developer Tools → Connections → prompt2test-github → Click "Update pending connection" → Authorize**

### Step 3 — Trigger first pipeline run
```bash
aws codepipeline start-pipeline-execution --name prompt2test-agent-pipeline
```

Every subsequent push to `master` auto-triggers the pipeline.

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Run agent locally
uvicorn agent.main:app --reload --port 8080

# Test plan mode (needs AWS credentials locally)
python scripts/test_local.py
```
