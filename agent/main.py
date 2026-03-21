"""
Bedrock AgentCore Runtime — HTTP entry point.

Official AgentCore protocol (bedrock-agentcore runtime):
  POST /invocations  — agent invocation
  GET  /ping         — health check
  Port 8080 (default) — but confirmed working on 8000 from runtime logs
"""

import json
import logging
import os
import traceback
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Prompt2Test Agent", version="1.0.0")

logger.info("Prompt2Test Agent starting on port 8080")


# ── Health check — AgentCore calls GET /ping ──────────────────────────────
@app.get("/ping")
def ping():
    logger.info("GET /ping")
    return {"status": "healthy"}


# Keep /health as alias
@app.get("/health")
def health():
    logger.info("GET /health")
    return {"status": "healthy"}


# ── Main invocation — AgentCore calls POST /invocations ───────────────────
@app.post("/invocations")
async def invocations(request: Request):
    body_bytes = await request.body()
    logger.info(f"POST /invocations body_len={len(body_bytes)}")

    try:
        body = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        body = {}

    session_id = body.get("sessionId") or str(uuid.uuid4())
    mode = body.get("mode", "plan")
    prompt = body.get("inputText", "").strip()

    if not prompt:
        return JSONResponse({"sessionId": session_id, "mode": mode, "error": "inputText is required"})

    try:
        from agent.agent_runner import AgentRunner
        runner = AgentRunner(
            model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5"),
            region=os.environ.get("AWS_REGION", "us-east-1"),
        )
        if mode == "plan":
            result = runner.plan(
                prompt=prompt,
                session_id=session_id,
                team_id=body.get("teamId", "default"),
                conversation_history=body.get("conversationHistory", ""),
            )
            return JSONResponse({"sessionId": result["sessionId"], "mode": "plan", "plan": result["plan"]})
        elif mode == "automate":
            plan = body.get("plan")
            if not plan:
                return JSONResponse({"sessionId": session_id, "mode": mode, "error": "plan required"})
            # Stream newline-delimited JSON events so frontend gets noVNC URL
            # immediately (before test starts) and result when test completes.
            return StreamingResponse(
                runner.automate_stream(plan=plan, session_id=session_id, team_id=body.get("teamId", "default")),
                media_type="application/x-ndjson",
            )
        else:
            return JSONResponse({"sessionId": session_id, "mode": mode, "error": f"Unknown mode: {mode}"})

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Agent error: {e}\n{tb}")
        return JSONResponse({"sessionId": session_id, "mode": mode, "error": str(e), "traceback": tb})


# Keep /invoke as alias for backwards compatibility
@app.post("/invoke")
async def invoke(request: Request):
    return await invocations(request)


# Catch-all — log unexpected paths
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def catch_all(path: str, request: Request):
    body_bytes = await request.body()
    logger.warning(f"UNEXPECTED: {request.method} /{path} body_len={len(body_bytes)}")
    return JSONResponse({"error": f"Not found: {request.method} /{path}"}, status_code=404)
