"""
ECS Session Manager — spins up a dedicated Playwright MCP Fargate task per session.

Called by AgentRunner.automate() at the start of each test execution.
Each session gets its own isolated browser container; task is stopped when done.
"""

import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

logger = logging.getLogger(__name__)

SSM_PREFIX = "/prompt2test/playwright"

# Cache SSM params for the lifetime of the container — they never change at runtime.
# Eliminates 4 sequential API calls on every start_session after the first.
_SSM_CACHE: dict[str, str] = {}


def _get_ssm(ssm_client, name: str) -> str:
    if name not in _SSM_CACHE:
        response = ssm_client.get_parameter(Name=f"{SSM_PREFIX}/{name}")
        _SSM_CACHE[name] = response["Parameter"]["Value"]
    return _SSM_CACHE[name]


def _load_ssm_params(ssm_client) -> dict[str, str]:
    """Fetch all required SSM params in parallel — ~4x faster than sequential."""
    keys = ["cluster-name", "task-definition-family", "subnet-ids", "security-group-id"]
    missing = [k for k in keys if k not in _SSM_CACHE]
    if missing:
        with ThreadPoolExecutor(max_workers=len(missing)) as ex:
            futures = {ex.submit(_get_ssm, ssm_client, k): k for k in missing}
            for f in as_completed(futures):
                f.result()  # raises on error
    return {k: _SSM_CACHE[k] for k in keys}


class ECSSession:
    """
    Context manager that owns the lifecycle of one Playwright MCP Fargate task.

    Usage:
        with ECSSession(region="us-east-1") as session:
            # session.mcp_endpoint  → "http://<public-ip>:3000"
            # session.novnc_url     → "http://<public-ip>:6080/vnc.html"
            ...
        # task is automatically stopped on exit
    """

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self.ecs = boto3.client("ecs", region_name=region)
        self.ssm = boto3.client("ssm", region_name=region)
        self.ec2 = boto3.client("ec2", region_name=region)
        self.task_arn: str | None = None
        self.cluster: str | None = None
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
        Acquire a browser task — tries warm pool first (instant), falls back to RunTask (~60s).

        Warm pool strategy:
          1. List RUNNING tasks in the cluster
          2. If one exists, claim it immediately (0 cold start)
          3. Otherwise, call RunTask and wait for it to start (slow path)
        """
        # Read all SSM params in parallel — cached after first call
        params = _load_ssm_params(self.ssm)
        self.cluster   = params["cluster-name"]
        task_family    = params["task-definition-family"]
        subnet_ids     = params["subnet-ids"].split(",")
        sg_id          = params["security-group-id"]

        # ── Try warm pool first ───────────────────────────────────────────────
        warm_task_arn = self._claim_warm_task()
        if warm_task_arn:
            self.task_arn = warm_task_arn
            logger.info(f"[ecs_session] Claimed warm task: {self.task_arn}")
            self.public_ip = self._get_public_ip()
            logger.info(f"[ecs_session] Task IP: {self.public_ip}")
            self.mcp_endpoint = f"http://{self.public_ip}:3000"
            self.novnc_url    = f"http://{self.public_ip}:6080/vnc.html"
            # Warm task already has playwright-mcp running — skip port poll
            logger.info("[ecs_session] Warm task claimed — skipping port wait")
        else:
            # ── Cold start fallback ───────────────────────────────────────────
            logger.info(f"[ecs_session] No warm task available — launching cold task family={task_family}")
            response = self.ecs.run_task(
                cluster=self.cluster,
                taskDefinition=task_family,
                launchType="FARGATE",
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": subnet_ids,
                        "securityGroups": [sg_id],
                        "assignPublicIp": "ENABLED",
                    }
                },
            )
            if not response.get("tasks"):
                failures = response.get("failures", [])
                raise RuntimeError(f"ECS RunTask failed: {failures}")
            self.task_arn = response["tasks"][0]["taskArn"]
            logger.info(f"[ecs_session] Cold task launched: {self.task_arn}")
            self._wait_for_running()
            self.public_ip = self._get_public_ip()
            logger.info(f"[ecs_session] Task IP: {self.public_ip}")
            self.mcp_endpoint = f"http://{self.public_ip}:3000"
            self.novnc_url    = f"http://{self.public_ip}:6080/vnc.html"
            # Cold start — wait for playwright-mcp to be ready
            self._wait_for_mcp_port()

    def _claim_warm_task(self) -> str | None:
        """
        Return the ARN of a RUNNING task in the cluster, or None if none available.
        The warm pool ECS Service will automatically replace the claimed task.
        """
        try:
            response = self.ecs.list_tasks(
                cluster=self.cluster,
                desiredStatus="RUNNING",
            )
            task_arns = response.get("taskArns", [])
            if task_arns:
                return task_arns[0]
        except Exception as e:
            logger.warning(f"[ecs_session] Failed to list warm tasks: {e}")
        return None

    def _wait_for_running(self, timeout: int = 120):
        """Poll ECS until task reaches RUNNING state (Fargate cold start ~30-60s)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.ecs.describe_tasks(cluster=self.cluster, tasks=[self.task_arn])
            task = resp["tasks"][0]
            status = task.get("lastStatus", "")
            logger.info(f"[ecs_session] Task status: {status}")
            if status == "RUNNING":
                return
            if status in ("STOPPED", "DEPROVISIONING"):
                reason = task.get("stoppedReason", "unknown")
                raise RuntimeError(f"Task stopped unexpectedly: {reason}")
            time.sleep(5)
        raise TimeoutError(f"Task did not reach RUNNING within {timeout}s")

    def _get_public_ip(self) -> str:
        """Extract the public IPv4 address from the task's elastic network interface."""
        resp = self.ecs.describe_tasks(cluster=self.cluster, tasks=[self.task_arn])
        task = resp["tasks"][0]

        eni_id = None
        for attachment in task.get("attachments", []):
            if attachment.get("type") == "ElasticNetworkInterface":
                for detail in attachment.get("details", []):
                    if detail["name"] == "networkInterfaceId":
                        eni_id = detail["value"]
                        break

        if not eni_id:
            raise RuntimeError("Could not find ENI attached to task")

        resp = self.ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
        association = resp["NetworkInterfaces"][0].get("Association", {})
        public_ip = association.get("PublicIp")

        if not public_ip:
            raise RuntimeError(f"ENI {eni_id} has no public IP — check subnet has auto-assign public IP enabled")

        return public_ip

    def _wait_for_mcp_port(self, timeout: int = 60):
        """Poll until port 3000 accepts a TCP connection — playwright-mcp is ready."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((self.public_ip, 3000), timeout=2):
                    logger.info("[ecs_session] MCP port 3000 is ready")
                    return
            except (ConnectionRefusedError, OSError):
                logger.info("[ecs_session] Waiting for MCP port 3000...")
                time.sleep(3)
        raise TimeoutError(f"MCP server at {self.public_ip}:3000 did not become ready within {timeout}s")

    # ── Teardown ──────────────────────────────────────────────────────────────

    def _stop(self):
        """
        Stop the ECS task immediately after test completion (pass or fail).
        The warm pool ECS Service (desiredCount=1) will automatically start
        a fresh replacement task, ready for the next session.
        """
        if not self.task_arn:
            return
        try:
            self.ecs.stop_task(
                cluster=self.cluster,
                task=self.task_arn,
                reason="Prompt2Test session completed",
            )
            logger.info(f"[ecs_session] Task stopped: {self.task_arn}")
        except Exception as e:
            logger.warning(f"[ecs_session] Failed to stop task {self.task_arn}: {e}")
