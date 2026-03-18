"""
Bedrock AgentCore Runner — Plan Mode.

Sends the user's prompt to Claude via Amazon Bedrock and returns a
structured execution plan (steps the agent would take to author the test).

Phase 1: Plan Mode only — LLM generates the plan, no browser/MCP execution yet.
Phase 2: Will wire in Playwright MCP and REST Client MCP for live execution.
"""

import json
import logging
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# System prompt that instructs Claude how to behave as a test authoring agent
SYSTEM_PROMPT = """You are Prompt2Test, an AI test authoring agent that runs on Amazon Bedrock.

Your job is to read a plain-English test description and produce a structured execution plan
that a QA engineer can review before it is executed.

The execution plan must be returned as a JSON object with this exact shape:
{
  "summary": "<one-line summary of what is being tested>",
  "steps": [
    {
      "stepNumber": 1,
      "type": "navigate | click | assert | fetch | wait | fill | screenshot",
      "tool": "playwright | rest-client | ssm | secrets",
      "action": "<short action label shown in the UI>",
      "detail": "<full description of what this step does>",
      "placeholder": "<any {{PLACEHOLDER}} values that need to be resolved from config>"
    }
  ],
  "configNeeded": ["BASE_URL", "EXPECTED_PLAN"],
  "estimatedTokens": 450,
  "mcpCalls": 6
}

Rules:
- Always start with an SSM step to resolve BASE_URL and any env-specific config.
- Use Playwright MCP for UI interactions (navigate, click, assert on DOM).
- Use REST Client MCP for API-level checks.
- Use Secrets Manager tool when credentials are needed.
- Wrap all environment-specific values in {{DOUBLE_BRACES}} so they resolve at runtime.
- Keep steps granular — one action per step.
- Return ONLY the JSON object, no markdown, no explanation outside the JSON.
"""


class AgentRunner:
    """Invokes Claude via Bedrock to generate a test execution plan."""

    def __init__(self, model_id: str, region: str):
        self.model_id = model_id
        self.region = region
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def plan(self, prompt: str, session_id: str, team_id: str) -> dict[str, Any]:
        """
        Generate a test execution plan from a plain-English prompt.

        Returns:
            {
                "sessionId": str,
                "mode": "plan",
                "plan": { ... }   # structured execution plan from Claude
            }
        """
        if not session_id:
            session_id = str(uuid.uuid4())

        logger.info(f"Generating plan | session={session_id} | team={team_id}")

        try:
            raw = self._invoke_claude(prompt)
            plan = self._parse_plan(raw)
        except ClientError as e:
            logger.error(f"Bedrock error: {e}")
            raise
        except json.JSONDecodeError as e:
            logger.warning(f"Claude returned non-JSON — wrapping as raw text: {e}")
            plan = {"summary": prompt, "steps": [], "raw": raw}

        return {
            "sessionId": session_id,
            "mode": "plan",
            "plan": plan,
        }

    def _invoke_claude(self, user_prompt: str) -> str:
        """Call Bedrock InvokeModel with Claude messages API."""
        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": user_prompt,
                }
            ],
        }

        response = self.client.invoke_model(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(payload),
        )

        body = json.loads(response["body"].read())
        return body["content"][0]["text"]

    def _parse_plan(self, raw: str) -> dict:
        """Parse Claude's JSON response into a plan dict."""
        # Claude may wrap JSON in markdown code fences — strip them
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1])
        return json.loads(text)
