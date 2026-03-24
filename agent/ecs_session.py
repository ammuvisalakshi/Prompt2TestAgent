"""
ECS Session Manager — hybrid fixed-endpoint approach.

noVNC  → ALB DNS (fixed, instant lookup, no Host-header issues on port 6080)
MCP    → task's own public IP registered in SSM at startup (direct connection,
          bypasses ALB which rewrites Host header and breaks playwright-mcp SSE)

On task startup, entrypoint.sh calls:
    curl checkip.amazonaws.com → gets public IP → aws ssm put-parameter current-mcp-host

start_session reads both SSM params (cached after first call — ~0ms on repeat).
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

logger = logging.getLogger(__name__)

SSM_PREFIX = "/prompt2test/playwright"

# Cache SSM params for the lifetime of the container — they never change at runtime.
_SSM_CACHE: dict[str, str] = {}


def _get_ssm(ssm_client, name: str) -> str:
    if name not in _SSM_CACHE:
        response = ssm_client.get_parameter(Name=f"{SSM_PREFIX}/{name}")
        _SSM_CACHE[name] = response["Parameter"]["Value"]
    return _SSM_CACHE[name]


def _load_ssm_params(ssm_client, keys: list[str]) -> dict[str, str]:
    """Fetch multiple SSM params in parallel — eliminates sequential API latency."""
    missing = [k for k in keys if k not in _SSM_CACHE]
    if missing:
        with ThreadPoolExecutor(max_workers=len(missing)) as ex:
            futures = {ex.submit(_get_ssm, ssm_client, k): k for k in missing}
            for f in as_completed(futures):
                f.result()  # raises on error
    return {k: _SSM_CACHE[k] for k in keys}


class ECSSession:
    """
    Resolves endpoints for the always-on playwright-mcp ECS Service.

    noVNC  → ALB DNS (fixed URL, instant, no CSRF issues on port 6080)
    MCP    → task's direct public IP from SSM (avoids ALB Host-header rewrite
             that breaks playwright-mcp SSE CSRF on port 3000)
    """

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.ssm = boto3.client("ssm", region_name=region)
        self.task_arn: str | None = None    # kept for API compatibility
        self.cluster: str | None = None     # kept for API compatibility
        self.public_ip: str | None = None
        self.mcp_endpoint: str | None = None
        self.novnc_url: str | None = None

    def __enter__(self):
        self._start()
        return self

    def __exit__(self, *args):
        self._stop()

    # ── Startup ───────────────────────────────────────────────────────────────

    def _start(self):
        """
        Fetch both SSM params in parallel (cached after first call).
        alb-dns         → fixed ALB DNS for noVNC (port 6080)
        current-mcp-host → task's own public IP for direct MCP (port 3000)
        """
        params = _load_ssm_params(self.ssm, ["alb-dns", "current-mcp-host"])

        alb_dns  = params["alb-dns"]
        mcp_host = params["current-mcp-host"]

        self.public_ip    = mcp_host
        self.mcp_endpoint = f"http://{mcp_host}:3000"        # direct IP — no ALB
        self.novnc_url    = f"http://{alb_dns}:6080/vnc.html"  # ALB — fixed URL
        logger.info(f"[ecs_session] MCP → {mcp_host}:3000  noVNC → {alb_dns}:6080")

    # ── Teardown ──────────────────────────────────────────────────────────────

    def _stop(self):
        """
        No-op. Task is a shared warm resource; playwright-mcp --isolated
        gives each test its own browser context without restarting the task.
        """
        logger.info("[ecs_session] Shared task — not stopped after session")
