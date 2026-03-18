"""
Bedrock AgentCore Runtime — HTTP entry point.

AgentCore calls this container via HTTP when the agent is invoked.
The container runs as a managed service — AgentCore handles:
  - Session management
  - Scaling (spin up/down containers)
  - Health checks
  - Tool routing (Phase 2: Playwright MCP, REST Client MCP)

Endpoints:
  POST /invoke  — main agent invocation (called by AgentCore)
  GET  /health  — health check (called by AgentCore before routing traffic)
"""

import logging
import os
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent.agent_runner import AgentRunner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Prompt2Test Agent", version="1.0.0")

# Initialise the runner once at startup (reused across requests)
runner = AgentRunner(
    model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5"),
    region=os.environ.get("AWS_REGION", "us-east-1"),
)


# ── Request / Response models ────────────────────────────────────────────

class InvokeRequest(BaseModel):
    """Payload sent by AgentCore (or directly from the UI for testing)."""
    inputText: str                        # The user's plain-English prompt
    sessionId: str = ""                   # Conversation session ID
    mode: str = "plan"                    # "plan" | "automate"
    teamId: str = "default"              # Injected from Cognito JWT (Phase 2)
    sessionAttributes: dict = {}
    promptSessionAttributes: dict = {}


class InvokeResponse(BaseModel):
    sessionId: str
    mode: str
    plan: dict | None = None
    error: str | None = None


# ── Routes ───────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """AgentCore health check — must return 200 for traffic to be routed."""
    return {"status": "healthy", "agent": "Prompt2Test"}


@app.post("/invoke", response_model=InvokeResponse)
async def invoke(request: InvokeRequest):
    """
    Main agent invocation.

    Plan mode  → Claude generates a structured test execution plan.
    Automate   → Phase 2: executes plan via Playwright MCP (not yet implemented).
    """
    session_id = request.sessionId or str(uuid.uuid4())
    prompt = request.inputText.strip()

    if not prompt:
        raise HTTPException(status_code=400, detail="inputText is required")

    logger.info(f"invoke | mode={request.mode} | session={session_id} | prompt={prompt[:80]}")

    try:
        if request.mode == "plan":
            result = runner.plan(
                prompt=prompt,
                session_id=session_id,
                team_id=request.teamId,
            )
            return InvokeResponse(
                sessionId=result["sessionId"],
                mode="plan",
                plan=result["plan"],
            )

        elif request.mode == "automate":
            # Phase 2 — Playwright MCP execution
            raise HTTPException(status_code=501, detail="Automate mode is Phase 2")

        else:
            raise HTTPException(status_code=400, detail=f"Unknown mode: {request.mode}")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Agent error")
        return InvokeResponse(
            sessionId=session_id,
            mode=request.mode,
            error=str(e),
        )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"error": str(exc)})
