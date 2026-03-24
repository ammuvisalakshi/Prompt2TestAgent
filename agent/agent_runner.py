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

Your job is to have a focused conversation with the QA engineer to fully understand what they want
to test, then produce a structured execution plan they can review and execute.

HOW TO BEHAVE:

1. CLARIFY FIRST — Ask specific, targeted questions to fill in any gaps before generating the plan.
   Good questions to ask (one at a time, never all at once):
   - "Does this test require logging in? If so, what are the credentials or which secret should I use?"
   - "What should the page show after clicking X — what's the expected result?"
   - "Which environment should I test against — production, staging, or a specific URL?"
   - "After searching for X, should I assert that results appear, click a specific result, or just screenshot?"

2. NEVER ASK VAGUE QUESTIONS — Forbidden phrases:
   - "Could you provide more details?" ← NEVER say this
   - "Can you clarify?" ← too vague, always be specific about WHAT you need
   - "Please provide more information" ← forbidden

3. NEVER REPEAT — If you already asked something in the conversation history, do not ask it again.
   Use the answer already given, or make a reasonable assumption and note it.

4. GENERATE THE PLAN when you have enough to write clear, unambiguous steps.
   For simple tasks (navigate → search → screenshot), 1-2 rounds of clarification is enough.
   For complex tasks (login → multi-step form → assertion), ask until each step is clear.
   Before returning the JSON, include a "confirmationMessage" field that summarises in plain English
   exactly what was agreed — this is shown to the user as a chat message so they can confirm.
   Always return pure JSON with no markdown, no extra text outside the JSON object.

Return the execution plan as a JSON object with this exact shape:
{
  "confirmationMessage": "<friendly 1-2 sentence summary of exactly what test was agreed upon, e.g. 'Got it! We'll navigate to amazon.com, search for iPhone 17 Pro, and screenshot the search results page.'>",
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
  "configNeeded": ["BASE_URL"],
  "estimatedTokens": 450,
  "mcpCalls": 6
}

Additional rules:
- You are a PLANNER only — you cannot execute tests or control a browser.
- If the user asks you to execute or run the test, tell them to click the Automate tab.
  Do NOT fabricate execution results.
- Use Playwright MCP for UI interactions (navigate, click, assert on DOM).
- Use REST Client MCP for API-level checks.
- Use Secrets Manager tool when credentials are needed.
- Wrap all environment-specific values in {{DOUBLE_BRACES}}.
- Keep steps granular — one action per step.
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


def _build_mcp_client(endpoint: str):
    """
    Phase 2 — Return a connected MCPClient for the Playwright MCP server.
    Must be used as a context manager: `with _build_mcp_client(endpoint) as client:`

    Args:
        endpoint: Full base URL of the Playwright MCP server, e.g. http://1.2.3.4:3000
    """
    from urllib.parse import urlparse

    from strands.tools.mcp import MCPClient  # lazy import — avoids startup crash if SDK mismatch
    from mcp.client.sse import sse_client

    sse_url = endpoint.rstrip("/") + "/sse"

    # playwright-mcp validates the Host header must be localhost for CSRF protection.
    # Override it regardless of actual host so the server accepts the connection.
    parsed = urlparse(endpoint)
    port = parsed.port or 3000
    host_header = f"localhost:{port}"

    return MCPClient(lambda: sse_client(sse_url, headers={"Host": host_header}))


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

    def plan(self, prompt: str, session_id: str, team_id: str, conversation_history: str = "") -> dict[str, Any]:
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

        # Include conversation history so the agent has memory across turns
        if conversation_history:
            full_prompt = f"Conversation so far:\n{conversation_history}\n\nLatest message: {prompt}"
        else:
            full_prompt = prompt

        # Invoke the agent
        response = agent(full_prompt)
        raw_text = str(response)
        logger.info(f"[plan] raw response ({len(raw_text)} chars): {raw_text[:500]}")

        # Parse the JSON plan from the agent's response
        plan = self._parse_plan(raw_text)

        return {
            "sessionId": session_id,
            "mode": "plan",
            "plan": plan,
        }

    def start_session(self, session_id: str, team_id: str) -> dict[str, Any]:
        """
        Start Mode — spin up an ECS browser task and return the noVNC URL immediately.
        The task stays running; automate_stream will stop it when the test finishes.
        """
        if not session_id:
            session_id = str(uuid.uuid4())

        logger.info(f"[start_session] session={session_id} team={team_id}")

        from agent.ecs_session import ECSSession

        ecs = ECSSession(region=self.region)
        ecs._start()  # start but don't stop — caller owns lifecycle

        logger.info(f"[start_session] task={ecs.task_arn} novnc={ecs.novnc_url}")

        return {
            "sessionId": session_id,
            "mode": "start_session",
            "novnc_url": ecs.novnc_url,
            "mcp_endpoint": ecs.mcp_endpoint,
            "task_arn": ecs.task_arn,
            "cluster": ecs.cluster,
        }

    def automate_stream(self, plan: dict, session_id: str, team_id: str,
                        task_arn: str | None = None, cluster: str | None = None,
                        mcp_endpoint: str | None = None):
        """
        Automate Mode — executes the test plan against a browser session.

        If task_arn + cluster + mcp_endpoint are provided, reuses an existing
        pre-started ECS session (2-call flow). Otherwise spins up a new one.
        Always stops the task when done.
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

        from agent.ecs_session import ECSSession

        if task_arn and cluster and mcp_endpoint:
            # ── Reuse pre-started session (2-call flow) ───────────────────────
            logger.info(f"[automate] Using ALB endpoint: {mcp_endpoint}")
            with _build_mcp_client(mcp_endpoint) as mcp:
                tools = mcp.list_tools_sync()
                agent = Agent(
                    model=_build_model(),
                    system_prompt=AUTOMATE_SYSTEM_PROMPT,
                    tools=tools,
                )
                response = agent(prompt)
            result = self._parse_plan(str(response))

            yield json.dumps({
                "event": "complete",
                "sessionId": session_id,
                "mode": "automate",
                "result": result,
            }) + "\n"
            return

        # ── Spin up new session (legacy single-call flow) ─────────────────────
        with ECSSession(region=self.region) as ecs_session:
            logger.info(f"[automate] MCP endpoint: {ecs_session.mcp_endpoint}")
            logger.info(f"[automate] noVNC URL: {ecs_session.novnc_url}")

            # ── Event 1: session ready ────────────────────────────────────────
            yield json.dumps({
                "event": "session_ready",
                "novnc_url": ecs_session.novnc_url,
            }) + "\n"

            # ── Execute the test plan ─────────────────────────────────────────
            with _build_mcp_client(ecs_session.mcp_endpoint) as mcp:
                tools = mcp.list_tools_sync()
                agent = Agent(
                    model=_build_model(),
                    system_prompt=AUTOMATE_SYSTEM_PROMPT,
                    tools=tools,
                )
                response = agent(prompt)

            result = self._parse_plan(str(response))

            # ── Event 2: test complete — task stops immediately after this ─────
            yield json.dumps({
                "event": "complete",
                "sessionId": session_id,
                "mode": "automate",
                "result": result,
            }) + "\n"

    def _parse_plan(self, raw: str) -> dict:
        """Parse agent response — strip markdown fences, extract JSON if embedded in text."""
        text = raw.strip()
        # Strip markdown fences
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try extracting JSON block from mixed content (e.g. agent added conversational text)
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        # Conversational response — return raw so UI can display it as chat
        return {"summary": "Plan generated", "raw": text, "steps": []}
