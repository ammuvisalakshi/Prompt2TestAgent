"""
Minimal Bedrock AgentCore container — pure Python stdlib HTTP server.
No FastAPI, no Strands at startup. Eliminates all library startup issues.
"""

import json
import logging
import os
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class AgentHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        logger.info(f"GET {self.path}")
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"status": "healthy", "agent": "Prompt2Test"})
        else:
            logger.warning(f"Unknown GET path: {self.path}")
            self._send_json(404, {"error": f"Not found: GET {self.path}"})

    def do_POST(self):
        body_bytes = self._read_body()
        logger.info(f"POST {self.path} body_len={len(body_bytes)}")

        try:
            body = json.loads(body_bytes) if body_bytes else {}
        except Exception:
            body = {}

        if self.path.rstrip("/") != "/invoke":
            logger.warning(f"Unknown POST path: {self.path}")
            self._send_json(404, {"error": f"Not found: POST {self.path}"})
            return

        session_id = body.get("sessionId") or str(uuid.uuid4())
        mode = body.get("mode", "plan")
        prompt = body.get("inputText", "").strip()

        if not prompt:
            self._send_json(200, {"sessionId": session_id, "mode": mode, "error": "inputText is required"})
            return

        try:
            from agent.agent_runner import AgentRunner
            runner = AgentRunner(
                model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5"),
                region=os.environ.get("AWS_REGION", "us-east-1"),
            )
            if mode == "plan":
                result = runner.plan(prompt=prompt, session_id=session_id, team_id=body.get("teamId", "default"))
                self._send_json(200, {"sessionId": result["sessionId"], "mode": "plan", "plan": result["plan"]})
            elif mode == "automate":
                plan = body.get("plan")
                if not plan:
                    self._send_json(200, {"sessionId": session_id, "mode": mode, "error": "plan required"})
                    return
                result = runner.automate(plan=plan, session_id=session_id, team_id=body.get("teamId", "default"))
                self._send_json(200, {"sessionId": result["sessionId"], "mode": "automate", "result": result["result"]})
            else:
                self._send_json(200, {"sessionId": session_id, "mode": mode, "error": f"Unknown mode: {mode}"})

        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"Agent error: {e}\n{tb}")
            self._send_json(200, {"sessionId": session_id, "mode": mode, "error": str(e), "traceback": tb})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Prompt2Test agent on port {port}")
    server = HTTPServer(("0.0.0.0", port), AgentHandler)
    logger.info("Ready — listening for requests")
    server.serve_forever()
