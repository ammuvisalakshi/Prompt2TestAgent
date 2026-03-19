"""
Bedrock AgentCore Runtime — HTTP entry point.

Diagnostic build: Strands SDK imports deferred to invocation time
so startup errors don't silently crash the container.
"""

import json
import logging
import os
import traceback
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Prompt2Test Agent", version="1.0.0")


@app.get("/health")
def health():
    return {"status": "healthy", "agent": "Prompt2Test"}


@app.post("/invoke")
async def invoke(request: Request):
    """Main agent invocation — imports Strands inside the handler to catch import errors."""
    body_bytes = await request.body()
    session_id = str(uuid.uuid4())

    try:
        body = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        body = {}

    logger.info(f"invoke called | path={request.url.path} | body_len={len(body_bytes)}")

    mode = body.get("mode", "plan")
    prompt = body.get("inputText", "").strip()
    session_id = body.get("sessionId") or session_id

    if not prompt:
        return JSONResponse({"sessionId": session_id, "mode": mode, "error": "inputText is required"})

    try:
        from agent.agent_runner import AgentRunner

        runner = AgentRunner(
            model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5"),
            region=os.environ.get("AWS_REGION", "us-east-1"),
        )

        if mode == "plan":
            result = runner.plan(prompt=prompt, session_id=session_id, team_id=body.get("teamId", "default"))
            return JSONResponse({"sessionId": result["sessionId"], "mode": "plan", "plan": result["plan"]})

        elif mode == "automate":
            plan = body.get("plan")
            if not plan:
                return JSONResponse({"sessionId": session_id, "mode": mode, "error": "plan is required for automate mode"})
            result = runner.automate(plan=plan, session_id=session_id, team_id=body.get("teamId", "default"))
            return JSONResponse({"sessionId": result["sessionId"], "mode": "automate", "result": result["result"]})

        else:
            return JSONResponse({"sessionId": session_id, "mode": mode, "error": f"Unknown mode: {mode}"})

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Agent error: {e}\n{tb}")
        return JSONResponse({"sessionId": session_id, "mode": mode, "error": str(e), "traceback": tb})


# Catch-all: log any unexpected paths AgentCore might call
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def catch_all(path: str, request: Request):
    body_bytes = await request.body()
    logger.warning(f"UNEXPECTED PATH: {request.method} /{path} | body_len={len(body_bytes)}")
    return JSONResponse({"error": f"Not found: {request.method} /{path}"}, status_code=404)
