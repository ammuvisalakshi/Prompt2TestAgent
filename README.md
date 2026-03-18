# Prompt2Test Agent — Backend

AI-powered test authoring agent built on **Amazon Bedrock AgentCore Runtime**.

## Architecture

```
UI (Prompt2TestUI)
    │
    ▼
API Gateway  POST /api/run
    │
    ▼
Lambda Router  (lambda/router/handler.py)
    │
    ├──► PLAN MODE  → Bedrock Agent Core Runtime
    │                   ├── Claude claude-sonnet-4-6 (LLM reasoning)
    │                   ├── Playwright MCP (headed browser · DEV only)
    │                   └── REST Client MCP (external API calls)
    │
    └──► AUTOMATE MODE  → ECS Fargate (deterministic replay — Phase 2)
```

## What's Built Here

| Folder | Purpose |
|---|---|
| `lambda/router/` | Entry point — receives POST /api/run, routes to Plan or Automate |
| `agent/` | Bedrock AgentCore runtime — LLM reasoning, session memory, tool routing |
| `agent/tools/` | MCP tool implementations (Playwright, REST Client) |
| `infra/` | AWS CDK stack — deploys Lambda, API Gateway, IAM roles |
| `scripts/` | Deploy and test helper scripts |

## Build Phases

### Phase 1 — Plan Mode (Current)
- User types a prompt in the UI chat
- Lambda → Bedrock Claude claude-sonnet-4-6
- Agent reasons and returns a structured execution plan
- Plan displayed in the UI right panel

### Phase 2 — Automate Mode (Next)
- Agent executes the plan using Playwright MCP
- Results streamed back to UI
- Test case saved to DynamoDB

## Tech Stack

- **Runtime**: Python 3.12
- **LLM**: Amazon Bedrock — Claude claude-sonnet-4-6
- **Agent Runtime**: Amazon Bedrock AgentCore
- **MCP Tools**: Playwright MCP, REST Client MCP
- **Infrastructure**: AWS CDK (TypeScript)
- **Entry Point**: AWS Lambda + API Gateway

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Deploy

```bash
cd infra
npm install
npx cdk deploy
```
