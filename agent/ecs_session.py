"""
ECS Session Manager — spins up a dedicated Playwright MCP Fargate task per session.

Called by AgentRunner.automate() at the start of each test execution.
Each session gets its own isolated browser container; task is stopped when done.
"""

import logging
import socket
import time

import boto3

logger = logging.getLogger(__name__)

SSM_PREFIX = "/prompt2test/playwright"


def _get_ssm(ssm_client, name: str) -> str:
    response = ssm_client.get_parameter(Name=f"{SSM_PREFIX}/{name}")
    return response["Parameter"]["Value"]


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
        """Launch a dedicated ECS task and wait until MCP is ready."""
        # Read infra config from SSM — no hardcoded values
        self.cluster = _get_ssm(self.ssm, "cluster-name")
        task_family = _get_ssm(self.ssm, "task-definition-family")
        subnet_ids = _get_ssm(self.ssm, "subnet-ids").split(",")
        sg_id = _get_ssm(self.ssm, "security-group-id")

        logger.info(f"[ecs_session] Launching task family={task_family} cluster={self.cluster}")

        response = self.ecs.run_task(
            cluster=self.cluster,
            taskDefinition=task_family,   # resolves to latest active revision
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
        logger.info(f"[ecs_session] Task launched: {self.task_arn}")

        self._wait_for_running()
        self.public_ip = self._get_public_ip()
        logger.info(f"[ecs_session] Task running at public IP: {self.public_ip}")

        self.mcp_endpoint = f"http://{self.public_ip}:3000"
        self.novnc_url = f"http://{self.public_ip}:6080/vnc.html"

        # Wait until playwright-mcp is actually accepting connections on port 3000
        self._wait_for_mcp_port()

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

    GRACE_PERIOD_SECONDS = 300  # 5 minutes — user can inspect browser state after test

    def _stop(self):
        """
        Schedule the ECS task to stop after a grace period.

        The task stays alive for GRACE_PERIOD_SECONDS after the test finishes so the
        user can pop out the noVNC window and inspect the final browser state.
        Uses a daemon thread so the agent response is returned immediately.
        """
        if not self.task_arn:
            return

        task_arn = self.task_arn
        cluster = self.cluster

        def stop_after_grace():
            time.sleep(self.GRACE_PERIOD_SECONDS)
            try:
                self.ecs.stop_task(
                    cluster=cluster,
                    task=task_arn,
                    reason="Prompt2Test session ended — grace period elapsed",
                )
                logger.info(f"[ecs_session] Task stopped after grace period: {task_arn}")
            except Exception as e:
                logger.warning(f"[ecs_session] Failed to stop task {task_arn}: {e}")

        import threading
        thread = threading.Thread(target=stop_after_grace, daemon=True)
        thread.start()
        logger.info(f"[ecs_session] Task {task_arn} will stop in {self.GRACE_PERIOD_SECONDS}s")
