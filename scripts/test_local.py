"""
Local test script — simulates a POST /api/run request without deploying to AWS.

Usage:
    python scripts/test_local.py

Requires AWS credentials with bedrock:InvokeModel permission.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lambda_.router.handler import handler


def run_test(prompt: str, mode: str = "plan"):
    """Simulate a Lambda invocation."""
    event = {
        "httpMethod": "POST",
        "body": json.dumps({"prompt": prompt, "mode": mode, "sessionId": "test-session-001"}),
        "requestContext": {},
    }

    print(f"\n{'='*60}")
    print(f"Prompt : {prompt}")
    print(f"Mode   : {mode}")
    print(f"{'='*60}")

    response = handler(event, None)
    print(f"Status : {response['statusCode']}")

    body = json.loads(response["body"])
    print(json.dumps(body, indent=2))
    return body


if __name__ == "__main__":
    run_test("Test that the billing plan shows Enterprise for Acme Corp")
    run_test("Verify the export button is visible for Enterprise plan users")
