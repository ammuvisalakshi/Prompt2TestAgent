"""
ECS Session Manager — NLB fixed-endpoint approach.

MCP    → NLB DNS on port 3000 (TCP passthrough, Host header unchanged,
          playwright-mcp SSE CSRF check passes, load-balanced across tasks)
noVNC  → NLB DNS on port 6080 (TCP passthrough, WebSocket friendly)

Both endpoints resolved from a single SSM param: nlb-dns (set at CDK deploy time,
never changes at runtime). Cached after first call — ~0ms on repeat.
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

    Both MCP (:3000) and noVNC (:6080) connect through the NLB DNS.
    NLB is TCP passthrough — no Host-header rewrite, no CSRF issues.
    """

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.ssm = boto3.client("ssm", region_name=region)
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
        Fetch NLB DNS from SSM (cached after first call — ~0ms on repeat).
        nlb-dns → NLB DNS name, used for both MCP (:3000) and noVNC (:6080).
        """
        params = _load_ssm_params(self.ssm, ["nlb-dns"])

        nlb_dns = params["nlb-dns"]

        self.mcp_endpoint = f"http://{nlb_dns}:3000"
        self.novnc_url    = f"http://{nlb_dns}:6080/vnc.html"
        logger.info(f"[ecs_session] MCP -> {nlb_dns}:3000  noVNC -> {nlb_dns}:6080")

    # ── Teardown ──────────────────────────────────────────────────────────────

    def _stop(self):
        """
        No-op. Task is a shared warm resource; playwright-mcp --isolated
        gives each test its own browser context without restarting the task.
        """
        logger.info("[ecs_session] Shared task — not stopped after session")
