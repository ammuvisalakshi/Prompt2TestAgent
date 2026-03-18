"""
POST /api/run — Lambda entry point.

Receives requests from API Gateway (UI, webhook, cron, direct API).
Routes to:
  - Plan mode   → Bedrock AgentCore (LLM reasoning, generates test plan)
  - Automate mode → ECS Fargate (deterministic replay — Phase 2)
"""

import json
import os
import sys
import logging

# Allow importing from the agent package when bundled with Lambda
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.agent_runner import AgentRunner

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Content-Type": "application/json",
}


def handler(event: dict, context) -> dict:
    """Main Lambda handler."""

    # Handle CORS preflight
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    try:
        body = _parse_body(event)
        mode = body.get("mode", "plan")          # "plan" | "automate"
        prompt = body.get("prompt", "").strip()
        session_id = body.get("sessionId", "")
        team_id = _extract_team_id(event)

        logger.info(f"mode={mode} team={team_id} session={session_id} prompt={prompt[:80]}")

        if not prompt:
            return _error(400, "prompt is required")

        if mode == "plan":
            result = _handle_plan(prompt, session_id, team_id)
        elif mode == "automate":
            # Phase 2 — ECS dispatch
            result = _error(501, "Automate mode not yet implemented")
            return result
        else:
            return _error(400, f"Unknown mode: {mode}")

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps(result),
        }

    except Exception as e:
        logger.exception("Unhandled error")
        return _error(500, str(e))


def _handle_plan(prompt: str, session_id: str, team_id: str) -> dict:
    """Route to Bedrock AgentCore for plan generation."""
    runner = AgentRunner(
        model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5"),
        region=os.environ.get("AWS_REGION", "us-east-1"),
    )
    return runner.plan(prompt=prompt, session_id=session_id, team_id=team_id)


def _parse_body(event: dict) -> dict:
    body = event.get("body", "{}")
    if isinstance(body, str):
        return json.loads(body or "{}")
    return body or {}


def _extract_team_id(event: dict) -> str:
    """Extract team-id injected by API Gateway from Cognito JWT (Phase 1: default)."""
    ctx = event.get("requestContext", {})
    authorizer = ctx.get("authorizer", {})
    claims = authorizer.get("claims", {})
    return claims.get("custom:team_id", "default")


def _error(status: int, message: str) -> dict:
    return {
        "statusCode": status,
        "headers": CORS_HEADERS,
        "body": json.dumps({"error": message}),
    }
