from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from bot.config import config
from bot.models.project import Project, ProjectStatus, db
from bot.services.progress import ProgressTracker

logger = logging.getLogger(__name__)

# Tries to detect project type and pick the right Dockerfile
DOCKERFILE_NODE = """FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install --production
COPY . .
EXPOSE {port}
ENV PORT={port}
CMD ["npm", "start"]
"""

DOCKERFILE_PYTHON = """FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE {port}
ENV PORT={port}
CMD ["python", "app.py"]
"""

DOCKERFILE_STATIC = """FROM node:20-alpine
WORKDIR /app
RUN npm install -g serve
COPY . .
EXPOSE {port}
ENV PORT={port}
CMD ["serve", "-s", ".", "-l", "{port}"]
"""


class DockerManager:
    """Manages Docker containers for deployed projects."""

    def __init__(self, project: Project, tracker: ProgressTracker):
        self.project = project
        self.tracker = tracker

    async def containerize_and_run(self) -> bool:
        """Build a Docker image and start the container."""
        project_dir = self.project.project_dir
        container_name = f"appfactory-{self.project.slug}"

        try:
            # ── Step 1: Generate Dockerfile ──────────
            await self.tracker.step_start("docker_build", "Generating Dockerfile...")

            dockerfile_content = self._pick_dockerfile(project_dir)
            dockerfile_path = project_dir / "Dockerfile"
            if not dockerfile_path.exists():
                dockerfile_path.write_text(dockerfile_content)
                self.tracker.log(f"Generated Dockerfile ({self._detect_type(project_dir)})")
            else:
                self.tracker.log("Using existing Dockerfile")

            # Remove .appfactory_checkpoint.json from Docker context
            dockerignore = project_dir / ".dockerignore"
            ignore_entries = {".appfactory_checkpoint.json", "node_modules", ".git"}
            if dockerignore.exists():
                existing = set(dockerignore.read_text().strip().split("\n"))
                ignore_entries |= existing
            dockerignore.write_text("\n".join(sorted(ignore_entries)) + "\n")

            # ── Step 2: Build image ──────────────────
            image_tag = f"appfactory-{self.project.slug}:latest"
            await self.tracker.step_start("docker_build", f"Building image {image_tag}...")

            build_ok, build_output = await self._run_cmd_capture(
                ["docker", "build", "-t", image_tag, "--no-cache", "."],
                cwd=str(project_dir),
                label="docker build",
            )
            if not build_ok:
                # Log the last part of build output for debugging
                self.tracker.log(f"Docker build FAILED:\n{build_output[-500:]}")
                await self.tracker.step_fail("docker_build", "Image build failed")
                await self.tracker.fail(f"Docker build failed:\n{build_output[-500:]}")
                return False

            await self.tracker.step_done("docker_build", f"Image built: {image_tag}")

            # ── Step 3: Start container ──────────────
            await self.tracker.step_start("docker_start", "Preparing to start...")

            # Stop any existing container with the same name
            await self._run_cmd(
                ["docker", "rm", "-f", container_name],
                label="cleanup old container",
                allow_fail=True,
            )

            # Kill any rogue process using our port
            await self._run_cmd(
                ["fuser", "-k", f"{self.project.port}/tcp"],
                label="kill rogue port process",
                allow_fail=True,
            )
            await asyncio.sleep(1)

            # Run container
            self.tracker.log(f"Starting container on port {self.project.port}...")
            run_ok, run_output = await self._run_cmd_capture(
                [
                    "docker", "run", "-d",
                    "--name", container_name,
                    "--restart", "unless-stopped",
                    "-p", f"127.0.0.1:{self.project.port}:{self.project.port}",
                    "-e", f"PORT={self.project.port}",
                    image_tag,
                ],
                cwd=str(project_dir),
                label="docker run",
            )
            if not run_ok:
                self.tracker.log(f"Docker run FAILED:\n{run_output[-300:]}")
                await self.tracker.step_fail("docker_start", "Container failed to start")
                await self.tracker.fail(f"Docker run failed:\n{run_output[-300:]}")
                return False

            # Get the container ID
            result = await self._capture_cmd(
                ["docker", "ps", "-q", "--filter", f"name={container_name}"]
            )
            if result:
                self.project.container_id = result.strip()
                await db.save(self.project)

            await self.tracker.step_done("docker_start", f"Container {container_name} running")

            # ── Step 4: Health check ─────────────────
            await self.tracker.step_start("health_check", f"Waiting for port {self.project.port}...")

            # Wait a moment for the app to start
            await asyncio.sleep(3)

            healthy = False
            for attempt in range(8):
                health_result = await self._check_health_detailed()
                if health_result["ok"]:
                    healthy = True
                    self.tracker.log(f"Health check passed: HTTP {health_result['status_code']}")
                    break

                detail = f"Attempt {attempt + 1}/8: {health_result['detail']}"
                step = self.tracker.get_step("health_check")
                if step:
                    step.detail = detail

                if attempt < 7:
                    await asyncio.sleep(3)

            if healthy:
                await self.tracker.step_done("health_check", f"HTTP {health_result['status_code']} OK")
            else:
                # Get container logs for debugging
                logs = await self._capture_cmd(
                    ["docker", "logs", "--tail", "80", container_name]
                )
                # Check if container is still running
                running = await self._capture_cmd(
                    ["docker", "inspect", "-f", "{{.State.Running}}", container_name]
                )
                is_running = running.strip() == "true"

                self.tracker.log(f"Health check FAILED after 8 attempts. Container running: {is_running}")
                self.tracker.log(f"Container logs (last 30 lines):\n{logs[-1000:]}")

                if not is_running:
                    # Container crashed — this is a build issue
                    exit_code = await self._capture_cmd(
                        ["docker", "inspect", "-f", "{{.State.ExitCode}}", container_name]
                    )
                    await self.tracker.step_fail("health_check",
                                                  f"Container exited (code {exit_code.strip()})")
                    await self.tracker.fail(
                        f"Container crashed on startup (exit code {exit_code.strip()}).\n\n"
                        f"Last logs:\n{logs[-500:]}"
                    )
                    return False
                else:
                    # Container is running but not responding — might just be slow
                    # Let it continue, the tunnel verify step will catch it
                    self.tracker.log("⚠️ Container running but not responding on health check — continuing anyway")
                    await self.tracker.step_done("health_check",
                                                  "⚠️ Container running, not responding yet")

            return True

        except Exception as e:
            logger.exception(f"Docker build/run failed for {self.project.slug}")
            self.tracker.log(f"💥 Docker error: {e}")
            await self.tracker.fail(f"Docker error: {str(e)}")
            return False

    async def stop(self):
        """Stop and remove the container."""
        container_name = f"appfactory-{self.project.slug}"
        await self._run_cmd(
            ["docker", "rm", "-f", container_name],
            label="stop container",
            allow_fail=True,
        )
        self.project.status = ProjectStatus.STOPPED
        self.project.container_id = ""
        await db.save(self.project)

    async def get_logs(self, tail: int = 50) -> str:
        """Fetch recent container logs."""
        container_name = f"appfactory-{self.project.slug}"
        return await self._capture_cmd(
            ["docker", "logs", "--tail", str(tail), container_name]
        ) or "No container logs available."

    def _detect_type(self, project_dir: Path) -> str:
        """Detect project type from files."""
        if (project_dir / "package.json").exists():
            return "node"
        if (project_dir / "requirements.txt").exists():
            return "python"
        if (project_dir / "index.html").exists():
            return "static"
        return "static"

    def _pick_dockerfile(self, project_dir: Path) -> str:
        """Detect project type and return the right Dockerfile template."""
        port = self.project.port

        if (project_dir / "package.json").exists():
            try:
                pkg = json.loads((project_dir / "package.json").read_text())
                if "start" in pkg.get("scripts", {}):
                    return DOCKERFILE_NODE.format(port=port)
            except Exception:
                pass
            return DOCKERFILE_NODE.format(port=port)

        if (project_dir / "requirements.txt").exists():
            return DOCKERFILE_PYTHON.format(port=port)

        if (project_dir / "index.html").exists():
            return DOCKERFILE_STATIC.format(port=port)

        return DOCKERFILE_STATIC.format(port=port)

    async def _check_health_detailed(self) -> dict:
        """Detailed health check: returns ok, status_code, detail."""
        try:
            result = await self._capture_cmd(
                ["curl", "-s", "-o", "/dev/null",
                 "-w", "%{http_code}",
                 "--connect-timeout", "3",
                 "--max-time", "5",
                 f"http://127.0.0.1:{self.project.port}/"]
            )
            code = result.strip()
            if code and code[0] in ("2", "3"):
                return {"ok": True, "status_code": code, "detail": f"HTTP {code}"}
            elif code == "000":
                return {"ok": False, "status_code": code, "detail": "Connection refused"}
            else:
                return {"ok": False, "status_code": code, "detail": f"HTTP {code}"}
        except Exception as e:
            return {"ok": False, "status_code": "000", "detail": str(e)}

    async def _run_cmd(
        self,
        cmd: list[str],
        cwd: str | None = None,
        label: str = "",
        allow_fail: bool = False,
    ) -> bool:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0 and not allow_fail:
            error = stderr.decode("utf-8", errors="replace")[-500:]
            logger.error(f"{label} failed: {error}")
            return False
        return True

    async def _run_cmd_capture(
        self,
        cmd: list[str],
        cwd: str | None = None,
        label: str = "",
    ) -> tuple[bool, str]:
        """Run a command and return (success, combined output)."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            logger.error(f"{label} failed (exit {proc.returncode})")
            return False, output
        return True, output

    async def _capture_cmd(self, cmd: list[str]) -> str:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (stdout + stderr).decode("utf-8", errors="replace")
