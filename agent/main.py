"""
Bedrock AgentCore Runtime — HTTP entry point.

Strands SDK powers the agent logic.
AgentCore calls this container via HTTP:
  POST /invoke  — agent invocation
  GET  /health  — liveness check before routing traffic
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

app = FastAPI(title="Prompt2Test Agent (Strands SDK)", version="1.0.0")

# Initialise runner once at startup — reused across all requests
runner = AgentRunner(
    model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5"),
    region=os.environ.get("AWS_REGION", "us-east-1"),
)


# ── Models ───────────────────────────────────────────────────────────────

class InvokeRequest(BaseModel):
    inputText: str                  # Plain-English test prompt from the UI
    sessionId: str = ""             # Conversation session (managed by AgentCore)
    mode: str = "plan"              # "plan" | "automate"
    teamId: str = "default"        # From Cognito JWT (Phase 2)
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
    """AgentCore health check — must return 200 for traffic routing."""
    return {"status": "healthy", "agent": "Prompt2Test", "sdk": "strands-agents"}


@app.post("/invoke", response_model=InvokeResponse)
async def invoke(request: InvokeRequest):
    """Main agent invocation — called by Bedrock AgentCore."""
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
            raise HTTPException(status_code=501, detail="Automate mode is Phase 2")

        else:
            raise HTTPException(status_code=400, detail=f"Unknown mode: {request.mode}")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Strands agent error")
        return InvokeResponse(sessionId=session_id, mode=request.mode, error=str(e))


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"error": str(exc)})
