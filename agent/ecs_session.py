"""
ECS Session Manager — on-demand RunTask per test session.

Each test gets its own dedicated Fargate task:
  _start()  → RunTask → poll until RUNNING → get public IP via ENI
  _stop()   → StopTask (called when test finishes)

MCP (:3000) and noVNC (:6080) both connect to the same task's public IP,
so the browser the agent controls is exactly what the user sees in noVNC.

Typical startup time: ~50-60 seconds (ECS scheduling + image + Chromium).
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

logger = logging.getLogger(__name__)

SSM_PREFIX     = "/prompt2test/playwright"
POLL_INTERVAL  = 3    # seconds between DescribeTasks polls
TASK_TIMEOUT   = 120  # max seconds to wait for RUNNING + public IP
READY_BUFFER   = 5    # extra seconds after RUNNING before connecting

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


def _get_task_public_ip(ec2_client, task: dict) -> str | None:
    """Extract public IP from the task's ENI attachment."""
    for att in task.get("attachments", []):
        if att.get("type") == "ElasticNetworkInterface":
            eni_id = next(
                (d["value"] for d in att.get("details", []) if d["name"] == "networkInterfaceId"),
                None,
            )
            if eni_id:
                resp = ec2_client.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
                return resp["NetworkInterfaces"][0].get("Association", {}).get("PublicIp")
    return None


class ECSSession:
    """
    On-demand Fargate task per test session.

    Each session gets a dedicated task with its own Xvfb display, so
    MCP and noVNC always show the same browser — no cross-task confusion.
    """

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.ssm = boto3.client("ssm", region_name=region)
        self.ecs = boto3.client("ecs", region_name=region)
        self.ec2 = boto3.client("ec2", region_name=region)
        self.task_arn: str | None = None
        self.cluster: str | None = None
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
        1. Read ECS config from SSM (cached).
        2. RunTask → get task ARN.
        3. Poll DescribeTasks until RUNNING + public IP available.
        4. Set MCP and noVNC endpoints to that task's public IP.
        """
        params = _load_ssm_params(self.ssm, [
            "cluster-name", "task-definition-family",
            "subnet-ids", "security-group-id",
        ])

        cluster = params["cluster-name"]
        subnets = params["subnet-ids"].split(",")
        sg_id   = params["security-group-id"]

        resp = self.ecs.run_task(
            cluster=cluster,
            taskDefinition=params["task-definition-family"],
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnets,
                    "securityGroups": [sg_id],
                    "assignPublicIp": "ENABLED",
                }
            },
        )

        if not resp.get("tasks"):
            raise RuntimeError(f"RunTask failed: {resp.get('failures', [])}")

        self.task_arn = resp["tasks"][0]["taskArn"]
        self.cluster  = cluster
        logger.info(f"[ecs_session] RunTask started: {self.task_arn}")

        public_ip = self._wait_for_running()

        self.mcp_endpoint = f"http://{public_ip}:3000"
        self.novnc_url    = f"http://{public_ip}:6080/vnc.html"
        logger.info(f"[ecs_session] Ready — {public_ip}  MCP→:3000  noVNC→:6080")

    def _wait_for_running(self) -> str:
        """Poll until task is RUNNING and has a public IP, then return it."""
        deadline = time.time() + TASK_TIMEOUT
        while time.time() < deadline:
            resp = self.ecs.describe_tasks(cluster=self.cluster, tasks=[self.task_arn])
            if not resp["tasks"]:
                time.sleep(POLL_INTERVAL)
                continue

            task   = resp["tasks"][0]
            status = task.get("lastStatus", "")
            logger.info(f"[ecs_session] Task status: {status}")

            if status == "STOPPED":
                raise RuntimeError(f"Task stopped unexpectedly: {task.get('stoppedReason', 'unknown')}")

            if status == "RUNNING":
                ip = _get_task_public_ip(self.ec2, task)
                if ip:
                    time.sleep(READY_BUFFER)  # let playwright-mcp finish binding port 3000
                    return ip

            time.sleep(POLL_INTERVAL)

        raise RuntimeError(f"Task did not reach RUNNING within {TASK_TIMEOUT}s")

    # ── Teardown ──────────────────────────────────────────────────────────────

    def _stop(self):
        """Stop the dedicated task when the test session ends."""
        if self.task_arn and self.cluster:
            try:
                self.ecs.stop_task(
                    cluster=self.cluster,
                    task=self.task_arn,
                    reason="Test session completed",
                )
                logger.info(f"[ecs_session] Stopped task: {self.task_arn}")
            except Exception as exc:
                logger.warning(f"[ecs_session] Could not stop task: {exc}")
