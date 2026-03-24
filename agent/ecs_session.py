"""
ECS Session Manager — direct task IP from NLB target group.

Problem: NLB routes MCP (agent) and noVNC (browser) by separate 5-tuple hashes,
so they can land on different tasks → blank noVNC screen.

Fix: agent calls DescribeTargetHealth → picks one healthy task IP → uses that
IP directly for BOTH MCP (:3000) and noVNC (:6080). Guarantees same task.

start_session reads mcp-tg-arn from SSM (cached), then picks a healthy task.
"""

import logging
import random
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


def _pick_healthy_task_ip(elbv2_client, tg_arn: str) -> str:
    """Return a random healthy task IP from the NLB MCP target group."""
    resp = elbv2_client.describe_target_health(TargetGroupArn=tg_arn)
    healthy = [
        t["Target"]["Id"]
        for t in resp["TargetHealthDescriptions"]
        if t["TargetHealth"]["State"] == "healthy"
    ]
    if not healthy:
        raise RuntimeError("No healthy playwright-mcp tasks in NLB target group")
    return random.choice(healthy)


class ECSSession:
    """
    Resolves endpoints for the always-on playwright-mcp ECS Service.

    Picks one healthy task IP from the NLB MCP target group, then connects
    both MCP (:3000) and noVNC (:6080) directly to that task IP.
    This guarantees MCP and noVNC land on the same task (same browser screen).
    """

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.ssm   = boto3.client("ssm",   region_name=region)
        self.elbv2 = boto3.client("elbv2", region_name=region)
        self.task_arn: str | None = None    # kept for API compatibility
        self.cluster: str | None = None     # kept for API compatibility
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
        1. Read mcp-tg-arn from SSM (cached after first call).
        2. DescribeTargetHealth → pick one healthy task IP.
        3. Set both MCP and noVNC endpoints to that task's direct IP.
        """
        params  = _load_ssm_params(self.ssm, ["mcp-tg-arn"])
        tg_arn  = params["mcp-tg-arn"]
        task_ip = _pick_healthy_task_ip(self.elbv2, tg_arn)

        self.mcp_endpoint = f"http://{task_ip}:3000"
        self.novnc_url    = f"http://{task_ip}:6080/vnc.html"
        logger.info(f"[ecs_session] task_ip={task_ip}  MCP -> :3000  noVNC -> :6080")

    # ── Teardown ──────────────────────────────────────────────────────────────

    def _stop(self):
        """
        No-op. Task is a shared warm resource; playwright-mcp --isolated
        gives each test its own browser context without restarting the task.
        """
        logger.info("[ecs_session] Shared task — not stopped after session")
