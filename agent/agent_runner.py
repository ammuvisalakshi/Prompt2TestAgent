"""
Prompt2Test Agent — built with AWS Strands SDK.

Strands handles:
  - Tool calling loop (LLM → tool → LLM → ...)
  - MCP server connections (Playwright MCP, REST Client MCP)
  - Session memory
  - Streaming responses

Phase 1 (Plan Mode):
  - Agent receives a plain-English prompt
  - Uses Claude claude-sonnet-4-5 via Bedrock to generate a structured test plan
  - MCP tools are defined but not yet connected to live servers

Phase 2 (Automate Mode):
  - Playwright MCP server connected (headed browser, DEV only)
  - REST Client MCP server connected
  - Agent executes the plan step-by-step
"""

import json
import logging
import os
import uuid
from typing import Any

from strands import Agent

logger = logging.getLogger(__name__)

AUTOMATE_SYSTEM_PROMPT = """You are Prompt2Test, an AI test execution agent running on Amazon Bedrock AgentCore.

You have access to Playwright MCP tools to control a real browser. Execute each step of the test plan
exactly as described. After each step, report whether it succeeded or failed.

Return results as a JSON object with this exact shape:
{
  "summary": "<one-line summary of test execution>",
  "passed": true | false,
  "steps": [
    {
      "stepNumber": 1,
      "action": "<action label>",
      "status": "passed | failed | skipped",
      "detail": "<what happened>"
    }
  ],
  "error": "<error message if overall test failed, else null>"
}

Rules:
- Execute steps in order. Stop and mark as failed if a step throws an unrecoverable error.
- Use playwright_navigate for navigation, playwright_click for clicks, playwright_fill for inputs.
- Use playwright_get_visible_text or playwright_snapshot to verify assertions.
- Return ONLY the JSON object, no markdown, no extra text.
"""

SYSTEM_PROMPT = """You are Prompt2Test, an AI test authoring agent running on Amazon Bedrock AgentCore.

Your job is to read a plain-English test description and produce a structured execution plan
that a QA engineer can review before it is executed.

Return the execution plan as a JSON object with this exact shape:
{
  "summary": "<one-line summary of what is being tested>",
  "steps": [
    {
      "stepNumber": 1,
      "type": "navigate | click | assert | fetch | wait | fill | screenshot",
      "tool": "playwright | rest-client | ssm | secrets",
      "action": "<short action label shown in the UI>",
      "detail": "<full description of what this step does>",
      "placeholder": "<any {{PLACEHOLDER}} values resolved from config at runtime>"
    }
  ],
  "configNeeded": ["BASE_URL", "EXPECTED_PLAN"],
  "estimatedTokens": 450,
  "mcpCalls": 6
}

Rules:
- Always start with an SSM step to resolve BASE_URL and env-specific config.
- Use Playwright MCP for UI interactions (navigate, click, assert on DOM).
- Use REST Client MCP for API-level checks.
- Use Secrets Manager tool when credentials are needed.
- Wrap all environment-specific values in {{DOUBLE_BRACES}}.
- Keep steps granular — one action per step.
- Return ONLY the JSON object, no markdown, no extra text.
"""


def _build_model() -> str:
    """Return the Bedrock model ID string for the Strands agent."""
    return os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-3-5-sonnet-20241022-v2:0")


def _build_tools_phase1() -> list:
    """
    Phase 1 — Plan Mode tools only (no live MCP connections yet).
    These are lightweight Python functions the agent can call during planning.
    """
    from strands import tool

    @tool
    def resolve_config(key: str) -> str:
        """
        Resolve a configuration value from SSM Parameter Store.
        During plan mode, returns a placeholder so the QA can see what will be resolved at runtime.

        Args:
            key: The config key to resolve (e.g. BASE_URL, EXPECTED_PLAN)
        """
        return f"{{{{{key}}}}}"   # returns {{KEY}} as a placeholder

    @tool
    def resolve_secret(secret_name: str) -> str:
        """
        Resolve a secret from AWS Secrets Manager.
        During plan mode, returns a masked placeholder.

        Args:
            secret_name: The secret name (e.g. ACCOUNT_PASSWORD)
        """
        return f"***{secret_name}***"

    return [resolve_config, resolve_secret]


def _build_mcp_tools_phase2() -> list:
    """
    Phase 2 — Connect to live MCP servers.
    Playwright MCP runs as a standalone ECS service behind an ALB.
    Called only in Automate mode.
    """
    from strands.tools.mcp import MCPClient  # lazy import — avoids startup crash if SDK mismatch

    playwright_endpoint = os.environ.get(
        "PLAYWRIGHT_MCP_ENDPOINT",
        "http://localhost:3000"
    )
    # Strands MCPClient expects the SSE endpoint URL
    sse_url = playwright_endpoint.rstrip("/") + "/sse"

    playwright_mcp = MCPClient(url=sse_url)
    return playwright_mcp.tools


class AgentRunner:
    """
    Prompt2Test agent built on AWS Strands SDK.
    Runs inside Amazon Bedrock AgentCore Runtime.
    """

    def __init__(self, model_id: str, region: str):
        self.model_id = model_id
        self.region = region
        # Override env vars so _build_model picks them up
        os.environ["BEDROCK_MODEL_ID"] = model_id
        os.environ["AWS_REGION"] = region

    def plan(self, prompt: str, session_id: str, team_id: str) -> dict[str, Any]:
        """
        Plan Mode — generate a structured test execution plan using Strands agent.

        Returns:
            { "sessionId": str, "mode": "plan", "plan": { ... } }
        """
        if not session_id:
            session_id = str(uuid.uuid4())

        logger.info(f"[plan] session={session_id} team={team_id} prompt={prompt[:80]}")

        # Build Strands agent with Phase 1 tools
        agent = Agent(
            model=_build_model(),
            system_prompt=SYSTEM_PROMPT,
            tools=_build_tools_phase1(),
        )

        # Invoke the agent
        response = agent(prompt)

        # Parse the JSON plan from the agent's response
        plan = self._parse_plan(str(response))

        return {
            "sessionId": session_id,
            "mode": "plan",
            "plan": plan,
        }

    def automate(self, plan: dict, session_id: str, team_id: str) -> dict[str, Any]:
        """
        Automate Mode — execute the plan using Playwright MCP.
        Connects to the Playwright MCP server via PLAYWRIGHT_MCP_ENDPOINT env var.
        """
        if not session_id:
            session_id = str(uuid.uuid4())

        logger.info(f"[automate] session={session_id} team={team_id} steps={len(plan.get('steps', []))}")

        steps_text = "\n".join([
            f"Step {s['stepNumber']}: {s['action']} — {s['detail']}"
            for s in plan.get("steps", [])
        ])
        prompt = (
            f"Execute this test plan:\n\nSummary: {plan.get('summary', '')}\n\n"
            f"Steps:\n{steps_text}"
        )

        tools = _build_mcp_tools_phase2()
        agent = Agent(
            model=_build_model(),
            system_prompt=AUTOMATE_SYSTEM_PROMPT,
            tools=tools,
        )

        response = agent(prompt)
        result = self._parse_plan(str(response))

        return {
            "sessionId": session_id,
            "mode": "automate",
            "result": result,
        }

    def _parse_plan(self, raw: str) -> dict:
        """Parse agent response — strip markdown fences if present."""
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Return raw text wrapped so the UI can still display it
            return {"summary": "Plan generated", "raw": text, "steps": []}
