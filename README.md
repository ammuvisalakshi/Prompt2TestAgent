# Prompt2Test Agent — Backend

AI-powered test authoring agent running on **Amazon Bedrock AgentCore Runtime**.

## Architecture

```
UI (Prompt2TestUI)
      │
      ▼
Bedrock AgentCore Runtime   ← managed agent container
      │
      ├── Claude claude-sonnet-4-5 (LLM reasoning via Amazon Bedrock)
      ├── Playwright MCP  (headed browser · DEV only · Phase 2)
      └── REST Client MCP (external API calls · Phase 2)
```

## Deployment Flow

```
Push to GitHub (master)
      ↓  GitHub Actions triggers automatically
Build Docker image
      ↓
Push to Amazon ECR (container registry)
      ↓
CDK deploys ECR repo + IAM roles
      ↓
Agent is live on Bedrock AgentCore
```

## Project Structure

| Path | Purpose |
|---|---|
| `Dockerfile` | Agent container — Python 3.12 + FastAPI |
| `agent/main.py` | AgentCore HTTP entry point (`POST /invoke`, `GET /health`) |
| `agent/agent_runner.py` | Claude invocation — generates test execution plan |
| `agent/tools/playwright_mcp.py` | Playwright MCP (Phase 2) |
| `agent/tools/rest_client_mcp.py` | REST Client MCP (Phase 2) |
| `agent/config/agent_config.yaml` | Agent + MCP + SSM config |
| `.github/workflows/deploy.yml` | GitHub Actions CI/CD pipeline |
| `infra/` | AWS CDK stack — ECR + IAM roles |
| `scripts/test_local.py` | Local test without deploying |

## Build Phases

### Phase 1 — Plan Mode (Current)
- User types a prompt in the UI
- AgentCore receives the request → calls Claude
- Claude returns a structured execution plan
- Plan displayed in the UI right panel

### Phase 2 — Automate Mode (Next)
- Agent executes the plan using Playwright MCP
- Results streamed back to the UI
- Test case saved to DynamoDB

## Setup — GitHub Actions Secrets Required

In your GitHub repo → **Settings → Secrets and variables → Actions**, add:

| Secret | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | Your AWS access key |
| `AWS_SECRET_ACCESS_KEY` | Your AWS secret key |

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Run agent locally
uvicorn agent.main:app --reload --port 8080

# Test plan mode
python scripts/test_local.py
```

## Deploy Infrastructure (first time only)

```bash
cd infra
npm install
npx cdk bootstrap   # one-time setup per AWS account
npx cdk deploy
```

After that, every push to `master` auto-deploys via GitHub Actions.
