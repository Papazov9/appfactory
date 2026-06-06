from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from bot.config import config
from bot.models.project import Project, ProjectStatus, db
from bot.services.progress import ProgressTracker
from bot.services import stacks

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  Dockerfile templates (stack-aware)
# ──────────────────────────────────────────────
#  Every app reads PORT from the environment and binds 0.0.0.0 — matching the
#  STACK RULES the agents follow. Builds keep layer caching (no --no-cache) so
#  redeploys are fast: the dependency layer only rebuilds when the manifest
#  changes.

DOCKERFILE_PYTHON = """FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/data
EXPOSE {port}
ENV PORT={port}
CMD ["python", "app.py"]
"""

DOCKERFILE_SPRING = """FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build
COPY pom.xml ./
RUN mvn -q -B dependency:go-offline
COPY src ./src
RUN mvn -q -B -DskipTests package

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=build /build/target/*.jar app.jar
EXPOSE {port}
ENV PORT={port}
CMD ["sh", "-c", "java -jar app.jar"]
"""

# Angular: build with the CLI, then serve the static dist with `serve` (handles
# SPA fallback to index.html and honours PORT). Robustly locate the build output
# whether it's dist/<proj>/browser (Angular 17+) or dist/<proj>.
DOCKERFILE_ANGULAR = """FROM node:20-alpine AS build
WORKDIR /build
COPY package*.json ./
RUN npm ci || npm install
COPY . .
RUN npm run build \\
 && mkdir -p /out \\
 && d=$(find dist -maxdepth 2 -type d -name browser | head -n1); \\
    [ -z "$d" ] && d=$(find dist -maxdepth 1 -mindepth 1 -type d | head -n1); \\
    [ -z "$d" ] && d=dist; \\
    cp -r "$d"/. /out

FROM node:20-alpine
WORKDIR /app
RUN npm install -g serve
COPY --from=build /out ./public
EXPOSE {port}
ENV PORT={port}
CMD ["sh", "-c", "serve -s public -l tcp://0.0.0.0:${{PORT}}"]
"""

# Spring + Angular: build Angular (./frontend), copy its output into the Spring
# static resources, then build the single jar that serves API + SPA.
DOCKERFILE_SPRING_ANGULAR = """FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build \\
 && mkdir -p /webout \\
 && d=$(find dist -maxdepth 2 -type d -name browser | head -n1); \\
    [ -z "$d" ] && d=$(find dist -maxdepth 1 -mindepth 1 -type d | head -n1); \\
    [ -z "$d" ] && d=dist; \\
    cp -r "$d"/. /webout

FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build
COPY pom.xml ./
RUN mvn -q -B dependency:go-offline
COPY src ./src
COPY --from=web /webout ./src/main/resources/static
RUN mvn -q -B -DskipTests package

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=build /build/target/*.jar app.jar
EXPOSE {port}
ENV PORT={port}
CMD ["sh", "-c", "java -jar app.jar"]
"""

# Legacy fallbacks (projects created before the stack rework / detected by file).
DOCKERFILE_NODE = """FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install --production
COPY . .
EXPOSE {port}
ENV PORT={port}
CMD ["npm", "start"]
"""

DOCKERFILE_STATIC = """FROM node:20-alpine
WORKDIR /app
RUN npm install -g serve
COPY . .
EXPOSE {port}
ENV PORT={port}
CMD ["sh", "-c", "serve -s . -l tcp://0.0.0.0:${{PORT}}"]
"""

# Postgres connection defaults (must match the STACK RULES the agents follow).
PG_DB = "app"
PG_USER = "app"
PG_PASSWORD = "app"


class DockerManager:
    """Manages Docker images, containers, networks and databases for a project.

    Layout per project:
      - image      appfactory-<slug>:latest
      - app cont.  appfactory-<slug>
      - db cont.   appfactory-<slug>-db      (only when db_kind == postgres)
      - network    appfactory-<slug>-net     (only when db_kind == postgres)
      - volumes    appfactory-<slug>-data    (sqlite)  /  -dbdata (postgres)
    The DB container and its volume are long-lived and survive app rebuilds, so
    redeploys/upgrades never lose data.
    """

    def __init__(self, project: Project, tracker: ProgressTracker):
        self.project = project
        self.tracker = tracker
        self.slug = project.slug

    # ── Names ────────────────────────────────
    @property
    def container_name(self) -> str:
        return f"appfactory-{self.slug}"

    @property
    def image_tag(self) -> str:
        return f"appfactory-{self.slug}:latest"

    @property
    def network_name(self) -> str:
        return f"appfactory-{self.slug}-net"

    @property
    def db_container(self) -> str:
        return f"appfactory-{self.slug}-db"

    @property
    def db_volume(self) -> str:
        return f"appfactory-{self.slug}-dbdata"

    @property
    def data_volume(self) -> str:
        return f"appfactory-{self.slug}-data"

    def _uses_postgres(self) -> bool:
        return self.project.db_kind == "postgres"

    def _uses_sqlite(self) -> bool:
        return self.project.db_kind == "sqlite"

    # ── Top-level pipeline ───────────────────
    async def containerize_and_run(self) -> bool:
        """Build the image, provision the DB if needed, and (re)start the app in
        place. Used for the FIRST deploy; redeploys use the zero-downtime swap in
        orchestrator.deploy_project, which reuses the building blocks below."""
        if not await self.build_image():
            return False

        await self.tracker.step_start("docker_start", "Preparing runtime...")
        if not await self.ensure_runtime():
            await self.tracker.step_fail("docker_start", "Database not ready")
            await self.tracker.fail("Postgres container did not become ready in time.")
            return False

        # Replace any existing app container, free the port.
        await self.remove_container(self.container_name)
        await self._run_cmd(["fuser", "-k", f"{self.project.port}/tcp"],
                            label="kill rogue port process", allow_fail=True)
        await asyncio.sleep(1)

        run_ok, run_output = await self.run_app(self.container_name, self.project.port)
        if not run_ok:
            self.tracker.log(f"Docker run FAILED:\n{run_output[-400:]}")
            await self.tracker.step_fail("docker_start", "Container failed to start")
            await self.tracker.fail(f"Docker run failed:\n{run_output[-400:]}")
            return False

        cid = await self.container_id(self.container_name)
        if cid:
            self.project.container_id = cid
            await db.save(self.project)
        await self.tracker.step_done("docker_start", f"Container {self.container_name} running")

        return await self.finish_health(self.container_name, self.project.port)

    # ── Building blocks (shared with the zero-downtime swap) ──
    async def build_image(self) -> bool:
        """Generate the Dockerfile and build the image. Never touches a running container."""
        project_dir = self.project.project_dir
        try:
            await self.tracker.step_start("docker_build", "Generating Dockerfile...")
            self._write_dockerfile(project_dir)
            self._write_dockerignore(project_dir)
            await self.tracker.step_start("docker_build", f"Building image {self.image_tag}...")
            ok, out = await self._run_cmd_capture(
                ["docker", "build", "-t", self.image_tag, "."],
                cwd=str(project_dir), label="docker build",
            )
            if not ok:
                self.tracker.log(f"Docker build FAILED:\n{out[-800:]}")
                await self.tracker.step_fail("docker_build", "Image build failed")
                await self.tracker.fail(f"Docker build failed:\n{out[-800:]}")
                return False
            self.tracker.note("🐳 Container image built successfully.")
            await self.tracker.step_done("docker_build", f"Image built: {self.image_tag}")
            return True
        except Exception as e:
            logger.exception(f"Docker build failed for {self.slug}")
            await self.tracker.fail(f"Docker error: {str(e)}")
            return False

    async def ensure_runtime(self) -> bool:
        """Ensure the network + database exist and are ready (no-op without postgres)."""
        if self._uses_postgres():
            return await self._ensure_database()
        return True

    async def finish_health(self, name: str, port: int) -> bool:
        """Health-check a freshly-started canonical container; detect crashes."""
        if await self.await_health(port):
            self.project.last_good_image = self.image_tag
            await db.save(self.project)
            return True
        logs = await self._capture_cmd(["docker", "logs", "--tail", "80", name])
        if not await self.is_running(name):
            exit_code = await self._capture_cmd(
                ["docker", "inspect", "-f", "{{.State.ExitCode}}", name]
            )
            await self.tracker.step_fail("health_check", f"Container exited (code {exit_code.strip()})")
            await self.tracker.fail(
                f"Container crashed on startup (exit code {exit_code.strip()}).\n\n"
                f"Last logs:\n{logs[-600:]}"
            )
            return False
        self.tracker.log("⚠️ Container running but not responding yet — continuing")
        await self.tracker.step_done("health_check", "⚠️ Container running, not responding yet")
        self.project.last_good_image = self.image_tag
        await db.save(self.project)
        return True

    async def run_app(self, name: str, port: int) -> tuple[bool, str]:
        """Start the application container with the right network/env/volumes."""
        cmd = [
            "docker", "run", "-d",
            "--name", name,
            "--restart", "unless-stopped",
            "-p", f"127.0.0.1:{port}:{port}",
            "-e", f"PORT={port}",
        ]

        if self._uses_postgres():
            cmd += ["--network", self.network_name]
            cmd += [
                "-e", f"SPRING_DATASOURCE_URL=jdbc:postgresql://db:5432/{PG_DB}",
                "-e", f"SPRING_DATASOURCE_USERNAME={PG_USER}",
                "-e", f"SPRING_DATASOURCE_PASSWORD={PG_PASSWORD}",
                "-e", f"DATABASE_URL=postgresql://{PG_USER}:{PG_PASSWORD}@db:5432/{PG_DB}",
            ]
        if self._uses_sqlite():
            cmd += ["-v", f"{self.data_volume}:/app/data"]

        cmd.append(self.image_tag)
        self.tracker.log(f"Starting container '{name}' on port {port}...")
        return await self._run_cmd_capture(cmd, label="docker run")

    async def is_running(self, name: str) -> bool:
        out = await self._capture_cmd(["docker", "inspect", "-f", "{{.State.Running}}", name])
        return out.strip() == "true"

    async def remove_container(self, name: str):
        await self._run_cmd(["docker", "rm", "-f", name], label=f"rm {name}", allow_fail=True)

    async def rename_container(self, old: str, new: str):
        await self._run_cmd(["docker", "rename", old, new], label="rename container", allow_fail=True)

    async def container_id(self, name: str) -> str:
        out = await self._capture_cmd(["docker", "ps", "-q", "--filter", f"name=^{name}$"])
        return out.strip()

    # ── Database provisioning ────────────────
    async def _ensure_network(self):
        exists = await self._capture_cmd(
            ["docker", "network", "ls", "-q", "--filter", f"name=^{self.network_name}$"]
        )
        if not exists.strip():
            await self._run_cmd(["docker", "network", "create", self.network_name],
                                label="create network", allow_fail=True)

    async def _ensure_database(self) -> bool:
        """Ensure the per-project Postgres container is up; wait until ready."""
        await self.tracker.step_start("docker_start", "Starting database...")
        await self._ensure_network()

        running = await self._capture_cmd(
            ["docker", "ps", "-q", "--filter", f"name=^{self.db_container}$"]
        )
        if not running.strip():
            # Start it if it merely stopped, otherwise create it fresh.
            exists = await self._capture_cmd(
                ["docker", "ps", "-aq", "--filter", f"name=^{self.db_container}$"]
            )
            if exists.strip():
                await self._run_cmd(["docker", "start", self.db_container],
                                    label="start db", allow_fail=True)
            else:
                self.tracker.note("🗄️ Provisioning a dedicated PostgreSQL container for this app...")
                await self._run_cmd_capture([
                    "docker", "run", "-d",
                    "--name", self.db_container,
                    "--network", self.network_name,
                    "--network-alias", "db",
                    "--restart", "unless-stopped",
                    "-e", f"POSTGRES_DB={PG_DB}",
                    "-e", f"POSTGRES_USER={PG_USER}",
                    "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
                    "-v", f"{self.db_volume}:/var/lib/postgresql/data",
                    "postgres:16-alpine",
                ], label="docker run postgres")

        # Make sure the db is attached to the network (no-op if already attached).
        await self._run_cmd(
            ["docker", "network", "connect", "--alias", "db", self.network_name, self.db_container],
            label="attach db to network", allow_fail=True,
        )

        # Wait for readiness.
        for attempt in range(20):
            ready = await self._capture_cmd(
                ["docker", "exec", self.db_container, "pg_isready", "-U", PG_USER]
            )
            if "accepting connections" in ready:
                self.tracker.note("🗄️ Database is ready.")
                return True
            await asyncio.sleep(1.5)
        self.tracker.log("⚠️ Postgres did not report ready after 30s.")
        return False

    # ── Health check ─────────────────────────
    async def await_health(self, port: int) -> bool:
        await self.tracker.step_start("health_check", f"Waiting for port {port}...")
        await asyncio.sleep(3)
        last = {}
        for attempt in range(10):
            last = await self._check_health_detailed(port)
            if last["ok"]:
                self.tracker.log(f"Health check passed: HTTP {last['status_code']}")
                await self.tracker.step_done("health_check", f"HTTP {last['status_code']} OK")
                return True
            step = self.tracker.get_step("health_check")
            if step:
                step.detail = f"Attempt {attempt + 1}/10: {last['detail']}"
            if attempt < 9:
                await asyncio.sleep(3)
        self.tracker.log(f"Health check did not pass: {last.get('detail', 'unknown')}")
        return False

    async def _check_health_detailed(self, port: int) -> dict:
        """A server that answers on the port (incl. 4xx) counts as up; 5xx/000 don't."""
        try:
            result = await self._capture_cmd(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--connect-timeout", "3", "--max-time", "5",
                 f"http://127.0.0.1:{port}/"]
            )
            code = result.strip()
            if code and code[0] in ("2", "3", "4"):
                return {"ok": True, "status_code": code, "detail": f"HTTP {code}"}
            if code == "000":
                return {"ok": False, "status_code": code, "detail": "Connection refused"}
            return {"ok": False, "status_code": code, "detail": f"HTTP {code}"}
        except Exception as e:
            return {"ok": False, "status_code": "000", "detail": str(e)}

    # ── Lifecycle ────────────────────────────
    async def stop(self):
        """Stop & remove the app container (the DB container is left intact)."""
        await self._run_cmd(["docker", "rm", "-f", self.container_name],
                            label="stop container", allow_fail=True)
        self.project.status = ProjectStatus.STOPPED
        self.project.container_id = ""
        await db.save(self.project)

    async def teardown(self):
        """Remove everything for this project: app + db containers, network, volumes."""
        await self._run_cmd(["docker", "rm", "-f", self.container_name],
                            label="rm app", allow_fail=True)
        await self._run_cmd(["docker", "rm", "-f", self.db_container],
                            label="rm db", allow_fail=True)
        await self._run_cmd(["docker", "network", "rm", self.network_name],
                            label="rm network", allow_fail=True)
        await self._run_cmd(["docker", "volume", "rm", self.db_volume, self.data_volume],
                            label="rm volumes", allow_fail=True)
        await self._run_cmd(["docker", "rmi", "-f", self.image_tag],
                            label="rm image", allow_fail=True)

    async def get_logs(self, tail: int = 50, name: str | None = None) -> str:
        return await self._capture_cmd(
            ["docker", "logs", "--tail", str(tail), name or self.container_name]
        ) or "No container logs available."

    # ── Dockerfile selection ─────────────────
    def _write_dockerfile(self, project_dir: Path):
        dockerfile_path = project_dir / "Dockerfile"
        if dockerfile_path.exists():
            self.tracker.note("📄 Using the repo's own Dockerfile.")
            return
        content = self._dockerfile_for(project_dir)
        dockerfile_path.write_text(content)
        self.tracker.note(
            f"📄 Generated a Dockerfile for the {self.project.stack or self._detect_type(project_dir)} stack."
        )

    def _dockerfile_for(self, project_dir: Path) -> str:
        port = self.project.port
        stack = self.project.stack
        if stack == stacks.STACK_PYTHON:
            return DOCKERFILE_PYTHON.format(port=port)
        if stack == stacks.STACK_SPRING:
            return DOCKERFILE_SPRING.format(port=port)
        if stack == stacks.STACK_ANGULAR:
            return DOCKERFILE_ANGULAR.format(port=port)
        if stack == stacks.STACK_SPRING_ANGULAR:
            return DOCKERFILE_SPRING_ANGULAR.format(port=port)
        # Fallback: detect from files (legacy projects without a stack).
        return self._dockerfile_by_detection(project_dir, port)

    def _dockerfile_by_detection(self, project_dir: Path, port: int) -> str:
        if (project_dir / "pom.xml").exists():
            return DOCKERFILE_SPRING.format(port=port)
        if (project_dir / "angular.json").exists():
            return DOCKERFILE_ANGULAR.format(port=port)
        if (project_dir / "requirements.txt").exists():
            return DOCKERFILE_PYTHON.format(port=port)
        if (project_dir / "package.json").exists():
            return DOCKERFILE_NODE.format(port=port)
        return DOCKERFILE_STATIC.format(port=port)

    def _detect_type(self, project_dir: Path) -> str:
        if (project_dir / "pom.xml").exists():
            return "spring"
        if (project_dir / "angular.json").exists():
            return "angular"
        if (project_dir / "requirements.txt").exists():
            return "python"
        if (project_dir / "package.json").exists():
            return "node"
        return "static"

    def _write_dockerignore(self, project_dir: Path):
        dockerignore = project_dir / ".dockerignore"
        ignore_entries = {
            ".appfactory_checkpoint.json", ".git", ".appfactory", "design_refs",
            "node_modules", "**/node_modules",
            "target", "**/target", "dist", "**/dist",
        }
        if dockerignore.exists():
            ignore_entries |= set(dockerignore.read_text().strip().split("\n"))
        dockerignore.write_text("\n".join(sorted(e for e in ignore_entries if e)) + "\n")

    # ── Subprocess helpers ───────────────────
    async def _run_cmd(self, cmd: list[str], cwd: str | None = None,
                       label: str = "", allow_fail: bool = False) -> bool:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 and not allow_fail:
            logger.error(f"{label} failed: {stderr.decode('utf-8', errors='replace')[-500:]}")
            return False
        return True

    async def _run_cmd_capture(self, cmd: list[str], cwd: str | None = None,
                               label: str = "") -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            logger.error(f"{label} failed (exit {proc.returncode})")
            return False, output
        return True, output

    async def _capture_cmd(self, cmd: list[str]) -> str:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (stdout + stderr).decode("utf-8", errors="replace")
