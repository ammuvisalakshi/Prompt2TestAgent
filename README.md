# Prompt2Test Agent — Backend

AI-powered test authoring agent running on **Amazon Bedrock AgentCore Runtime**.
Built with **AWS Strands Agents SDK**.

## Deployment Flow (same concept as Amplify for the UI)

```
Push to GitHub (master)
      ↓  AWS CodePipeline watches GitHub and pulls automatically
AWS CodeBuild builds Docker image
      ↓
Pushes image to Amazon ECR (container registry)
      ↓
Agent runs on Bedrock AgentCore Runtime
```

No AWS credentials needed in GitHub — AWS pulls the code itself.

## Architecture

```
UI (Prompt2TestUI)
      │
      ▼
Bedrock AgentCore Runtime  (your agent container)
      │  Strands SDK
      ├── Claude claude-sonnet-4-5  (LLM via Amazon Bedrock)
      ├── Playwright MCP  (headed browser · DEV only · Phase 2)
      └── REST Client MCP (external API calls · Phase 2)
```

## Project Structure

| Path | Purpose |
|---|---|
| `Dockerfile` | Agent container — Python 3.12 + FastAPI + Strands SDK |
| `buildspec.yml` | CodeBuild instructions — build Docker image + push to ECR |
| `agent/main.py` | AgentCore HTTP entry point (`POST /invoke`, `GET /health`) |
| `agent/agent_runner.py` | Strands agent — Claude generates test execution plan |
| `agent/tools/playwright_mcp.py` | Playwright MCP (Phase 2) |
| `agent/tools/rest_client_mcp.py` | REST Client MCP (Phase 2) |
| `agent/config/agent_config.yaml` | Agent + MCP + SSM config |
| `infra/` | CDK stack — ECR + CodePipeline + CodeBuild + IAM |
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

This is the one-time step (same as authorizing GitHub in Amplify).

### Step 3 — Done!
Every push to `master` now:
1. CodePipeline detects the push
2. Pulls code from GitHub
3. CodeBuild builds Docker image
4. Pushes to ECR
5. Agent updated on AgentCore

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

## Build Phases

### Phase 1 — Plan Mode (Current)
- User types prompt in UI → AgentCore → Strands agent → Claude
- Claude returns structured execution plan
- Plan shown in UI right panel

### Phase 2 — Automate Mode (Next)
- Agent executes plan via Playwright MCP (headed browser)
- Results streamed to UI
- Test case saved to DynamoDB
