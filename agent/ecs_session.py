"""
ECS Session Manager — returns fixed ALB endpoints for the always-on Playwright MCP service.

The ALB provides a stable DNS name that never changes, so start_session is instant:
  1. Read alb-dns from SSM (cached after first call — ~0ms on repeat)
  2. Return mcp_endpoint + novnc_url immediately

No IP discovery, no ListTasks, no DescribeNetworkInterfaces.
The ECS Service (desiredCount=1) keeps one warm task behind the ALB at all times.
playwright-mcp --isolated gives each test session its own browser context.
"""

import logging

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


class ECSSession:
    """
    Thin wrapper that resolves the ALB DNS name and constructs the MCP + noVNC URLs.

    Usage (unchanged from caller's perspective):
        session = ECSSession(region="us-east-1")
        session._start()
        # session.mcp_endpoint  → "http://<alb-dns>:3000"
        # session.novnc_url     → "http://<alb-dns>:6080/vnc.html"
        session._stop()   # no-op — task is a shared resource managed by ECS Service
    """

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.ssm = boto3.client("ssm", region_name=region)
        self.task_arn: str | None = None    # kept for API compatibility
        self.cluster: str | None = None     # kept for API compatibility
        self.public_ip: str | None = None   # kept for API compatibility
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
        Resolve the ALB DNS name from SSM and build the endpoint URLs.
        Cached after the first call — effectively free on subsequent calls.
        """
        alb_dns = _get_ssm(self.ssm, "alb-dns")
        self.mcp_endpoint = f"http://{alb_dns}:3000"
        self.novnc_url    = f"http://{alb_dns}:6080/vnc.html"
        logger.info(f"[ecs_session] ALB endpoint ready: {alb_dns}")

    # ── Teardown ──────────────────────────────────────────────────────────────

    def _stop(self):
        """
        No-op. The ECS task is a shared resource managed by the ECS Service.
        playwright-mcp --isolated ensures each test session gets a fresh browser context.
        """
        logger.info("[ecs_session] ALB mode — task is shared, not stopped after session")
