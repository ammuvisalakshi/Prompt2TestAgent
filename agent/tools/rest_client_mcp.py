"""
REST Client MCP Tool — Phase 2.

Gives the agent the ability to make HTTP requests to the system under test.
Used for API-level test steps (e.g. verify a REST endpoint returns the expected value).

Phase 1 status: STUB — plan mode does not execute API calls.
Phase 2: Will be wired into the AgentCore tool loop.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RestResult:
    status_code: int
    body: dict | str
    headers: dict


class RestClientMCP:
    """
    Interface to the REST Client MCP server.

    The MCP server runs as a sidecar container in ECS.
    It exposes tools: http.get, http.post, http.put, http.delete.
    Available in ALL environments (no IAM restriction like Playwright/Bedrock).
    """

    def tool_definitions(self) -> list[dict]:
        """Return MCP tool definitions for the Bedrock agent tool config."""
        return [
            {
                "name": "rest_get",
                "description": "Make an HTTP GET request to an API endpoint.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "headers": {"type": "object", "description": "Optional HTTP headers"},
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "rest_post",
                "description": "Make an HTTP POST request with a JSON body.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "body": {"type": "object"},
                        "headers": {"type": "object"},
                    },
                    "required": ["url", "body"],
                },
            },
            {
                "name": "rest_assert_status",
                "description": "Assert that the last HTTP response had the expected status code.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "expected": {"type": "integer", "description": "Expected HTTP status code"}
                    },
                    "required": ["expected"],
                },
            },
        ]

    def execute(self, tool_name: str, inputs: dict) -> RestResult:
        """Execute a REST Client MCP tool call — Phase 2 implementation."""
        # TODO Phase 2: POST to MCP sidecar with tool_name and inputs
        raise NotImplementedError("REST Client MCP execution is Phase 2")
