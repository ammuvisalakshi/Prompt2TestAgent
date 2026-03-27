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

PLAN_SCENARIO_SYSTEM_PROMPT = """You are Prompt2Test, an AI test scenario authoring assistant running on Amazon Bedrock AgentCore.

Your job is to turn test scenarios into structured, executable test steps — each with a clear action and a verifiable expected result.

WORKFLOW:
1. Call get_service_config immediately to fetch the real config values for the service from SSM.
2. Parse the scenario into discrete steps. For each step identify:
   - action: exactly what the tester/system does (use real URLs, field names, button labels from config)
   - expected: what should be visible or verifiable after that action
3. Replace ALL placeholder values (e.g. <URL>, {{BASE_URL}}) with real values from config.
4. Always respond in this exact format — no deviations:

NOTE: <1-2 sentences: what you enriched, or ONE focused question if something is unclear>
STEPS:
[
  {"step": 1, "action": "...", "expected": "..."},
  {"step": 2, "action": "...", "expected": "..."}
]

RULES:
- Actions must be specific and executable (exact URLs, exact field labels, exact button text).
- Expected results must be verifiable assertions (what appears on screen, what changes).
- Never ask vague questions. Forbidden: "Can you provide more details?", "Please clarify".
- Never repeat a question already answered in history.
- Each conversation turn must output the FULL updated steps list, not just the changed step.

FINAL GENERATION:
When the input is exactly "generate_final", output ONLY:
SUMMARY: <one-line description of what is being tested>
STEPS:
[{"step": 1, "action": "...", "expected": "..."}, ...]
Nothing else — no NOTE, no preamble, no questions.
"""

AUTOMATE_SYSTEM_PROMPT = """You are Prompt2Test, an AI test execution agent running on Amazon Bedrock AgentCore.

You have access to Playwright MCP tools to control a real browser.

CRITICAL — SCOPE RULES (these override everything else):
- Execute ONLY the steps listed in the plan. Nothing more.
- Do NOT perform any browser action that is not explicitly described in a step.
- Do NOT add extra steps, explore the UI, click additional buttons, or "helpfully" complete flows beyond what is listed.
- Do NOT add to cart, submit forms, make purchases, or take any destructive/transactional action unless it is explicitly listed as a step.
- After executing the last step, STOP immediately and return results.
- If a step says "verify X is visible", only verify — do not interact further.

Return results as a JSON object with this exact shape:
{
  "summary": "<one-line summary — use the test plan title, not a description of what you did>",
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

Execution rules:
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



class _CapturingTool:
    """Proxy that records playwright MCP calls for LLM-free replay.
    Strands calls tool.stream(tool_use, invocation_state) — an async generator.
    tool_use is a TypedDict: {"toolUseId": str, "name": str, "input": dict}
    """
    def __init__(self, tool, script: list):
        object.__setattr__(self, '_orig', tool)
        object.__setattr__(self, '_script', script)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_orig'), name)

    async def stream(self, tool_use, invocation_state, **kwargs):
        orig = object.__getattribute__(self, '_orig')
        params = tool_use.get('input', {}) if isinstance(tool_use, dict) else getattr(tool_use, 'input', {})
        object.__getattribute__(self, '_script').append({'tool': orig.tool_name, 'params': params})
        logger.info(f"[capture] {orig.tool_name}({list(params.keys())})")
        async for event in orig.stream(tool_use, invocation_state, **kwargs):
            yield event


def _wrap_tools(tools: list, script: list) -> list:
    """Wrap playwright MCP tools with _CapturingTool; leave others unchanged."""
    return [_CapturingTool(t, script) if getattr(t, 'tool_name', '').startswith('playwright_') else t for t in tools]


def _build_model() -> str:
    """Return the Bedrock model ID string for the Strands agent."""
    return os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")


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

    def plan_scenario(self, prompt: str, session_id: str, service: str, env: str,
                      conversation_history: str = "") -> dict[str, Any]:
        """
        Plan Scenario Mode — enrich a pasted test scenario with real SSM config values.

        Returns:
            { "sessionId": str, "mode": "plan_scenario", "text": str }
        """
        import boto3

        if not session_id:
            session_id = str(uuid.uuid4())

        logger.info(f"[plan_scenario] session={session_id} service={service} env={env} prompt={prompt[:80]}")

        from strands import tool

        region = self.region

        @tool
        def get_service_config(service_name: str, environment: str) -> str:
            """
            Fetch all configuration parameters for a service from SSM Parameter Store.
            Call this at the start to get the real values needed to enrich the scenario.

            Args:
                service_name: The service name (e.g. amazon, checkout)
                environment: The environment (dev, qa, prod)
            """
            try:
                ssm_client = boto3.client('ssm', region_name=region)
                path = f'/prompt2test/config/{environment}/services/{service_name}'
                params: dict = {}
                next_token = None
                while True:
                    kwargs: dict = {'Path': path, 'Recursive': True, 'WithDecryption': True}
                    if next_token:
                        kwargs['NextToken'] = next_token
                    resp = ssm_client.get_parameters_by_path(**kwargs)
                    for p in resp.get('Parameters', []):
                        key = p['Name'].split('/')[-1]
                        params[key] = p['Value']
                    next_token = resp.get('NextToken')
                    if not next_token:
                        break
                if params:
                    return json.dumps(params)
                return f'No config found for service "{service_name}" in environment "{environment}". Path: {path}'
            except Exception as e:
                logger.error(f"get_service_config error: {e}")
                return f'Error fetching config: {e}'

        agent = Agent(
            model=_build_model(),
            system_prompt=PLAN_SCENARIO_SYSTEM_PROMPT,
            tools=[get_service_config],
        )

        if conversation_history:
            full_prompt = (
                f"Service: {service}\nEnvironment: {env}\n\n"
                f"Conversation so far:\n{conversation_history}\n\n"
                f"Latest message: {prompt}"
            )
        else:
            full_prompt = (
                f"Service: {service}\nEnvironment: {env}\n\n"
                f"Scenario to enrich:\n{prompt}"
            )

        response = agent(full_prompt)
        raw_text = str(response)
        logger.info(f"[plan_scenario] response ({len(raw_text)} chars): {raw_text[:500]}")

        return {
            "sessionId": session_id,
            "mode": "plan_scenario",
            "text": raw_text,
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
            f"Execute EXACTLY these {len(plan.get('steps', []))} steps and NO others. "
            f"Stop after the last step.\n\n"
            f"Test: {plan.get('summary', '')}\n\n"
            f"Steps to execute:\n{steps_text}\n\n"
            f"IMPORTANT: Do not perform any action not listed above. After step {len(plan.get('steps', []))}, stop and return results."
        )

        from agent.ecs_session import ECSSession

        if task_arn and cluster and mcp_endpoint:
            # ── Reuse pre-started session (2-call flow) ───────────────────────
            logger.info(f"[automate] MCP endpoint: {mcp_endpoint}")
            result = None
            try:
                script: list = []
                with _build_mcp_client(mcp_endpoint) as mcp:
                    agent = Agent(
                        model=_build_model(),
                        system_prompt=AUTOMATE_SYSTEM_PROMPT,
                        tools=_wrap_tools(mcp.list_tools_sync(), script),
                    )
                    response = agent(prompt)
                result = self._parse_plan(str(response))
                result["replay_script"] = script
                logger.info(f"[automate] captured {len(script)} playwright calls")
            except Exception as exc:
                logger.error(f"[automate] Error during automation: {exc}", exc_info=True)
                result = {"passed": False, "summary": "Automation error", "steps": [], "error": str(exc), "replay_script": []}
            finally:
                # Always stop the task when test finishes (success or error)
                try:
                    import boto3
                    ecs = boto3.client("ecs", region_name=self.region)
                    ecs.stop_task(cluster=cluster, task=task_arn, reason="Test session completed")
                    logger.info(f"[automate] Stopped task: {task_arn}")
                except Exception as exc:
                    logger.warning(f"[automate] Could not stop task: {exc}")

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
            script2: list = []
            try:
                with _build_mcp_client(ecs_session.mcp_endpoint) as mcp:
                    agent = Agent(
                        model=_build_model(),
                        system_prompt=AUTOMATE_SYSTEM_PROMPT,
                        tools=_wrap_tools(mcp.list_tools_sync(), script2),
                    )
                    response = agent(prompt)
                result = self._parse_plan(str(response))
                result["replay_script"] = script2
                logger.info(f"[automate] captured {len(script2)} playwright calls")
            except Exception as exc:
                logger.error(f"[automate] Error during automation: {exc}", exc_info=True)
                result = {"passed": False, "summary": "Automation error", "steps": [], "error": str(exc), "replay_script": []}

            # ── Event 2: test complete — task stops immediately after this ─────
            yield json.dumps({
                "event": "complete",
                "sessionId": session_id,
                "mode": "automate",
                "result": result,
            }) + "\n"

    def replay_stream(self, replay_script: list, session_id: str,
                      task_arn: str | None = None, cluster: str | None = None,
                      mcp_endpoint: str | None = None):
        """
        Replay Mode — execute a saved replay script directly via Playwright MCP.
        No LLM involved. Calls each recorded tool with its exact parameters.
        Passes if all commands succeed without error, fails if any throws.
        """
        if not session_id:
            session_id = str(uuid.uuid4())

        logger.info(f"[replay] session={session_id} commands={len(replay_script)}")

        if not replay_script:
            yield json.dumps({
                "event": "complete",
                "sessionId": session_id,
                "mode": "replay",
                "result": {"passed": False, "summary": "No replay script found", "steps": [], "error": "Empty replay script"},
            }) + "\n"
            return

        from agent.ecs_session import ECSSession

        def _run_replay(mcp):
            tools = mcp.list_tools_sync()
            tools_by_name = {t.tool_name: t for t in tools}
            steps = []
            failed = False
            for i, cmd in enumerate(replay_script):
                tool_fn = tools_by_name.get(cmd["tool"])
                if not tool_fn:
                    logger.warning(f"[replay] unknown tool: {cmd['tool']}, skipping")
                    continue
                try:
                    logger.info(f"[replay] step {i+1}: {cmd['tool']} params={str(cmd['params'])[:120]}")
                    tool_fn(**cmd["params"])
                    steps.append({"stepNumber": i + 1, "tool": cmd["tool"], "status": "passed"})
                except Exception as exc:
                    logger.error(f"[replay] step {i+1} FAILED: {exc}")
                    steps.append({"stepNumber": i + 1, "tool": cmd["tool"], "status": "failed", "error": str(exc)})
                    failed = True
                    break
            return steps, not failed

        if mcp_endpoint:
            try:
                with _build_mcp_client(mcp_endpoint) as mcp:
                    steps, passed = _run_replay(mcp)
            finally:
                if task_arn and cluster:
                    try:
                        import boto3
                        ecs = boto3.client("ecs", region_name=self.region)
                        ecs.stop_task(cluster=cluster, task=task_arn, reason="Replay session completed")
                        logger.info(f"[replay] Stopped task: {task_arn}")
                    except Exception as exc:
                        logger.warning(f"[replay] Could not stop task: {exc}")

            yield json.dumps({
                "event": "complete",
                "sessionId": session_id,
                "mode": "replay",
                "result": {
                    "passed": passed,
                    "summary": f"Replay {'passed' if passed else 'failed'} — {len(steps)} steps executed",
                    "steps": steps,
                },
            }) + "\n"
            return

        # Spin up new ECS session if no existing session provided
        with ECSSession(region=self.region) as ecs_session:
            yield json.dumps({"event": "session_ready", "novnc_url": ecs_session.novnc_url}) + "\n"
            with _build_mcp_client(ecs_session.mcp_endpoint) as mcp:
                steps, passed = _run_replay(mcp)

        yield json.dumps({
            "event": "complete",
            "sessionId": session_id,
            "mode": "replay",
            "result": {
                "passed": passed,
                "summary": f"Replay {'passed' if passed else 'failed'} — {len(steps)} steps executed",
                "steps": steps,
            },
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
